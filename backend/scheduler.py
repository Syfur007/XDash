"""Experiment scheduler.

Deliberately layered *on top of* terminals.py rather than duplicating it:
when the scheduler decides an item should run, it just calls
terminals.launch(...) — the exact same thing the Configs page's "Launch in
terminal" button calls — so a scheduled item becomes an ordinary tmux
terminal the moment it starts, visible on the Terminals page too. The
scheduler's only job is deciding *when* to call that, based on how many
scheduled items are currently running versus the configured limit.

This is the one place in the app with a real background thread (everything
else computes status on demand when the frontend polls). That's a deliberate,
narrow exception: unattended overnight scheduling needs something to notice
a slot has freed up and start the next item even if nobody has the dashboard
open in a browser tab.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import settings
from . import configs as cfg
from . import terminals

_lock = threading.RLock()

TERMINAL_STATUSES = {"completed", "failed", "stopped", "interrupted"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load() -> Dict[str, Any]:
    if not settings.scheduler_file.exists():
        return {"items": [], "max_concurrent": 1}
    try:
        data = json.loads(settings.scheduler_file.read_text())
    except Exception:
        return {"items": [], "max_concurrent": 1}
    data.setdefault("items", [])
    data.setdefault("max_concurrent", 1)
    return data


def _save(data: Dict[str, Any]):
    settings.scheduler_file.write_text(json.dumps(data, indent=2))


def _new_item(config_path: str, mode: str, extra_args: str, depends_on: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:10],
        "config_path": config_path,
        "mode": mode,
        "extra_args": extra_args.strip(),
        "experiment_name": cfg.get_experiment_name(config_path),
        "depends_on": depends_on,
        "status": "pending",  # pending | running | cancelling | completed | failed | cancelled | skipped
        "session_name": None,
        "created_at": _now(),
        "started_at": None,
        "ended_at": None,
        "return_code": None,
    }


# ------------------------------------------------------------------- write API
def add_item(config_path: str, mode: str, extra_args: str = "") -> List[Dict[str, Any]]:
    if mode not in ("train", "eval", "both"):
        raise ValueError("mode must be 'train', 'eval', or 'both'")
    cfg.read_config(config_path)  # raises if the config doesn't exist / is invalid

    with _lock:
        data = _load()
        if mode == "both":
            train_item = _new_item(config_path, "train", extra_args)
            eval_item = _new_item(config_path, "eval", extra_args, depends_on=train_item["id"])
            data["items"] += [train_item, eval_item]
            created = [train_item, eval_item]
        else:
            item = _new_item(config_path, mode, extra_args)
            data["items"].append(item)
            created = [item]
        _save(data)
    _tick()
    return created


def set_max_concurrent(value: int) -> int:
    value = max(1, int(value))
    with _lock:
        data = _load()
        data["max_concurrent"] = value
        _save(data)
    _tick()
    return value


def cancel_item(item_id: str) -> Dict[str, Any]:
    with _lock:
        data = _load()
        item = next((i for i in data["items"] if i["id"] == item_id), None)
        if item is None:
            raise ValueError("Unknown scheduler item")
        if item["status"] == "running" and item.get("session_name"):
            terminals.stop(item["session_name"])
            item["status"] = "cancelling"
        elif item["status"] == "pending":
            item["status"] = "cancelled"
            item["ended_at"] = _now()
        _save(data)
        return dict(item)


def remove_item(item_id: str) -> bool:
    with _lock:
        data = _load()
        item = next((i for i in data["items"] if i["id"] == item_id), None)
        if item is None:
            return False
        if item["status"] in ("running", "cancelling") and item.get("session_name"):
            terminals.kill(item["session_name"])
        data["items"] = [i for i in data["items"] if i["id"] != item_id]
        _save(data)
    return True


def reorder_pending(ordered_ids: List[str]):
    with _lock:
        data = _load()
        pending_by_id = {i["id"]: i for i in data["items"] if i["status"] == "pending"}
        others = [i for i in data["items"] if i["status"] != "pending"]
        new_pending = [pending_by_id[i] for i in ordered_ids if i in pending_by_id]
        new_pending += [i for i in data["items"] if i["status"] == "pending" and i["id"] not in ordered_ids]
        data["items"] = others + new_pending
        _save(data)


# -------------------------------------------------------------------- read API
def list_items() -> Dict[str, Any]:
    with _lock:
        data = _load()
    items = [dict(i) for i in data["items"]]
    for item in items:
        if item["status"] == "running" and item.get("session_name"):
            term = terminals.get_terminal(item["session_name"])
            if term:
                item["latest_metrics"] = term.get("latest_metrics")
    return {"items": items, "max_concurrent": data.get("max_concurrent", 1)}


# ----------------------------------------------------------------- scheduling
def _tick():
    """Advance the schedule: notice finished/cancelled items, chain a 'both'
    mode's eval half once its train half completes (or skip it if the train
    half didn't succeed), and launch new pending items up to max_concurrent.
    Safe to call frequently and from multiple threads (guarded by _lock).
    """
    with _lock:
        data = _load()
        items = data["items"]
        changed = False
        running_count = 0

        for item in items:
            if item["status"] == "running" and item.get("session_name"):
                term = terminals.get_terminal(item["session_name"])
                tstatus = term.get("status") if term else "interrupted"
                if tstatus in TERMINAL_STATUSES:
                    item["ended_at"] = _now()
                    item["return_code"] = term.get("return_code") if term else None
                    item["status"] = "completed" if tstatus == "completed" else ("cancelled" if tstatus == "stopped" else "failed")
                    changed = True
                else:
                    running_count += 1
            elif item["status"] == "cancelling":
                term = terminals.get_terminal(item["session_name"]) if item.get("session_name") else None
                if not term or term.get("status") != "running":
                    item["status"] = "cancelled"
                    item["ended_at"] = _now()
                    changed = True
                else:
                    running_count += 1

        # a 'both'-mode eval half only makes sense if its train half succeeded
        for item in items:
            if item["status"] == "pending" and item.get("depends_on"):
                dep = next((i for i in items if i["id"] == item["depends_on"]), None)
                if dep and dep["status"] in ("failed", "cancelled"):
                    item["status"] = "skipped"
                    item["ended_at"] = _now()
                    changed = True

        max_concurrent = data.get("max_concurrent", 1)
        for item in items:
            if running_count >= max_concurrent:
                break
            if item["status"] != "pending":
                continue
            if item.get("depends_on"):
                dep = next((i for i in items if i["id"] == item["depends_on"]), None)
                if not dep or dep["status"] != "completed":
                    continue
            try:
                launched = terminals.launch(item["config_path"], item["mode"], item.get("extra_args", ""))
            except Exception:
                item["status"] = "failed"
                item["ended_at"] = _now()
                changed = True
                continue
            item["session_name"] = launched["session_name"]
            item["status"] = "running"
            item["started_at"] = _now()
            running_count += 1
            changed = True

        if changed:
            _save(data)


_worker_started = False


def ensure_worker_started():
    global _worker_started
    if _worker_started:
        return
    _worker_started = True

    def loop():
        while True:
            try:
                _tick()
            except Exception:
                pass
            time.sleep(3)

    threading.Thread(target=loop, daemon=True, name="scheduler-tick").start()
