"""Kaggle-only resume snapshots (Multi_runner_XDash.md Phase 5b) — the
up-leg of cross-account resume. SSH/Colab need none of this:
MachineRunner.seed() just rsyncs straight into the target host's own
filesystem, since XDash has a real shell there. Kaggle has no shell mid-run
— its only input channel is an attached dataset — which is what this module
creates/keeps current.

**One per account, reused as a buffer**, never per-experiment: the
installed `kaggle` CLI has no `datasets delete`, so anything keyed
per-experiment would accumulate permanently with no programmatic cleanup.
Re-versioned immediately before every resume dispatch with whatever leg is
about to run — its prior contents are irrelevant, it is a transport buffer,
not an archive. Safe because the Slot model allows exactly one in-flight
kernel per account, so two legs can never contend for one account's
snapshot at once.

Mirrors backend/kaggle.py's `_run_kaggle()` per-account credential
isolation exactly (reused directly, not reimplemented).
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from . import kaggle as kaggle_backend
from . import results_ingest
from .config import settings

_TITLE_MAX = 50  # Kaggle: both slug and title must be 6-50 chars
_READY_STATUS = "ready"
_FAILED_STATUSES = ("failed", "deleted")
_POLL_INTERVAL_SECONDS = 5.0


class SnapshotError(Exception):
    """Expected failure (bad account, CLI error, never reached ready) —
    dispatch maps this to the `snapshot-failed` blocked code."""


def _slug_for(profile_name: str) -> str:
    slug = ("xdash-snapshot-%s" % profile_name).lower()
    return slug[:_TITLE_MAX]


def snapshot_ref(account_name: str) -> str:
    """"<owner>/<slug>" for *account_name*'s snapshot dataset — computable
    with no network call, since both halves are already known
    (kaggle_username on the account record, slug from the active profile)."""
    account = kaggle_backend._find_account(kaggle_backend._load_accounts(), account_name)
    if account is None:
        raise SnapshotError(f"Unknown Kaggle account '{account_name}'")
    return "%s/%s" % (account["kaggle_username"], _slug_for(settings.profile_name))


def _dataset_exists(account_name: str, ref: str) -> bool:
    proc = kaggle_backend._run_kaggle(["datasets", "status", ref], account_name, timeout=30)
    return proc.returncode == 0


def status(account_name: str) -> Dict[str, Any]:
    """Read-only snapshot-buffer state for *account_name* — Compute tab
    diagnostics (Multi_runner_XDash.md Phase 6), so a `snapshot-failed`
    block is diagnosable without leaving it. A real `kaggle datasets
    status` call (there is no cheaper way to ask), so this is meant to be
    triggered on demand by a button, never auto-polled. `exists: False`
    covers both "no dataset created yet" and "couldn't ask right now" (no
    `kaggle` on PATH, bad credentials, network) — caught directly against a
    real (CLI-absent) environment: _run_kaggle raises KaggleOpsError rather
    than returning a non-zero CompletedProcess in that case, so this must
    degrade on the exception too, not just the returncode — same "a status
    read must degrade, never 500" discipline as colab.list_sessions()."""
    ref = snapshot_ref(account_name)
    try:
        proc = kaggle_backend._run_kaggle(["datasets", "status", ref], account_name, timeout=30)
    except kaggle_backend.KaggleOpsError:
        return {"ref": ref, "exists": False}
    if proc.returncode != 0:
        return {"ref": ref, "exists": False}
    return {"ref": ref, "exists": True, "status": (proc.stdout or "").strip()}


