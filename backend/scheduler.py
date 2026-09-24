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
from . import hosts
from . import terminals
from . import notifications as notif

_lock = threading.RLock()

TERMINAL_STATUSES = {"completed", "failed", "stopped", "interrupted"}
_DEFAULTS = {"items": [], "max_concurrent": 1, "paused": False, "notify_on_finish": False, "templates": []}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load() -> Dict[str, Any]:
    if not settings.scheduler_file.exists():
        return dict(_DEFAULTS, items=[], templates=[])
    try:
        data = json.loads(settings.scheduler_file.read_text())
    except Exception:
        return dict(_DEFAULTS, items=[], templates=[])
    for key, default in _DEFAULTS.items():
        data.setdefault(key, [] if isinstance(default, list) else default)
    return data


def _save(data: Dict[str, Any]):
    settings.scheduler_file.write_text(json.dumps(data, indent=2))


def _new_item(
    config_path: str, mode: str, extra_args: str,
    depends_on: Optional[str] = None, host_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:10],
        "config_path": config_path,
        "mode": mode,
        "extra_args": extra_args.strip(),
        "experiment_name": cfg.get_experiment_name(config_path),
        "depends_on": depends_on,
        "host_id": host_id or hosts.LOCAL_HOST_ID,
        "status": "pending",  # pending | running | cancelling | completed | failed | cancelled | skipped
        "session_name": None,
        "created_at": _now(),
        "started_at": None,
        "ended_at": None,
        "return_code": None,
    }


