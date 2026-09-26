"""bridge_scripts/status.py — reports which optional host-repo modules
import cleanly under bridge_python_executable, so the dashboard can show
accurate per-feature availability (e.g. "schema validation needs the
orchestration package, not found in this repo") instead of one
all-or-nothing flag. Never raises itself — an import failure is the
expected, reportable outcome, not an error in this script.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _common import run_main  # noqa: E402


def _importable(module_name: str) -> bool:
    try:
        __import__(module_name)
        return True
    except Exception:
        return False


def main(argv):
    # Each checks the new src/dissert/ package path first, old flat layout
    # second — see XDASH_PROGRESS.md's Phase 2 section for the switch-over.
    return {
        "orchestration": _importable("dissert.config.schema") or _importable("orchestration.schema"),
        "models": _importable("dissert.models.registry") or _importable("models.registry"),
        "metrics": _importable("dissert.metrics.aggregate") or _importable("metrics.aggregate"),
        "datasets": _importable("dissert.datasets.channels") or _importable("datasets.channels"),
        "pandas": _importable("pandas"),
    }


if __name__ == "__main__":
    run_main(main)
