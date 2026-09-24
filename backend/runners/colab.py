"""ColabRunner — one instance per configured Colab account (Multi_runner_
XDash.md Phase 4). Subclasses MachineRunner, adding only what MachineRunner
itself can't know: a Colab account has no persistent machine until
dispatch() provisions one. Once a VM is up, it IS an ordinary SSH host —
dispatch/poll/collect/cancel/seed/heartbeat are all inherited from
MachineRunner completely unchanged; the only code here is provisioning,
Colab-specific eligibility checks, and reclaiming an idle VM.

**The provisioning trick:** when dispatch() provisions a VM, it registers a
REAL (if short-lived) host record via `hosts.upsert_host()` — not an
in-memory-only object. This is what keeps everything downstream working
unmodified: `transport.for_host(host_id)` is called fresh on every single
tmux operation (stop/kill/poll, possibly from a ColabRunner instance
constructed ticks later), and it can only resolve a host it can actually
look up. "Generated rather than typed" (Multi_runner_XDash.md's own phrase)
describes how a human never fills this host in on a form — it doesn't mean
unpersisted. Torn down the same way: reap_idle() calls hosts.remove_host()
right after `colab stop`.

Presentation (list_units/capacity) is Attempt-backed like KaggleRunner, not
inherited from MachineRunner's tmux-probing versions — MachineRunner assumes
a real, already-resolvable host, which is false for an idle (no-VM) account,
and probing a VM that may not exist on every ordinary `/api/runners` GET
would make an idle Colab fleet slow to even list.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from .. import colab
from .. import hosts
from .. import transport as transport_mod
from ..config import settings
from .base import CapacitySnapshot, LaunchSpec, RunnerCapabilities, RunnerCapabilityError, RunUnit
from .machine import MachineRunner
from .registry import slot_id

RUNNER_KIND = "colab"

# Same VM naming convention as backend/colab.py's own _session_name() —
# duplicated as a one-line format rather than importing a private helper
# across the module boundary.
_REMOTE_REPO_ROOT = "~/xdash-repo"


def _session_name(account_name: str) -> str:
    return "xdash-%s" % account_name


def _host_id_for(account_name: str) -> str:
    # Distinct namespace from the runner's own slot id ("colab:<account>",
    # via registry.slot_id() below) — hosts.json is a flat, id-keyed
    # registry shared with human-typed SSH hosts, so this must never collide
    # with something a person might type there.
    return "colab-%s" % account_name


def _host_record(account_name: str, proxy_command: str = "") -> Dict[str, Any]:
    return {
        "id": _host_id_for(account_name), "kind": "colab",
        "label": "Colab · %s" % account_name,
        "max_concurrent": 1,  # one ssh connection at a time — Colab-constraints table, not configurable
        "repos": {settings.profile_name: {"repo_root": _REMOTE_REPO_ROOT}},
        "ssh": {"host": _session_name(account_name), "proxy_command": proxy_command},
    }


def _to_unit(account_name: str, runner_id: str, view: Dict[str, Any]) -> RunUnit:
    attempt = view.get("current_attempt") or {}
    unit_ref = attempt.get("unit_ref") or {}
    return RunUnit(
        unit_id=attempt.get("attempt_id") or view["experiment_id"],
        runner_id=runner_id,
        label=view["experiment_id"],
        status=attempt.get("status") or "unknown",
        raw_status=attempt.get("raw_status") or attempt.get("status") or "unknown",
        config_path=view.get("config_path"),
        mode=None,
        extra={
            "experiment_id": view["experiment_id"],
            "account": account_name,
            "accelerator": unit_ref.get("accelerator"),
            "started_at": attempt.get("started_at"),
            "stages": attempt.get("stages") or [],
            "blocked": attempt.get("blocked"),
        },
    )


class ColabRunner(MachineRunner):
    capabilities = RunnerCapabilities(
        # No direct_launch: same reasoning as Kaggle — provisioning needs the
        # full can_accept()/dispatch() pipeline, not a bare one-off launch.
        direct_launch=False, live_log=True, stop=True, kill=True, restart=True, queue=True,
        live_checkpoints=True,   # true once a VM is up — a real shell, unlike Kaggle
        volatile=True,           # the VM disappears if XDash stops mid-run and isn't reattached in time
        provisioned=True,        # capacity must be created (colab new) before use, not just claimed
    )

    def __init__(self, account_name: str):
        self.account_name = account_name
        host_id = _host_id_for(account_name)
        existing = next((h for h in hosts.list_hosts() if h.id == host_id), None)
        host = existing or hosts._Host(_host_record(account_name))
        super().__init__(host)
        # Decoupled from host.id's own namespacing (see _host_id_for's
        # comment) — the runner's public slot id is the clean "colab:<name>"
        # regardless of what the underlying hosts.json entry is called.
        self.id = slot_id(RUNNER_KIND, account_name)
        self.label = "Colab · %s" % account_name

    def _busy(self) -> bool:
        from .. import experiments
        return experiments.is_slot_busy(self.id)

    def _session_cap_hours(self) -> float:
        limit = colab.session_limit_hours(self.account_name)
        return max(0.1, limit - settings.colab_setup_reserve_hours - settings.colab_teardown_reserve_hours)

    # ------------------------------------------------------------ presentation
    def list_units(self) -> List[RunUnit]:
        from .. import experiments
        return [_to_unit(self.account_name, self.id, v) for v in experiments.list_experiments(slot=self.id)]

    def launch(self, spec: LaunchSpec) -> RunUnit:
        raise RunnerCapabilityError(
            "Colab is not launched directly — create an experiment (POST /api/experiments) and "
            "the dispatcher will provision this account's VM and claim its slot."
        )

    def capacity(self) -> CapacitySnapshot:
        live = bool(colab.colab_available() and colab.list_sessions(self.account_name))
        return CapacitySnapshot(
            unit="slots", used=(1 if self._busy() else 0), limit=1,
            extra={"volatile": True, "provisioned": live},
        )

    # ---------------------------------------------------------------- dispatch
    def can_accept(self, experiment: Dict[str, Any], est_hours: float) -> Optional[Dict[str, Any]]:
        """Colab-specific gates only — deliberately does NOT delegate to
        MachineRunner.can_accept(), which assumes a resolvable host/transport
        that an idle (no-VM) account doesn't have yet. Mirrors
        KaggleRunner.can_accept()'s shape (account -> dataset -> busy ->
        session cap), swapping "account has quota" for "account has a
        session cap", since Colab is a one-VM-per-account resource exactly
        like Kaggle is a one-kernel-per-account one — not an open queue like
        a lab SSH box."""
        account = colab.find_account(self.account_name)
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

        if not colab.colab_available():
            return {"code": "host-unreachable", "detail": f"'{settings.colab_executable}' is not on PATH"}
        if self._busy():
            return {"code": "pool-busy", "detail": "This account's one Colab slot is in use"}

        limit = self._session_cap_hours()
        if est_hours > limit:
            return {
                "code": "exceeds-session-cap",
                "detail": f"Estimated {est_hours:.1f}h exceeds this account's session cap ({limit:.1f}h)",
            }
        return None

    def dispatch(self, experiment: Dict[str, Any], attempt: Dict[str, Any]) -> Dict[str, Any]:
        """Provisions (or reuses a still-warm) VM first, registers it as a
        real host record so every later tmux/rsync op can resolve it by id
        (see module docstring), then delegates to MachineRunner.dispatch()
        completely unchanged for the actual push+launch."""
        account = colab.find_account(self.account_name) or {}
        result = colab.ensure_session(self.account_name, gpu=account.get("gpu", ""))
        self.host = hosts.upsert_host(_host_record(self.account_name, result["proxy_command"]))
        self._transport = transport_mod.for_host_record(self.host)

        patch = super().dispatch(experiment, attempt)
        patch["unit_ref"]["account"] = self.account_name
        patch["unit_ref"]["accelerator"] = result.get("accelerator")
        return patch

    # ---------------------------------------------------------------- teardown
    def reap_idle(self) -> None:
        """`colab stop` + deregister the host record once nothing has used
        this slot for colab_idle_grace_minutes and nothing is in flight on
        it now. A slot with no Attempt history at all (host record exists
        but was never dispatched through, e.g. a crash mid-provision) is
        treated as immediately reapable rather than kept forever."""
        if self._busy():
            return
        host_id = _host_id_for(self.account_name)
        if not hosts.host_exists(host_id):
            return

        from .. import experiments
        timestamps = [
            a.get("ended_at") or a.get("started_at")
            for a in experiments.attempts_for_slot(self.id)
            if a.get("ended_at") or a.get("started_at")
        ]
        if timestamps:
            try:
                idle_minutes = (datetime.now() - datetime.fromisoformat(max(timestamps))).total_seconds() / 60.0
            except ValueError:
                idle_minutes = float("inf")
        else:
            idle_minutes = float("inf")

        if idle_minutes < settings.colab_idle_grace_minutes:
            return
        if colab.stop_session(self.account_name):
            hosts.remove_host(host_id)


def list_colab_runners() -> List[ColabRunner]:
    return [ColabRunner(a["name"]) for a in colab.list_accounts()]
