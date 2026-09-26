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
from .runners.base import ACTIVE_STATUSES, Runner


def _quota(runner: Runner, capacity) -> Optional[Dict[str, Any]]:
    """`{used, limit, unit, resets_at, source}` for a budget-metered runtime
    (Kaggle's weekly quota so far — Colab's `colab usage` CU balance lands
    with Phase 5's CLI 2.x). None for a plain slot-counted one (local, ssh)."""
    if not runner.capabilities.budget_metered:
        return None
    extra = capacity.extra or {}
    return {
        "used": extra.get("hours_this_week"),
        "limit": extra.get("weekly_budget_hours"),
        "unit": "h/week",
        "resets_at": extra.get("clears_at"),
        "source": "self-tracked",  # no measured Kaggle CLI 2.x quota yet (Phase 5)
    }


def _state(runner: Runner, capacity) -> str:
    extra = capacity.extra or {}
    if runner.capabilities.provisioned and not extra.get("provisioned"):
        return "unconfigured"
    if extra.get("reachable") is False or extra.get("tmux_available") is False:
        return "offline"
    if capacity.limit and capacity.used >= capacity.limit:
        return "busy"
    return "idle" if capacity.used == 0 else "online"


def _running_labels(runner: Runner) -> List[str]:
    try:
        units = runner.list_units()
    except Exception:  # noqa: BLE001 — a listing hiccup must not break the whole board
        return []
    return [u.label for u in units if u.status in ACTIVE_STATUSES]


def runtime_view(runner: Runner) -> Dict[str, Any]:
    capacity = runner.capacity()
    return {
        "id": runner.id,
        "kind": runner.kind,
        "label": runner.label,
        "state": _state(runner, capacity),
        "accelerator": runner.accelerator(),
        "capacity": {"used": capacity.used, "limit": capacity.limit},
        "quota": _quota(runner, capacity),
        "capabilities": vars(runner.capabilities),
        "running": _running_labels(runner),
        "queued_for": [],  # Phase 3+ (needs the dispatcher's own per-runtime queue projection)
        "health": {"last_ok": None, "error": None},
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
