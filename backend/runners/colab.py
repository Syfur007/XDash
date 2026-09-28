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

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .. import colab
from .. import hosts
from .. import tools
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


def _host_record(account_name: str, proxy_command: str = "", identity_file: str = "") -> Dict[str, Any]:
    ssh: Dict[str, Any] = {
        # Only an alias (ControlPath key, log label): ProxyCommand does the
        # actual connecting. `root` is the only user on a Colab VM.
        "host": _session_name(account_name), "user": "root", "proxy_command": proxy_command,
        # Every `colab new` is a fresh VM with a fresh host key behind the
        # same alias — see SshTransport._opts().
        "options": ["StrictHostKeyChecking=no", "UserKnownHostsFile=/dev/null"],
    }
    if identity_file:
        # The same key `colab ssh -i` announced to the VM (X5).
        ssh["identity_file"] = identity_file
    return {
        "id": _host_id_for(account_name), "kind": "colab",
        "label": "Colab · %s" % account_name,
        "max_concurrent": 1,  # one ssh connection at a time — Colab-constraints table, not configurable
        "repos": {settings.profile_name: {"repo_root": _REMOTE_REPO_ROOT}},
        "ssh": ssh,
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


# Usable memory of the GPUs `colab --gpu` can request (colab-cli-reference.md
# lists T4 L4 G4 H100 A100). G4 is left unknown rather than guessed.
_COLAB_VRAM_GB = {"T4": 15.0, "L4": 22.5, "A100": 40.0, "H100": 80.0}


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
        cli_status = tools.status("colab")
        return CapacitySnapshot(
            unit="slots", used=(1 if self._busy() else 0), limit=1,
            extra={
                "volatile": True, "provisioned": live,
                # XDASH_FIXES_PLAN.md F2 — read by runtimes._health()'s new
                # "colab" branch.
                "cli_ok": cli_status.ok, "cli_detail": cli_status.error,
            },
        )

    def accelerator(self) -> Optional[Dict[str, Any]]:
        """What this account asks Colab for (its `gpu`), with that GPU's
        usable memory. Colab may grant less than asked — dispatch records
        what it actually got on unit_ref.accelerator."""
        account = colab.find_account(self.account_name) or {}
        gpu = str(account.get("gpu") or settings.colab_default_gpu or "").upper()
        vram = _COLAB_VRAM_GB.get(gpu)
        return {"name": gpu or "unknown", "vram_gb": vram, "source": "account gpu (as requested)"} if gpu else None

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

        # DATASETS_PLAN.md §4.2: Colab has no data of its own — every dispatch
        # must resolve a plan (download on the VM from Kaggle when a data
        # account is set, else push the local copy).
        from .. import datasets
        plan = datasets.plan_delivery_for_config(experiment["config_path"], self.id, self.kind)
        if plan.get("state") == "blocked":
            return {"code": plan.get("code") or "dataset-unavailable", "detail": plan.get("detail", "")}

        if not colab.colab_available():
            return {"code": "host-unreachable", "detail": f"'{tools.path('colab')}' is not on PATH"}
        if not colab.has_credentials(self.account_name, account):
            return {
                "code": "colab-not-connected",
                "detail": "This account has no CLI login yet. Run once: %s" % colab.connect_command(self.account_name),
            }
        try:
            colab.validate_gpu(account.get("gpu") or settings.colab_default_gpu)
            colab.ssh_key_for(self.account_name)
        except colab.ColabOpsError as e:
            return {"code": "colab-misconfigured", "detail": str(e)}
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
        self.host = hosts.upsert_host(_host_record(self.account_name, result["proxy_command"], result.get("identity_file", "")))
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
        treated as immediately reapable rather than kept forever.

        **Hard guard (XDASH_PLAN.md §6.6, X3):** never while any attempt on
        this slot is uncollected — in flight, or finished with its outputs
        still only on the VM (`collect.state == "pending"`). `colab stop`
        deletes the VM's disk; before this guard, it deleted every Colab
        run's outputs, because nothing ever collected them first. A VM held
        by a collection that keeps failing stays up (and is notified about);
        the manual Stop button (/api/colab/accounts/<n>/stop) is the
        deliberate way out."""
        from .. import experiments
        if experiments.slot_has_uncollected(self.id):
            return
        host_id = _host_id_for(self.account_name)
        if not hosts.host_exists(host_id):
            return

        timestamps = []
        for a in experiments.attempts_for_slot(self.id):
            stamp = a.get("ended_at") or a.get("started_at")
            if not stamp:
                continue
            try:
                parsed = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            # Attempt timestamps are UTC-aware (experiments._now_iso()); the
            # previous naive datetime.now() subtraction raised TypeError,
            # which the poll loop swallowed — no VM was ever reaped.
            timestamps.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc))
        if timestamps:
            idle_minutes = (datetime.now(timezone.utc) - max(timestamps)).total_seconds() / 60.0
        else:
            idle_minutes = float("inf")

        if idle_minutes < settings.colab_idle_grace_minutes:
            return
        if colab.stop_session(self.account_name):
            hosts.remove_host(host_id)


def list_colab_runners() -> List[ColabRunner]:
    return [ColabRunner(a["name"]) for a in colab.list_accounts()]
