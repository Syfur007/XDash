"""Shared dataclasses + the Runner interface. See backend/runners/__init__.py
for the package's overall purpose."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# One canonical status vocabulary every runner's native status maps into
# (DASHBOARD_REDESIGN_PLAN.md §2.3) — the local device's `running/completed/failed/
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
    stop: bool               # soft interrupt (keep the session/kernel, stop the command)
    kill: bool               # hard stop (end the session/kernel entirely)
    restart: bool            # re-launch the same config/mode/args
    queue: bool               # has its own "launch the next queued thing automatically" policy
    budget_metered: bool = False   # capacity is a time/hour budget, not just a slot count
    # Multi_runner_XDash.md Phase 2 — all three false by default (matches
    # every pre-existing runner's real behaviour), so a kind that doesn't set
    # them is silently correct rather than silently wrong.
    volatile: bool = False         # the runtime disappears if XDash stops (e.g. an unprovisioned Colab VM)
    provisioned: bool = False      # capacity must be created (colab new/…) before use — not just claimed
    live_checkpoints: bool = False # a mid-run heartbeat can pull checkpoints back (false: no shell mid-run)


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

    # ----------------------------------------------------------------
    # Presentation facade (pre-existing) — /api/runners, /api/experiments/active.
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

    # ----------------------------------------------------------------
    # Dispatch lifecycle (Multi_runner_XDash.md Phase 2) — what
    # backend/experiments.py's dispatcher calls instead of branching on
    # kind. Every method takes and returns the plain Experiment/Attempt
    # dicts experiments.py already works with — this is an extraction of
    # existing per-kind logic into one shape, not a new data model.
    def can_accept(self, experiment: Dict[str, Any], est_hours: float) -> Optional[Dict[str, Any]]:
        """None if this runner could take *experiment* right now. Otherwise a
        `blocked`-shaped dict ({code, detail, ...}) explaining why not — a
        pure read, never claims anything."""
        raise NotImplementedError

    def dispatch_priority(self, experiment: Dict[str, Any], est_hours: float) -> Tuple:
        """Sort key used to pick among several *eligible* runners of the
        same kind (e.g. several idle Kaggle accounts) — lower sorts first.
        The default treats every runner as equally good, which is correct
        for any kind with only one instance (local, one SSH host, …).
        Override where "best-fit" is meaningful, as KaggleRunner does."""
        return (0,)

    def dispatch(self, experiment: Dict[str, Any], attempt: Dict[str, Any]) -> Dict[str, Any]:
        """*attempt* has just been claimed (status already "dispatching",
        slot already this runner's id). Launches the unit and returns the
        patch — at minimum {"unit_ref": ..., "stages": [...]} — the caller
        merges in along with status="running". Raises on failure; the
        caller turns that into a blocked/retry, never a crash."""
        raise NotImplementedError

    def poll(self, attempt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Fresh live status for an in-flight *attempt* this runner owns.
        Returns None if this attempt's unit_ref doesn't match this runner's
        shape (not owned). Otherwise:
            {"raw_status": str|None, "stages": [...],
             "finished": bool,          # the underlying unit reached a
                                         # terminal state the store hasn't
                                         # recorded yet
             "succeeded": bool|None}    # only meaningful when finished
        Called both for live display (frequent, cheap) and by the
        background dispatch tick (to notice completion) — the same method
        serves both, which is what collapses the old key-sniffing branches."""
        raise NotImplementedError

    def collect(self, attempt: Dict[str, Any]) -> Optional[str]:
        """Called once, after poll() reports finished=True: pull the run's
        artifacts to where results_ingest.register_ledger() can find them,
        register them, and return the results_dir (or None if there was
        nothing to collect). A failure here must never strand the attempt —
        callers resolve it regardless."""
        raise NotImplementedError

    def cancel(self, attempt: Dict[str, Any]) -> bool:
        """Best-effort stop of the underlying unit for an in-flight
        *attempt* — the attempt record itself is already marked cancelled
        by the caller before this runs. Returns False on a best-effort
        failure to actually stop it (never raises for "no cooperative stop
        exists", which is a real, honest True — see KaggleRunner)."""
        raise NotImplementedError

    # ----------------------------------------------------------------
    # Resume support (Multi_runner_XDash.md Phase 5) — stubs for now. The
    # four-way asymmetry between runner kinds lives entirely in these two
    # methods and nowhere else once Phase 3/5 land.
    def seed(self, experiment: Dict[str, Any], attempt: Dict[str, Any], checkpoint_files: List[str]) -> Dict[str, Any]:
        """Makes *checkpoint_files* available to the runtime about to be
        dispatched. Returns whatever dispatch() needs to know about it, or
        {} when there's nothing to do (every kind, for a fresh leg-1
        attempt with no checkpoints yet)."""
        return {}

    def heartbeat(self, attempt: Dict[str, Any]) -> None:
        """Pull a running attempt's latest checkpoint back without waiting
        for it to finish. A no-op wherever `capabilities.live_checkpoints`
        is False (Kaggle: no shell mid-run)."""
        return None
