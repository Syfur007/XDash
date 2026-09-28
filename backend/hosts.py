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

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DATA_DIR, settings
from .store import JsonStore

HOSTS_FILE = DATA_DIR / "hosts.json"
_store = JsonStore(HOSTS_FILE, lambda: {"hosts": []})

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
    """scheduler.json's own max_concurrent, read through the store directly
    rather than by importing backend/scheduler.py — that module reaches
    tmux_runner, which reaches transport, which reaches this one, so a real
    import would close a cycle. It stays the single source of truth for local
    concurrency; this is a read of it, not a second copy. A corrupt
    scheduler.json raises here exactly as it would in scheduler.py itself."""
    data = JsonStore(settings.scheduler_file, dict).load()
    if not isinstance(data, dict):
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
        """XDASH_FIXES_PLAN.md D5 — only the local machine reads through to
        the active profile's own `commands.python`. A remote host that
        hasn't declared its own interpreter gets the literal default
        ("python" on its own PATH), never silently borrows this machine's —
        issue 2/6's root cause was exactly that inheritance running an SSH
        box against a conda env that only exists here."""
        if self.is_local:
            return self._fallback("python_executable", settings.python_executable)
        return self._r.get("python_executable") or "python"

    @property
    def env_activate_cmd(self) -> str:
        """See python_executable above — same D5 reasoning. Unset on a
        remote host means no activation at all, not this machine's
        `conda activate thesis`."""
        if self.is_local:
            return self._fallback("env_activate_cmd", settings.env_activate_cmd)
        return self._r.get("env_activate_cmd") or ""

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

    @property
    def accelerator(self) -> Optional[Dict[str, Any]]:
        """`{"name": ..., "vram_gb": ...}` when the record declares it — what
        an experiment's `runtime.requires.min_vram_gb` is matched against
        (XDASH_PLAN.md §6.3). Phase 2's runtime probe fills it in; until
        then it is typed on the host record by hand (and the local machine
        is probed with nvidia-smi, see runners/machine.py)."""
        acc = self._r.get("accelerator")
        return dict(acc) if isinstance(acc, dict) else None

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
    data = _store.load()
    records = data.get("hosts") if isinstance(data, dict) else None
    return [r for r in records if isinstance(r, dict) and r.get("id")] if isinstance(records, list) else []


def _save_records(records: List[Dict[str, Any]]) -> None:
    _store.save({"hosts": records})


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


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """*patch* merged onto *base*: a dict value merges key-by-key
    (recursively), anything else (a scalar, a list, an explicit None)
    replaces the base value outright. What lets a PATCH that only touched
    `repos.<profile>.repo_root` (XDASH_FIXES_PLAN.md F1.3) leave every other
    key — `accelerator`, another profile's repo entry, `ssh.port` it never
    saw — exactly as it was, instead of upsert_host()'s own full-record
    replace (§2/#6's root cause: a Settings Save that doesn't show
    `accelerator` used to erase it)."""
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def patch_host(host_id: str, patch: Dict[str, Any]) -> _Host:
    """Merges *patch* onto the existing record (XDASH_FIXES_PLAN.md F1.3) —
    the only writer the Settings tab and the legacy Machines editor use to
    *edit* a host now; POST stays create-only (server.py returns 409 for an
    id that already exists). 'local' is synthesized first if it has no row
    yet, same as set_accelerator() already does, since PATCHing it (e.g. a
    per-profile repo_root override) must work even before its first real
    write. id/kind are never changed by a patch — a PATCH is not how a host
    changes identity or kind."""
    if not isinstance(patch, dict):
        raise HostError("PATCH body must be an object")
    with _lock:
        records = _load_records()
        existing = next((r for r in records if r.get("id") == host_id), None)
        if existing is None:
            if host_id != LOCAL_HOST_ID:
                raise HostError("Unknown host '%s'" % host_id)
            existing = _default_local_record()
        merged = _deep_merge(existing, patch)
        merged["id"] = existing["id"]
        merged["kind"] = existing.get("kind") or KIND_LOCAL
        records = [r for r in records if r.get("id") != host_id]
        records.append(merged)
        _save_records(records)
    return get_host(host_id)


def set_accelerator(host_id: str, accelerator: Optional[Dict[str, Any]]) -> _Host:
    """Persists a probed (or hand-typed) `{name, vram_gb}` onto *host_id* —
    the Compute Diagnostics "GPU probe" action (XDASH_PLAN.md §8.4), and the
    only writer of this field for a host that isn't the local machine (which
    is instead probed once per process, see runners/machine.py). Synthesizes
    the local record first, same as upsert_host would need to, since 'local'
    may not have a row in hosts.json yet."""
    with _lock:
        records = _load_records()
        if not any(r.get("id") == host_id for r in records):
            if host_id != LOCAL_HOST_ID:
                raise HostError("Unknown host '%s'" % host_id)
            records = [_default_local_record()] + records
        for r in records:
            if r.get("id") == host_id:
                r["accelerator"] = dict(accelerator) if accelerator else None
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
