"""LocalRunner — the local machine the dashboard itself runs on. A thin
facade over terminals.py/tmux_runner.py (execution) and scheduler.py (the
concurrency ceiling it already computes) — no new state, no behavior
change to any of those modules.

Since Multi_runner_XDash.md Phase 2 this is also where the local half of the
old `_claim_and_dispatch_local`/`_resolve_attempt_live`/`_local_free_slots`
logic in backend/experiments.py now lives — moved, not duplicated; the
dispatcher calls this class's `can_accept`/`dispatch`/`poll`/`collect`/
`cancel` instead of branching on `slot == "local"`.

Phase 3 replaces this file with `machine.py`, generalizing it to any host
(local or SSH) behind `backend/transport.py`. Everything here is written so
that swap is mechanical: the only thing specific to *this* being the local
machine is that `scheduler.add_item`/`scheduler.list_items` always mean the
local queue — Phase 3's `MachineRunner` takes a host and threads `host_id`
through the same calls instead.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import scheduler
from .. import terminals
from .. import tmux_runner as tmux
from .base import ACTIVE_STATUSES, CapacitySnapshot, LaunchSpec, Runner, RunnerCapabilities, RunUnit
from .registry import LOCAL as RUNNER_ID

_STATUS_MAP = {
    "running": "running",
    "completed": "done",
    "failed": "failed",
    # Ctrl-C'd (stop()) and reboot-loss both leave a restartable, non-running
    # unit — the canonical vocabulary doesn't distinguish *why* a unit needs
    # a restart, only that it does (DASHBOARD_REDESIGN_PLAN.md §2.3).
    "stopped": "interrupted",
    "interrupted": "interrupted",
    "unmanaged": "unmanaged",
}


def _to_unit(term: Dict[str, Any]) -> RunUnit:
    return RunUnit(
        unit_id=term["session_name"],
        runner_id=RUNNER_ID,
        label=term.get("experiment_name") or term["session_name"],
        status=_STATUS_MAP.get(term["status"], "unknown"),
        raw_status=term["status"],
        config_path=term.get("config_path"),
        mode=term.get("mode"),
        extra={
            "managed": term.get("managed", False),
            "alive": term.get("alive", False),
            "restart_available": term.get("restart_available", False),
            "return_code": term.get("return_code"),
            "latest_metrics": term.get("latest_metrics"),
            "created_at": term.get("created_at"),
            "restart_count": term.get("restart_count", 0),
        },
    )


def _local_free_slots() -> int:
    """How many more local scheduler items could launch right now. Moved
    verbatim from backend/experiments.py's own helper (originally ported
    from batch_runner.py, XDASH_V2_PLAN.md D9's fix) — deliberately reads
    scheduler.list_items() fresh rather than any mirror, so repeated calls
    within one dispatch tick (once per pending experiment considered) always
    see the effect of anything already claimed earlier in that same tick."""
    data = scheduler.list_items()
    items = data["items"]
    if data.get("paused"):
        return 0
    running = sum(1 for i in items if i["status"] == "running")
    by_id = {i["id"]: i for i in items}

    def launch_eligible(item: Dict[str, Any]) -> bool:
        dep_id = item.get("depends_on")
        if not dep_id:
            return True
        dep = by_id.get(dep_id)
        return dep is not None and dep["status"] == "completed"

    pending = sum(1 for i in items if i["status"] == "pending" and launch_eligible(i))
    return max(0, data["max_concurrent"] - running - pending)


def _live_stage(item_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not item_id:
        return None
    item = next((i for i in scheduler.list_items()["items"] if i["id"] == item_id), None)
    if item is None:
        return None
    return {"item_id": item_id, "status": item["status"]}


class LocalRunner(Runner):
    id = RUNNER_ID
    kind = "local"
    label = "Local device"
    capabilities = RunnerCapabilities(
        direct_launch=True, live_log=True, stop=True, kill=True, restart=True, queue=True,
        live_checkpoints=True,  # trivially true: no transport needed to see a file that's already here
    )

    # ------------------------------------------------------------ presentation
    def list_units(self) -> List[RunUnit]:
        return [_to_unit(t) for t in terminals.list_terminals()]

    def active_units(self) -> List[RunUnit]:
        return [u for u in self.list_units() if u.status in ACTIVE_STATUSES]

    def launch(self, spec: LaunchSpec) -> RunUnit:
        return _to_unit(terminals.launch(spec.config_path, spec.mode, spec.extra_args))

    def stop(self, unit_id: str) -> bool:
        return terminals.stop(unit_id)

    def kill(self, unit_id: str) -> bool:
        return terminals.kill(unit_id)

    def restart(self, unit_id: str) -> RunUnit:
        return _to_unit(terminals.restart(unit_id))

    def capacity(self) -> CapacitySnapshot:
        used = sum(1 for u in self.list_units() if u.extra.get("managed") and u.status == "running")
        sched = scheduler.list_items()
        return CapacitySnapshot(
            unit="slots", used=used, limit=sched.get("max_concurrent"),
            extra={"tmux_available": tmux.tmux_available(), "scheduler_paused": sched.get("paused", False)},
        )

    # ---------------------------------------------------------------- dispatch
    def can_accept(self, experiment: Dict[str, Any], est_hours: float) -> Optional[Dict[str, Any]]:
        if _local_free_slots() <= 0:
            return {"code": "pool-busy", "detail": "Nothing free this tick"}
        return None

    def dispatch(self, experiment: Dict[str, Any], attempt: Dict[str, Any]) -> Dict[str, Any]:
        """Queues both halves through scheduler.py rather than launching a
        tmux session directly — that queue is what actually enforces
        max_concurrent (a direct terminals.launch() here would bypass it,
        exactly the bug XDASH_V2_PLAN.md §0.4 flagged in the old
        `runners/` facade's own launch())."""
        items = scheduler.add_item(experiment["config_path"], "both", experiment.get("extra_args") or "")
        return {
            "unit_ref": {"train_item_id": items[0]["id"], "eval_item_id": items[1]["id"]},
            "stages": [{"name": "train", "status": "pending"}, {"name": "eval", "status": "pending"}],
        }

    def poll(self, attempt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Live train/eval stage status for display. Always reports
        finished=False: a local attempt's completion is discovered
        push-style, the moment it happens, via scheduler._tick() calling
        backend/experiments.py's on_scheduler_item_finished — polling here
        for the same thing would just be a slower, redundant second path.
        The one gap that leaves (a completion that lands in the small
        window XDash itself is down) is closed within a few seconds of
        restart by scheduler.py's own tick loop re-detecting the terminal
        tmux pane and firing that same callback — not by this method."""
        unit_ref = attempt.get("unit_ref") or {}
        if "eval_item_id" not in unit_ref:
            return None
        train = _live_stage(unit_ref.get("train_item_id"))
        evalu = _live_stage(unit_ref.get("eval_item_id"))
        return {
            "raw_status": (evalu or {}).get("status"),
            "stages": [
                {"name": "train", "status": (train or {}).get("status", "unknown")},
                {"name": "eval", "status": (evalu or {}).get("status", "unknown")},
            ],
            "finished": False,
            "succeeded": None,
        }

    def collect(self, attempt: Dict[str, Any]) -> Optional[str]:
        """No-op: a local run's train.py/eval.py write straight into the
        host repo's own ledger — there is nothing to pull in from anywhere."""
        return None

    def cancel(self, attempt: Dict[str, Any]) -> bool:
        unit_ref = attempt.get("unit_ref") or {}
        ok = True
        if unit_ref.get("eval_item_id"):
            try:
                scheduler.cancel_item(unit_ref["eval_item_id"])
            except ValueError:
                ok = False
        if unit_ref.get("train_item_id"):
            try:
                scheduler.cancel_item(unit_ref["train_item_id"])
            except ValueError:
                pass
        return ok
