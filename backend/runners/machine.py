"""MachineRunner — one instance per host record (local or SSH). Replaces
backend/runners/local.py (Multi_runner_XDash.md Phase 3): the local machine
is not a separate class or a special case, it is the host whose Transport
happens to be LocalTransport. Every difference between running here and
running on a lab box was already captured by backend/transport.py's own
local-vs-remote branch (Phase 1) — this class holds no branch of its own.

A thin facade over terminals.py/tmux_runner.py/scheduler.py (execution) plus
backend/transport.py (getting the working tree there and results back) — no
new state, no behavior change to any of those modules for the local host,
since LocalTransport's push/pull are no-ops.

`seed` is implemented for real as of Multi_runner_XDash.md Phase 5 — stages
the previous leg's checkpoints (wherever they actually are, possibly a
Kaggle download) into a canonical tree and transport.push()es it onto this
host; `dispatch()` adds --resume to the train half alone when the attempt
is a resume. `heartbeat` (mid-run checkpoint pull for live progress/crash
resilience) stays at base.Runner's no-op default — not required for legs to
chain correctly, only for a heartbeat display nothing calls yet.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import hosts
from .. import results_ingest
from .. import scheduler
from .. import terminals
from .. import tmux_runner as tmux
from .. import transport as transport_mod
from ..config import settings
from .base import ACTIVE_STATUSES, CapacitySnapshot, LaunchSpec, Runner, RunnerCapabilities, RunUnit
from .registry import slot_id

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

# transport.available() is a real network round-trip for an SshTransport
# (up to its own connect timeout) — called from can_accept() once per
# pending experiment per tick, so an unreachable host must not cost that
# every time. Shared across every MachineRunner instance, keyed by host_id.
_AVAILABILITY_TTL = 30.0
_availability_lock = threading.Lock()
_availability_cache: Dict[str, tuple] = {}


def _cached_available(host_id: str, transport: transport_mod.Transport) -> bool:
    now = time.monotonic()
    with _availability_lock:
        cached = _availability_cache.get(host_id)
        if cached is not None and now - cached[0] < _AVAILABILITY_TTL:
            return cached[1]
    reachable = transport.available()
    with _availability_lock:
        _availability_cache[host_id] = (now, reachable)
    return reachable


def _rel(path: Path) -> Path:
    """*path* (always under settings.repo_root — see backend/config.py) as a
    repo-relative path, so the same relative layout can be re-rooted under a
    host's own repo_root or under a local results_dir. This is what lets
    collect() work unchanged for both manifest_layout shapes without hardcoding
    either one's directory names."""
    return path.relative_to(settings.repo_root)


