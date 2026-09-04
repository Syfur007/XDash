"""Assignment board (DASHBOARD_REDESIGN_PLAN.md §7 / Phase 7): a config x
seed -> runner planning board, answering "which runner should run this
not-yet-launched config" — a different question from Experiments -> Active
("what's running now") or Runs & Results ("what has run"). Retires the
hand-maintained `experiment_status.csv` at the repo root (real evidence for
this feature: its `worker` column already tracks exactly this, by hand, in
values like "w3", "mclab", "w1, mclab").

Deliberately lightweight and decoupled from the Runner registry: `runner_id`
here is a free-form label (best matched against GET /api/runners' ids when
one exists, e.g. "local" or "kaggle:tanvir", but not enforced) rather than a
foreign key — a plan should be capturable ("w3 is doing the BUSI block")
before every runner it names is necessarily registered the same way, and a
CSV imported from the pre-existing hand-maintained sheet won't have used
this dashboard's own runner-id spelling at all. Purely a dashboard-owned
planning layer, same load/save-under-a-lock shape as run_notes.py — never
mutates a config file, a runner, or the orchestration ledger.
"""
from __future__ import annotations

import csv
import io
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import settings

_lock = threading.Lock()

# The full column set of experiment_status.csv (repo root) — import_csv()
# accepts exactly this shape and keeps every column, not just the ones this
# board treats as first-class (config/seed/runner_id/status/notes); columns
# beyond those land in a row's `extra` dict so nothing from the
# hand-maintained sheet is lost on migration.
class AssignmentError(Exception):
    """Expected failure (bad row id, malformed CSV) — routes map this to a 4xx."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load() -> List[Dict[str, Any]]:
    if not settings.assignments_file.exists():
        return []
    try:
        data = json.loads(settings.assignments_file.read_text())
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _save(rows: List[Dict[str, Any]]) -> None:
    settings.assignments_file.write_text(json.dumps(rows, indent=2))


def list_rows() -> List[Dict[str, Any]]:
    with _lock:
        return list(_load())


def add_row(
    config_path: str, seed: Optional[Any] = None, runner_id: str = "",
    status: str = "planned", notes: str = "", block: str = "", extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    config_path = (config_path or "").strip()
    if not config_path:
        raise AssignmentError("config_path is required")
    row = {
        "row_id": uuid.uuid4().hex[:10],
        "config_path": config_path,
        "seed": seed if seed not in ("", None) else None,
        "block": (block or "").strip(),
        "runner_id": (runner_id or "").strip(),
        "status": (status or "planned").strip() or "planned",
        "notes": (notes or "").strip(),
        "extra": extra or {},
        "updated_at": _now(),
    }
    with _lock:
        rows = _load()
        rows.append(row)
        _save(rows)
    return row


def update_row(row_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    editable = {"config_path", "seed", "block", "runner_id", "status", "notes"}
    with _lock:
        rows = _load()
        row = next((r for r in rows if r["row_id"] == row_id), None)
        if row is None:
            raise AssignmentError(f"Unknown assignment row '{row_id}'")
        for key, value in (patch or {}).items():
            if key not in editable:
                continue
            row[key] = value.strip() if isinstance(value, str) and key != "notes" else value
        row["updated_at"] = _now()
        _save(rows)
    return row


def remove_row(row_id: str) -> bool:
    with _lock:
        rows = _load()
        before = len(rows)
        rows = [r for r in rows if r["row_id"] != row_id]
        if len(rows) == before:
            return False
        _save(rows)
    return True


# --------------------------------------------------------------------------- CSV import/export
def import_csv(csv_path: str) -> Dict[str, Any]:
    """Imports the legacy hand-maintained sheet (experiment_status.csv's own
    column shape — config/dataset/block/worker/status/seeds_done/
    mean_test_dice/gpu_hours_total/git_commit). *csv_path* is resolved
    relative to the repo root, same convention as a worker's notebook_path.
    Every row becomes one assignment row (config_path=its `config` column,
    runner_id=its `worker` column, status=its `status` column); every other
    legacy column is preserved verbatim in `extra` rather than dropped.
    Existing rows are left untouched — this only appends, so importing twice
    just duplicates rows rather than silently overwriting manual edits;
    dedupe by hand afterward if that's not wanted for a given import."""
    csv_path = (csv_path or "").strip()
    if not csv_path:
        raise AssignmentError("csv_path is required")
    abs_path = (settings.repo_root / csv_path).resolve()
    repo_root = settings.repo_root.resolve()
    if repo_root not in abs_path.parents and abs_path != repo_root:
        raise AssignmentError("csv_path escapes the repo root")
    if not abs_path.is_file():
        raise AssignmentError(f"CSV not found: {csv_path}")

    try:
        with open(abs_path, newline="") as f:
            reader = csv.DictReader(f)
            legacy_rows = list(reader)
    except (OSError, csv.Error) as e:
        raise AssignmentError(f"Could not read CSV: {e}")

    imported: List[Dict[str, Any]] = []
    with _lock:
        rows = _load()
        for legacy in legacy_rows:
            config_path = (legacy.get("config") or "").strip()
            if not config_path:
                continue
            extra = {k: v for k, v in legacy.items() if k not in ("config", "block", "worker", "status")}
            row = {
                "row_id": uuid.uuid4().hex[:10],
                "config_path": config_path,
                "seed": None,  # legacy sheet is per-config (seeds_done is a count, not one row per seed)
                "block": (legacy.get("block") or "").strip(),
                "runner_id": (legacy.get("worker") or "").strip(),
                "status": (legacy.get("status") or "planned").strip() or "planned",
                "notes": "",
                "extra": extra,
                "updated_at": _now(),
            }
            rows.append(row)
            imported.append(row)
        if imported:
            _save(rows)
    return {"imported": len(imported), "rows": imported}


def export_csv() -> str:
    """The board's own rows as CSV — a superset of the legacy sheet's shape
    (adds row_id/seed/notes; keeps every legacy column that survived import
    inside `extra`, flattened back into its own columns here)."""
    rows = list_rows()
    extra_keys: List[str] = []
    for r in rows:
        for k in (r.get("extra") or {}):
            if k not in extra_keys:
                extra_keys.append(k)
    fieldnames = ["row_id", "config_path", "seed", "block", "runner_id", "status", "notes", "updated_at"] + extra_keys

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        flat = {k: r.get(k, "") for k in fieldnames if k not in extra_keys}
        flat.update({k: (r.get("extra") or {}).get(k, "") for k in extra_keys})
        writer.writerow(flat)
    return buf.getvalue()
