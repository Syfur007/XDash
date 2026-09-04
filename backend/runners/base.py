"""Shared dataclasses + the Runner interface. See backend/runners/__init__.py
for the package's overall purpose."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# One canonical status vocabulary every runner's native status maps into
# (DASHBOARD_REDESIGN_PLAN.md §2.3) — mclab's `running/completed/failed/
# stopped/interrupted/unmanaged` (terminals.py), the scheduler's `pending/
# running/cancelling/completed/failed/cancelled/skipped` (scheduler.py), and
# Kaggle's `queued/preparing/running/complete/error/cancelAcknowledged`
# (kaggle.py) each collapse into this set. `raw_status` on RunUnit always
# keeps the untranslated native value alongside it — nothing is lost, this
# is an added lens, not a replacement.
CANONICAL_STATUSES = (
    "pending", "running", "stopping", "done", "failed",
    "interrupted", "cancelled", "skipped", "unmanaged", "unknown",
)

# Statuses that belong on an "Active" / in-flight view rather than a
# completed-history one.
ACTIVE_STATUSES = frozenset({"pending", "running", "stopping", "interrupted"})


@dataclass
class RunnerCapabilities:
    """What a runner can actually do — every lifecycle view branches on
    these instead of assuming parity between runner kinds. `stop`/`kill`
    false is a real, load-bearing "no" (e.g. confirmed absent from the
    Kaggle CLI as of this writing — DASHBOARD_REDESIGN_PLAN.md §2.1's
    fact-check), not a placeholder for "not implemented yet"."""
    direct_launch: bool     # can this runner launch one config+mode+args on demand?
    live_log: bool          # is a live output stream available while running?
    stop: bool              # soft interrupt (keep the session/kernel, stop the command)
    kill: bool               # hard stop (end the session/kernel entirely)
    restart: bool            # re-launch the same config/mode/args
    queue: bool               # has its own "launch the next queued thing automatically" policy
    budget_metered: bool = False  # capacity is a time/hour budget, not just a slot count


@dataclass
class CapacitySnapshot:
    unit: str                      # "slots" | "budget_hours"
    used: float
    limit: Optional[float] = None  # None = no fixed ceiling known
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RunUnit:
    unit_id: str            # tmux session_name, or a Kaggle worker_id
    runner_id: str          # "local" | "kaggle:<account_name>"
    label: str
    status: str              # canonical (CANONICAL_STATUSES)
    raw_status: str          # the runner's own native status string
    config_path: Optional[str] = None
    mode: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)  # runner-specific detail, passed through as-is


@dataclass
class LaunchSpec:
    config_path: str
    mode: str = "train"
    extra_args: str = ""
    target: Optional[str] = None   # worker_id for a KaggleRunner; ignored by LocalRunner


class RunnerCapabilityError(Exception):
    """Raised when a caller invokes an action a runner's own
    RunnerCapabilities says it doesn't support (e.g. stop() on Kaggle,
    confirmed absent from its CLI — DASHBOARD_REDESIGN_PLAN.md §2.1) — a
    clear, typed refusal instead of a silent no-op or an AttributeError."""


class Runner:
    id: str
    kind: str
    label: str
    capabilities: RunnerCapabilities

    def list_units(self) -> List[RunUnit]:
        raise NotImplementedError

    def launch(self, spec: LaunchSpec) -> RunUnit:
        raise NotImplementedError

    def stop(self, unit_id: str) -> bool:
        raise NotImplementedError

    def kill(self, unit_id: str) -> bool:
        raise NotImplementedError

    def restart(self, unit_id: str) -> RunUnit:
        raise NotImplementedError

    def capacity(self) -> CapacitySnapshot:
        raise NotImplementedError

    def as_dict(self) -> Dict[str, Any]:
        cap = self.capacity()
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "capabilities": vars(self.capabilities),
            "capacity": {"unit": cap.unit, "used": cap.used, "limit": cap.limit, "extra": cap.extra},
        }
