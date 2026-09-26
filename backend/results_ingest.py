"""Lands a finished run's outputs in the host repo's canonical tree and
ledger, and asks the framework what state the run is in.

Split out of backend/kaggle.py (Multi_runner_XDash.md Phase 2) so every
runner kind lands results through the same code. Since XDASH_PLAN.md Phase 0
the main entry point is `canonicalize()` (§3.5): XDash knows each attempt's
planned run dir before launch (`attempt.run.run_dir`, from the framework's
`locate_run` hook — §4.4), so a collected run is moved from its staging dir
(`outputs/kaggle/<experiment_id>/`, `outputs/remote/<host>/<attempt_id>/`)
into exactly that dir under the local `experiments_dir`, and its ledger rows
are registered with `manifest_path` rewritten to the canonical copy. Every
reader then looks in one place, whichever runtime produced the run.

`register_ledger()` is the older, plan-less path, kept for a profile with no
`locate_run` hook (segpriors): it registers every manifest a results dir
holds and leaves the files where they are.

Stdlib-only, like backend/ledger.py's read side: the host repo's
orchestration package is never imported here (`classify_run()` asks it
through the bridge instead).
"""
from __future__ import annotations

import csv
import errno
import io
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import settings
from .store import atomic_write_text

_ledger_lock = threading.Lock()   # guards XDash's own writes to the host repo's runs.csv

# Where manifest.json sits under experiments_dir, for both dissert layouts:
# the hash-scoped one it writes since commit 52477d1
# (<experiment_name>/<hash7>-s<seed>[-r<repeat>]/checkpoints/[fold<N>/]) and
# the older flat <experiment_name>-s<seed>/checkpoints/ one. Before Phase 0
# only the flat patterns were globbed, so no run dissert wrote after 52477d1
# was ever found (XDASH_PLAN.md X2).
EXPERIMENTS_MANIFEST_GLOBS = (
    "*/checkpoints/manifest.json", "*/checkpoints/fold*/manifest.json",
    "*/*/checkpoints/manifest.json", "*/*/checkpoints/fold*/manifest.json",
)

# Wherever a checkpoint file physically sits — a staging dir or an
# already-canonical outputs/experiments/<name>/<hash7>-s<seed>/... — it always
# contains exactly one genuine "outputs/experiments/" segment: its own run
# root's ancestor. Re-rooting there, instead of copying at the literal
# repo-relative position, is what lets stage_checkpoint_files() below produce
# the same plain outputs/... tree shape regardless of which runner kind ran
# the leg being resumed from.
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


# Mirrors orchestration/ledger.py's RUNS_FIELDS in the host repo — used only
# as the header of a runs.csv that doesn't exist yet on either side. An
# existing file's own header always wins (see _upsert_ledger_rows): dissert
# has since added a `repeat` column, and rewriting its file with this list
# would have silently dropped that column from every row.
RUNS_FIELDS = [
    "run_id", "config_hash", "experiment_name", "model_name", "dataset_name",
    "seed", "repeat", "fold", "status", "start_time", "end_time", "gpu_hours",
    "best_metric", "monitor_metric", "git_commit", "git_dirty", "manifest_path",
]

# A row at one of these statuses is a finished verdict on that run_id and is
# never overwritten. Anything else (interrupted/running/pending — dissert's
# own manifest vocabulary, orchestration/status.py's _STATUS_PRECEDENCE) is
# still open, and is upserted in place, so the same run_id going
# interrupted -> interrupted -> done across three chained legs lands as one
# updated row instead of the first leg's row winning forever.
_TERMINAL_RUN_STATUSES = {"done", "failed"}


def _rel(path: Path) -> Path:
    """*path* (under settings.repo_root) as a repo-relative path."""
    return Path(path).resolve().relative_to(settings.repo_root.resolve())


