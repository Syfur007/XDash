"""Runner abstraction (DASHBOARD_REDESIGN_PLAN.md §2, extended to a real
write path by Multi_runner_XDash.md Phase 2): one shared shape every
lifecycle view — and, since Phase 2, the dispatcher itself — can drive over
every execution backend this dashboard controls. `local.py` is a thin facade
over terminals.py/tmux_runner.py/scheduler.py; `kaggle.py` is a thin facade
over backend/kaggle.py, Attempt-backed since the worker registry retired
(§3.7). Neither facade holds its own state or duplicates logic. `registry.py`
is the single source of truth for slot identity (`slot_id`/`parse_slot_id`)
and for listing/looking up runners — the thing a fifth kind needs to touch.
"""
from __future__ import annotations

from .base import CapacitySnapshot, LaunchSpec, Runner, RunnerCapabilities, RunnerCapabilityError, RunUnit
from .registry import LOCAL, get_runner, list_runners, parse_slot_id, slot_id
from .local import LocalRunner
from .kaggle import KaggleRunner, list_kaggle_runners

__all__ = [
    "CapacitySnapshot", "LaunchSpec", "Runner", "RunnerCapabilities", "RunnerCapabilityError", "RunUnit",
    "LocalRunner", "KaggleRunner", "list_runners", "get_runner",
    "LOCAL", "slot_id", "parse_slot_id",
]
