"""Runner abstraction (DASHBOARD_REDESIGN_PLAN.md §2): one shared shape every
lifecycle view can render (status, capacity, "can I do X here") over the two
execution backends this dashboard drives — mclab (`local.py`, a thin facade
over terminals.py/tmux_runner.py) and each configured Kaggle account
(`kaggle.py`, a thin facade over backend/kaggle.py). Neither facade holds its
own state or duplicates logic; they call the existing, already-tested
functions verbatim and translate their native status vocabularies into one
canonical set (see base.CANONICAL_STATUSES) so a single status-badge
component can render every runner consistently. Every pre-existing route
(`/api/terminals/*`, `/api/scheduler/*`, `/api/kaggle/*`) keeps working
unchanged — this package is purely additive.
"""
from __future__ import annotations

from typing import List

from .base import CapacitySnapshot, LaunchSpec, Runner, RunnerCapabilities, RunnerCapabilityError, RunUnit
from .local import LocalRunner
from .kaggle import KaggleRunner, list_kaggle_runners

__all__ = [
    "CapacitySnapshot", "LaunchSpec", "Runner", "RunnerCapabilities", "RunnerCapabilityError", "RunUnit",
    "LocalRunner", "KaggleRunner", "list_runners", "get_runner",
]


def list_runners() -> List[Runner]:
    """mclab (always present) + one KaggleRunner per configured account."""
    return [LocalRunner(), *list_kaggle_runners()]


def get_runner(runner_id: str) -> Runner:
    for r in list_runners():
        if r.id == runner_id:
            return r
    raise KeyError(f"Unknown runner '{runner_id}'")
