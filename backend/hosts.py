"""Machine records — every place XDash can open a tmux session.

**The local machine is a host like any other.** Its only distinction is that
this module *synthesizes* it when `data/hosts.json` has no entry for it, so
the file stays purely additive: a deployment that has never seen it behaves
exactly as it did before, and `list_hosts()` still returns one usable host.

System scope, sibling to data/kaggle_accounts.json rather than under
data/<profile>/ — a machine is a property of the fleet, not of the repo,
for the same reason a Kaggle account is (see backend/config.py's
SYSTEM_KAGGLE_ACCOUNTS_FILE comment). One box can hold checkouts of several
repos, so the per-profile checkout location nests *inside* the record
(`repos`) instead of the record living inside a profile.

Every machine fact is nullable and falls back to the active profile
(`repos/<profile>.yaml`). That is deliberate: for a single-machine
deployment the profile stays the one place these are configured, and the
local host record never becomes a second, competing copy of them.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DASHBOARD_DIR, settings

HOSTS_FILE = DASHBOARD_DIR / "data" / "hosts.json"

LOCAL_HOST_ID = "local"
KIND_LOCAL = "local"
KIND_SSH = "ssh"
KIND_COLAB = "colab"

_lock = threading.Lock()


class HostError(Exception):
    """Expected failure (unknown id, bad record) — routes map this to a 4xx."""


def _default_local_record() -> Dict[str, Any]:
    """The local machine as a record. Every machine fact is None so it reads
    through to the active profile — see _Host's resolvers below."""
    return {
        "id": LOCAL_HOST_ID,
        "kind": KIND_LOCAL,
        "label": "This machine",
        "max_concurrent": None,      # None -> scheduler.json, see _Host.max_concurrent
        "python_executable": None,
        "env_activate_cmd": None,
        "tmux_session_prefix": None,
        "repos": {},
    }


def _scheduler_max_concurrent() -> int:
    """scheduler.json's own max_concurrent, read as plain JSON rather than by
    importing backend/scheduler.py — that module reaches tmux_runner, which
    reaches transport, which reaches this one, so a real import would close a
    cycle. It stays the single source of truth for local concurrency; this is
    a read of it, not a second copy."""
    try:
        data = json.loads(settings.scheduler_file.read_text())
    except (OSError, ValueError):
        return 1
    try:
        return max(1, int(data.get("max_concurrent", 1)))
    except (TypeError, ValueError):
        return 1


class _Host:
    """One machine record, with every machine fact resolved against the
    active profile. Read-only: mutate through upsert_host()."""

    def __init__(self, record: Dict[str, Any]):
        self._r = record

    # -- identity ---------------------------------------------------------
    @property
    def id(self) -> str:
        return self._r["id"]

    @property
    def kind(self) -> str:
        return self._r.get("kind") or KIND_SSH

    @property
    def label(self) -> str:
        return self._r.get("label") or self.id

    @property
    def is_local(self) -> bool:
        return self.kind == KIND_LOCAL

    # -- machine facts (None -> the active profile) -----------------------
    def _fallback(self, key: str, default: Any) -> Any:
        value = self._r.get(key)
        return default if value is None or value == "" else value

    @property
    def repo_root(self) -> Path:
        """This host's checkout of the *active* profile's repo. A host that
        doesn't declare one for this profile falls back to the profile's own
        repo_root, which is correct for local and a configuration error for a
        remote — surfaced by can_accept(), not by guessing here."""
        entry = (self._r.get("repos") or {}).get(settings.profile_name) or {}
        root = entry.get("repo_root")
        return Path(root) if root else settings.repo_root

    @property
    def declares_repo_root(self) -> bool:
        return bool(((self._r.get("repos") or {}).get(settings.profile_name) or {}).get("repo_root"))

    @property
    def python_executable(self) -> str:
        return self._fallback("python_executable", settings.python_executable)

    @property
    def env_activate_cmd(self) -> str:
        return self._fallback("env_activate_cmd", settings.env_activate_cmd)

    @property
    def tmux_session_prefix(self) -> str:
        return self._fallback("tmux_session_prefix", settings.tmux_session_prefix)

    @property
    def tmux_pane_width(self) -> int:
        return int(self._fallback("tmux_pane_width", settings.tmux_pane_width))

    @property
    def tmux_pane_height(self) -> int:
        return int(self._fallback("tmux_pane_height", settings.tmux_pane_height))

    @property
    def tmux_history_limit(self) -> int:
        return int(self._fallback("tmux_history_limit", settings.tmux_history_limit))

    @property
    def max_concurrent(self) -> int:
        """An explicit value on the record wins. Otherwise the local host
        reads scheduler.json — which keeps POST /api/scheduler/max_concurrent
        working unchanged and avoids a migration — and a remote defaults to 1."""
        value = self._r.get("max_concurrent")
        if value is not None:
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                pass
        return _scheduler_max_concurrent() if self.is_local else 1

    # -- ssh --------------------------------------------------------------
    @property
    def ssh(self) -> Dict[str, Any]:
        return dict(self._r.get("ssh") or {})

    def as_dict(self) -> Dict[str, Any]:
        """The record plus everything it resolved to, so the UI can show both
        "inherited" and the effective value without re-deriving the fallback."""
        return {
            **self._r,
            "resolved": {
                "repo_root": str(self.repo_root),
                "declares_repo_root": self.declares_repo_root,
                "python_executable": self.python_executable,
                "env_activate_cmd": self.env_activate_cmd,
                "tmux_session_prefix": self.tmux_session_prefix,
                "max_concurrent": self.max_concurrent,
            },
        }


