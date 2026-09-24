"""Machine Stats: a small, permanent list of system/GPU monitoring commands
(nvidia-smi, htop, nvtop, df -h, ...), each launched in its own tmux session
exactly like Terminals — so viewing live output reuses the same
capture-pane mechanism, no new execution machinery needed.

A handful of built-in entries ship by default and can't be removed; entries
added through the dashboard can be. The list itself (name/command/interval/
host_id) persists in monitors.json regardless of whether anything is
currently running — that's just configuration, not live state.

**Host-aware since backend/hosts.py landed.** Each monitor entry carries its
own `host_id` (default the local machine, exactly as before this existed),
so "nvidia-smi on mclab-gpu2" and "nvidia-smi here" are two independent
entries rather than one that only ever meant local. The *local* entry's
session name is kept byte-identical to before hosts existed at all — only a
non-local entry's name grows a host suffix, so nothing already running is
orphaned by this change.
"""
from __future__ import annotations

import json
import shlex
import threading
import uuid
from typing import Any, Dict, List, Optional

from .config import settings
from . import hosts
from . import tmux_runner as tmux

_lock = threading.Lock()

MONITOR_SESSION_INFIX = "_mon_"

# Commands that print one static snapshot and exit (nvidia-smi, df, free) need
# `watch` to refresh; full-screen monitors that already refresh themselves
# (htop, nvtop) should just run directly — hence watch_interval: 0 for those.
DEFAULT_MONITORS: List[Dict[str, Any]] = [
    {"id": "builtin-nvidia-smi", "name": "GPU (nvidia-smi)", "command": "nvidia-smi", "watch_interval": 2, "builtin": True, "host_id": hosts.LOCAL_HOST_ID},
    {"id": "builtin-nvtop", "name": "GPU (nvtop)", "command": "nvtop", "watch_interval": 0, "builtin": True, "host_id": hosts.LOCAL_HOST_ID},
    {"id": "builtin-htop", "name": "CPU / processes (htop)", "command": "htop", "watch_interval": 0, "builtin": True, "host_id": hosts.LOCAL_HOST_ID},
    {"id": "builtin-free", "name": "Memory (free)", "command": "free -h", "watch_interval": 3, "builtin": True, "host_id": hosts.LOCAL_HOST_ID},
    {"id": "builtin-df", "name": "Disk usage (df)", "command": "df -h", "watch_interval": 5, "builtin": True, "host_id": hosts.LOCAL_HOST_ID},
]


def _load() -> List[Dict[str, Any]]:
    if not settings.monitors_file.exists():
        _save(DEFAULT_MONITORS)
        return [dict(m) for m in DEFAULT_MONITORS]
    try:
        data = json.loads(settings.monitors_file.read_text())
        return data if isinstance(data, list) else [dict(m) for m in DEFAULT_MONITORS]
    except Exception:
        return [dict(m) for m in DEFAULT_MONITORS]


def _save(records: List[Dict[str, Any]]):
    settings.monitors_file.write_text(json.dumps(records, indent=2))


def _host_id_of(record: Dict[str, Any]) -> str:
    return record.get("host_id") or hosts.LOCAL_HOST_ID


def _session_name(monitor_id: str, host_id: Optional[str] = None) -> str:
    host_id = host_id or hosts.LOCAL_HOST_ID
    if host_id == hosts.LOCAL_HOST_ID:
        return f"{settings.tmux_session_prefix}{MONITOR_SESSION_INFIX}{monitor_id}"
    return f"{settings.tmux_session_prefix}{MONITOR_SESSION_INFIX}{host_id}_{monitor_id}"


def is_monitor_session(session_name: str) -> bool:
    """Used by terminals.py to keep monitor sessions off the Terminals page."""
    return MONITOR_SESSION_INFIX in session_name


def _launch_line(record: Dict[str, Any]) -> str:
    interval = record.get("watch_interval") or 0
    if interval and interval > 0:
        # Wrapped in `bash -c` so pipes/redirects in a user-supplied command
        # (e.g. "free -h | grep Mem") still work under watch.
        return f"watch -n {int(interval)} bash -c {shlex.quote(record['command'])}"
    return record["command"]


def list_monitors() -> List[Dict[str, Any]]:
    records = _load()
    return [
        {**r, "session_name": _session_name(r["id"], _host_id_of(r)),
         "alive": tmux.has_session(_session_name(r["id"], _host_id_of(r)), host_id=_host_id_of(r))}
        for r in records
    ]


def add_monitor(name: str, command: str, watch_interval: int = 0, host_id: Optional[str] = None) -> Dict[str, Any]:
    name, command = (name or "").strip(), (command or "").strip()
    if not name or not command:
        raise ValueError("Both a name and a command are required")
    host = hosts.get_host(host_id)  # raises HostError on an unknown host
    with _lock:
        records = _load()
        record = {
            "id": uuid.uuid4().hex[:10], "name": name, "command": command,
            "watch_interval": max(0, int(watch_interval or 0)), "builtin": False,
            "host_id": host.id,
        }
        records.append(record)
        _save(records)
    return {**record, "session_name": _session_name(record["id"], host.id), "alive": False}


def remove_monitor(monitor_id: str) -> bool:
    with _lock:
        records = _load()
        target = next((r for r in records if r["id"] == monitor_id), None)
        if target is None:
            return False
        if target.get("builtin"):
            raise ValueError("Built-in monitors can't be removed")
        host_id = _host_id_of(target)
        session = _session_name(monitor_id, host_id)
        if tmux.has_session(session, host_id=host_id):
            tmux.kill_session(session, host_id=host_id)
        _save([r for r in records if r["id"] != monitor_id])
    return True


def _get_record(monitor_id: str) -> Dict[str, Any]:
    record = next((r for r in _load() if r["id"] == monitor_id), None)
    if record is None:
        raise ValueError(f"Unknown monitor '{monitor_id}'")
    return record


def start_monitor(monitor_id: str) -> Dict[str, Any]:
    record = _get_record(monitor_id)
    host_id = _host_id_of(record)
    session = _session_name(monitor_id, host_id)
    if not tmux.has_session(session, host_id=host_id):
        tmux.new_session(session, host_id=host_id)
        tmux.send_keys(session, _launch_line(record), host_id=host_id)
    return {**record, "session_name": session, "alive": True}


def stop_monitor(monitor_id: str) -> Dict[str, Any]:
    record = _get_record(monitor_id)
    host_id = _host_id_of(record)
    session = _session_name(monitor_id, host_id)
    if tmux.has_session(session, host_id=host_id):
        tmux.kill_session(session, host_id=host_id)
    return {**record, "session_name": session, "alive": False}


def get_output(monitor_id: str) -> Dict[str, Any]:
    record = _get_record(monitor_id)  # validates the id and 404s cleanly if unknown
    host_id = _host_id_of(record)
    session = _session_name(monitor_id, host_id)
    if not tmux.has_session(session, host_id=host_id):
        return {"alive": False, "output": ""}
    return {"alive": True, "output": tmux.capture_pane(session, host_id=host_id) or ""}
