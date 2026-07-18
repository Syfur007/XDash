"""Tracks experiments as tmux sessions instead of a job queue.

There is deliberately no background worker thread here. Starting an
experiment just creates a tmux session and records what config/mode launched
it; everything else (is it still running? did it finish? with what exit
code?) is computed on demand by reading the tmux pane when the frontend
asks — via a small sentinel line (`echo __EXPDASH_DONE__<session>:$?`)
appended to the launched command, the same trick as before, just without a
thread continuously watching it.

This also means restarting the dashboard process loses nothing: there's no
in-memory state to reconstruct, only the small metadata file recording which
sessions were launched for which config.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import settings
from . import configs as cfg
from . import tmux_runner as tmux
from . import reports
from . import monitors
from .log_parser import parse_log_text

_lock = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def marker_for(session_name: str) -> str:
    return f"{tmux.DONE_MARKER}_{session_name}"


# ------------------------------------------------------------------ storage
def _load() -> List[Dict[str, Any]]:
    if not settings.state_file.exists():
        return []
    try:
        return json.loads(settings.state_file.read_text())
    except Exception:
        return []


def _save(records: List[Dict[str, Any]]):
    settings.state_file.write_text(json.dumps(records, indent=2))


# -------------------------------------------------------------------- start
def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return slug[:40] or "run"


def _start_session(session_name: str, config_path: str, mode: str, extra_args: str) -> str:
    """Creates the tmux session and types the launch command. Returns the
    exact command string that was run (for display)."""
    script = settings.train_script if mode == "train" else settings.eval_script
    extra_flags = list(settings.eval_default_args) if mode == "eval" else []
    cli_config_path = cfg.repo_relative_path(config_path)
    launch_cmd = tmux.build_launch_command(
        settings.python_executable, script, cli_config_path, extra_flags, extra_args
    )
    marker = marker_for(session_name)

    tmux.new_session(session_name)
    tmux.send_keys(session_name, f"cd {settings.repo_root}")
    if settings.env_activate_cmd:
        tmux.send_keys(session_name, settings.env_activate_cmd)
    tmux.send_keys(session_name, f"{launch_cmd}; echo {marker}:$?")
    return launch_cmd


def launch(config_path: str, mode: str, extra_args: str = "") -> Dict[str, Any]:
    cfg.read_config(config_path)  # raises FileNotFoundError / ValueError if bad
    experiment_name = cfg.get_experiment_name(config_path)
    session_name = f"{settings.tmux_session_prefix}_{_slugify(experiment_name)}_{uuid.uuid4().hex[:6]}"

    command = _start_session(session_name, config_path, mode, extra_args)

    record = {
        "session_name": session_name,
        "config_path": config_path,
        "mode": mode,
        "extra_args": extra_args.strip(),
        "experiment_name": experiment_name,
        "command": command,
        "created_at": _now(),
        "restart_count": 0,
    }
    with _lock:
        records = _load()
        records.append(record)
        _save(records)
    return _status_for(record)


def restart(session_name: str) -> Dict[str, Any]:
    with _lock:
        records = _load()
        record = next((r for r in records if r["session_name"] == session_name), None)
        if record is None:
            raise ValueError("Unknown terminal")

        alive_sessions = set(tmux.list_sessions())
        if session_name in alive_sessions:
            current = _status_for(record, alive_sessions)
            if current["status"] == "running":
                raise ValueError("This terminal is still running — stop or kill it before restarting.")
            # Alive but idle (stopped/completed/failed): snapshot and clear it
            # out first so we can relaunch under the same session name.
            text = tmux.capture_pane(session_name)
            if text:
                _save_snapshot(session_name, text)
            tmux.kill_session(session_name)

        cfg.read_config(record["config_path"])  # re-validate the config still exists
        command = _start_session(session_name, record["config_path"], record["mode"], record["extra_args"])
        record["command"] = command
        record["created_at"] = _now()
        record["restart_count"] = record.get("restart_count", 0) + 1
        _save(records)
    return _status_for(record)


# ------------------------------------------------------------------ control
def stop(session_name: str) -> bool:
    """Interrupt the running command (Ctrl-C) but keep the session/shell alive."""
    if session_name not in tmux.list_sessions():
        return False
    tmux.send_ctrl_c(session_name)
    time.sleep(0.4)
    tmux.send_keys(session_name, f"echo {marker_for(session_name)}:130")
    return True


def kill(session_name: str) -> bool:
    """Kill the tmux session entirely and forget about it. Best-effort saves
    a final snapshot of its output first, so the last thing it printed isn't
    just lost."""
    if session_name in tmux.list_sessions():
        text = tmux.capture_pane(session_name)
        if text:
            _save_snapshot(session_name, text)
        tmux.kill_session(session_name)
    with _lock:
        records = _load()
        records = [r for r in records if r["session_name"] != session_name]
        _save(records)
    return True


def _snapshot_path(session_name: str):
    return settings.dashboard_log_dir / f"{session_name}.log"


def _save_snapshot(session_name: str, text: str):
    try:
        _snapshot_path(session_name).write_text(text)
    except Exception:
        pass


# ------------------------------------------------------------------- status
_MARKER_RE_CACHE: Dict[str, "re.Pattern"] = {}


def _marker_code(text: str, session_name: str) -> Optional[int]:
    marker = marker_for(session_name)
    pattern = _MARKER_RE_CACHE.get(marker)
    if pattern is None:
        pattern = re.compile(rf"{re.escape(marker)}:(\d+)\s*$", re.MULTILINE)
        _MARKER_RE_CACHE[marker] = pattern
    m = pattern.search(text)
    return int(m.group(1)) if m else None


def _status_for(record: Dict[str, Any], alive_sessions: Optional[set] = None) -> Dict[str, Any]:
    session_name = record["session_name"]
    alive = session_name in (alive_sessions if alive_sessions is not None else set(tmux.list_sessions()))
    latest_metrics = None
    restart_available = False
    return_code = None

    if alive:
        text = tmux.capture_pane_tail(session_name, lines=300) or ""
        code = _marker_code(text, session_name)
        if code is None:
            status = "running"
        elif code == 0:
            status = "completed"
            return_code = 0
        elif code == 130:
            status = "stopped"
            return_code = 130
            restart_available = True
        else:
            status = "failed"
            return_code = code
            restart_available = True
        series = parse_log_text(text)["series"]
        latest_metrics = series[-1] if series else None
    else:
        report = reports.find_latest_report_for_experiment(record.get("experiment_name") or "")
        if report:
            status = "completed"
        else:
            status = "interrupted"
            restart_available = True

    return {
        **record,
        "managed": True,
        "alive": alive,
        "status": status,
        "return_code": return_code,
        "latest_metrics": latest_metrics,
        "restart_available": restart_available,
    }


def list_terminals() -> List[Dict[str, Any]]:
    records = _load()
    alive_sessions = set(tmux.list_sessions())
    managed_names = {r["session_name"] for r in records}

    result = [_status_for(r, alive_sessions) for r in records]
    result.sort(key=lambda r: r.get("created_at") or "", reverse=True)

    unmanaged = sorted(
        name for name in (alive_sessions - managed_names)
        if not monitors.is_monitor_session(name)
    )
    for name in unmanaged:
        result.append({
            "session_name": name,
            "managed": False,
            "config_path": None,
            "mode": None,
            "extra_args": "",
            "experiment_name": None,
            "command": None,
            "created_at": None,
            "restart_count": 0,
            "alive": True,
            "status": "unmanaged",
            "return_code": None,
            "latest_metrics": None,
            "restart_available": False,
        })
    return result


def get_terminal(session_name: str, include_log: bool = False) -> Optional[Dict[str, Any]]:
    records = _load()
    record = next((r for r in records if r["session_name"] == session_name), None)
    alive_sessions = set(tmux.list_sessions())

    if record is not None:
        enriched = _status_for(record, alive_sessions)
    elif session_name in alive_sessions:
        enriched = {
            "session_name": session_name, "managed": False, "config_path": None,
            "mode": None, "extra_args": "", "experiment_name": None, "command": None,
            "created_at": None, "restart_count": 0, "alive": True, "status": "unmanaged",
            "return_code": None, "latest_metrics": None, "restart_available": False,
        }
    else:
        return None

    if include_log:
        if enriched["alive"]:
            text = tmux.capture_pane(session_name) or ""
        else:
            snap = _snapshot_path(session_name)
            text = snap.read_text() if snap.exists() else ""
        marker = marker_for(session_name)
        lines = [l for l in text.split("\n") if marker not in l]
        while lines and not lines[-1].strip():
            lines.pop()
        enriched["log_text"] = "\n".join(lines)
        enriched["metrics_series"] = parse_log_text(text)["series"]
    return enriched
