"""Machine tools: a small, permanent list of system/GPU monitoring commands
(nvidia-smi, htop, nvtop, df -h, ...), each launched in its own tmux session
exactly like Terminals — so viewing live output reuses the same
capture-pane mechanism, no new execution machinery needed.

A handful of built-in entries ship by default and can't be removed; entries
added through the dashboard can be. The list itself (name/command/interval)
persists in monitors.json regardless of whether anything is currently
running — that's just configuration, not live state.

**Host-agnostic catalog (XDASH_FIXES_PLAN.md F3, issues #3/#4/#5).** A
catalog entry is no longer bound to a host at all — it used to carry its own
`host_id` (defaulting to local), which is exactly what made starting a
built-in always run on local regardless of which host the caller had picked
(#5): `start_monitor()` used to read the host off the *stored record*,
never off the caller. Every function here now takes the host explicitly
(from the URL, at the server.py route layer — never from the catalog), and
a "running instance" is the pair (tool, host): the same catalog entry can be
alive on local and on three SSH hosts at once, as three independent tmux
sessions. `_session_name()` keeps local's session name byte-identical to
before this existed (only a non-local host's name grows a host suffix), so
nothing already running was orphaned by this change.

The catalog itself is deployment-level (`data/monitors.json`, sibling to
`hosts.json` — follows `XDASH_DATA_DIR` like everything else in that scope),
not per-repo-profile: a machine tool has nothing to do with which repo is
active. `migrate_from_profiles()` is the one-time startup migration off the
old per-profile `data/<profile>/monitors.json` files.
"""
from __future__ import annotations

import json
import shlex
import threading
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

from .config import DATA_DIR, list_profile_names, settings
from . import hosts
from . import tmux_runner as tmux
from . import transport as transport_mod
from .store import JsonStore

_lock = threading.Lock()

MONITOR_SESSION_INFIX = "_mon_"

# Deployment-level, sibling to hosts.HOSTS_FILE — a machine tool is a
# property of the fleet, not of whichever repo profile happens to be active
# (XDASH_FIXES_PLAN.md D2's same reasoning for backend/tools.py, applied
# here to the catalog itself rather than to CLI paths).
MONITORS_FILE = DATA_DIR / "monitors.json"

# Commands that print one static snapshot and exit (nvidia-smi, df, free) need
# `watch` to refresh; full-screen monitors that already refresh themselves
# (htop, nvtop) should just run directly — hence watch_interval: 0 for those.
DEFAULT_MONITORS: List[Dict[str, Any]] = [
    {"id": "builtin-nvidia-smi", "name": "GPU (nvidia-smi)", "command": "nvidia-smi", "watch_interval": 2, "builtin": True},
    {"id": "builtin-nvtop", "name": "GPU (nvtop)", "command": "nvtop", "watch_interval": 0, "builtin": True},
    {"id": "builtin-htop", "name": "CPU / processes (htop)", "command": "htop", "watch_interval": 0, "builtin": True},
    {"id": "builtin-free", "name": "Memory (free)", "command": "free -h", "watch_interval": 3, "builtin": True},
    {"id": "builtin-df", "name": "Disk usage (df)", "command": "df -h", "watch_interval": 5, "builtin": True},
]


_store = JsonStore(MONITORS_FILE, lambda: [dict(m) for m in DEFAULT_MONITORS])


def _load() -> List[Dict[str, Any]]:
    data = _store.load()  # raises on a corrupt file, or a missing one whose .bak survives
    if not _store.exists():
        _save(data)  # first run: persist the built-in defaults
    return data if isinstance(data, list) else [dict(m) for m in DEFAULT_MONITORS]


def _save(records: List[Dict[str, Any]]):
    _store.save(records)


def _session_name(monitor_id: str, host_id: Optional[str] = None) -> str:
    host_id = host_id or hosts.LOCAL_HOST_ID
    if host_id == hosts.LOCAL_HOST_ID:
        return f"{_prefix()}{MONITOR_SESSION_INFIX}{monitor_id}"
    return f"{_prefix()}{MONITOR_SESSION_INFIX}{host_id}_{monitor_id}"


def _prefix() -> str:
    # tmux_session_prefix is a machine fact (local's own, from the active
    # profile) — every session name in this module is prefixed with it, same
    # as before this existed.
    return settings.tmux_session_prefix