# ------------------------------------------------------------------- write API
def add_item(config_path: str, mode: str, extra_args: str = "", host_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """*host_id* defaults to the local machine — every pre-existing caller
    (the Configs-page "Add to schedule" button, backend/experiments.py's
    local dispatch) keeps queueing local work exactly as before. Passing a
    remote host_id queues it against *that* host's own concurrency limit
    (hosts.get_host(host_id).max_concurrent) instead of the local one — one
    FIFO, host-partitioned, rather than a second queueing mechanism per host."""
    if mode not in ("train", "eval", "both"):
        raise ValueError("mode must be 'train', 'eval', or 'both'")
    cfg.read_config(config_path)  # raises if the config doesn't exist / is invalid
    host = hosts.get_host(host_id)  # raises HostError on an unknown host

    with _lock:
        data = _load()
        n_new = 2 if mode == "both" else 1
        if len(data["items"]) + n_new > settings.scheduler_max_queue_size:
            raise ValueError(
                f"Scheduler queue is at its limit ({settings.scheduler_max_queue_size} items). "
                "Remove some completed/cancelled items before adding more."
            )
        if mode == "both":
            train_item = _new_item(config_path, "train", extra_args, host_id=host.id)
            eval_item = _new_item(config_path, "eval", extra_args, depends_on=train_item["id"], host_id=host.id)
            data["items"] += [train_item, eval_item]
            created = [train_item, eval_item]
        else:
            item = _new_item(config_path, mode, extra_args, host_id=host.id)
            data["items"].append(item)
            created = [item]
        _save(data)
    _tick()
    return created


def set_max_concurrent(value: int) -> int:
    """The *local* machine's concurrency — the only one this legacy field
    ever meant. A remote host's limit lives on its own host record
    (backend/hosts.py), set via the Compute tab, not here."""
    value = max(1, min(int(value), settings.scheduler_max_concurrent_limit))
    with _lock:
        data = _load()
        data["max_concurrent"] = value
        _save(data)
    _tick()
    return value


def set_paused(value: bool) -> bool:
    """Pausing only stops *new* pending items from launching — items already
    running keep being monitored to completion by _tick, they just aren't
    joined by anything new. Unpausing calls _tick() immediately so queued
    work resumes without waiting for the next 3s poll."""
    value = bool(value)
    with _lock:
        data = _load()
        data["paused"] = value
        _save(data)
    if not value:
        _tick()
    return value


def set_notify_on_finish(value: bool) -> bool:
    """Reuses the same 5-channel notification settings the Kaggle tab
    configures (backend/notifications.py) — this only toggles whether the
    scheduler's own _tick fires them on a completed/failed/cancelled item."""
    value = bool(value)
    with _lock:
        data = _load()
        data["notify_on_finish"] = value
        _save(data)
    return value


# ------------------------------------------------------------------ templates
def list_templates() -> List[Dict[str, Any]]:
    with _lock:
        return list(_load().get("templates", []))


def add_template(name: str, config_path: str, mode: str, extra_args: str = "") -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("Template name can't be empty")
    if mode not in ("train", "eval", "both"):
        raise ValueError("mode must be 'train', 'eval', or 'both'")
    cfg.read_config(config_path)  # raises if the config doesn't exist / is invalid
    template = {
        "id": uuid.uuid4().hex[:10],
        "name": name,
        "config_path": config_path,
        "mode": mode,
        "extra_args": extra_args.strip(),
        "created_at": _now(),
    }
    with _lock:
        data = _load()
        data["templates"].append(template)
        _save(data)
    return template


def remove_template(template_id: str) -> bool:
    with _lock:
        data = _load()
        before = len(data["templates"])
        data["templates"] = [t for t in data["templates"] if t["id"] != template_id]
        if len(data["templates"]) == before:
            return False
        _save(data)
    return True


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


def total_epochs(config_path: str) -> Optional[int]:
    """Best-effort epoch count for the progress bar/ETA — configs vary in
    whether they even set this, so a missing/malformed value just means no
    progress bar rather than an error (see IMPLEMENTATION_PLAN.md's
    "degrade honestly" principle). Public (not module-private) because
    backend/estimates.py's est_hours() also needs it, for the same "does
    this config declare an epoch count" question
    (EXPERIMENT_AUTOMATION_PLAN.md §4.1)."""
    try:
        parsed = cfg.read_config(config_path)["parsed"] or {}
        epochs = (parsed.get("training") or {}).get("epochs")
        return int(epochs) if epochs else None
    except Exception:
        return None


def _log_tail(session_name: str, n: int = 6) -> Optional[str]:
    try:
        term = terminals.get_terminal(session_name, include_log=True)
    except Exception:
        return None
    if not term:
        return None
    text = term.get("log_text") or ""
    lines = [l for l in text.split("\n") if l.strip()]
    return "\n".join(lines[-n:]) if lines else None


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
            item["total_epochs"] = total_epochs(item["config_path"])
            item["log_tail"] = _log_tail(item["session_name"])
    return {
        "items": items,
        "max_concurrent": data.get("max_concurrent", 1),
        "max_concurrent_limit": settings.scheduler_max_concurrent_limit,
        "paused": data.get("paused", False),
        "notify_on_finish": data.get("notify_on_finish", False),
    }


# ----------------------------------------------------------------- scheduling
def _item_host_id(item: Dict[str, Any]) -> str:
    return item.get("host_id") or hosts.LOCAL_HOST_ID


def _host_limit(host_id: str) -> int:
    try:
        return hosts.get_host(host_id).max_concurrent
    except hosts.HostError:
        # The host was removed out from under an already-queued item — treat
        # it as full rather than raising out of a background tick; the item
        # stays pending, visibly stuck, instead of the tick loop dying.
        return 0


def _tick():
    """Advance the schedule: notice finished/cancelled items, chain a 'both'
    mode's eval half once its train half completes (or skip it if the train
    half didn't succeed), and launch new pending items up to each item's own
    host's concurrency limit. One flat queue, partitioned by host_id — not a
    separate queue per host — so item order (and reorder_pending) still means
    one thing. Safe to call frequently and from multiple threads (guarded by
    _lock).
    """
    with _lock:
        data = _load()
        items = data["items"]
        changed = False
        running_by_host: Dict[str, int] = {}
        just_finished: List[Dict[str, Any]] = []

        for item in items:
            host_id = _item_host_id(item)
            if item["status"] == "running" and item.get("session_name"):
                term = terminals.get_terminal(item["session_name"])
                tstatus = term.get("status") if term else "interrupted"
                if tstatus in TERMINAL_STATUSES:
                    item["ended_at"] = _now()
                    item["return_code"] = term.get("return_code") if term else None
                    item["status"] = "completed" if tstatus == "completed" else ("cancelled" if tstatus == "stopped" else "failed")
                    changed = True
                    just_finished.append(item)
                else:
                    running_by_host[host_id] = running_by_host.get(host_id, 0) + 1
            elif item["status"] == "cancelling":
                term = terminals.get_terminal(item["session_name"]) if item.get("session_name") else None
                if not term or term.get("status") != "running":
                    item["status"] = "cancelled"
                    item["ended_at"] = _now()
                    changed = True
                    just_finished.append(item)
                else:
                    running_by_host[host_id] = running_by_host.get(host_id, 0) + 1

        # a 'both'-mode eval half only makes sense if its train half succeeded
        for item in items:
            if item["status"] == "pending" and item.get("depends_on"):
                dep = next((i for i in items if i["id"] == item["depends_on"]), None)
                if dep and dep["status"] in ("failed", "cancelled"):
                    item["status"] = "skipped"
                    item["ended_at"] = _now()
                    changed = True
                    just_finished.append(item)

        # Paused: everything above (noticing completions, skipping dependents)
        # still runs — pausing only withholds *new* launches. Pause is still a
        # single flag: pausing the queue pauses every host queued through it.
        if not data.get("paused"):
            for item in items:
                if item["status"] != "pending":
                    continue
                host_id = _item_host_id(item)
                if running_by_host.get(host_id, 0) >= _host_limit(host_id):
                    continue  # this host is full; a later item may target a different one
                if item.get("depends_on"):
                    dep = next((i for i in items if i["id"] == item["depends_on"]), None)
                    if not dep or dep["status"] != "completed":
                        continue
                try:
                    launched = terminals.launch(
                        item["config_path"], item["mode"], item.get("extra_args", ""), host_id=host_id,
                    )
                except Exception:
                    item["status"] = "failed"
                    item["ended_at"] = _now()
                    changed = True
                    continue
                item["session_name"] = launched["session_name"]
                item["status"] = "running"
                item["started_at"] = _now()
                running_by_host[host_id] = running_by_host.get(host_id, 0) + 1
                changed = True

        if changed:
            _save(data)
        notify = data.get("notify_on_finish")

    # Outside the lock: notif.send_all() makes real network calls (Telegram/
    # Discord/SMTP/...) with multi-second timeouts — holding _lock through
    # that would stall every other scheduler operation (add/cancel/list)
    # until a possibly-slow or unreachable channel gives up.
    if notify:
        for item in just_finished:
            label = item.get("experiment_name") or item["config_path"]
            notif.send_all(f"Scheduled run '{label}' ({item['mode']}) is now {item['status']}.")

    # Attempt completion must observe both ordinary terminal transitions and
    # dependency skips, but the callback re-enters scheduler-owned APIs and
    # must run after _lock is released.
    if just_finished:
        from . import experiments
        for item in just_finished:
            experiments.on_scheduler_item_finished(item["id"])


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
