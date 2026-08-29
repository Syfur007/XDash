"""Read-only access to the orchestration layer's on-disk state: the CSV
ledger (artifacts/ledger/*.csv) and per-run manifests
(artifacts/runs/<run_id>/manifest.json).

Stdlib only (csv, json) — reads plain files the orchestration package
already writes, without importing that package itself, so this stays true
to the dashboard's minimal dependency footprint (see
IMPLEMENTATION_PLAN.md's design principles). Every reader tolerates a
missing file/directory (returns an empty list) since a host repo may not
have adopted the orchestration layer's artifacts/ layout at all.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import settings

LEDGER_TABLES = ("runs", "compute", "test_evals", "stats")


def _read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.is_file():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def list_ledger_rows(table: str) -> List[Dict[str, str]]:
    if table not in LEDGER_TABLES:
        raise ValueError(f"Unknown ledger table '{table}' (expected one of {LEDGER_TABLES})")
    return _read_csv(settings.ledger_dir / f"{table}.csv")


def _iter_manifest_paths():
    if not settings.runs_artifacts_dir.is_dir():
        return
    for p in sorted(settings.runs_artifacts_dir.glob("*/manifest.json")):
        if p.is_file():
            yield p


def _load_manifest(p: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    """One run's manifest, joined with its Runs-ledger row if one exists
    (the ledger row carries a couple of fields — best_metric, monitor_metric
    — the manifest itself doesn't record)."""
    p = settings.runs_artifacts_dir / run_id / "manifest.json"
    manifest = _load_manifest(p) if p.is_file() else None
    if manifest is None:
        return None
    ledger_row = next((r for r in list_ledger_rows("runs") if r.get("run_id") == run_id), None)
    return {**manifest, "ledger": ledger_row}


def list_runs() -> List[Dict[str, Any]]:
    """Every run with a manifest on disk, newest first, each joined with
    its Runs-ledger row when one exists."""
    ledger_by_id = {row.get("run_id"): row for row in list_ledger_rows("runs")}
    runs: List[Dict[str, Any]] = []
    for p in _iter_manifest_paths():
        manifest = _load_manifest(p)
        if manifest is None:
            continue
        run_id = manifest.get("run_id") or p.parent.name
        runs.append({**manifest, "ledger": ledger_by_id.get(run_id)})
    runs.sort(key=lambda r: r.get("start_time") or "", reverse=True)
    return runs


def runs_grouped_by_config_hash() -> List[Dict[str, Any]]:
    """Runs bucketed by config_hash — the Runs Hub's primary grouping, since
    two runs sharing a hash differ only by seed/fold, not by what's being
    tested (see orchestration/runid.py's own docstring on this distinction).
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for run in list_runs():
        groups.setdefault(run.get("config_hash") or "unknown", []).append(run)

    def _sort_key(run: Dict[str, Any]):
        fold = run.get("fold")
        return (run.get("seed") if run.get("seed") is not None else 0, fold if fold is not None else -1)

    def _newest(runs: List[Dict[str, Any]]) -> str:
        return max((r.get("start_time") or "" for r in runs), default="")

    return [
        {"config_hash": h, "runs": sorted(rs, key=_sort_key)}
        for h, rs in sorted(groups.items(), key=lambda kv: _newest(kv[1]), reverse=True)
    ]