def _repo_path(rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else settings.repo_root / p


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _manifests_in_run_dir(run_dir: Path) -> Iterable[Path]:
    """manifest.json files of one run root, both non-CV and K-Fold shapes."""
    for pattern in ("checkpoints/manifest.json", "checkpoints/fold*/manifest.json"):
        for p in sorted(run_dir.glob(pattern)):
            if p.is_file():
                yield p


def _iter_downloaded_manifests(results_dir: Path):
    """Yields (manifest_path, run_id, manifest) for every manifest.json under
    a collected results_dir, in whichever shape this profile's
    manifest_layout uses — mirrors backend/ledger.py's own
    _iter_manifest_paths(), since the two must agree on where a manifest
    lives or a collected run becomes invisible to the Runs/Ledger tabs even
    after registration succeeds."""
    if settings.manifest_layout == "experiments":
        base = results_dir / "outputs" / "experiments"
        if not base.is_dir():
            return
        seen = set()
        for pattern in EXPERIMENTS_MANIFEST_GLOBS:
            for p in sorted(base.glob(pattern)):
                if p.is_file() and p not in seen:
                    seen.add(p)
                    manifest = _read_json(p)
                    if manifest and manifest.get("run_id"):
                        yield p, manifest["run_id"], manifest
        return

    manifests_dir = results_dir / "artifacts" / "runs"
    if not manifests_dir.is_dir():
        return
    for p in sorted(manifests_dir.glob("*/manifest.json")):
        if not p.is_file():
            continue
        manifest = _read_json(p)
        if manifest is None:
            continue
        yield p, manifest.get("run_id") or p.parent.name, manifest


def _staged_runs_csv(results_dir: Path) -> Path:
    """Where a collected results dir keeps its own copy of the ledger — the
    same repo-relative position the runtime wrote it at."""
    return results_dir / _rel(settings.ledger_dir) / "runs.csv"


def _downloaded_ledger_rows(results_dir: Path) -> Tuple[List[str], Dict[str, Dict[str, str]]]:
    """(header, rows keyed by run_id) from a collected results dir's own
    runs.csv — ({}, []) when it has none."""
    src_runs_csv = _staged_runs_csv(results_dir)
    if not src_runs_csv.is_file():
        return [], {}
    with open(src_runs_csv, newline="") as f:
        reader = csv.DictReader(f)
        rows = {row.get("run_id"): row for row in reader if row.get("run_id")}
        return list(reader.fieldnames or []), rows


def _csv_line(values: List[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerow(values)
    return buf.getvalue()


def _upsert_ledger_rows(rows: List[Dict[str, Any]], source_header: Optional[List[str]] = None) -> List[str]:
    """Writes *rows* into settings.ledger_dir/runs.csv, by run_id.

    Line-level, not a DictReader/DictWriter round trip: every line XDash is
    not changing stays byte-for-byte what the framework wrote (including a
    file whose header predates a column the framework later added), and each
    new row is laid out in the *existing* file's column order. New run_ids
    are appended in place (the same `open(..., "a")` the framework's own
    LedgerWriter uses, so a training process appending concurrently can't
    lose a row to us). Only an upsert of an existing, still-open row rewrites
    the file, atomically. Returns the run_ids written."""
    dest = settings.ledger_dir / "runs.csv"
    changed: List[str] = []
    with _ledger_lock:
        table: List[List[str]] = []
        if dest.is_file():
            with open(dest, newline="") as f:
                table = [line for line in csv.reader(f)]
        header = table[0] if table else list(source_header or RUNS_FIELDS)
        body = table[1:]
        run_col = header.index("run_id") if "run_id" in header else 0
        status_col = header.index("status") if "status" in header else None
        index = {line[run_col]: i for i, line in enumerate(body) if len(line) > run_col}

        appended: List[List[str]] = []
        rewrite = False
        for row in rows:
            run_id = row.get("run_id")
            if not run_id:
                continue
            values = ["" if row.get(col) is None else str(row.get(col)) for col in header]
            i = index.get(run_id)
            if i is None:
                appended.append(values)
                index[run_id] = len(body) + len(appended) - 1
            else:
                prior = body[i]
                if status_col is not None and len(prior) > status_col and prior[status_col] in _TERMINAL_RUN_STATUSES:
                    continue  # a finished verdict for this run_id already landed — never overwrite it
                body[i] = values
                rewrite = True
            changed.append(run_id)

        if not changed:
            return changed
        dest.parent.mkdir(parents=True, exist_ok=True)
        if rewrite or not table:
            text = "".join(_csv_line(line) for line in [header] + body + appended)
            atomic_write_text(dest, text)
        else:
            with open(dest, "a", newline="") as f:
                for line in appended:
                    f.write(_csv_line(line))
                f.flush()
                os.fsync(f.fileno())
    return changed


def register_ledger(results_dir: Path) -> List[str]:
    """The plan-less path (a profile with no `locate_run` hook): copies each
    collected run's manifest.json into the host repo's own manifest layout
    and upserts its row into settings.ledger_dir/runs.csv. Files stay in
    *results_dir*. Returns the run_ids written (new or updated)."""
    header, src_rows_by_id = _downloaded_ledger_rows(results_dir)
    if not src_rows_by_id:
        return []
    rows: List[Dict[str, Any]] = []
    for manifest_path, run_id, manifest in _iter_downloaded_manifests(results_dir):
        row = src_rows_by_id.get(run_id)
        if row is None:
            continue
        if settings.manifest_layout == "experiments":
            rel = manifest_path.relative_to(results_dir / "outputs" / "experiments")
            dest_manifest_path = settings.experiments_dir / rel
        else:
            dest_manifest_path = settings.runs_artifacts_dir / run_id / "manifest.json"
        if not dest_manifest_path.exists() or dest_manifest_path.resolve() != manifest_path.resolve():
            atomic_write_text(dest_manifest_path, json.dumps(manifest, indent=2, sort_keys=True, default=str))
        rows.append(row)
    return _upsert_ledger_rows(rows, header)


# ------------------------------------------------------------------ canonicalize
def _created_at(run_dir: Path) -> Optional[str]:
    """run_meta.json's created_at: dissert writes it once, the first time a
    run dir is used, and every later leg (resumed from a seeded copy of it)
    keeps it. Two copies with the same value are the same run; different
    values are two different runs that happen to share a path."""
    meta = _read_json(run_dir / "run_meta.json") or {}
    return meta.get("created_at")


def _move_tree(src: Path, dst: Path) -> None:
    """Moves every file under *src* to the same relative path under *dst*,
    overwriting — a rename when both are on one filesystem (the usual case:
    staging lives under the same outputs/ tree), a copy otherwise."""
    for root, _dirs, files in os.walk(str(src)):
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            s, d = Path(root) / name, dst / rel / name
            try:
                os.replace(str(s), str(d))
            except OSError as e:
                if e.errno != errno.EXDEV:
                    raise
                shutil.copy2(str(s), str(d))
                os.unlink(str(s))


def _run_summary(run_dir: Path) -> Dict[str, Any]:
    """This copy's own GPU-hours and earliest start, read off its manifests
    *before* they are merged into the canonical dir — the canonical copy is
    overwritten by every later leg, so a leg's own hours only exist here
    (they feed Kaggle's self-tracked weekly quota, backend/kaggle.py)."""
    hours, starts = 0.0, []
    for p in _manifests_in_run_dir(run_dir):
        m = _read_json(p) or {}
        try:
            hours += float(m.get("gpu_hours") or 0)
        except (TypeError, ValueError):
            pass
        if m.get("start_time"):
            starts.append(str(m["start_time"]))
    return {"gpu_hours": round(hours, 4), "start_time": min(starts) if starts else None}


def _discard_staging(staging: Path) -> None:
    root = settings.repo_root.resolve()
    target = Path(staging).resolve()
    if root in target.parents:  # never remove anything outside the repo
        shutil.rmtree(str(target), ignore_errors=True)


def canonicalize(attempt: Dict[str, Any], staging: Optional[Path]) -> Dict[str, Any]:
    """Lands *attempt*'s collected run in the canonical tree (XDASH_PLAN.md
    §3.5). *staging* is what the runner's collect() returned — a re-rooted
    slice of the runtime's repo — or None for a local attempt, whose
    framework already wrote straight into the canonical tree.

    Returns the attempt's new `collect` record: `state` "done" (landed —
    `collected_dir` says where), "empty" (the runtime produced nothing at the
    planned path), or "legacy" (no planned run dir: plan-less registration,
    staging kept). A destination that already holds a *different* run
    (different run_meta created_at) is never merged into: the copy goes to
    `<run_dir>.<attempt_id>` and `conflict` is True (the `output-conflict`
    flag)."""
    run = attempt.get("run") or {}
    run_dir = run.get("run_dir")
    if not run_dir:
        if staging is None:
            return {"state": "empty"}
        return {"state": "legacy", "registered": register_ledger(Path(staging))}

    run_rel = Path(run_dir)
    if run_rel.is_absolute():
        raise ValueError("Planned run_dir %r is absolute — only repo-relative run dirs can be canonicalized" % run_dir)
    dest = settings.repo_root / run_rel

    if staging is None:
        if not dest.is_dir():
            return {"state": "empty"}
        return {"state": "done", "collected_dir": run_rel.as_posix(), "conflict": False, **_run_summary(dest)}

    staging = Path(staging)
    src = staging / run_rel
    if not src.is_dir():
        _discard_staging(staging)
        return {"state": "empty"}

    target_rel, conflict = run_rel, False
    if dest.is_dir() and any(dest.iterdir()) and _created_at(src) != _created_at(dest):
        target_rel, conflict = Path("%s.%s" % (run_rel.as_posix(), attempt["attempt_id"])), True
    target = settings.repo_root / target_rel

    summary = _run_summary(src)
    header, rows_by_id = _downloaded_ledger_rows(staging)
    _move_tree(src, target)

    # The fold partition is shared by every seed/repeat of one config hash,
    # so it sits beside the run roots, not inside one; a resumed K-Fold leg
    # needs it. Hash-scoped, so an existing canonical copy is identical.
    exp_root_rel = run_rel.parent
    for p in sorted((staging / exp_root_rel).glob("*-fold_splits.json")):
        dst = settings.repo_root / exp_root_rel / p.name
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(p), str(dst))

    canonical_manifests = {}
    for p in _manifests_in_run_dir(target):
        m = _read_json(p) or {}
        if m.get("run_id"):
            canonical_manifests[m["run_id"]] = _rel(p).as_posix()
    rows = [
        dict(rows_by_id[run_id], manifest_path=manifest_rel)
        for run_id, manifest_rel in canonical_manifests.items() if run_id in rows_by_id
    ]
    registered = _upsert_ledger_rows(rows, header)

    _discard_staging(staging)
    return {
        "state": "done", "collected_dir": target_rel.as_posix(), "conflict": conflict,
        "registered": registered, **summary,
    }


def classify_run(run_root: Path, verify: bool = False) -> Optional[Dict[str, Any]]:
    """Dissert-side classification of one run root — the attempt's
    canonical `run.collected_dir` (or, for a local attempt, its planned
    `run.run_dir`) — via orchestration.status.describe_run()
    (Multi_runner_XDash.md Phase 5): the manifest-level truth of done vs.
    interrupted-and-resumable, which a runner's own poll()/succeeded can't
    see. A Kaggle kernel that self-limited on --max-hours exits 0, exactly
    like one that finished; only the manifest tells the two apart.

    The caller passes the path it read off the attempt. This function never
    derives it (the old `outputs/experiments/<experiment_id>` guess is what
    X2 was). Returns None whenever this can't be answered, rather than
    raising — "classification unavailable, trust the runner's own
    succeeded/failed" is the correct fallback: a profile not on the
    experiments layout, nothing at the path, a host repo without
    orchestration.status, or a misconfigured bridge interpreter."""
    if settings.manifest_layout != "experiments":
        return None
    run_root = Path(run_root)
    if not run_root.is_dir():
        return None
    from . import bridge
    args = [str(run_root)] + (["--verify"] if verify else [])
    try:
        result = bridge.run_bridge_script("describe_run.py", args, timeout=60, use_cache=False)
    except (bridge.BridgeError, bridge.BridgeUnavailable):
        return None
    return result if isinstance(result, dict) else None


def read_xdash_status(results_dir: Path) -> Optional[Dict[str, Any]]:
    """The launch template's own {stage, returncode, started_at, ended_at,
    setup_seconds} record, written unconditionally by the template's run
    cell whether train/eval succeeded or not, so a failed attempt is
    diagnosable from what actually got downloaded instead of inferred from
    zip presence."""
    return _read_json(results_dir / "outputs" / "xdash_status.json")