def is_monitor_session(session_name: str) -> bool:
    """Used by terminals.py to keep monitor/TensorBoard sessions off the
    Terminals page (XDASH_FIXES_PLAN.md F3.4 — TensorBoard's own per-host
    session shares this exact naming convention via backend/host_tensorboard.py,
    so it's caught here too)."""
    return MONITOR_SESSION_INFIX in session_name


def _launch_line(record: Dict[str, Any]) -> str:
    interval = record.get("watch_interval") or 0
    if interval and interval > 0:
        # Wrapped in `bash -c` so pipes/redirects in a user-supplied command
        # (e.g. "free -h | grep Mem") still work under watch.
        return f"watch -n {int(interval)} bash -c {shlex.quote(record['command'])}"
    return record["command"]


def _binary_of(command: str) -> str:
    """The executable name a catalog command actually runs — what an
    availability probe checks with `command -v`, not the whole command line
    (e.g. "free -h" -> "free", "iostat -x 1" -> "iostat")."""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else command


# --------------------------------------------------------------------------- catalog CRUD (host-agnostic)
def list_monitors() -> List[Dict[str, Any]]:
    """The catalog itself — name/command/interval/builtin, no host, no live
    state. Backs GET /api/monitors (catalog CRUD only; per-host alive/
    available state is list_tools_for_host(), below)."""
    return _load()


def add_monitor(name: str, command: str, watch_interval: int = 0) -> Dict[str, Any]:
    name, command = (name or "").strip(), (command or "").strip()
    if not name or not command:
        raise ValueError("Both a name and a command are required")
    with _lock:
        records = _load()
        record = {
            "id": uuid.uuid4().hex[:10], "name": name, "command": command,
            "watch_interval": max(0, int(watch_interval or 0)), "builtin": False,
        }
        records.append(record)
        _save(records)
    return record


def remove_monitor(monitor_id: str) -> bool:
    with _lock:
        records = _load()
        target = next((r for r in records if r["id"] == monitor_id), None)
        if target is None:
            return False
        if target.get("builtin"):
            raise ValueError("Built-in monitors can't be removed")
        # Best-effort: kill it wherever it happens to be running, across
        # every registered host — a catalog entry can be alive on more than
        # one host at once now (each is an independent (tool, host) pair).
        for host in hosts.list_hosts():
            session = _session_name(monitor_id, host.id)
            if tmux.has_session(session, host_id=host.id):
                tmux.kill_session(session, host_id=host.id)
        _save([r for r in records if r["id"] != monitor_id])
    return True


def _get_record(monitor_id: str) -> Dict[str, Any]:
    record = next((r for r in _load() if r["id"] == monitor_id), None)
    if record is None:
        raise ValueError(f"Unknown monitor '{monitor_id}'")
    return record


# --------------------------------------------------------------------------- per-host availability (F3.3)
# One `command -v <binary>` probe per host, cached 10 minutes — cheap enough
# to run every time the Tools tab opens without hitting the host on every
# poll. Keyed by host id; a host that never resolves (unreachable, or local
# development without every tool installed) simply reports every binary
# unavailable rather than raising, same "a listing hiccup must not break the
# whole board" philosophy as backend/runtimes.py's own _running_labels().
_AVAILABILITY_TTL = 600.0
_availability_lock = threading.Lock()
_availability_cache: Dict[str, Dict[str, Any]] = {}  # host_id -> {"checked_at": float, "result": {binary: bool}}


def check_availability(host_id: str, binaries: Iterable[str], force: bool = False) -> Dict[str, bool]:
    host_id = host_id or hosts.LOCAL_HOST_ID
    wanted = sorted({b for b in binaries if b})
    if not wanted:
        return {}
    now = time.monotonic()
    with _availability_lock:
        cached = _availability_cache.get(host_id)
        if not force and cached and (now - cached["checked_at"] < _AVAILABILITY_TTL) and set(wanted) <= set(cached["result"]):
            return {b: cached["result"][b] for b in wanted}
    # One round trip for every binary this host's catalog needs, not one per
    # binary: `name:yes`/`name:no` lines, parsed below, rather than relying
    # on `command -v a b c`'s own (less portable) multi-operand behavior.
    probe = " ; ".join(
        "command -v %s >/dev/null 2>&1 && echo %s || echo %s"
        % (shlex.quote(b), shlex.quote(b + ":yes"), shlex.quote(b + ":no"))
        for b in wanted
    )
    result: Dict[str, bool] = {}
    try:
        transport = transport_mod.for_host(host_id)
        proc = transport.run(["bash", "-c", probe], timeout=15)
        for line in (proc.stdout or "").splitlines():
            name, sep, flag = line.rpartition(":")
            if sep and name:
                result[name] = flag.strip() == "yes"
    except Exception:  # noqa: BLE001 — availability is best-effort, never fatal
        pass
    for b in wanted:
        result.setdefault(b, False)
    with _availability_lock:
        merged = dict(_availability_cache.get(host_id, {}).get("result", {}))
        merged.update(result)
        _availability_cache[host_id] = {"checked_at": now, "result": merged}
    return {b: merged[b] for b in wanted}


