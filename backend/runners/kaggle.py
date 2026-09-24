"""KaggleRunner — one instance per configured Kaggle account. A thin, read-only
facade over the Attempt records backend/experiments.py owns: no new state, no
second source of truth. Named `kaggle.py` inside this package (distinct from
the top-level `backend/kaggle.py`, imported below as `kaggle_backend` to keep
the two unambiguous).

Attempt-backed since the worker registry was retired (XDASH_V2_PLAN.md §3.7).
A Kaggle account is a Slot, and what occupies it is an Attempt — there is no
longer any per-worker record to enumerate, so `list_units()` projects
experiments.list_experiments(slot=...) instead. Launching is likewise no
longer this facade's job: it is `POST /api/experiments`, which goes through
the one dispatcher rather than around it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import kaggle as kaggle_backend
from .base import CapacitySnapshot, LaunchSpec, Runner, RunnerCapabilities, RunnerCapabilityError, RunUnit

RUNNER_ID_PREFIX = "kaggle:"

# Confirmed against the current official `kaggle` CLI (Kaggle/kaggle-cli,
# 2026 — DASHBOARD_REDESIGN_PLAN.md §2.1's fact-check): `kernels` has no
# stop/cancel/interrupt subcommand. `kernels delete` exists but removes the
# kernel from the account entirely — a materially more destructive, more
# permanent action than the local device's "kill" (which just ends a tmux session, the
# run's history stays visible). Deliberately not wired as this runner's
# `kill` capability for that reason; a future "delete kernel" action, if
# wanted, should be its own explicit, separately-confirmed control, not
# hidden behind a button labeled the same as the local device's Kill.
_STOP_KILL_SUPPORTED = False

# Attempt statuses that occupy the account's one slot.
_OCCUPYING = ("dispatching", "running")


def _to_unit(account_name: str, view: Dict[str, Any]) -> RunUnit:
    """One Experiment view (as list_experiments returns it) -> one RunUnit.
    The Attempt's status is already canonical, so unlike the old worker-backed
    version there is no status map here — `raw_status` carries Kaggle's own
    word when the poller has recorded one."""
    attempt = view.get("current_attempt") or {}
    unit_ref = attempt.get("unit_ref") or {}
    return RunUnit(
        unit_id=attempt.get("attempt_id") or view["experiment_id"],
        runner_id="%s%s" % (RUNNER_ID_PREFIX, account_name),
        label=view["experiment_id"],
        status=attempt.get("status") or "unknown",
        raw_status=attempt.get("raw_status") or attempt.get("status") or "unknown",
        config_path=view.get("config_path"),
        # No mode: a Kaggle push runs train then eval together inside one kernel
        # (EXPERIMENT_AUTOMATION_PLAN.md §2.4), so there is no single mode to report.
        mode=None,
        extra={
            "experiment_id": view["experiment_id"],
            "kernel_slug": unit_ref.get("kernel_slug"),
            "results_dir": unit_ref.get("results_dir"),
            "started_at": attempt.get("started_at"),
            "stages": attempt.get("stages") or [],
            "blocked": attempt.get("blocked"),
        },
    )


class KaggleRunner(Runner):
    kind = "kaggle"
    capabilities = RunnerCapabilities(
        # direct_launch is False now: this facade no longer pushes. Launching a
        # Kaggle experiment means creating one (POST /api/experiments) and
        # letting the dispatcher claim this slot.
        direct_launch=False, live_log=False, stop=_STOP_KILL_SUPPORTED, kill=_STOP_KILL_SUPPORTED,
        restart=False, queue=True, budget_metered=True,
    )

    def __init__(self, account_name: str):
        self.account_name = account_name
        self.id = "%s%s" % (RUNNER_ID_PREFIX, account_name)
        self.label = "Kaggle · %s" % account_name

    def _account(self) -> Optional[Dict[str, Any]]:
        return next((a for a in kaggle_backend.list_accounts() if a["name"] == self.account_name), None)

    def _views(self) -> List[Dict[str, Any]]:
        # Imported here, not at module scope: experiments.py imports
        # backend/kaggle.py, so a top-level import would close a cycle through
        # this package's __init__.
        from .. import experiments
        return experiments.list_experiments(slot=self.id)

    def list_units(self) -> List[RunUnit]:
        return [_to_unit(self.account_name, v) for v in self._views()]

    def launch(self, spec: LaunchSpec) -> RunUnit:
        raise RunnerCapabilityError(
            "Kaggle is not launched directly any more — create an experiment "
            "(POST /api/experiments) and the dispatcher will claim this account's slot."
        )

    def stop(self, unit_id: str) -> bool:
        raise RunnerCapabilityError(
            "Kaggle has no stop/cancel API — let the push run to completion or its budget "
            "timeout, or cancel it manually on kaggle.com."
        )

    def kill(self, unit_id: str) -> bool:
        raise RunnerCapabilityError(
            "Kaggle has no soft-kill API for a running kernel (kernels delete removes it from "
            "the account entirely, which this runner deliberately doesn't expose as 'kill')."
        )

    def restart(self, unit_id: str) -> RunUnit:
        raise RunnerCapabilityError(
            "Retry a Kaggle experiment instead (POST /api/experiments/<id>/retry) — a retry is "
            "a new Attempt, which is what the object model records (XDASH_V2_PLAN.md §3.2)."
        )

    def capacity(self) -> CapacitySnapshot:
        account = self._account()
        if account is None:
            return CapacitySnapshot(unit="slots", used=0, limit=1)
        used = sum(1 for v in self._views() if (v.get("current_attempt") or {}).get("status") in _OCCUPYING)
        return CapacitySnapshot(
            unit="slots", used=used, limit=1,  # Kaggle runs ~1 kernel per account at a time
            extra={
                "budget_metered": True,
                "hours_this_week": account.get("usage_estimate", {}).get("hours_this_week"),
                "usage_history": account.get("usage_history"),
            },
        )


def list_kaggle_runners() -> List[KaggleRunner]:
    return [KaggleRunner(a["name"]) for a in kaggle_backend.list_accounts()]