# ------------------------------------------------------------------ storage
def _load_records() -> List[Dict[str, Any]]:
    if not HOSTS_FILE.exists():
        return []
    try:
        data = json.loads(HOSTS_FILE.read_text())
    except (OSError, ValueError):
        return []
    records = data.get("hosts") if isinstance(data, dict) else None
    return [r for r in records if isinstance(r, dict) and r.get("id")] if isinstance(records, list) else []


def _save_records(records: List[Dict[str, Any]]) -> None:
    HOSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HOSTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"hosts": records}, indent=2))
    tmp.replace(HOSTS_FILE)


def list_hosts() -> List[_Host]:
    """Every configured host, with the local one synthesized if absent. Local
    is always first — it is the default target and the one that always
    exists."""
    records = _load_records()
    if not any(r.get("id") == LOCAL_HOST_ID for r in records):
        records = [_default_local_record()] + records
    else:
        records = sorted(records, key=lambda r: r.get("id") != LOCAL_HOST_ID)
    return [_Host(r) for r in records]


def get_host(host_id: Optional[str]) -> _Host:
    """Resolve a host id. None/"" means local, so every call site that hasn't
    been taught about hosts yet keeps working."""
    wanted = host_id or LOCAL_HOST_ID
    for host in list_hosts():
        if host.id == wanted:
            return host
    raise HostError("Unknown host '%s'" % wanted)


def host_exists(host_id: str) -> bool:
    try:
        get_host(host_id)
        return True
    except HostError:
        return False


def upsert_host(record: Dict[str, Any]) -> _Host:
    host_id = (record.get("id") or "").strip()
    if not host_id:
        raise HostError("A host needs an id")
    kind = record.get("kind") or KIND_SSH
    if kind not in (KIND_LOCAL, KIND_SSH, KIND_COLAB):
        raise HostError("Unknown host kind '%s'" % kind)
    if kind != KIND_LOCAL and not (record.get("ssh") or {}).get("host"):
        raise HostError("A %s host needs ssh.host" % kind)
    with _lock:
        records = _load_records()
        records = [r for r in records if r.get("id") != host_id]
        records.append({**record, "id": host_id, "kind": kind})
        _save_records(records)
    return get_host(host_id)


def remove_host(host_id: str) -> bool:
    if host_id == LOCAL_HOST_ID:
        raise HostError("The local machine cannot be removed")
    with _lock:
        records = _load_records()
        remaining = [r for r in records if r.get("id") != host_id]
        if len(remaining) == len(records):
            return False
        _save_records(remaining)
    return True
