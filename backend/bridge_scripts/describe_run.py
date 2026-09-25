"""bridge_scripts/describe_run.py <run-root-dir> [--verify] — wraps
orchestration.status.describe_run() as JSON (Multi_runner_XDash.md Phase 5).
See that module's own docstring for the full returned shape (run_id, status,
resumable, epochs_completed, total_epochs, checkpoint_files, folds, ...).

*run-root-dir* is cwd-relative (cwd is the host repo root — see _common.py),
e.g. outputs/experiments/<experiment_id> for this profile's manifest_layout
("experiments"). This is dissert-specific (orchestration.status is that
repo's own module) — a profile without it gets a clean BridgeUnavailable
via the existing bridge.py mechanism, not a dashboard-wide failure.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _common import run_main  # noqa: E402


def main(argv):
    if not argv:
        raise ValueError("usage: describe_run.py <run-root-dir> [--verify]")
    run_root = argv[0]
    verify = "--verify" in argv[1:]

    from orchestration.status import describe_run

    return describe_run(run_root, verify=verify)


if __name__ == "__main__":
    run_main(main)