def _to_unit(runner_id: str, term: Dict[str, Any]) -> RunUnit:
    return RunUnit(
        unit_id=term["session_name"],
        runner_id=runner_id,
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


def _live_stage(item_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not item_id:
        return None
    item = next((i for i in scheduler.list_items()["items"] if i["id"] == item_id), None)
    if item is None:
        return None
    return {"item_id": item_id, "status": item["status"]}


class MachineRunner(Runner):
    capabilities = RunnerCapabilities(
        direct_launch=True, live_log=True, stop=True, kill=True, restart=True, queue=True,
        live_checkpoints=True,  # a shell is always available mid-run — true parity, no heartbeat needed
    )

    def __init__(self, host: "hosts._Host"):
        self.host = host
        self.kind = host.kind
        self.id = slot_id(host.kind, host.id)
        self.label = host.label
        # for_host_record(), not for_host(host.id): a ColabRunner (Phase 4)
        # constructs a host that may not be registered in hosts.json yet (no
        # VM provisioned), so resolving the transport must work from the
        # object already in hand rather than requiring a second, registry-
        # backed lookup by id.
        self._transport = transport_mod.for_host_record(host)

    # ------------------------------------------------------------ presentation
    def list_units(self) -> List[RunUnit]:
        return [_to_unit(self.id, t) for t in terminals.list_terminals(host_id=self.host.id)]

    def active_units(self) -> List[RunUnit]:
        return [u for u in self.list_units() if u.status in ACTIVE_STATUSES]

    def launch(self, spec: LaunchSpec) -> RunUnit:
        return _to_unit(self.id, terminals.launch(spec.config_path, spec.mode, spec.extra_args, host_id=self.host.id))

    def stop(self, unit_id: str) -> bool:
        return terminals.stop(unit_id)

    def kill(self, unit_id: str) -> bool:
        return terminals.kill(unit_id)

    def restart(self, unit_id: str) -> RunUnit:
        return _to_unit(self.id, terminals.restart(unit_id))

    def capacity(self) -> CapacitySnapshot:
        used = sum(1 for u in self.list_units() if u.extra.get("managed") and u.status == "running")
        sched = scheduler.list_items()
        return CapacitySnapshot(
            unit="slots", used=used, limit=self.host.max_concurrent,
            extra={
                "tmux_available": tmux.tmux_available(host_id=self.host.id),
                "scheduler_paused": sched.get("paused", False),
            },
        )

    # ---------------------------------------------------------------- dispatch
    def _free_slots(self) -> int:
        """This host's own share of the flat, host-partitioned scheduler queue
        (backend/scheduler.py's own _tick()) — generalizes the original
        LocalRunner's _local_free_slots() from "the one queue" to "this host's
        slice of the queue", reading fresh each call for the same reason the
        original did: repeated calls within one dispatch tick must see
        anything already claimed earlier in that same tick."""
        data = scheduler.list_items()
        if data.get("paused"):
            return 0
        items = [i for i in data["items"] if (i.get("host_id") or hosts.LOCAL_HOST_ID) == self.host.id]
        running = sum(1 for i in items if i["status"] == "running")
        by_id = {i["id"]: i for i in items}

        def launch_eligible(item: Dict[str, Any]) -> bool:
            dep_id = item.get("depends_on")
            if not dep_id:
                return True
            dep = by_id.get(dep_id)
            return dep is not None and dep["status"] == "completed"

        pending = sum(1 for i in items if i["status"] == "pending" and launch_eligible(i))
        return max(0, self.host.max_concurrent - running - pending)

    def can_accept(self, experiment: Dict[str, Any], est_hours: float) -> Optional[Dict[str, Any]]:
        # No dataset-mapping check here, unlike KaggleRunner: a machine has
        # direct filesystem access to data/ already (Multi_runner_XDash.md's
        # own Colab-constraints table: "on a lab SSH box data is already
        # present") — dataset_map.py exists solely to attach a Kaggle dataset
        # to a kernel, which is not how a machine with a shell gets its data.
        if not self.host.is_local and not self.host.declares_repo_root:
            return {
                "code": "host-not-configured",
                "detail": f"Host '{self.host.id}' has no repo_root configured for profile '{settings.profile_name}'",
            }
        if not _cached_available(self.host.id, self._transport):
            return {"code": "host-unreachable", "detail": f"Host '{self.host.id}' is not reachable"}
        if self._free_slots() <= 0:
            return {"code": "pool-busy", "detail": "Nothing free this tick"}
        return None

    def dispatch(self, experiment: Dict[str, Any], attempt: Dict[str, Any]) -> Dict[str, Any]:
        """Pushes the working tree (a no-op on local — LocalTransport.push()
        does nothing, so local dispatch is byte-identical to before this
        class existed), then queues both halves through scheduler.py exactly
        as the pre-Phase-3 LocalRunner did — that queue is what actually
        enforces per-host max_concurrent. A resumed leg (attempt.resume_of
        set) adds --resume to the *train* half only, via scheduler.add_item's
        train_extra_args — eval.py has no --resume of its own."""
        self._transport.push(settings.repo_root, self.host.repo_root, transport_mod.DEFAULT_PUSH_EXCLUDES)
        base_extra_args = experiment.get("extra_args") or ""
        train_extra_args = (base_extra_args + " --resume").strip() if attempt.get("resume_of") else base_extra_args
        items = scheduler.add_item(
            experiment["config_path"], "both", base_extra_args, host_id=self.host.id,
            train_extra_args=train_extra_args,
        )
        return {
            "unit_ref": {"train_item_id": items[0]["id"], "eval_item_id": items[1]["id"]},
            "stages": [{"name": "train", "status": "pending"}, {"name": "eval", "status": "pending"}],
        }

    def poll(self, attempt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Always reports finished=False, same reason as the pre-Phase-3
        LocalRunner: a machine attempt's completion is discovered push-style
        via scheduler._tick() -> on_scheduler_item_finished the moment it
        happens, not by this method. See that docstring (now here) for the
        remote gap this leaves (a completion landing while XDash itself is
        down) and why it's closed within a few ticks after restart instead."""
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
        """Local: a no-op — the run's train.py/eval.py already wrote straight
        into this repo's own ledger, there is nothing to pull. Remote: rsyncs
        the finished run's manifest(s) + ledger row back into a results_dir
        shaped like a re-rooted slice of the local repo (same relative paths
        under repo_root, via _rel()), which is what lets
        results_ingest.register_ledger() read it exactly like a Kaggle
        download regardless of this profile's manifest_layout."""
        if self.host.is_local:
            return None
        experiment_id = attempt.get("experiment_id")
        if not experiment_id:
            return None

        results_dir = settings.repo_root / "outputs" / "remote" / self.host.id / experiment_id
        if settings.manifest_layout == "experiments":
            manifests_rel = _rel(settings.experiments_dir) / experiment_id
        else:
            # Legacy layout keys a run's manifest by run_id, not experiment_id
            # — unknown until after collection — so this pulls the whole
            # runs/ tree rather than one experiment's slice. rsync is
            # incremental, so a repeated collect() stays cheap after the first.
            manifests_rel = _rel(settings.artifacts_dir) / "runs"
        ledger_rel = _rel(settings.ledger_dir)

        try:
            self._transport.pull(self.host.repo_root / manifests_rel, results_dir / manifests_rel)
            self._transport.pull(self.host.repo_root / ledger_rel, results_dir / ledger_rel)
        except transport_mod.TransportError:
            return None

        results_ingest.register_ledger(results_dir)
        return str(results_dir)

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

    def seed(self, experiment: Dict[str, Any], attempt: Dict[str, Any], checkpoint_files: List[str]) -> Dict[str, Any]:
        """Makes the previous leg's checkpoints available on this host before
        dispatch() runs (Multi_runner_XDash.md Phase 5) — stages them into a
        canonical outputs/experiments/... tree (results_ingest.stage_checkpoint_files,
        which re-roots regardless of which runner kind the previous leg
        actually ran on — a checkpoint being resumed onto a machine may have
        just been downloaded from Kaggle, not produced locally), then
        transport.push()es that tree onto this host's own repo_root.

        Local is *not* simply skipped: the previous leg may have run on
        Kaggle, in which case the files exist only under
        outputs/kaggle/<experiment_id>/... and still need staging into the
        canonical local position — only the transport hop is a genuine no-op
        for local, since LocalTransport.push() is one already. Unlike
        Kaggle, no snapshot dataset is needed either way: XDash has a real
        shell on any machine host."""
        if not checkpoint_files:
            return {}
        if self.host.is_local:
            results_ingest.stage_checkpoint_files(settings.repo_root, checkpoint_files)
            return {}
        tmpdir = Path(tempfile.mkdtemp(prefix="xdash_seed_"))
        try:
            copied = results_ingest.stage_checkpoint_files(tmpdir, checkpoint_files)
            if copied == 0:
                return {}
            self._transport.push(tmpdir / "outputs", self.host.repo_root / "outputs")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return {}
