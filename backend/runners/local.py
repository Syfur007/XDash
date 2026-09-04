"""LocalRunner — mclab, the machine the dashboard itself runs on. A thin
facade over terminals.py/tmux_runner.py (execution) and scheduler.py (the
concurrency ceiling it already computes) — no new state, no behavior
change to any of those modules."""
from __future__ import annotations

from typing import Any, Dict, List

from .. import scheduler
from .. import terminals
from .. import tmux_runner as tmux
from .base import ACTIVE_STATUSES, CapacitySnapshot, LaunchSpec, Runner, RunnerCapabilities, RunUnit

RUNNER_ID = "local"

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


class LocalRunner(Runner):
    id = RUNNER_ID
    kind = "local"
    label = "mclab"
    capabilities = RunnerCapabilities(
        direct_launch=True, live_log=True, stop=True, kill=True, restart=True, queue=True,
    )

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
