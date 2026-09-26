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

Completion (XDASH_PLAN.md §6.6, X3): `poll()` reports finished once the
eval half's scheduler item is terminal, and the dispatcher's one completion
path — the same one Kaggle uses — then calls `collect()` (pull the planned
run dir + ledger, persist the tmux panes), canonicalizes, classifies, and
resolves or opens the next leg. Before Phase 0, poll() always said "not
finished" and a separate push-style hook resolved machine attempts from the
exit code alone, so an SSH/Colab run was never collected at all.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import datasets
from .. import framework
from .. import hosts
from .. import results_ingest
from .. import scheduler
from .. import terminals
from .. import tmux_runner as tmux
from .. import transport as transport_mod
from ..config import settings
from ..store import atomic_write_text
from .base import ACTIVE_STATUSES, CapacitySnapshot, LaunchSpec, Runner, RunnerCapabilities, RunnerBlocked, RunUnit
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


# The local GPU, probed once per process (nvidia-smi, this machine only —
# never over ssh from the dispatch tick). False = probed, nothing found.
_local_accelerator: Any = None
_local_accelerator_lock = threading.Lock()


NVIDIA_SMI_ARGV = ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]


def parse_nvidia_smi_output(stdout: str) -> Optional[Dict[str, Any]]:
    """`{"name", "vram_gb", "source": "nvidia-smi"}` from one line of
    `--format=csv,noheader,nounits` output (only the first GPU — XDash has no
    multi-GPU-per-host concept yet), or None for empty/garbled output. A pure
    function so both the local probe below and probe_accelerator() (any
    transport, XDASH_PLAN.md §8.4's Compute Diagnostics "GPU probe") share one
    parser instead of two copies drifting apart."""
    first = (stdout or "").strip().splitlines()[:1]
    if not first or "," not in first[0]:
        return None
    name, mib = first[0].rsplit(",", 1)
    try:
        vram_gb = round(float(mib) / 1024.0, 1)
    except ValueError:
        return None
    return {"name": name.strip(), "vram_gb": vram_gb, "source": "nvidia-smi"}