def reset_availability_cache_for_tests() -> None:
    with _availability_lock:
        _availability_cache.clear()


# --------------------------------------------------------------------------- per-host instances (F3.2 — the #5 fix)
# Every function below takes host_id explicitly. server.py's routes are the
# only caller, and they always take the host from the URL
# (/api/hosts/<host_id>/tools/...) — never from the catalog entry itself.
def list_tools_for_host(host_id: Optional[str] = None) -> List[Dict[str, Any]]:
    host = hosts.get_host(host_id)  # raises HostError on an unknown host — routes map that to 404
    catalog = _load()
    availability = check_availability(host.id, (_binary_of(m["command"]) for m in catalog))
    out = []
    for m in catalog:
        session = _session_name(m["id"], host.id)
        binary = _binary_of(m["command"])
        available = availability.get(binary, False)
        out.append({
            **m,
            "session_name": session,
            "alive": tmux.has_session(session, host_id=host.id),
            "available": available,
            "available_detail": None if available else f"not installed on {host.label}",
        })
    return out


def start_monitor(monitor_id: str, host_id: Optional[str] = None) -> Dict[str, Any]:
    record = _get_record(monitor_id)
    host = hosts.get_host(host_id)
    session = _session_name(monitor_id, host.id)
    if not tmux.has_session(session, host_id=host.id):
        tmux.new_session(session, host_id=host.id)
        tmux.send_keys(session, _launch_line(record), host_id=host.id)
    return {**record, "session_name": session, "alive": True}


def stop_monitor(monitor_id: str, host_id: Optional[str] = None) -> Dict[str, Any]:
    record = _get_record(monitor_id)
    host = hosts.get_host(host_id)
    session = _session_name(monitor_id, host.id)
    if tmux.has_session(session, host_id=host.id):
        tmux.kill_session(session, host_id=host.id)
    return {**record, "session_name": session, "alive": False}


def get_output(monitor_id: str, host_id: Optional[str] = None) -> Dict[str, Any]:
    record = _get_record(monitor_id)  # validates the id and 404s cleanly if unknown
    host = hosts.get_host(host_id)
    session = _session_name(monitor_id, host.id)
    if not tmux.has_session(session, host_id=host.id):
        return {"alive": False, "output": ""}
    return {"alive": True, "output": tmux.capture_pane(session, host_id=host.id) or ""}


# --------------------------------------------------------------------------- migration (F3.1)
def migrate_from_profiles() -> None:
    """One-time startup migration: merges every `data/<profile>/monitors.json`
    (the old per-profile, host_id-bound catalog) into the new deployment-level
    `data/monitors.json`, dropping `host_id` entirely and deduping by command
    — a legacy entry whose command already exists in the new catalog (every
    built-in does, from DEFAULT_MONITORS) is simply skipped. Runs once per
    process start (server.py, at import time, right alongside
    backend/tools.py's own migration); every subsequent start is a no-op
    because there's nothing new left to merge."""
    with _lock:
        catalog = _load()
        seen_commands = {(m.get("command") or "").strip() for m in catalog}
        changed = False
        for profile_name in sorted(list_profile_names()):
            legacy_path = DATA_DIR / profile_name / "monitors.json"
            if not legacy_path.is_file():
                continue
            try:
                legacy_data = json.loads(legacy_path.read_text())
            except (OSError, ValueError):
                continue
            for entry in (legacy_data if isinstance(legacy_data, list) else []):
                if not isinstance(entry, dict):
                    continue
                command = (entry.get("command") or "").strip()
                if not command or command in seen_commands:
                    continue
                catalog.append({
                    "id": entry.get("id") or uuid.uuid4().hex[:10],
                    "name": entry.get("name") or command,
                    "command": command,
                    "watch_interval": max(0, int(entry.get("watch_interval") or 0)),
                    "builtin": bool(entry.get("builtin")),
                })
                seen_commands.add(command)
                changed = True
        if changed:
            _save(catalog)
