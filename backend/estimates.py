"""Duration estimates for a not-yet-run config — EXPERIMENT_AUTOMATION_PLAN.md
§4.1's est_hours(row), which the greedy batch dispatcher needs to order
pending rows (longest-first within a feasibility tier) and to size a
Kaggle push's quota reservation (§2.3's in-flight reservation) before the
run has produced any measurement of its own.

Three tiers, tried in order, cheapest and most-trusted first — the same
"degrade honestly" principle scheduler.py's total_epochs() already states:
never guess silently, and always say which tier produced a number, so a
schedule built on a tier-3 default doesn't render as confidently as one
built on measured hours (see §8's own non-goal about this: the greedy is
only as good as this estimate).
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

from . import bridge
from . import configs as cfg
from . import ledger
from . import scheduler
from .config import settings

TIER_MEASURED = 1     # median gpu_hours of prior runs of this exact experiment
TIER_EPOCH_RATE = 2   # this config's own epoch count x a measured per-epoch rate from the same model+dataset
TIER_DEFAULT = 3      # profile-level static fallback — no history to draw on at all


def _prior_runs_for_experiment(experiment_name: str) -> List[Dict[str, Any]]:
    """Every run whose manifest's own resolved_config.logging.experiment_name
    matches — read off the manifest directly (present on every manifest
    regardless of manifest_layout, since RunManifest always embeds it, per
    orchestration/manifest.py), not the ledger CSV join, so this also
    catches a run that has a manifest.json but wasn't ledger-registered
    (e.g. still mid-flight, or interrupted before §8.2's leg-chaining
    registers it). config_hash is deliberately not used here: computing
    what hash a not-yet-run config *would* get requires the host repo's own
    hashing logic, which the dashboard has never reimplemented (see
    backend/configs.py's find_config_by_experiment_name() docstring, which
    hits the same wall) — experiment_name is available without that."""
    matches = []
    for run in ledger.list_runs():
        resolved = run.get("resolved_config") or {}
        name = (resolved.get("logging") or {}).get("experiment_name")
        gpu_hours = run.get("gpu_hours")
        if name and name == experiment_name and gpu_hours:
            matches.append(run)
    return matches


def _prior_runs_for_model_dataset(model_name: str, dataset_name: str) -> List[Dict[str, Any]]:
    """Every prior run sharing the same composed model.name/dataset.name —
    a broader match than _prior_runs_for_experiment(), used only once tier 1
    has come up empty (a genuinely new experiment, but not necessarily a new
    model/dataset pairing)."""
    matches = []
    for run in ledger.list_runs():
        resolved = run.get("resolved_config") or {}
        m = (resolved.get("model") or {}).get("name")
        d = (resolved.get("dataset") or {}).get("name")
        epochs = (resolved.get("training") or {}).get("epochs")
        gpu_hours = run.get("gpu_hours")
        if m == model_name and d == dataset_name and gpu_hours and epochs:
            matches.append({"gpu_hours": float(gpu_hours), "epochs": int(epochs)})
    return matches


def est_hours(config_path: str) -> Dict[str, Any]:
    """{"hours": float, "tier": 1|2|3, "source": str} — see module docstring
    for what each tier means. Never raises: a resolve failure, missing
    history, or bridge unavailability all just fall through to a cruder
    tier rather than blocking estimation entirely."""
    experiment_name = cfg.get_experiment_name(config_path)

    prior = _prior_runs_for_experiment(experiment_name)
    if prior:
        hours = statistics.median(float(r["gpu_hours"]) for r in prior)
        return {
            "hours": round(hours, 2), "tier": TIER_MEASURED,
            "source": f"median of {len(prior)} prior run(s) of '{experiment_name}'",
        }

    target_epochs = scheduler.total_epochs(config_path)
    if target_epochs:
        # Needs the *composed* config — model.name/dataset.name are never inlined in a
        # compose-based configs/*.yaml (they come from configs/model|dataset/*.yaml
        # fragments merged in), so this tier costs one bridge subprocess call
        # (cached 60s by bridge.run_bridge_script itself). Any failure here — repo
        # doesn't expose the bridge, config doesn't validate, whatever — degrades to
        # tier 3 rather than raising. Caught broadly, not just
        # bridge.BridgeError/BridgeUnavailable (that pair is the *documented* contract,
        # but this function feeds an unattended dispatch tick — a duration estimate
        # quietly degrading to a default beats an uncaught exception from host-repo
        # code taking down the whole tick).
        resolved = None
        try:
            repo_rel = cfg.repo_relative_path(config_path)
            result = bridge.run_bridge_script("resolve_config.py", [repo_rel])
            if isinstance(result, dict) and result.get("valid"):
                resolved = result.get("resolved") or {}
        except Exception:
            resolved = None

        if resolved:
            model_name = (resolved.get("model") or {}).get("name")
            dataset_name = (resolved.get("dataset") or {}).get("name")
            if model_name and dataset_name:
                same_md = _prior_runs_for_model_dataset(model_name, dataset_name)
                rates = [r["gpu_hours"] / r["epochs"] for r in same_md if r["epochs"]]
                if rates:
                    per_epoch = statistics.median(rates)
                    hours = per_epoch * target_epochs
                    return {
                        "hours": round(hours, 2), "tier": TIER_EPOCH_RATE,
                        "source": (
                            f"{target_epochs} epochs x {round(per_epoch, 3)}h/epoch "
                            f"(median over {len(rates)} prior {model_name}/{dataset_name} run(s))"
                        ),
                    }

    return {
        "hours": settings.est_hours_default, "tier": TIER_DEFAULT,
        "source": "profile default (no prior runs of this experiment or its model+dataset)",
    }
