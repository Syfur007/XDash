"""Registers a downloaded/collected run into the host repo's own ledger.

Split out of backend/kaggle.py (Multi_runner_XDash.md Phase 2) so every
runner kind lands results through the same code — not just Kaggle's. The
logic itself is unchanged: copy each newly-produced run's manifest.json into
the host repo's own ledger/manifest layout and append its row into
settings.ledger_dir/runs.csv, mirroring orchestration/manifest.py's
atomic-write style and orchestration/ledger.py's RUNS_FIELDS exactly,
stdlib-only (no import of that package, same as backend/ledger.py's read
side). Branches on settings.manifest_layout exactly as backend/ledger.py
does on the read side.

Callers: backend/kaggle.py's download_experiment() (results already
extracted from a Kaggle kernel's zip), and the machine runner's collect()
once Phase 3 lands (results already rsynced in from a remote host) — both
hand this the same shape: a results_dir containing outputs/experiments/...
or artifacts/runs/..., whichever this profile's manifest_layout is.
"""
from __future__ import annotations

import csv
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import settings

_ledger_lock = threading.Lock()   # guards concurrent appends to the host repo's runs.csv

# Mirrors orchestration/ledger.py's RUNS_FIELDS in the host repo exactly —
# a downloaded/collected run's own artifacts/ledger/runs.csv already has
# these columns, so registration is a straight copy-and-append, not a
# re-derivation.
RUNS_FIELDS = [
    "run_id", "config_hash", "experiment_name", "model_name", "dataset_name",
    "seed", "fold", "status", "start_time", "end_time", "gpu_hours",
    "best_metric", "monitor_metric", "git_commit", "git_dirty", "manifest_path",
]


def _iter_downloaded_manifests(results_dir: Path):
    """Yields (manifest_path, run_id, manifest) for every manifest.json under
    a collected results_dir, in whichever shape this profile's
    manifest_layout uses — mirrors backend/ledger.py's own
    _iter_manifest_paths() exactly, since the two must agree on where a
    manifest lives or a collected run becomes invisible to the Runs/Ledger
    tabs even after registration succeeds."""
    if settings.manifest_layout == "experiments":
        base = results_dir / "outputs" / "experiments"
        if not base.is_dir():
            return
        seen = set()
        for pattern in ("*/checkpoints/manifest.json", "*/checkpoints/fold*/manifest.json"):
            for p in sorted(base.glob(pattern)):
                if p.is_file() and p not in seen:
                    seen.add(p)
                    try:
                        manifest = json.loads(p.read_text())
                    except Exception:
                        continue
                    run_id = manifest.get("run_id")
                    if run_id:
                        yield p, run_id, manifest
        return

    manifests_dir = results_dir / "artifacts" / "runs"
    if not manifests_dir.is_dir():
        return
    for p in sorted(manifests_dir.glob("*/manifest.json")):
        if not p.is_file():
            continue
        try:
            manifest = json.loads(p.read_text())
        except Exception:
            continue
        run_id = manifest.get("run_id") or p.parent.name
        yield p, run_id, manifest


def _downloaded_ledger_rows(results_dir: Path) -> Dict[str, Dict[str, str]]:
    """The collected results' own runs.csv, keyed by run_id — same two
    layouts as _iter_downloaded_manifests(), since ledger_dir sits at a
    different place relative to the manifests in each (nested under
    artifacts/ in "legacy", a sibling of experiments/ in "experiments")."""
    if settings.manifest_layout == "experiments":
        src_runs_csv = results_dir / "outputs" / "ledger" / "runs.csv"
    else:
        src_runs_csv = results_dir / "artifacts" / "ledger" / "runs.csv"
    if not src_runs_csv.is_file():
        return {}
    with open(src_runs_csv, newline="") as f:
        return {row.get("run_id"): row for row in csv.DictReader(f)}


def register_ledger(results_dir: Path) -> List[str]:
    """Copies each newly-collected run's manifest.json into the host repo's
    own ledger/manifest layout and appends its row into
    settings.ledger_dir/runs.csv.

    Idempotent by run_id, regardless of status (not just "done" — a run
    previously registered as failed/interrupted is not re-appended on a
    later collection of the same results). A resumed run's own status
    transition — the same run_id going from "interrupted" to "done" across
    two chained legs (Multi_runner_XDash.md Phase 5) — needs its row
    *updated*, not skipped; that is leg-chaining's problem, not this
    function's, and isn't handled here. Returns the run_ids newly
    registered."""
    src_rows_by_id = _downloaded_ledger_rows(results_dir)
    if not src_rows_by_id:
        return []

    dest_ledger_dir = settings.ledger_dir
    dest_runs_csv = dest_ledger_dir / "runs.csv"

    newly_registered: List[str] = []
    with _ledger_lock:
        known_ids = set()
        if dest_runs_csv.is_file():
            with open(dest_runs_csv, newline="") as f:
                known_ids = {row.get("run_id") for row in csv.DictReader(f)}

        dest_ledger_dir.mkdir(parents=True, exist_ok=True)
        is_new_csv = not dest_runs_csv.is_file()
        with open(dest_runs_csv, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=RUNS_FIELDS)
            if is_new_csv:
                writer.writeheader()
            for manifest_path, run_id, manifest in _iter_downloaded_manifests(results_dir):
                if not run_id or run_id in known_ids:
                    continue
                row = src_rows_by_id.get(run_id)
                if row is None:
                    continue

                if settings.manifest_layout == "experiments":
                    # Mirror the source path's own tail (…/<experiment_id>/checkpoints/[fold*/]manifest.json)
                    # under settings.experiments_dir — this layout doesn't name a manifest's
                    # directory after run_id at all, so there's no run_id-keyed dest path to
                    # target the way the legacy branch below has.
                    rel = manifest_path.relative_to(results_dir / "outputs" / "experiments")
                    dest_manifest_path = settings.experiments_dir / rel
                else:
                    dest_manifest_path = settings.runs_artifacts_dir / run_id / "manifest.json"

                dest_manifest_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = dest_manifest_path.with_suffix(".json.tmp")
                tmp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str))
                os.replace(tmp_path, dest_manifest_path)

                writer.writerow({k: row.get(k, "") for k in RUNS_FIELDS})
                newly_registered.append(run_id)

    return newly_registered


def read_xdash_status(results_dir: Path) -> Optional[Dict[str, Any]]:
    """The launch template's own {stage, returncode, started_at, ended_at,
    setup_seconds} record, written unconditionally by the template's run
    cell whether train/eval succeeded or not, so a failed attempt is
    diagnosable from what actually got downloaded instead of inferred from
    zip presence."""
    status_path = results_dir / "outputs" / "xdash_status.json"
    if not status_path.is_file():
        return None
    try:
        return json.loads(status_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
