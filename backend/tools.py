"""Deployment-level registry of the external CLI/binaries XDash shells out
to: `kaggle`, `colab`, `tensorboard`, `tmux`, `ssh`, `rsync`, `git` and
`nvidia-smi` (XDASH_FIXES_PLAN.md F2, closes issue 7).

**D2 — a deployment setting, not a profile one.** Which env XDash itself
runs in (venv/conda/Docker) determines where these binaries live; the active
repo profile has nothing to do with it. So overrides live in `data/tools.json`
(follows XDASH_DATA_DIR, same as every other deployment-level store — see
backend/hosts.py's HOSTS_FILE), never in `repos/<profile>.yaml`. The two keys
that used to live there (`kaggle_executable`, `colab_executable`) are moved
out once by migrate_from_profiles(), called at server startup.

**D3 — resolution order**, per tool: an explicit override (data/tools.json)
-> the bin/ dir next to whichever Python is running server.py
(`Path(sys.executable).parent`) -> PATH (`shutil.which`). The middle step is
what makes "whatever env XDash runs in brings its own CLIs" true even when
that env isn't activated — e.g. `/path/to/some/env/bin/python server.py`
without `conda activate`/`source venv/bin/activate` first still finds that
same env's `kaggle`/`tensorboard`, because they sit right next to the
interpreter that's running this process.

`tools.path(name)` is the one call every site that used to hardcode a bare
command name, or read `settings.kaggle_executable`/`settings.colab_executable`,
now uses instead. It never raises and never blocks on "not found" — an
unresolved tool falls back to its own bare name, so a subprocess call still
fails exactly the way it always did (FileNotFoundError / returncode 127);
`tools.status(name)` is where "missing"/"too old" is reported, for Settings
-> Tools and for runtimes._health().

**Remote is a different concern.** When a command like `tmux` or
`nvidia-smi` is typed into (or run over) an ssh connection to reach some
other host, that binary executes on the REMOTE host's own PATH — resolving
it against this machine's env would be wrong, and Settings -> Tools has no
opinion about what's installed on a lab box. Only `ssh`/`rsync` themselves
are always local (they're what *makes* the remote connection); every other
call site is expected to resolve tools.path(...) only for the
locally-executed half of a remote operation. See backend/tmux_runner.py's
_run() and backend/runners/machine.py's probe_accelerator() for the
local/remote split in practice.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config as config_mod
from .config import DATA_DIR
from .store import JsonStore, atomic_write_text

TOOLS_FILE = DATA_DIR / "tools.json"
_store = JsonStore(TOOLS_FILE, lambda: {"overrides": {}})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    label: str
    used_for: str                              # one-line "needed for" — shown in Settings -> Tools
    version_argv: Tuple[str, ...] = ("--version",)
    # First capturing group is the version string. None = existence/executable
    # is all that's checked (most of these tools have no version XDash cares about).
    version_regex: Optional[str] = None
    min_version: Optional[Tuple[int, ...]] = None
    optional: bool = False                     # never counted against "missing required dependency"


# Confirmed against the actually-installed binaries on this machine (2026-09-28,
# read-only `--version`/`-V` calls, no network) — see each tool's own regex
# comment for the exact string it was matched against.
TOOL_SPECS: Dict[str, ToolSpec] = {
    "kaggle": ToolSpec(
        "kaggle", "Kaggle CLI", "Kaggle runtimes",
        version_argv=("--version",),
        # `kaggle --version` prints "Kaggle CLI 2.2.4" (installed CLI, confirmed).
        version_regex=r"Kaggle CLI ([\d.]+)",
        min_version=(2, 2, 1),
    ),
    "colab": ToolSpec(
        "colab", "Colab CLI", "Colab runtimes",
        version_argv=("--version",),
        version_regex=r"([\d.]+)",
    ),
    "tensorboard": ToolSpec(
        "tensorboard", "TensorBoard", "TensorBoard",
        version_argv=("--version",),
        # Prints a bare version number to stdout ("2.21.0"); a "TensorFlow
        # installation not found" line on stderr is normal and harmless.
        version_regex=r"([\d.]+)",
    ),
    "tmux": ToolSpec(
        "tmux", "tmux", "Local runs",
        version_argv=("-V",),
        version_regex=r"tmux ([\d.]+\w*)",
    ),
    "ssh": ToolSpec(
        "ssh", "OpenSSH client", "SSH hosts and Colab",
        version_argv=("-V",),
        # OpenSSH writes its version to stderr: "OpenSSH_9.6p1 Ubuntu-3ubuntu13.19, ...".
        version_regex=r"OpenSSH_([\d.]+\w*)",
    ),
    "rsync": ToolSpec(
        "rsync", "rsync", "SSH hosts and Colab",
        version_argv=("--version",),
        version_regex=r"version ([\d.]+)",
    ),
    "git": ToolSpec(
        "git", "git", "Code provenance (push checks)",
        version_argv=("--version",),
        version_regex=r"git version ([\d.]+)",
    ),
    "nvidia-smi": ToolSpec(
        "nvidia-smi", "nvidia-smi", "Local GPU probe",
        version_argv=("--query-gpu=name", "--format=csv,noheader"),
        optional=True,  # XDASH_FIXES_PLAN.md §2/#7 — not on this machine's PATH today
    ),
}


@dataclass
class ToolStatus:
    name: str
    path: str                       # resolved path, or the bare tool name if unresolved
    source: str                      # "override" | "sibling" | "path" | "not-found"
    exists: bool
    executable: bool
    version: Optional[str]
    min_version: Optional[str]       # displayable "2.2.1", or None if this tool has no floor
    version_ok: Optional[bool]       # None: no min_version declared, or version couldn't be read
    required: bool
    error: Optional[str]

    @property
    def ok(self) -> bool:
        return self.exists and self.executable and self.version_ok is not False

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["ok"] = self.ok
        return d


def _overrides() -> Dict[str, str]:
    data = _store.load()
    return dict(data.get("overrides") or {})


def _resolve(name: str) -> Tuple[str, str]:
    """(path, source) per D3's order. *path* is always something runnable —
    the bare *name* itself when nothing resolved, so a caller that still
    execs it gets the exact same FileNotFoundError/127 it always did."""
    override = _overrides().get(name)
    if override:
        return override, "override"
    sibling = Path(sys.executable).parent / name
    if sibling.is_file():
        return str(sibling), "sibling"
    found = shutil.which(name)
    if found:
        return found, "path"
    return name, "not-found"


def _version_tuple(text: str) -> Tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text))


def _probe_version(path: str, spec: ToolSpec) -> Tuple[Optional[str], Optional[str]]:
    if spec.version_regex is None:
        return None, None
    try:
        proc = subprocess.run([path, *spec.version_argv], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "version check failed: %s" % e
    combined = "%s\n%s" % (proc.stdout or "", proc.stderr or "")
    m = re.search(spec.version_regex, combined)
    if not m:
        return None, "couldn't parse a version from: %s" % combined.strip()[:200]
    return m.group(1), None


def _compute_status(name: str) -> ToolStatus:
    spec = TOOL_SPECS[name]
    resolved, source = _resolve(name)
    exists = source != "not-found" and Path(resolved).is_file()
    executable = exists and os.access(resolved, os.X_OK)
    version: Optional[str] = None
    version_ok: Optional[bool] = None
    error: Optional[str] = None
    min_version_str = ".".join(str(n) for n in spec.min_version) if spec.min_version else None

    if not exists:
        error = "not found (checked an override, %s, and PATH)" % (Path(sys.executable).parent)
    elif not executable:
        error = "found at %s but it isn't executable" % resolved
    else:
        version, verr = _probe_version(resolved, spec)
        if verr:
            error = verr
        if spec.min_version is not None:
            if version is None:
                version_ok = False
                error = error or "couldn't determine version (need >= %s)" % min_version_str
            else:
                version_ok = _version_tuple(version) >= spec.min_version
                if not version_ok:
                    error = "%s %s found, need >= %s" % (spec.label, version, min_version_str)

    return ToolStatus(
        name=name, path=resolved, source=source, exists=exists, executable=executable,
        version=version, min_version=min_version_str, version_ok=version_ok,
        required=not spec.optional, error=error,
    )


_cache_lock = threading.Lock()
_cache: Dict[str, ToolStatus] = {}


def status(name: str, refresh: bool = False) -> ToolStatus:
    if name not in TOOL_SPECS:
        raise ValueError("Unknown tool %r" % name)
    if not refresh:
        with _cache_lock:
            cached = _cache.get(name)
        if cached is not None:
            return cached
    computed = _compute_status(name)
    with _cache_lock:
        _cache[name] = computed
    return computed


def all_status(refresh: bool = False) -> Dict[str, ToolStatus]:
    return {name: status(name, refresh=refresh) for name in TOOL_SPECS}


def path(name: str) -> str:
    """The resolved path for *name* — every call site that used to hardcode
    a bare command, or read settings.kaggle_executable/colab_executable,
    calls this instead. See the module docstring for the local/remote split
    a caller reaching a *different* host must still apply itself."""
    return status(name).path


def is_available(name: str) -> bool:
    return status(name).ok


def set_override(name: str, override_path: Optional[str]) -> ToolStatus:
    """*override_path* falsy clears back to sibling-of-python/PATH resolution."""
    if name not in TOOL_SPECS:
        raise ValueError("Unknown tool %r" % name)
    data = _store.load()
    overrides = data.setdefault("overrides", {})
    override_path = (override_path or "").strip()
    if override_path:
        overrides[name] = override_path
    else:
        overrides.pop(name, None)
    _store.save(data)
    return status(name, refresh=True)


def reset_cache_for_tests() -> None:
    """The one seam the test harness needs (XDASH_FIXES_PLAN.md F2's own
    "give the registry a clean override path for tests"): data/tools.json
    already follows XDASH_DATA_DIR, so every test gets its own file for
    free — this just makes sure no test sees a *previous* test's in-memory
    resolution once that file has been wiped out from under it."""
    with _cache_lock:
        _cache.clear()


def validate_startup() -> List[str]:
    """Freshly resolves every declared tool and prints/returns one line per
    missing or too-old REQUIRED dependency (never for the optional
    nvidia-smi probe) — called once at server startup. Also what a
    Diagnostics/Settings "Test" click re-runs for one tool via
    status(name, refresh=True)."""
    messages = []
    for name, spec in TOOL_SPECS.items():
        st = status(name, refresh=True)
        if spec.optional or st.ok:
            continue
        messages.append("[XDash] missing/broken dependency '%s' (%s): %s" % (name, spec.label, st.error))
    for msg in messages:
        print(msg, file=sys.stderr)
    return messages


# --------------------------------------------------------------------------- migration (D2)
# One-time move of kaggle_executable/colab_executable out of every
# repos/<profile>.yaml, comment-preservingly — same ruamel.yaml round-trip
# technique backend/migrate_profile.py already uses (a key's value plus its
# own same-line comment popped together, never a byte-diff rewrite of
# anything else in the file). Idempotent: a profile with neither key left is
# a no-op, safe to call on every startup.
_MIGRATED_KEYS = {"kaggle_executable": "kaggle", "colab_executable": "colab"}


def migrate_from_profiles() -> Dict[str, Any]:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096

    changed_profiles: List[str] = []
    seeded: Dict[str, str] = {}
    existing = _overrides()

    for profile_name in sorted(config_mod.list_profile_names()):
        prof_path = config_mod.REPOS_DIR / ("%s.yaml" % profile_name)
        try:
            text = prof_path.read_text()
        except OSError:
            continue
        doc = yaml.load(text)
        if not isinstance(doc, CommentedMap):
            continue
        touched = False
        for key, tool_name in _MIGRATED_KEYS.items():
            if key not in doc:
                continue
            value = str(doc[key] or "").strip()
            # Only a value that actually differs from the tool's own bare
            # name is a real override worth keeping — a profile that just
            # spelled out the default ("kaggle_executable: kaggle") has
            # nothing to seed, but the key is removed from its file either
            # way, per D2.
            if value and value != tool_name and tool_name not in existing and tool_name not in seeded:
                seeded[tool_name] = value
            doc.pop(key)
            doc.ca.items.pop(key, None)
            touched = True
        if touched:
            buf = io.StringIO()
            yaml.dump(doc, buf)
            atomic_write_text(prof_path, buf.getvalue())
            changed_profiles.append(profile_name)

    if seeded:
        data = _store.load()
        overrides = data.setdefault("overrides", {})
        for tool_name, value in seeded.items():
            overrides.setdefault(tool_name, value)
        _store.save(data)
        with _cache_lock:
            for tool_name in seeded:
                _cache.pop(tool_name, None)

    return {"changed_profiles": changed_profiles, "seeded_overrides": seeded}
