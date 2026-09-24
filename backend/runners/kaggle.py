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

Since Multi_runner_XDash.md Phase 2, this is also where the Kaggle half of
the old `_claim_and_dispatch_kaggle`/`_kaggle_candidate_accounts`/
`_kaggle_block`/`_pick_kaggle_account`/`_poll_kaggle_attempts` logic in
backend/experiments.py now lives — moved, not duplicated. `can_accept()`
below does the job both `_kaggle_candidate_accounts()` (silently filter) and
`_kaggle_block()` (explain why none qualified) used to do separately, since
an eligibility check and its own rejection reason are the same computation.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from .. import kaggle as kaggle_backend
from .base import CapacitySnapshot, LaunchSpec, Runner, RunnerCapabilities, RunnerCapabilityError, RunUnit
from .registry import slot_id

RUNNER_KIND = "kaggle"

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
        runner_id=slot_id(RUNNER_KIND, account_name),
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
    kind = RUNNER_KIND
    capabilities = RunnerCapabilities(
        # direct_launch is False now: this facade no longer pushes. Launching a
        # Kaggle experiment means creating one (POST /api/experiments) and
        # letting the dispatcher claim this slot.
        direct_launch=False, live_log=False, stop=_STOP_KILL_SUPPORTED, kill=_STOP_KILL_SUPPORTED,
        restart=False, queue=True, budget_metered=True,
        # No mid-run heartbeat: a running kernel gives XDash no shell, only a
        # status string and, once it finishes, one zip — see the module
        # docstring's closing sentence.
        live_checkpoints=False,
    )

    def __init__(self, account_name: str):
        self.account_name = account_name
        self.id = slot_id(RUNNER_KIND, account_name)
        self.label = "Kaggle · %s" % account_name

    def _account(self) -> Optional[Dict[str, Any]]:
        return next((a for a in kaggle_backend.list_accounts() if a["name"] == self.account_name), None)

    def _views(self) -> List[Dict[str, Any]]:
        # Imported here, not at module scope: experiments.py imports this
        # package (for the slot registry), so a top-level import back would
        # close a cycle.
        from .. import experiments
        return experiments.list_experiments(slot=self.id)

    def _busy(self) -> bool:
        from .. import experiments
        return experiments.is_slot_busy(self.id)

    # ------------------------------------------------------------ presentation
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

    # ---------------------------------------------------------------- dispatch
    def can_accept(self, experiment: Dict[str, Any], est_hours: float) -> Optional[Dict[str, Any]]:
        """Both selects (None = eligible) and explains (the block code) in
        one pass — the account-level half of what used to be two separate
        functions (_kaggle_candidate_accounts filtering silently,
        _kaggle_block separately re-deriving why). The config-level check
        (no-dataset-mapping) is identical for every account, so every
        blocked Kaggle runner for the same experiment agrees on it —
        exactly the earlier "every account uniformly blocked" behaviour."""
        account = self._account()
        if account is None:
            return {"code": "no-account", "detail": "Account not found"}

        from .. import dataset_map
        required_dataset = dataset_map.resolve_kaggle_dataset(experiment["config_path"])
        if required_dataset is None:
            name, _explicit = dataset_map.config_dataset_identity(experiment["config_path"])
            return {
                "code": "no-dataset-mapping",
                "detail": f"'{name}' has no Kaggle dataset mapping" if name else "Config declares no dataset",
            }

        if self._busy():
            return {"code": "pool-busy", "detail": "This account is currently busy"}

        session_cap = float(account.get("weekly_budget_hours") or kaggle_backend.settings.kaggle_default_budget_hours)
        if est_hours > session_cap:
            return {
                "code": "exceeds-session-cap",
                "detail": f"Estimated {est_hours:.1f}h exceeds this account's session cap ({session_cap:.1f}h)",
            }

        remaining = (account.get("usage_estimate") or {}).get("remaining_hours")
        if remaining is not None and est_hours > remaining:
            clears_at = (kaggle_backend._utc_week_start() + timedelta(weeks=1)).isoformat()
            return {
                "code": "quota-exhausted", "detail": "This account is over its weekly budget",
                "clears_at": clears_at,
            }
        return None

    def dispatch_priority(self, experiment: Dict[str, Any], est_hours: float) -> Tuple:
        """Best-fit, not round robin: prefer the account with the most
        headroom left, breaking ties toward whichever has gone longest
        without activity — same Rule 3 the old _pick_kaggle_account() used."""
        account = self._account() or {}
        usage = account.get("usage_estimate") or {}
        remaining = usage.get("remaining_hours")
        remaining_key = remaining if remaining is not None else float("inf")
        return (remaining_key, self._last_activity())

    def _last_activity(self) -> str:
        from .. import experiments
        times = [a.get("started_at") for a in experiments.attempts_for_slot(self.id) if a.get("started_at")]
        return max(times) if times else ""

    def dispatch(self, experiment: Dict[str, Any], attempt: Dict[str, Any]) -> Dict[str, Any]:
        from .. import dataset_map
        required_dataset = dataset_map.resolve_kaggle_dataset(experiment["config_path"])
        result = kaggle_backend.push_experiment_attempt(
            self.account_name, experiment["experiment_id"], experiment["config_path"],
            experiment.get("extra_args") or "", [required_dataset] if required_dataset else [],
        )
        return {
            "unit_ref": {
                "account": self.account_name,
                "kernel_slug": result["kernel_slug"], "results_dir": result["results_dir"],
            },
            "stages": [{"name": "run", "status": "running"}],
        }

    def poll(self, attempt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        unit_ref = attempt.get("unit_ref") or {}
        if not unit_ref.get("kernel_slug"):
            return None
        try:
            result = kaggle_backend.refresh_experiment_status(unit_ref["account"], unit_ref["kernel_slug"])
        except kaggle_backend.KaggleOpsError:
            return {"raw_status": None, "stages": [{"name": "run", "status": "unknown"}], "finished": False, "succeeded": None}
        raw = result.get("status")
        finished = raw in kaggle_backend.FINAL_STATUSES
        return {
            "raw_status": raw,
            "stages": [{"name": "run", "status": raw or "unknown"}],
            "finished": finished,
            "succeeded": (raw == "complete") if finished else None,
        }

    def collect(self, attempt: Dict[str, Any]) -> Optional[str]:
        unit_ref = attempt.get("unit_ref") or {}
        if not unit_ref.get("kernel_slug"):
            return None
        try:
            result = kaggle_backend.download_experiment(unit_ref["account"], unit_ref["kernel_slug"], unit_ref["results_dir"])
        except kaggle_backend.KaggleOpsError:
            return None
        return result.get("results_dir")

    def cancel(self, attempt: Dict[str, Any]) -> bool:
        # No cooperative stop exists (see the class's stop()/kill() above) —
        # the caller has already marked the Attempt cancelled; there is
        # nothing more this runner can do to the kernel itself.
        return True


def list_kaggle_runners() -> List[KaggleRunner]:
    return [KaggleRunner(a["name"]) for a in kaggle_backend.list_accounts()]
