"""Google Colab account registry + CLI wrapper (Multi_runner_XDash.md
Phase 4) — provisions/tears down ephemeral VMs via the `google-colab-cli`
(`colab` on PATH), mirroring backend/kaggle.py's per-account subprocess
isolation (`_run_kaggle`'s own docstring) but for a fundamentally different
auth model: Kaggle uses a static username/key or token; Colab's `--auth
{oauth2,adc}` needs an interactive browser login once per account, captured
into a `sessions.json`/`oauth-config.json` pair XDash only stores paths to
and never generates or validates the *content* of. That interactive
"Connect account" capture flow is Compute-tab UI (Phase 6) — this module
assumes the pair already exists once a human has run `colab login`
out-of-band and dropped the files here, exactly as add_account() below
expects.

**UNVERIFIED.** Unlike kaggle.py's `_STATUS_ALIASES` (confirmed against a
real installed CLI — see its own comment), the exact `colab new`/`status`/
`sessions`/`stop` invocations and their output shape below are taken
directly from Multi_runner_XDash.md's own research, not from a live install
— none was available while writing this. Every parse is defensive (JSON
first, a plain-text fallback, `ColabOpsError` with the raw output attached
otherwise) specifically so a wrong guess here fails loud and diagnosably on
first real use instead of silently misreading a differently-shaped response.
Verify against the real CLI before the first live dispatch.

Single scope, unlike Kaggle's system/repo split: there is no pre-existing
repo-scoped Colab registry to stay backward compatible with (Kaggle's split
exists because one already existed), so every account is system-wide
(SYSTEM_COLAB_ACCOUNTS_FILE) — correct for the same reason a Kaggle system
account is (config.py's own comment): a Google account's session limit is a
property of the person/tier, shared across every repo profile.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import settings, SYSTEM_COLAB_ACCOUNTS_FILE, SYSTEM_COLAB_CREDS_DIR

_lock = threading.Lock()          # guards colab_accounts.json

SESSIONS_FILENAME = "sessions.json"
OAUTH_CONFIG_FILENAME = "oauth-config.json"


class ColabOpsError(Exception):
    """Expected failure (bad account, subprocess error, CLI absent) — routes
    map this to a 4xx, not a stack trace."""


class ProvisionError(ColabOpsError):
    """`colab new` (or the wait for SSH afterward) failed outright — maps to
    the `provision-failed` blocked code."""


class NoAcceleratorError(ColabOpsError):
    """`colab new` succeeded but `colab status` reports no GPU/TPU actually
    granted (Colab-constraints table: "a Pro+ request for A100 can be handed
    a T4; free tier can be denied a GPU entirely") — maps to the
    `no-accelerator` blocked code, distinct from a bare provisioning failure
    since the fix is different (retry later / different tier), not "broken"."""


# --------------------------------------------------------------------------- storage
def _load() -> Dict[str, Any]:
    if not SYSTEM_COLAB_ACCOUNTS_FILE.exists():
        return {"accounts": []}
    try:
        data = json.loads(SYSTEM_COLAB_ACCOUNTS_FILE.read_text())
    except Exception:
        return {"accounts": []}
    data.setdefault("accounts", [])
    return data


def _save(data: Dict[str, Any]) -> None:
    SYSTEM_COLAB_ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SYSTEM_COLAB_ACCOUNTS_FILE.write_text(json.dumps(data, indent=2))


def _find(data: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    return next((a for a in data["accounts"] if a["name"] == name), None)


def find_account(name: str) -> Optional[Dict[str, Any]]:
    return _find(_load(), name)


def _creds_dir(name: str) -> Path:
    return SYSTEM_COLAB_CREDS_DIR / name


def list_accounts() -> List[Dict[str, Any]]:
    """Accounts with whether each has a credential pair on disk. Never
    touches the network — see list_sessions()/session_status() for the live
    VM-state queries."""
    data = _load()
    result = []
    for account in data["accounts"]:
        creds_dir = _creds_dir(account["name"])
        result.append({
            "name": account["name"],
            "label": account.get("label") or account["name"],
            "gpu": account.get("gpu") or settings.colab_default_gpu,
            "session_limit_hours": account.get("session_limit_hours"),
            "has_credentials": (creds_dir / SESSIONS_FILENAME).is_file(),
        })
    return result


def add_account(name: str, label: str = "", gpu: str = "", session_limit_hours: Optional[float] = None) -> Dict[str, Any]:
    """Registers *name*; does not itself capture credentials — a human runs
    `colab login` out-of-band (or, once Phase 6 builds it, an interactive
    "Connect account" flow) and drops sessions.json/oauth-config.json into
    this account's creds dir. Registering first (with no credentials yet) is
    deliberate: it's what gives the Connect-account flow a stable directory
    to write into."""
    name = (name or "").strip()
    if not name:
        raise ColabOpsError("Missing account name")
    with _lock:
        data = _load()
        if _find(data, name) is not None:
            raise ColabOpsError(f"Account '{name}' already exists")
        _creds_dir(name).mkdir(parents=True, exist_ok=True)
        record = {"name": name, "label": (label or "").strip() or name}
        if gpu.strip():
            record["gpu"] = gpu.strip()
        if session_limit_hours is not None:
            record["session_limit_hours"] = float(session_limit_hours)
        data["accounts"].append(record)
        _save(data)
    return record


def remove_account(name: str) -> bool:
    with _lock:
        data = _load()
        before = len(data["accounts"])
        data["accounts"] = [a for a in data["accounts"] if a["name"] != name]
        if len(data["accounts"]) == before:
            return False
        _save(data)
    return True


def set_session_limit(name: str, hours: Optional[float]) -> Dict[str, Any]:
    with _lock:
        data = _load()
        account = _find(data, name)
        if account is None:
            raise ColabOpsError(f"Unknown account '{name}'")
        if hours is None:
            account.pop("session_limit_hours", None)
        else:
            account["session_limit_hours"] = float(hours)
        _save(data)
    return account


def session_limit_hours(name: str) -> float:
    account = find_account(name)
    limit = (account or {}).get("session_limit_hours")
    return float(limit) if limit is not None else settings.colab_default_session_limit_hours


# --------------------------------------------------------------------------- CLI
def colab_available() -> bool:
    """Cheap PATH check, same discipline as tmux_runner.tmux_available()'s
    local branch — a missing `colab` binary degrades this kind to
    "unavailable", never a 500."""
    import shutil
    return shutil.which(settings.colab_executable) is not None


def _session_name(account_name: str) -> str:
    return "xdash-%s" % account_name


def _run_colab(args: List[str], account_name: str, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    creds_dir = _creds_dir(account_name)
    sessions_path, oauth_path = creds_dir / SESSIONS_FILENAME, creds_dir / OAUTH_CONFIG_FILENAME
    if not sessions_path.is_file():
        raise ColabOpsError(
            f"No credentials stored for Colab account '{account_name}' — run `colab login` into "
            f"{creds_dir} first (see this module's docstring)."
        )
    full_args = [settings.colab_executable, *args, "--config", str(sessions_path)]
    if oauth_path.is_file():
        full_args += ["-c", str(oauth_path)]
    try:
        return subprocess.run(full_args, env=dict(os.environ), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise ColabOpsError(
            f"'{settings.colab_executable}' was not found on PATH. Set colab_executable in "
            f"repos/{settings.profile_name}.yaml to wherever it's installed."
        )
    except subprocess.TimeoutExpired:
        raise ColabOpsError(f"colab {' '.join(args)} timed out after {timeout}s")


def _parse_json_or_none(text: str) -> Optional[Any]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


# Sessions/status listings are polled once per account per dispatch tick (the
# same shape as kaggle.py's own _usage_cache) — a TTL cache keeps an idle
# fleet of Colab accounts from re-invoking the CLI every ~30s poll tick per
# account for state that doesn't change nearly that often.
_SESSIONS_CACHE_TTL_SECONDS = 20.0
_sessions_cache: Dict[str, tuple] = {}  # account_name -> (monotonic_time, result)


def list_sessions(account_name: str) -> List[Dict[str, Any]]:
    """This account's currently-live VMs, however many `colab sessions`
    reports (expected: 0 or 1, given `-s xdash-<account>` names exactly one).
    Returns [] on any read failure — a poll loop must degrade to "unknown,
    treat as idle" rather than raise, same discipline as tmux_runner's own
    returncode-127 convention."""
    now = time.monotonic()
    cached = _sessions_cache.get(account_name)
    if cached is not None and (now - cached[0]) < _SESSIONS_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        proc = _run_colab(["sessions"], account_name, timeout=30)
    except ColabOpsError:
        return []
    result: List[Dict[str, Any]] = []
    if proc.returncode == 0:
        parsed = _parse_json_or_none(proc.stdout)
        rows = parsed if isinstance(parsed, list) else (parsed or {}).get("sessions") if isinstance(parsed, dict) else None
        if rows is None:
            # Fallback: at minimum detect whether *our* named session shows up
            # in plain-text output, so a CLI without --json/structured output
            # still degrades to a correct yes/no rather than a crash.
            rows = [{"name": _session_name(account_name)}] if _session_name(account_name) in proc.stdout else []
        result = [r for r in rows if isinstance(r, dict) and r.get("name") == _session_name(account_name)]
    _sessions_cache[account_name] = (now, result)
    return result


def _invalidate_sessions_cache(account_name: str) -> None:
    _sessions_cache.pop(account_name, None)


def session_status(account_name: str) -> Optional[Dict[str, Any]]:
    """Detail for this account's named session (accelerator granted, proxy
    command, host key info) — `colab status -s xdash-<account>`. None if no
    such session exists."""
    try:
        proc = _run_colab(["status", "-s", _session_name(account_name)], account_name, timeout=30)
    except ColabOpsError:
        return None
    if proc.returncode != 0:
        return None
    parsed = _parse_json_or_none(proc.stdout)
    return parsed if isinstance(parsed, dict) else {"raw": proc.stdout.strip()}


def ensure_session(account_name: str, gpu: str = "", timeout: float = 300.0) -> Dict[str, Any]:
    """Returns {"proxy_command": str, "accelerator": str|None} for a live VM
    for *account_name*, reusing one already up (keeps it warm across
    attempts of the same batch) or provisioning a fresh one via `colab new`.

    Raises ProvisionError if the CLI/provisioning itself fails, or
    NoAcceleratorError if provisioning succeeded but no GPU/TPU was actually
    granted — see this module's docstring for why these need to be
    distinguished (Colab-constraints table's compute-units-vs-GPU point)."""
    existing = list_sessions(account_name)
    if not existing:
        gpu = gpu or settings.colab_default_gpu
        try:
            proc = _run_colab(["new", "--gpu", gpu, "-s", _session_name(account_name)], account_name, timeout=timeout)
        except ColabOpsError as e:
            raise ProvisionError(str(e))
        if proc.returncode != 0:
            raise ProvisionError((proc.stderr or proc.stdout or "colab new failed").strip()[-500:])
        _invalidate_sessions_cache(account_name)

    status = session_status(account_name)
    if status is None:
        raise ProvisionError(f"'colab new' reported success but 'colab status' can't find session '{_session_name(account_name)}'")

    accelerator = status.get("accelerator") or status.get("gpu") or status.get("gpu_type")
    if not accelerator or str(accelerator).lower() in ("none", "cpu", ""):
        raise NoAcceleratorError(
            f"Colab granted no accelerator for account '{account_name}' this attempt "
            f"(status: {json.dumps(status)[:200]})"
        )

    proxy_command = status.get("proxy_command") or (
        "%s ssh --proxy-mode -s %s --config %s"
        % (settings.colab_executable, _session_name(account_name), _creds_dir(account_name) / SESSIONS_FILENAME)
    )
    return {"proxy_command": proxy_command, "accelerator": str(accelerator)}


def stop_session(account_name: str) -> bool:
    try:
        proc = _run_colab(["stop", "-s", _session_name(account_name)], account_name, timeout=60)
    except ColabOpsError:
        return False
    _invalidate_sessions_cache(account_name)
    return proc.returncode == 0
