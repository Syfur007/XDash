"""The unified runtime shape (XDASH_PLAN.md §3.6, closes X10): one row per
registered runner — local, every SSH host, every Colab account, every Kaggle
account — instead of `/api/runners` (kind-specific `as_dict()`, no capacity
semantics) and `/api/slots` (only local + Kaggle, §3.2's old vocabulary).

`GET /api/runtimes` serves this to new UI (Phase 3+); `/api/runners` and
`/api/slots` stay as thin aliases (server.py) for the callers that still use
them (static/js/views/compute.js, spine.js's Run Composer capacity estimate) —
see XDASH_PROGRESS.md's Phase 2 section for which.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .runners import registry
from .runners.base import OCCUPYING_STATUSES, Runner


def _quota(runner: Runner, capacity) -> Optional[Dict[str, Any]]:
    """`{used, limit, unit, resets_at, source}` for a budget-metered runtime
    (Kaggle's weekly quota so far — Colab's `colab usage` CU balance is a
    later phase). XDASH_FIXES_PLAN.md F2.6: measured (Kaggle's own `kaggle
    quota`, cached 15 minutes per account by backend/kaggle.py) wins when
    available; the self-tracked estimate is the fallback, with the reason
    carried in `source` either way. `weekly_budget_hours` narrows a measured
    limit (an optional cap below Kaggle's real one) but never widens it."""
    if not runner.capabilities.budget_metered:
        return None
    extra = capacity.extra or {}
    measured = extra.get("measured_quota") or {}
    weekly_cap = extra.get("weekly_budget_hours")
    if measured.get("available"):
        limit = measured["limit"]
        if weekly_cap is not None:
            limit = min(limit, float(weekly_cap))
        return {
            "used": measured["used"], "limit": limit, "unit": measured.get("unit", "h/week"),
            "resets_at": measured.get("resets_at"), "source": "measured",
        }
    detail = measured.get("detail")
    return {
        "used": extra.get("hours_this_week"),
        "limit": weekly_cap,
        "unit": "h/week",
        "resets_at": extra.get("clears_at"),
        "source": "self-tracked" + (" (%s)" % detail if detail else ""),
    }


def _health(runner: Runner, capacity) -> Dict[str, Any]:
    """XDASH_FIXES_PLAN.md F0.3 — real health instead of the stub `{last_ok:
    None, error: None}` every runtime used to report regardless of whether
    it could actually take an attempt. Reads only capacity().extra (already
    computed, cheap and cached per kind — MachineRunner.capacity()'s
    `reachable` reuses can_accept()'s own 30s-TTL transport cache;
    KaggleRunner.capacity()'s `code_pinnable` reuses framework.code_state()'s
    own cache), so this costs nothing extra beyond what /api/runtimes' 5s
    poll already pays for calling capacity() at all.

    CLI missing/too old (Kaggle/Colab, F2) and env check failed (SSH/local,
    F1.5's "Verify environment") plug in the same way F0.3 anticipated:
    another `extra` key this function reads, no change to callers of
    _health()/_state()."""
    extra = capacity.extra or {}
    if runner.kind == "kaggle":
        if extra.get("account_missing"):
            return {"last_ok": None, "error": "Account not found"}
        if extra.get("has_credentials") is False:
            return {"last_ok": None, "error": "No credentials stored for this account"}
        # XDASH_FIXES_PLAN.md F2 — KaggleRunner.capacity()'s cli_ok/cli_detail
        # come from backend/tools.py's cached registry (existence + version
        # gate, >= 2.2.1), never a subprocess call from here.
        if extra.get("cli_ok") is False:
            return {"last_ok": None, "error": "Kaggle CLI: %s" % (extra.get("cli_detail") or "not found")}
        if extra.get("code_pinnable") is False:
            return {"last_ok": None, "error": "Code isn't pinnable to a pushed commit (code-not-pushed)"}
    elif runner.kind == "colab":
        if extra.get("cli_ok") is False:
            return {"last_ok": None, "error": "Colab CLI: %s" % (extra.get("cli_detail") or "not found")}
    elif runner.kind in ("local", "ssh"):
        if extra.get("repo_root_configured") is False:
            return {"last_ok": None, "error": "No repo_root configured for this profile"}
        if extra.get("reachable") is False:
            return {"last_ok": None, "error": "Host is not reachable"}
        # XDASH_FIXES_PLAN.md F1.5 — the seam F0.3 left open, now filled:
        # MachineRunner.capacity()'s env_check_failed/env_check_detail come
        # from envcheck.cached_result() (read-only, never triggers a check
        # from here).
        if extra.get("env_check_failed"):
            detail = extra.get("env_check_detail")
            return {"last_ok": None, "error": "Environment check failed%s" % (": %s" % detail if detail else "")}
        # XDASH_FIXES_PLAN.md F2 — the local (tmux) or remote (ssh/rsync,
        # this machine's own copies, not anything checked on the remote
        # host) CLI a MachineRunner needs to operate at all.
        if extra.get("tools_ok") is False:
            return {"last_ok": None, "error": "Missing required tool: %s" % extra.get("tools_detail")}
    return {"last_ok": None, "error": None}


def _state(runner: Runner, capacity, health: Dict[str, Any]) -> str:
    extra = capacity.extra or {}
    # XDASH_FIXES_PLAN.md F2 — a real health problem (e.g. a missing/too-old
    # 'colab' CLI) outranks "unconfigured" (no VM provisioned yet): the tool
    # being broken is the more actionable, more fundamental fact, and it's
    # also what the Lab tile's "needs attention: <reason>" text is gated on
    # (labRuntimeAttentionText() only fires for state === "attention").
    # Checked before the provisioned gate below, which used to hide it.
    if health.get("error"):
        return "attention"
    if runner.capabilities.provisioned and not extra.get("provisioned"):
        return "unconfigured"
    if extra.get("tmux_available") is False:
        return "offline"
    if capacity.limit and capacity.used >= capacity.limit:
        return "busy"
    return "idle" if capacity.used == 0 else "online"


def _running_labels(runner: Runner) -> List[str]:
    """Units actually occupying a slot right now (XDASH_FIXES_PLAN.md F0.6,
    issue 9) — OCCUPYING_STATUSES, not ACTIVE_STATUSES: `interrupted` belongs
    on an attention view, not a "running: …" one."""
    try:
        units = runner.list_units()
    except Exception:  # noqa: BLE001 — a listing hiccup must not break the whole board
        return []
    return [u.label for u in units if u.status in OCCUPYING_STATUSES]


def _host_id_for_tools(runner: Runner) -> Optional[str]:
    """Which backend/hosts.py record (if any) this runtime maps to —
    XDASH_FIXES_PLAN.md F3's Tools tab keys its per-host routes
    (/api/hosts/<host_id>/tools/...) on exactly this. Local and SSH runtimes
    already ARE their own host record id, but a live Colab account's host
    record id ("colab-<name>", backend/runners/colab.py's own
    _host_id_for()) is a different string from its runtime/slot id
    ("colab:<name>") — MachineRunner (which ColabRunner subclasses) already
    carries the real host object as `.host`, read here rather than
    re-deriving the string from the runtime id. Kaggle has no host record
    (no shell) -> None, which is exactly "no Tools tab for this kind"."""
    host = getattr(runner, "host", None)
    return host.id if host is not None else None


def runtime_view(runner: Runner) -> Dict[str, Any]:
    capacity = runner.capacity()
    health = _health(runner, capacity)
    return {
        "id": runner.id,
        "kind": runner.kind,
        "label": runner.label,
        "state": _state(runner, capacity, health),
        "accelerator": runner.accelerator(),
        "capacity": {"used": capacity.used, "limit": capacity.limit},
        "quota": _quota(runner, capacity),
        "capabilities": vars(runner.capabilities),
        "running": _running_labels(runner),
        "queued_for": [],  # Phase 3+ (needs the dispatcher's own per-runtime queue projection)
        "health": health,
        "host_id": _host_id_for_tools(runner),
    }


def list_runtimes() -> List[Dict[str, Any]]:
    # Module-qualified (not `from .runners.registry import list_runners`),
    # so the test harness's `use_runners()` monkeypatch of
    # `registry.list_runners` is seen here too, not just by the original
    # binding's other callers.
    return [runtime_view(r) for r in registry.list_runners()]


# --------------------------------------------------------------------------- Add-runtime wizard (§8.4, §10 Phase 5)
# "a live test that must pass before the runtime saves as `online`" — one
# per-kind check against fields the wizard is *about* to save, run before
# anything is persisted. Kept here (not in hosts.py/kaggle.py/colab.py
# individually) so the frontend has exactly one endpoint regardless of kind,
# the same "one action endpoint" shape as /api/experiments/actions.
def test_runtime(kind: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    fields = fields or {}
    if kind == "local":
        # Always present, never configured through this wizard — trivially ok.
        return {"ok": True, "detail": "This machine is always available."}
    if kind == "ssh":
        from . import transport as transport_mod
        return transport_mod.test_ssh_connection(fields.get("ssh") or fields)
    if kind == "kaggle":
        from . import kaggle as kaggle_mod
        try:
            return kaggle_mod.test_credentials(
                fields.get("username", ""), fields.get("key", ""), fields.get("api_token", ""),
            )
        except kaggle_mod.KaggleOpsError as e:
            return {"ok": False, "detail": str(e)}
    if kind == "colab":
        from . import colab as colab_mod
        try:
            return colab_mod.test_config(fields.get("gpu", ""))
        except colab_mod.ColabOpsError as e:
            return {"ok": False, "detail": str(e)}
    return {"ok": False, "detail": f"Unknown runtime kind '{kind}'"}
