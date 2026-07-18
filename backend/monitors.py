"""Machine Stats: a small, permanent list of system/GPU monitoring commands
(nvidia-smi, htop, nvtop, df -h, ...), each launched in its own tmux session
exactly like Terminals — so viewing live output reuses the same
capture-pane mechanism, no new execution machinery needed.

A handful of built-in entries ship by default and can't be removed; entries
added through the dashboard can be. The list itself (name/command/interval)
persists in monitors.json regardless of whether anything is currently
running — that's just configuration, not live state.
"""
from __future__ import annotations

import json
import shlex
import threading
import uuid
from typing import Any, Dict, List

from .config import settings
from . import tmux_runner as tmux

_lock = threading.Lock()

MONITOR_SESSION_INFIX = "_mon_"

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


def _session_name(monitor_id: str) -> str:
    return f"{settings.tmux_session_prefix}{MONITOR_SESSION_INFIX}{monitor_id}"


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
    return [{**r, "session_name": _session_name(r["id"]), "alive": tmux.has_session(_session_name(r["id"]))} for r in records]


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
    return {**record, "session_name": _session_name(record["id"]), "alive": False}


def remove_monitor(monitor_id: str) -> bool:
    with _lock:
        records = _load()
        target = next((r for r in records if r["id"] == monitor_id), None)
        if target is None:
            return False
        if target.get("builtin"):
            raise ValueError("Built-in monitors can't be removed")
        session = _session_name(monitor_id)
        if tmux.has_session(session):
            tmux.kill_session(session)
        _save([r for r in records if r["id"] != monitor_id])
    return True


def _get_record(monitor_id: str) -> Dict[str, Any]:
    record = next((r for r in _load() if r["id"] == monitor_id), None)
    if record is None:
        raise ValueError(f"Unknown monitor '{monitor_id}'")
    return record


def start_monitor(monitor_id: str) -> Dict[str, Any]:
    record = _get_record(monitor_id)
    session = _session_name(monitor_id)
    if not tmux.has_session(session):
        tmux.new_session(session)
        tmux.send_keys(session, _launch_line(record))
    return {**record, "session_name": session, "alive": True}


def stop_monitor(monitor_id: str) -> Dict[str, Any]:
    record = _get_record(monitor_id)
    session = _session_name(monitor_id)
    if tmux.has_session(session):
        tmux.kill_session(session)
    return {**record, "session_name": session, "alive": False}


def get_output(monitor_id: str) -> Dict[str, Any]:
    _get_record(monitor_id)  # validates the id and 404s cleanly if unknown
    session = _session_name(monitor_id)
    if not tmux.has_session(session):
        return {"alive": False, "output": ""}
    return {"alive": True, "output": tmux.capture_pane(session) or ""}