def probe_accelerator(transport: "transport_mod.Transport", timeout: float = 15.0) -> Optional[Dict[str, Any]]:
    """Runs nvidia-smi over *transport* — local or SSH alike (XDASH_PLAN.md
    §8.4's Compute Diagnostics "GPU probe", closing the gap Phase 2 left:
    only the local machine was ever probed). None on any failure (no GPU, no
    nvidia-smi, host unreachable) — a probe that can't tell must never be
    reported as "no GPU", so callers keep whatever was declared before."""
    try:
        proc = transport.run(NVIDIA_SMI_ARGV, timeout=timeout)
    except (transport_mod.TransportError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return parse_nvidia_smi_output(proc.stdout)


def _probe_local_accelerator() -> Optional[Dict[str, Any]]:
    global _local_accelerator
    with _local_accelerator_lock:
        if _local_accelerator is None:
            _local_accelerator = probe_accelerator(transport_mod.LocalTransport(), timeout=10.0) or False
        return dict(_local_accelerator) if _local_accelerator else None


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


# A scheduler item in one of these will never change again.
_ITEM_TERMINAL = frozenset({"completed", "failed", "cancelled", "skipped"})


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

    def accelerator(self) -> Optional[Dict[str, Any]]:
        declared = self.host.accelerator
        if declared:
            return {**declared, "source": declared.get("source") or "host record"}
        return _probe_local_accelerator() if self.host.is_local else None

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
        # The local machine has direct filesystem access to data/ already
        # (Multi_runner_XDash.md's own Colab-constraints table: "on a lab SSH
        # box data is already present" — well, *maybe*: a fresh SSH box may
        # not). A remote host's dataset binding is checked below (XDASH_PLAN.md
        # §5, X4) — `path` (unchecked, matching the local case) is the
        # default, so an already-staged SSH box needs no configuration at all.
        if not self.host.is_local and not self.host.declares_repo_root:
            return {
                "code": "host-not-configured",
                "detail": f"Host '{self.host.id}' has no repo_root configured for profile '{settings.profile_name}'",
            }
        if not _cached_available(self.host.id, self._transport):
            return {"code": "host-unreachable", "detail": f"Host '{self.host.id}' is not reachable"}
        if not self.host.is_local:
            data = datasets.data_mode_for_experiment(experiment["config_path"], self.id, self.kind)
            if not data.get("mode"):
                return {"code": data.get("code", "no-dataset-binding"), "detail": data.get("detail", "")}
        if self._free_slots() <= 0:
            return {"code": "pool-busy", "detail": "Nothing free this tick"}
        return None

    def dispatch(self, experiment: Dict[str, Any], attempt: Dict[str, Any]) -> Dict[str, Any]:
        """Pushes the working tree (a no-op on local — LocalTransport.push()
        does nothing, so local dispatch is byte-identical to before this
        class existed), then queues both halves through scheduler.py exactly
        as the pre-Phase-3 LocalRunner did — that queue is what actually
        enforces per-host max_concurrent.

        The two halves get their own arguments (XDASH_PLAN.md X12):
        framework.stage_args() puts the seed flags on both and each stage's
        own extra args on that stage only, and a resumed leg (attempt.
        resume_of set) adds --resume to the *train* half only — eval.py
        rejects any flag it doesn't define.

        Both halves get the same `--config` (§4.3): the experiment's overlay
        file when it has one. It is written into the local repo (where
        locate_run already read it), then pushed to a remote host's repo as
        its own step, so it gets there even if the tree push above ever
        excludes dotdirs.

        The actual command lines come from framework.render_command()
        (XDASH_PLAN.md §4.1) — the profile's `commands.train`/`commands.eval`
        templates, rendered with this host's own interpreter, so a local, an
        SSH and a Colab dispatch of the same attempt run the identical
        command line modulo *python*. `extra_args`/`train_extra_args` are
        still passed to scheduler.add_item alongside the rendered command
        (display/restart bookkeeping only — _start_session prefers
        full_command when it's set).

        Dataset placement (XDASH_PLAN.md §5, X4) runs first, for a remote
        host only: whatever `can_accept()` resolved gets *executed* now —
        path is symlinked, push rsyncs a local copy up, fetch downloads it
        on the runtime — over this runner's own Transport, so it's testable
        with a FakeTransport. A binding that stopped resolving between
        can_accept() and here (or a push/fetch that fails outright) blocks
        the dispatch rather than launching onto missing data."""
        if not self.host.is_local:
            try:
                datasets.place_dataset(
                    experiment["config_path"], self.id, self.kind, self._transport, self.host.repo_root,
                    local_repo_root=settings.repo_root, data_account_creds=datasets.data_account_creds(),
                )
            except datasets.PlacementError as e:
                raise RunnerBlocked({"code": e.code, "detail": e.detail})
            except transport_mod.TransportError as e:
                raise RunnerBlocked({"code": "no-dataset-binding", "detail": "Placement failed: %s" % e})
        self._transport.push(settings.repo_root, self.host.repo_root, transport_mod.DEFAULT_PUSH_EXCLUDES)
        overlay = framework.write_overlay(experiment)
        if overlay and not self.host.is_local:
            self._transport.push(settings.repo_root / framework.XDASH_DIR, self.host.repo_root / framework.XDASH_DIR)
        cli_config = framework.cli_config(experiment)
        train_extra, eval_extra = framework.extra_only(experiment)
        resume = bool(attempt.get("resume_of"))
        train_cmd = framework.render_command(
            cli_config, experiment.get("seed"), self.host.python_executable, "train", resume=resume, extra=train_extra,
        )
        eval_cmd = framework.render_command(
            cli_config, experiment.get("seed"), self.host.python_executable, "eval", extra=eval_extra,
        )
        train_args, eval_args = framework.stage_args(experiment)  # legacy display/restart fields only
        if resume:
            train_args = (train_args + " --resume").strip()
        items = scheduler.add_item(
            experiment["config_path"], "both", eval_args, host_id=self.host.id,
            train_extra_args=train_args, cli_config=overlay,
            train_full_command=train_cmd, eval_full_command=eval_cmd,
        )
        return {
            "unit_ref": {"train_item_id": items[0]["id"], "eval_item_id": items[1]["id"]},
            "stages": [{"name": "train", "status": "pending"}, {"name": "eval", "status": "pending"}],
        }

    def poll(self, attempt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Finished once the eval half's scheduler item is terminal — which
        includes `skipped`, what scheduler._tick() makes of the eval half when
        train failed. Succeeded only when eval completed. An eval item that no
        longer exists (removed from the Scheduler tab under a running
        attempt) is finished-and-failed: nothing is left to wait for.

        Reads the scheduler store directly (scheduler.items_by_id()), never
        tmux: this runs for every in-flight attempt on every view and tick."""
        unit_ref = attempt.get("unit_ref") or {}
        if "eval_item_id" not in unit_ref:
            return None
        items = scheduler.items_by_id()
        train = items.get(unit_ref.get("train_item_id") or "")
        evalu = items.get(unit_ref.get("eval_item_id") or "")
        stages = [
            {"name": "train", "status": (train or {}).get("status", "unknown")},
            {"name": "eval", "status": (evalu or {}).get("status", "unknown")},
        ]
        if evalu is None:
            return {"raw_status": "unit-missing", "stages": stages, "finished": True, "succeeded": False}
        status = evalu["status"]
        finished = status in _ITEM_TERMINAL
        return {
            "raw_status": status, "stages": stages,
            "finished": finished, "succeeded": (status == "completed") if finished else None,
        }

    def _persist_logs(self, attempt: Dict[str, Any]) -> None:
        """Each half's tmux pane -> data/<profile>/logs/<attempt_id>/{train,
        eval}.log (XDASH_PLAN.md §3.5), so killing a session — or a Colab VM
        going away — never loses a run's console output. Best-effort per
        stage: a pane already gone falls back to terminals' own kill-time
        snapshot, and a stage with neither is simply skipped."""
        unit_ref = attempt.get("unit_ref") or {}
        items = scheduler.items_by_id()
        log_dir = settings.attempt_log_dir(attempt["attempt_id"])
        for stage, key in (("train", "train_item_id"), ("eval", "eval_item_id")):
            session = (items.get(unit_ref.get(key) or "") or {}).get("session_name")
            if not session:
                continue
            try:
                text = (terminals.get_terminal(session, include_log=True) or {}).get("log_text")
            except Exception:
                text = None
            if text:
                atomic_write_text(log_dir / ("%s.log" % stage), text)

    def collect(self, attempt: Dict[str, Any]) -> Optional[str]:
        """Persists both panes' logs, then — remote hosts only — pulls
        exactly the attempt's planned run dir (`attempt.run.run_dir`, from
        the framework's locate_run hook at dispatch; X2), its K-Fold split
        file, and the ledger into
        outputs/remote/<host>/<attempt_id>/, a re-rooted slice of the repo
        (same relative paths under it). The dispatcher then canonicalizes
        that into the local tree (results_ingest.canonicalize).

        Local returns None: train.py/eval.py already wrote straight into the
        canonical tree and ledger. A remote host always returns its staging
        dir, even empty — None would read as "already canonical".
        Raises transport.TransportError when the host can't be reached, so
        the dispatcher retries instead of recording a real run as empty."""
        self._persist_logs(attempt)
        if self.host.is_local:
            return None

        staging = settings.repo_root / "outputs" / "remote" / self.host.id / attempt["attempt_id"]
        staging.mkdir(parents=True, exist_ok=True)
        remote_root = self.host.repo_root
        run = attempt.get("run") or {}
        run_dir = run.get("run_dir")
        if run_dir:
            rel = Path(run_dir)
            if self._transport.exists(remote_root / rel, "d"):
                self._transport.pull(remote_root / rel, staging / rel)
            if run.get("config_hash"):
                splits = rel.parent / ("%s-fold_splits.json" % str(run["config_hash"])[:7])
                if self._transport.exists(remote_root / splits, "f"):
                    self._transport.pull_file(remote_root / splits, staging / splits)
        elif settings.manifest_layout != "experiments":
            # No plan (no locate_run hook): the legacy layout keys manifests by
            # run_id, so the whole runs/ tree comes back for plan-less
            # registration. The experiments layout has no plan-less answer —
            # without a planned run dir there is nothing specific to pull.
            runs_rel = _rel(settings.artifacts_dir) / "runs"
            if self._transport.exists(remote_root / runs_rel, "d"):
                self._transport.pull(remote_root / runs_rel, staging / runs_rel)
        ledger_rel = _rel(settings.ledger_dir)
        if self._transport.exists(remote_root / ledger_rel, "d"):
            self._transport.pull(remote_root / ledger_rel, staging / ledger_rel)
        return str(staging)

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
