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
    batch_name: Optional[str] = None, pool: Optional[str] = None,
    run_mode: str = "fresh", resume_from_worker_id: str = "",
    resume_from_run_id: str = "", resume_from_results_dir: str = "",
    resume_from_manifest_path: str = "", chain_id: str = "", version_index: Optional[int] = None,
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
        # Batch-automation fields (EXPERIMENT_AUTOMATION_PLAN.md §3). None/0/None for a
        # hand-added row — these only mean something once a batch dispatcher owns the row.
        "batch_name": batch_name,
        "pool": pool,               # "either" | "local_only" | "kaggle_only", or None if not batch-owned
        "run_mode": (run_mode or "fresh").strip() or "fresh",
        "resume_from_worker_id": (resume_from_worker_id or "").strip(),
        "resume_from_run_id": (resume_from_run_id or "").strip(),
        "resume_from_results_dir": (resume_from_results_dir or "").strip(),
        "resume_from_manifest_path": (resume_from_manifest_path or "").strip(),
        "chain_id": (chain_id or "").strip(),
        "version_index": version_index,
        "attempt_count": 0,
        "unit_ref": None,           # {"train_item_id","eval_item_id"} (local) or {"account","worker_id"} (kaggle)
        "blocked_reason": None,     # set when status == "blocked" (§4.1 Rule 6) — why no resource fit this tick
        "updated_at": _now(),
    }
    with _lock:
        rows = _load()
        rows.append(row)
        _save(rows)
    return row


def bulk_add(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Inserts many rows under a single lock acquisition — what a sweep-spec
    loader uses to expand configs x seeds into rows (EXPERIMENT_AUTOMATION_PLAN.md
    §3) without N separate lock/load/save cycles for a large sweep. Each
    entry takes the same fields as add_row()'s kwargs, as a dict; config_path
    is the only required one. Returns the created rows in the same order."""
    created: List[Dict[str, Any]] = []
    with _lock:
        rows = _load()
        for entry in entries:
            config_path = (entry.get("config_path") or "").strip()
            if not config_path:
                raise AssignmentError("Every bulk_add entry requires config_path")
            seed = entry.get("seed")
            row = {
                "row_id": uuid.uuid4().hex[:10],
                "config_path": config_path,
                "seed": seed if seed not in ("", None) else None,
                "block": (entry.get("block") or "").strip(),
                "runner_id": (entry.get("runner_id") or "").strip(),
                "status": (entry.get("status") or "pending").strip() or "pending",
                "notes": (entry.get("notes") or "").strip(),
                "extra": entry.get("extra") or {},
                "batch_name": entry.get("batch_name"),
                "pool": entry.get("pool"),
                "run_mode": (entry.get("run_mode") or "fresh").strip() or "fresh",
                "resume_from_worker_id": (entry.get("resume_from_worker_id") or "").strip(),
                "resume_from_run_id": (entry.get("resume_from_run_id") or "").strip(),
                "resume_from_results_dir": (entry.get("resume_from_results_dir") or "").strip(),
                "resume_from_manifest_path": (entry.get("resume_from_manifest_path") or "").strip(),
                "chain_id": (entry.get("chain_id") or "").strip(),
                "version_index": entry.get("version_index"),
                "attempt_count": 0,
                "unit_ref": None,
                "blocked_reason": None,
                "updated_at": _now(),
            }
            rows.append(row)
            created.append(row)
        if created:
            _save(rows)
    return created


def update_row(row_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    editable = {
        "config_path", "seed", "block", "runner_id", "status", "notes",
        "batch_name", "pool", "attempt_count", "unit_ref", "blocked_reason",
        "run_mode", "resume_from_worker_id", "resume_from_run_id", "resume_from_results_dir",
        "resume_from_manifest_path", "chain_id", "version_index",
    }
    with _lock:
        rows = _load()
        row = next((r for r in rows if r["row_id"] == row_id), None)
        if row is None:
            raise AssignmentError(f"Unknown assignment row '{row_id}'")
        for key, value in (patch or {}).items():
            if key not in editable:
                continue
            row[key] = value.strip() if isinstance(value, str) and key not in ("notes",) else value
        row["updated_at"] = _now()
        _save(rows)
    return row


def claim_row(row_id: str, patch: Dict[str, Any], expected_status: str = "pending") -> Optional[Dict[str, Any]]:
    """Atomic compare-and-swap: applies *patch* to *row_id* only if its
    current status is still *expected_status*, returning the updated row —
    or None if it isn't (already claimed by a concurrent caller, cancelled,
    etc.), doing nothing in that case.

    This is the primitive the batch dispatcher's slot handout needs
    (EXPERIMENT_AUTOMATION_PLAN.md §4's "claiming is atomic"), refined from
    that section's original "claim_next(pool)" sketch: §4.1's greedy policy
    scores *every* pending row (feasibility, then longest-first) to pick
    which one to dispatch next, which needs the full row list up front
    (list_rows()) — a single-call "claim the next pending row in a pool"
    can't express that ordering. So the dispatcher does its own scoring over
    list_rows(), then calls claim_row(row_id, ...) on whichever row it
    picked; the compare-and-swap here is what stops two racing callers (the
    dispatch tick and a concurrent one, or two ticks in flight) from both
    successfully claiming the same row — the loser gets None back and moves
    on to its next-best candidate rather than double-dispatching."""
    with _lock:
        rows = _load()
        row = next((r for r in rows if r["row_id"] == row_id), None)
        if row is None or row.get("status") != expected_status:
            return None
        row.update(patch)
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
