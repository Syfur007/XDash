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
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import settings

_ledger_lock = threading.Lock()   # guards concurrent appends to the host repo's runs.csv

# Wherever a checkpoint file physically sits — a Kaggle leg's own
# outputs/kaggle/<experiment_id>/outputs/experiments/<experiment_id>/... download
# staging area, or an already-canonical outputs/experiments/<experiment_id>/... —
# it always contains exactly one genuine "outputs/experiments/" segment: its own
# run root's ancestor. Re-rooting there, instead of copying at the literal
# repo-relative position, is what lets stage_checkpoint_files() below produce the
# same plain outputs/... tree shape regardless of which runner kind ran the leg
# being resumed from.
_CANONICAL_ANCHOR = ("outputs", "experiments")


def _canonical_rel(abs_path: Path) -> Optional[Path]:
    parts = abs_path.parts
    n = len(_CANONICAL_ANCHOR)
    for i in range(len(parts) - n + 1):
        if parts[i:i + n] == _CANONICAL_ANCHOR:
            return Path(*parts[i:])
    return None


def stage_checkpoint_files(dest_root: Path, checkpoint_files: List[str]) -> int:
    """Copies each repo-relative *checkpoint_files* path (as returned by
    orchestration.status.describe_run() via classify_run() below) into
    *dest_root*, re-rooted onto its own "outputs/experiments/..." segment —
    shared by backend/snapshot.py (Kaggle's seed(), staging a dataset
    payload) and backend/runners/machine.py (a machine's seed(), staging
    what gets rsynced to the next leg's host), so both produce the exact
    same payload shape from the exact same source data
    (Multi_runner_XDash.md Phase 5). Returns how many files were actually
    copied — 0 means nothing on disk matched, a caller's signal that
    there's nothing to seed with."""
    copied = 0
    for rel in checkpoint_files:
        src = settings.repo_root / rel
        if not src.is_file():
            continue
        canonical = _canonical_rel(src)
        if canonical is None:
            continue
        dest = dest_root / canonical
        if dest.resolve() == src.resolve():
            # A local leg resuming an already-canonical local checkpoint —
            # shutil.copy2() onto itself raises SameFileError; it's already
            # exactly where it needs to be, so this still counts as staged.
            copied += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1
    return copied

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


# A row at one of these statuses is a finished verdict on that run_id and is
# never overwritten. Anything else (interrupted/running/pending — dissert's
# own manifest vocabulary, orchestration/status.py's _STATUS_PRECEDENCE) is
# still open, and register_ledger() below upserts it in place instead of
# skipping it the way a genuinely-already-registered run_id normally would.
_TERMINAL_RUN_STATUSES = {"done", "failed"}


def register_ledger(results_dir: Path) -> List[str]:
    """Copies each newly-collected (or newly-advanced) run's manifest.json
    into the host repo's own ledger/manifest layout and writes its row into
    settings.ledger_dir/runs.csv.

    Upserts by run_id: a row already at a terminal status (done/failed) is
    left alone (idempotent — a re-collection of the same finished results
    never rewrites it), but a row still open (interrupted/running/pending,
    or no row yet) is written or overwritten with the freshly-collected
    manifest's own row. This is what makes a chained leg's own transition —
    the same run_id going interrupted -> interrupted -> done across three
    legs (Multi_runner_XDash.md Phase 5) — land as *one* updated ledger row
    instead of the first leg's row silently winning forever. Returns the
    run_ids written (new or updated)."""
    src_rows_by_id = _downloaded_ledger_rows(results_dir)
    if not src_rows_by_id:
        return []

    dest_ledger_dir = settings.ledger_dir
    dest_runs_csv = dest_ledger_dir / "runs.csv"

    changed: List[str] = []
    with _ledger_lock:
        existing_rows: List[Dict[str, str]] = []
        index_by_id: Dict[str, int] = {}
        if dest_runs_csv.is_file():
            with open(dest_runs_csv, newline="") as f:
                existing_rows = list(csv.DictReader(f))
            for i, r in enumerate(existing_rows):
                index_by_id[r.get("run_id")] = i

        for manifest_path, run_id, manifest in _iter_downloaded_manifests(results_dir):
            if not run_id:
                continue
            row = src_rows_by_id.get(run_id)
            if row is None:
                continue
            prior_idx = index_by_id.get(run_id)
            prior = existing_rows[prior_idx] if prior_idx is not None else None
            if prior is not None and prior.get("status") in _TERMINAL_RUN_STATUSES:
                continue  # a finished verdict for this run_id already landed — never overwrite it

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

            new_row = {k: row.get(k, "") for k in RUNS_FIELDS}
            if prior_idx is None:
                existing_rows.append(new_row)
                index_by_id[run_id] = len(existing_rows) - 1
            else:
                existing_rows[prior_idx] = new_row
            changed.append(run_id)

        if changed:
            dest_ledger_dir.mkdir(parents=True, exist_ok=True)
            tmp_csv = dest_runs_csv.with_suffix(".csv.tmp")
            with open(tmp_csv, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=RUNS_FIELDS)
                writer.writeheader()
                for r in existing_rows:
                    writer.writerow({k: r.get(k, "") for k in RUNS_FIELDS})
            os.replace(tmp_csv, dest_runs_csv)

    return changed


def classify_run(results_dir: Path, experiment_id: str, verify: bool = False) -> Optional[Dict[str, Any]]:
    """Best-effort dissert-side classification of one just-collected run via
    orchestration.status.describe_run() (Multi_runner_XDash.md Phase 5) —
    the manifest-level truth of done vs. interrupted-and-resumable, which a
    runner's own poll()/succeeded can't see: a Kaggle kernel that
    self-limited on --max-hours still exits 0, identically to one that
    actually finished, so only the manifest tells the two apart.

    Returns None whenever this can't be answered, rather than raising —
    the correct fallback in every one of these cases is "classification
    unavailable, trust the runner's own succeeded/failed", not a crash:
    a profile not on manifest_layout == "experiments" (describe_run's own
    directory walk assumes that shape), a results_dir with nothing at the
    expected run-root path yet, a host repo without orchestration.status at
    all (older/partial checkout), or bridge_python_executable itself being
    misconfigured."""
    if settings.manifest_layout != "experiments":
        return None
    run_root = results_dir / "outputs" / "experiments" / experiment_id
    if not run_root.is_dir():
        return None
    from . import bridge
    args = [str(run_root)] + (["--verify"] if verify else [])
    try:
        result = bridge.run_bridge_script("describe_run.py", args, timeout=30, use_cache=False)
    except (bridge.BridgeError, bridge.BridgeUnavailable):
        return None
    return result if isinstance(result, dict) else None


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
