"""KaggleRunner — one instance per configured Kaggle account. A thin facade
over backend/kaggle.py: no new state, no behavior change to that module.
Named `kaggle.py` inside this package (distinct from the top-level
`backend/kaggle.py` it wraps — imported below as `kaggle_backend` to keep
the two unambiguous)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import kaggle as kaggle_backend
from .base import CapacitySnapshot, LaunchSpec, Runner, RunnerCapabilities, RunnerCapabilityError, RunUnit

RUNNER_ID_PREFIX = "kaggle:"

# Kaggle's own native worker statuses (backend/kaggle.py) -> canonical. A
# worker with no status yet (never pushed) isn't represented as a RunUnit at
# all — see KaggleRunner.list_units() — the same way an mclab config that's
# never been launched has no Terminals entry either.
_STATUS_MAP = {
    "push_failed": "failed",
    "pushed": "pending",       # uploaded; Kaggle hasn't reported queued/running yet
    "queued": "pending",
    "preparing": "pending",
    "running": "running",
    "complete": "done",
    "downloaded": "done",
    "error": "failed",
    "cancelAcknowledged": "cancelled",
    "unknown": "unknown",
}

# Confirmed against the current official `kaggle` CLI (Kaggle/kaggle-cli,
# 2026 — DASHBOARD_REDESIGN_PLAN.md §2.1's fact-check): `kernels` has no
# stop/cancel/interrupt subcommand. `kernels delete` exists but removes the
# kernel from the account entirely — a materially more destructive, more
# permanent action than mclab's "kill" (which just ends a tmux session, the
# run's history stays visible). Deliberately not wired as this runner's
# `kill` capability for that reason; a future "delete kernel" action, if
# wanted, should be its own explicit, separately-confirmed control, not
# hidden behind a button labeled the same as mclab's Kill.
_STOP_KILL_SUPPORTED = False


def _to_unit(account_name: str, w: Dict[str, Any]) -> RunUnit:
    raw_status = w.get("status") or "unknown"
    return RunUnit(
        unit_id=w["worker_id"],
        runner_id=f"{RUNNER_ID_PREFIX}{account_name}",
        label=w["worker_id"],
        status=_STATUS_MAP.get(raw_status, "unknown"),
        raw_status=raw_status,
        config_path=w.get("last_config_path"),
        mode=w.get("last_mode"),
        extra={
            "kernel_slug": w.get("kernel_slug"),
            "over_budget": w.get("over_budget", False),
            "budget_hours": w.get("budget_hours"),
            "pushed_at": w.get("pushed_at"),
            "notebook_backed": bool(w.get("notebook_path")),
            "notebook_changed": w.get("notebook_changed"),
            "last_error": w.get("last_error"),
        },
    )


class KaggleRunner(Runner):
    kind = "kaggle"
    capabilities = RunnerCapabilities(
        direct_launch=True, live_log=False, stop=_STOP_KILL_SUPPORTED, kill=_STOP_KILL_SUPPORTED,
        restart=True, queue=True, budget_metered=True,
    )

    def __init__(self, account_name: str):
        self.account_name = account_name
        self.id = f"{RUNNER_ID_PREFIX}{account_name}"
        self.label = f"Kaggle · {account_name}"

    def _account(self) -> Optional[Dict[str, Any]]:
        return next((a for a in kaggle_backend.list_accounts() if a["name"] == self.account_name), None)

    def list_units(self) -> List[RunUnit]:
        account = self._account()
        if account is None:
            return []
        return [_to_unit(self.account_name, w) for w in account.get("workers", []) if w.get("status")]

    def launch(self, spec: LaunchSpec) -> RunUnit:
        if not spec.target:
            raise ValueError("KaggleRunner.launch() needs spec.target set to a worker_id")
        push_result = kaggle_backend.push(spec.target, spec.config_path, spec.mode, spec.extra_args)
        unit = self._unit_for(spec.target)
        if push_result.get("concurrent_warning"):
            unit.extra["concurrent_warning"] = push_result["concurrent_warning"]
        return unit

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
        kaggle_backend.restart(unit_id)
        return self._unit_for(unit_id)

    def _unit_for(self, worker_id: str) -> RunUnit:
        account = self._account()
        worker = next((w for w in (account or {}).get("workers", []) if w["worker_id"] == worker_id), None)
        if worker is None:
            raise KeyError(f"Unknown worker '{worker_id}'")
        return _to_unit(self.account_name, worker)

    def capacity(self) -> CapacitySnapshot:
        account = self._account()
        if account is None:
            return CapacitySnapshot(unit="slots", used=0, limit=1)
        in_progress = sum(
            1 for w in account.get("workers", [])
            if (w.get("status") or "") in ("pushed", "queued", "preparing", "running")
        )
        return CapacitySnapshot(
            unit="slots", used=in_progress, limit=1,  # Kaggle runs ~1 kernel per account at a time
            extra={
                "budget_metered": True,
                "hours_this_week": account.get("usage_estimate", {}).get("hours_this_week"),
                "usage_history": account.get("usage_history"),
                "auto_chain": account.get("auto_chain", False),
            },
        )


def list_kaggle_runners() -> List[KaggleRunner]:
    return [KaggleRunner(a["name"]) for a in kaggle_backend.list_accounts()]
