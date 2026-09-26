"""User-added tags/notes on runs — e.g. "baseline", "broken augmentation".

Purely a dashboard-owned annotation layer: it never touches
artifacts/runs/<id>/manifest.json or the orchestration ledger (both of
which belong to the orchestration layer, not this dashboard). A note simply
sits alongside a run_id in its own gitignored JSON file, the same
load/save-under-a-lock shape as monitors.py.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Dict

from .config import settings
from .store import JsonStore

_lock = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


_store = JsonStore(lambda: settings.run_notes_file, dict)


def _load() -> Dict[str, Any]:
    data = _store.load()
    return data if isinstance(data, dict) else {}


def _save(data: Dict[str, Any]) -> None:
    _store.save(data)


def list_notes() -> Dict[str, Any]:
    """run_id -> {tag, note, updated_at} for every run that has one, in one
    call — the Runs tab needs this for every group/cell it renders, not one
    run at a time."""
    with _lock:
        return dict(_load())


def get_note(run_id: str) -> Dict[str, Any]:
    with _lock:
        return _load().get(run_id, {"tag": "", "note": ""})


def set_note(run_id: str, tag: str = "", note: str = "") -> Dict[str, Any]:
    tag = (tag or "").strip()
    note = (note or "").strip()
    with _lock:
        data = _load()
        if not tag and not note:
            data.pop(run_id, None)
        else:
            data[run_id] = {"tag": tag, "note": note, "updated_at": _now()}
        _save(data)
    return data.get(run_id, {"tag": "", "note": ""})
