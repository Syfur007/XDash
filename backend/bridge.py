"""Subprocess bridge into the host repo's own Python environment.

Some dashboard features (schema-aware config validation, model-registry
introspection, and similar) need code that lives in the host repo
(orchestration/, models/, ...) and dependencies the dashboard deliberately
does not install itself (pydantic, torch, ... — see requirements.txt's
header comment and IMPLEMENTATION_PLAN.md's design principles). Instead of
importing any of that here, this module shells out to
`bridge_python_executable` running a small script under bridge_scripts/,
which does the importing in its own process and prints one JSON value to
stdout.

This also means a host repo that hasn't adopted the orchestration/models/
layout at all (an older or partial checkout) simply gets a clean
"unavailable" result instead of the whole dashboard failing to start —
nothing here is imported eagerly, and every route that calls into this
module is read-only or profiles a hypothetical object in a subprocess; none
of it executes user-supplied shell text (subprocess.run is always called
with an argument list, never shell=True, exactly like tmux_runner.py's
existing launch-command handling).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import settings

SCRIPTS_DIR = Path(__file__).resolve().parent / "bridge_scripts"

# Schema/registry output only changes when the host repo's own code changes —
# a short TTL (rather than tracking file mtimes) is a deliberately simple
# cache that's good enough for a single-user local tool and avoids adding a
# second cache-invalidation mechanism to reason about.
_CACHE_TTL_SECONDS = 60
_cache: Dict[Tuple[str, Tuple[str, ...]], Tuple[float, Any]] = {}


class BridgeError(Exception):
    """The script ran but reported an error — e.g. the host repo doesn't
    have this module, or the requested config/model kwargs are invalid.
    Expected and common; callers should show this message, not a 500."""


class BridgeUnavailable(Exception):
    """The interpreter/subprocess mechanism itself failed — a bad
    bridge_python_executable, a timeout, or output that wasn't JSON at
    all. Points at misconfiguration rather than "this repo lacks feature
    X"."""


def _script_path(name: str) -> Path:
    p = SCRIPTS_DIR / name
    if not p.is_file():
        raise BridgeUnavailable(f"Unknown bridge script '{name}'")
    return p


def run_bridge_script(
    name: str,
    args: Optional[List[str]] = None,
    timeout: float = 15.0,
    use_cache: bool = True,
) -> Any:
    """Run bridge_scripts/<name> with *args* in the host repo's own
    interpreter and working directory, returning its parsed JSON stdout.

    Raises BridgeUnavailable if the interpreter/timeout/JSON-decoding
    itself fails, or BridgeError if the script ran but reported
    {"__bridge_error__": true, "message": ...} — see bridge_scripts/
    _common.py's run_main(), which every script uses to turn its own
    exceptions into that shape instead of a raw traceback on stdout.
    """
    args = args or []
    cache_key = (name, tuple(args))
    if use_cache:
        cached = _cache.get(cache_key)
        if cached is not None and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]

    script = _script_path(name)
    env = dict(os.environ)
    # A bare cwd=repo_root does NOT put repo_root on sys.path for an
    # absolute script path (Python only adds the script's own directory) —
    # PYTHONPATH is what makes `import orchestration`/`import models` work
    # from a script that lives inside dashboard/backend/bridge_scripts/.
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(settings.repo_root) + (os.pathsep + existing if existing else "")

    try:
        proc = subprocess.run(
            [settings.bridge_python_executable, str(script), *args],
            cwd=str(settings.repo_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise BridgeUnavailable(
            f"bridge_python_executable '{settings.bridge_python_executable}' not found"
        )
    except subprocess.TimeoutExpired:
        raise BridgeUnavailable(f"Bridge script '{name}' timed out after {timeout}s")

    if proc.returncode != 0:
        raise BridgeUnavailable(
            f"Bridge script '{name}' exited {proc.returncode}: {proc.stderr.strip()[-2000:]}"
        )

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise BridgeUnavailable(
            f"Bridge script '{name}' did not print valid JSON: {proc.stdout[-500:]!r}"
        )

    if isinstance(result, dict) and result.get("__bridge_error__"):
        raise BridgeError(result.get("message", "Unknown bridge error"))

    if use_cache:
        _cache[cache_key] = (time.monotonic(), result)
    return result


def clear_cache() -> None:
    _cache.clear()


def bridge_status() -> Dict[str, Any]:
    """Per-module availability (orchestration/models/metrics/datasets/
    pandas) so the frontend can show accurate per-feature 'unavailable in
    this repo' states instead of one all-or-nothing flag. Never raises —
    a total bridge failure (e.g. bridge_python_executable misconfigured)
    is itself reported as {"error": "..."} rather than propagating.
    """
    try:
        return run_bridge_script("status.py", timeout=10)
    except (BridgeError, BridgeUnavailable) as e:
        return {"error": str(e)}