def _poll_ready(account_name: str, ref: str, timeout: float = 300.0) -> None:
    """Kaggle's own status ladder: not_yet_persisted -> blobs_received ->
    blobs_decompressed -> blobs_copied_to_sds -> individual_blobs_compressed
    -> ready (or failed/deleted/reprocessing). Polling until ready before
    the next kernel push attaches this dataset is load-bearing — attaching
    it too early would otherwise produce a leg that silently trains from
    scratch against an empty/partial mount."""
    deadline = time.monotonic() + timeout
    last_status = None
    while time.monotonic() < deadline:
        proc = kaggle_backend._run_kaggle(["datasets", "status", ref], account_name, timeout=30)
        if proc.returncode != 0:
            raise SnapshotError(
                f"'kaggle datasets status {ref}' failed: {(proc.stderr or proc.stdout).strip()[-300:]}"
            )
        last_status = (proc.stdout or "").strip().lower()
        if _READY_STATUS in last_status:
            return
        if any(s in last_status for s in _FAILED_STATUSES):
            raise SnapshotError(f"Snapshot dataset '{ref}' reached status '{last_status}', not ready")
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise SnapshotError(f"Snapshot dataset '{ref}' did not reach 'ready' within {timeout:.0f}s (last: {last_status!r})")


def push(account_name: str, experiment_id: str, checkpoint_files: List[str]) -> str:
    """Stages *checkpoint_files* (repo-relative paths, as returned by
    orchestration.status.describe_run()'s own `checkpoint_files`) into this
    account's resume-snapshot dataset — versioning it if it already exists,
    creating it on first use — then polls until Kaggle finishes processing
    the upload. Returns the "<owner>/<slug>" ref to attach to the next
    leg's kernel push. Raises SnapshotError on any failure."""
    if not checkpoint_files:
        raise SnapshotError("push() called with no checkpoint_files")
    ref = snapshot_ref(account_name)
    slug = _slug_for(settings.profile_name)

    tmpdir = Path(tempfile.mkdtemp(prefix="xdash_snapshot_"))
    try:
        copied = results_ingest.stage_checkpoint_files(tmpdir, checkpoint_files)
        if copied == 0:
            raise SnapshotError("None of the given checkpoint_files exist on disk — nothing to snapshot")
        (tmpdir / "README.md").write_text(
            "This dataset is XDash's resume-snapshot buffer for this Kaggle account. It is "
            "overwritten before every resume dispatch and holds only the leg about to run — "
            "never a stable archive of a finished experiment. See Multi_runner_XDash.md Phase 5.\n"
        )
        # Written directly rather than via `kaggle datasets init`, per the plan's own verified
        # mechanics — subtitle omitted entirely (validated 20-80 chars only when present).
        (tmpdir / "dataset-metadata.json").write_text(json.dumps({
            "title": slug, "id": ref, "licenses": [{"name": "CC0-1.0"}],
        }, indent=2))

        if _dataset_exists(account_name, ref):
            # -t/--keep-tabular is mandatory: without it the client converts tabular files to
            # CSV, which would rewrite manifest.json/runs.csv inside the payload.
            # -d/--delete-old-versions keeps the buffer at one version, per this module's own
            # "reused as a buffer, never an archive" contract.
            args = [
                "datasets", "version", "-p", str(tmpdir), "-t", "-r", "zip", "-q", "-d",
                "-m", f"{experiment_id} leg resume",
            ]
        else:
            args = ["datasets", "create", "-p", str(tmpdir), "-t", "-r", "zip", "-q"]
        proc = kaggle_backend._run_kaggle(args, account_name, timeout=180)
        if proc.returncode != 0:
            raise SnapshotError(
                f"'kaggle {' '.join(args[:2])}' failed for account '{account_name}': "
                f"{(proc.stderr or proc.stdout).strip()[-500:]}"
            )
        _poll_ready(account_name, ref)
    except kaggle_backend.KaggleOpsError as e:
        # _dataset_exists()/_run_kaggle itself can raise this directly (no
        # `kaggle` on PATH, bad credentials) rather than returning a
        # non-zero CompletedProcess — caught here so every failure inside
        # push() surfaces as SnapshotError -> the `snapshot-failed` blocked
        # code, not a generic uncaught exception one level up
        # (_claim_and_dispatch's dispatch-failed catch-all).
        raise SnapshotError(str(e))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return ref
