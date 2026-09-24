"""The Experiment/Attempt/Slot object model (XDASH_V2_PLAN.md §3) and its
dispatcher (§4's greedy policy, generalized) — the backend for Phase B's API
(§5): GET/POST /api/experiments, /api/slots, /api/pulse.

**This is the only dispatcher.** The strangler migration of §6.8 is
complete: `backend/batch_runner.py`, `backend/assignments.py` and the
`/api/assignments*` routes are deleted, and the Kaggle *worker* registry
they dispatched to went with them (§3.7). The coexistence window this
module was written inside — two dispatchers racing for one Kaggle slot
without a shared lock — is therefore closed by construction, not by
agreement.

Identity: `experiment_id = f"{experiment_name}-s{seed}"` (no seed ->
lower-cased so a hand-declared `logging.experiment_name` and dissert's own
`outputs/experiments/<experiment_name>-s<seed>/` layout agree — see
backend/configs.py's get_experiment_name(), already used the same way by
scheduler.py and estimates.py.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import configs as cfg
from . import dataset_map
from . import estimates
from . import kaggle as kaggle_backend
from . import ledger
from . import notifications as notif
from . import scheduler
from .config import settings

_lock = threading.RLock()          # guards experiments.json
_dispatch_lock = threading.Lock()  # serializes _dispatch_tick() — see batch_runner.py's twin

PRE_DISPATCH_STATUSES = {"pending", "blocked"}      # not yet claimed by a slot
IN_FLIGHT_STATUSES = {"dispatching", "running"}     # claimed; a unit exists or is being created
TERMINAL_STATUSES = {"done", "failed", "cancelled"}


class ExperimentError(Exception):
    """Expected failure (bad id, bad spec) — routes map this to a 4xx."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- storage
def _load() -> Dict[str, Any]:
    if not settings.experiments_store_file.exists():
        return {"experiments": {}, "attempts": {}, "batches": {}}
    try:
        data = json.loads(settings.experiments_store_file.read_text())
    except Exception:
        return {"experiments": {}, "attempts": {}, "batches": {}}
    data.setdefault("experiments", {})
    data.setdefault("attempts", {})
    data.setdefault("batches", {})
    return data


def _save(data: Dict[str, Any]) -> None:
    settings.experiments_store_file.write_text(json.dumps(data, indent=2))


# --------------------------------------------------------------------------- identity
def experiment_id_for(config_path: str, seed: Optional[Any]) -> str:
    """`{experiment_name}-s{seed}` (XDASH_V2_PLAN.md §3.1) — the same string
    dissert's own OUTPUT_LAYOUT.md uses as an output directory name, so the
    dashboard's primary key and the host repo's on-disk layout need no
    mapping table. No seed -> just the bare experiment_name."""
    name = cfg.get_experiment_name(config_path)
    return f"{name}-s{seed}" if seed not in (None, "") else name


# --------------------------------------------------------------------------- experiments (read)
def _attempts_for(data: Dict[str, Any], experiment: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [data["attempts"][aid] for aid in experiment.get("attempt_ids", []) if aid in data["attempts"]]


def _current_attempt(data: Dict[str, Any], experiment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    aid = experiment.get("current_attempt_id")
    return data["attempts"].get(aid) if aid else None


def _find_run_id_for(experiment_name: str, seed: Optional[Any]) -> Optional[str]:
    """Best-effort join to the host repo's ledger (XDASH_V2_PLAN.md §3.6's
    "Run" — XDash never writes it, only reads it). Matches on the same
    logging.experiment_name field estimates.py already keys off of, plus
    seed when the ledger row carries one; takes the most recent match since
    list_runs() is already sorted newest-first."""
    if not experiment_name:
        return None
    for run in ledger.list_runs():
        row = run.get("ledger") or {}
        resolved = run.get("resolved_config") or {}
        name = row.get("experiment_name") or (resolved.get("logging") or {}).get("experiment_name")
        if name != experiment_name:
            continue
        if seed not in (None, "") and str(row.get("seed") or "") not in ("", str(seed)):
            continue
        return run.get("run_id") or row.get("run_id")
    return None


def _live_local_stage(item_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not item_id:
        return None
    item = next((i for i in scheduler.list_items()["items"] if i["id"] == item_id), None)
    if item is None:
        return None
    return {"item_id": item_id, "status": item["status"]}


def _resolve_attempt_live(attempt: Dict[str, Any]) -> Dict[str, Any]:
    """Attempt as stored, with its `stages`/`raw_status` re-derived from its
    unit_ref when still in flight — mirrors how the old system always
    computed a worker's/item's status fresh rather than trusting a
    continuously-synced mirror (kaggle.list_accounts(), scheduler.list_items()).
    Terminal/pre-dispatch attempts are returned unchanged; there is nothing
    live left to resolve."""
    if attempt["status"] not in IN_FLIGHT_STATUSES:
        return attempt
    unit_ref = attempt.get("unit_ref") or {}
    out = dict(attempt)
    if attempt["slot"] == "local" and "eval_item_id" in unit_ref:
        train = _live_local_stage(unit_ref.get("train_item_id"))
        evalu = _live_local_stage(unit_ref.get("eval_item_id"))
        out["stages"] = [
            {"name": "train", "status": (train or {}).get("status", "unknown")},
            {"name": "eval", "status": (evalu or {}).get("status", "unknown")},
        ]
        out["raw_status"] = (evalu or {}).get("status")
    elif unit_ref.get("account") and unit_ref.get("kernel_slug"):
        try:
            result = kaggle_backend.refresh_experiment_status(unit_ref["account"], unit_ref["kernel_slug"])
            raw = result.get("status")
        except kaggle_backend.KaggleOpsError:
            raw = None
        out["raw_status"] = raw
        out["stages"] = [{"name": "run", "status": raw or "unknown"}]
    return out


def _snapshot_pairs(data: Dict[str, Any]) -> List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
    """(experiment, current_attempt) pairs — pure dict access, safe to call
    under _lock. Live resolution (which may shell out to `kaggle`) must
    happen only *after* the lock is released — see _view_live's docstring."""
    return [(e, _current_attempt(data, e)) for e in data["experiments"].values()]


def _view_live(experiment: Dict[str, Any], current: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolves *current*'s live status/stages (kaggle.py's own `kernels
    status` subprocess can take real wall-clock time) — callers must invoke
    this only after releasing _lock, never while holding it, or a slow
    Kaggle poll stalls every other create/cancel/dispatch call in the
    process."""
    current_live = _resolve_attempt_live(current) if current else None
    return {
        **experiment,
        "status": current_live["status"] if current_live else "pending",
        "current_attempt": current_live,
        "attempt_count": len(experiment.get("attempt_ids", [])),
    }


def list_experiments(
    batch: Optional[str] = None, status: Optional[str] = None,
    config: Optional[str] = None, slot: Optional[str] = None,
) -> List[Dict[str, Any]]:
    with _lock:
        pairs = _snapshot_pairs(_load())
    views = [_view_live(e, current) for e, current in pairs]
    if batch:
        views = [v for v in views if v.get("batch_name") == batch]
    if status:
        views = [v for v in views if v["status"] == status]
    if config:
        views = [v for v in views if v.get("config_path") == config]
    if slot:
        views = [v for v in views if (v.get("current_attempt") or {}).get("slot") == slot]
    return sorted(views, key=lambda v: v.get("created_at") or "", reverse=True)


def get_experiment(experiment_id: str) -> Dict[str, Any]:
    with _lock:
        data = _load()
        experiment = data["experiments"].get(experiment_id)
        if experiment is None:
            raise ExperimentError(f"Unknown experiment '{experiment_id}'")
        raw_attempts = list(_attempts_for(data, experiment))
    attempts = [_resolve_attempt_live(a) for a in raw_attempts]  # outside the lock — see _view_live
    view = {**experiment, "attempts": attempts, "attempt_count": len(attempts)}
    current = attempts[-1] if attempts else None
    view["status"] = current["status"] if current else "pending"
    view["current_attempt"] = current
    if current and current.get("run_id"):
        view["run"] = ledger.get_run(current["run_id"])
    else:
        view["run"] = None
    return view


# --------------------------------------------------------------------------- experiments (write)
def create_experiments(
    configs: List[Dict[str, Any]], extra_args: str = "", pool: str = "either",
    batch_name: Optional[str] = None, max_retries: int = 1, force_on_retry: bool = True,
) -> List[Dict[str, Any]]:
    """`POST /api/experiments` — the single launch verb (§5). *configs* is a
    list of `{"path": str, "seeds": [int|None, ...]}`; every (path, seed)
    pair becomes one Experiment. **Idempotent on identity**: re-posting a
    (config_path, seed) that already exists reuses that Experiment rather
    than creating a duplicate — an Experiment's identity is meant to be the
    thing a researcher means, not one row per click (XDASH_V2_PLAN.md §3.1).
    If its current attempt is pending/blocked, it's left alone (already
    queued); if terminal, a fresh Attempt is queued (the same effect as
    `POST /api/experiments/<id>/retry`); if in flight, it's left alone.
    """
    if pool not in ("either", "local_only", "kaggle_only"):
        raise ExperimentError(f"pool must be 'either', 'local_only' or 'kaggle_only', got {pool!r}")
    if not configs:
        raise ExperimentError("configs must declare at least one entry")

    created_or_touched: List[Dict[str, Any]] = []
    with _lock:
        data = _load()
        if batch_name:
            data["batches"].setdefault(batch_name, {
                "name": batch_name, "pool": pool,
                "max_retries": max_retries, "force_on_retry": force_on_retry,
                "paused": False, "created_at": _now_iso(),
            })

        for entry in configs:
            config_path = (entry.get("path") or "").strip()
            if not config_path:
                raise ExperimentError("Every config entry needs a path")
            try:
                cfg.read_config(config_path)
            except (FileNotFoundError, ValueError) as e:
                raise ExperimentError(f"Config not found: {config_path} ({e})")
            seeds = entry.get("seeds") or [None]
            for seed in seeds:
                eid = experiment_id_for(config_path, seed)
                experiment = data["experiments"].get(eid)
                row_extra_args = extra_args or ""
                if seed is not None and settings.seed_arg:
                    row_extra_args = " ".join(
                        part for part in (settings.seed_arg.format(seed=seed), row_extra_args) if part
                    )
                if experiment is None:
                    experiment = {
                        "experiment_id": eid, "config_path": config_path, "seed": seed,
                        "batch_name": batch_name, "pool": pool, "extra_args": row_extra_args,
                        "max_retries": max_retries, "force_on_retry": force_on_retry,
                        "created_at": _now_iso(), "attempt_ids": [], "current_attempt_id": None,
                    }
                    data["experiments"][eid] = experiment
                current = data["attempts"].get(experiment.get("current_attempt_id") or "")
                if current is None or current["status"] in TERMINAL_STATUSES:
                    _append_attempt(data, experiment)
                created_or_touched.append(experiment)
        _save(data)
    ensure_dispatcher_started()
    _dispatch_tick()
    return [get_experiment(e["experiment_id"]) for e in created_or_touched]


def _new_attempt(experiment_id: str, attempt_index: int) -> Dict[str, Any]:
    return {
        "attempt_id": f"atmpt_{uuid.uuid4().hex[:10]}",
        "experiment_id": experiment_id,
        "attempt_index": attempt_index,
        "slot": None,
        "status": "pending",
        "raw_status": None,
        "stages": [],
        "unit_ref": None,
        "started_at": None,
        "ended_at": None,
        "blocked": None,
        "run_id": None,
        "updated_at": _now_iso(),
    }


def _append_attempt(data: Dict[str, Any], experiment: Dict[str, Any]) -> Dict[str, Any]:
    """Must be called with _lock held and *data*/*experiment* the live
    (not copied) dicts, so the caller's own _save(data) persists it."""
    attempt = _new_attempt(experiment["experiment_id"], len(experiment["attempt_ids"]) + 1)
    data["attempts"][attempt["attempt_id"]] = attempt
    experiment["attempt_ids"].append(attempt["attempt_id"])
    experiment["current_attempt_id"] = attempt["attempt_id"]
    return attempt


def retry_experiment(experiment_id: str) -> Dict[str, Any]:
    """`POST /api/experiments/<id>/retry` — a retry is an Attempt (§3.2),
    never a new Experiment. Refuses while the current attempt is still
    live; the existing one must reach a terminal state first."""
    with _lock:
        data = _load()
        experiment = data["experiments"].get(experiment_id)
        if experiment is None:
            raise ExperimentError(f"Unknown experiment '{experiment_id}'")
        current = data["attempts"].get(experiment.get("current_attempt_id") or "")
        if current is not None and current["status"] not in TERMINAL_STATUSES:
            raise ExperimentError(f"Experiment '{experiment_id}' already has a live attempt")
        _append_attempt(data, experiment)
        _save(data)
    ensure_dispatcher_started()
    _dispatch_tick()
    return get_experiment(experiment_id)


def cancel_experiment(experiment_id: str) -> Dict[str, Any]:
    """`POST /api/experiments/<id>/cancel`. Pending/blocked: just marks the
    attempt cancelled. In flight: stops the underlying unit the same way
    the old Assignments board's cancel did (local: scheduler.cancel_item()
    on the eval item, which stops the terminal per scheduler.py's own
    cancel semantics; Kaggle: no cooperative stop exists in the CLI, so this
    only stops the dashboard from tracking it further — Kaggle's own
    `kernels status` may keep reporting it running until it finishes or
    times out, matching backend/runners/base.py's documented `stop`/`kill`
    capability gap for Kaggle)."""
    with _lock:
        data = _load()
        experiment = data["experiments"].get(experiment_id)
        if experiment is None:
            raise ExperimentError(f"Unknown experiment '{experiment_id}'")
        attempt = data["attempts"].get(experiment.get("current_attempt_id") or "")
        if attempt is None or attempt["status"] in TERMINAL_STATUSES:
            return get_experiment(experiment_id)
        unit_ref = attempt.get("unit_ref") or {}
        attempt["status"] = "cancelled"
        attempt["ended_at"] = _now_iso()
        attempt["updated_at"] = _now_iso()
        _save(data)
    if unit_ref.get("eval_item_id"):
        try:
            scheduler.cancel_item(unit_ref["eval_item_id"])
        except ValueError:
            pass
        if unit_ref.get("train_item_id"):
            try:
                scheduler.cancel_item(unit_ref["train_item_id"])
            except ValueError:
                pass
    return get_experiment(experiment_id)


def delete_experiment(
    experiment_id: str, remove_results: bool = False, remove_ledger: bool = False,
) -> bool:
    """`DELETE /api/experiments/<id>` (§5). Extended 2026-09-22 past its
    original "only while pending/blocked" scope: a done/failed/cancelled
    experiment has no live unit to protect, and refusing to delete it just
    left failed experiments with no way to ever clear them — there was no
    delete switch for exactly the state a user most wants one for. Still
    refuses while genuinely in flight (dispatching/running); cancel it
    first, so this never silently orphans a live scheduler item or Kaggle
    push.

    Two tiers under one call, both "soft" by default (customizable, per the
    user's own framing — nothing beyond the dashboard record is touched
    unless asked):
      - bare call: removes the Experiment + every Attempt from XDash's own
        store only.
      - remove_results=True: additionally deletes
        outputs/kaggle/<experiment_id> under repo_root — XDash's own
        downloaded-results cache, always safe to remove (re-downloadable
        from Kaggle as long as the kernel output itself still exists there).
      - remove_ledger=True: additionally deletes every run this
        experiment's attempts registered from the host repo's own ledger
        (backend/ledger.py's delete_run() — manifest.json + its runs.csv
        row). This touches data the host repo's orchestration layer
        considers its own record of what happened, not just XDash's
        dashboard state, hence opt-in and off by default.

    Deliberately never deletes the underlying Kaggle kernel itself:
    kernel_slug_for_account() means one kernel is shared by every
    experiment that account ever runs (XDASH_V2_PLAN.md's Kaggle-secrets-
    driven revert, 2026-09-22) — deleting it would destroy every other
    experiment's history on that kernel too, not just this one's."""
    with _lock:
        data = _load()
        experiment = data["experiments"].get(experiment_id)
        if experiment is None:
            return False
        current = data["attempts"].get(experiment.get("current_attempt_id") or "")
        if current is not None and current["status"] in IN_FLIGHT_STATUSES:
            raise ExperimentError(
                f"Experiment '{experiment_id}' is {current['status']} — cancel it first"
            )
        run_ids = [a["run_id"] for a in _attempts_for(data, experiment) if a.get("run_id")]
        for aid in experiment.get("attempt_ids", []):
            data["attempts"].pop(aid, None)
        del data["experiments"][experiment_id]
        _save(data)

    if remove_results:
        results_dir = (settings.repo_root / kaggle_backend.results_dir_for_experiment(experiment_id)).resolve()
        repo_root = settings.repo_root.resolve()
        if repo_root in results_dir.parents:  # never remove anything outside the repo
            shutil.rmtree(results_dir, ignore_errors=True)
    if remove_ledger:
        for run_id in run_ids:
            ledger.delete_run(run_id)
    return True


# --------------------------------------------------------------------------- batches (derived status)
def list_batches() -> List[Dict[str, Any]]:
    """Batch status is *derived* from its experiments, never stored
    (XDASH_V2_PLAN.md §3.6) — removes the whole class of "batch says done,
    rows say otherwise" bugs Phase A's D5 was one instance of."""
    with _lock:
        data = _load()
        policies = list(data["batches"].values())
        pairs = _snapshot_pairs(data)
    by_batch: Dict[str, List[Dict[str, Any]]] = {}
    for e, current in pairs:
        if e.get("batch_name"):
            by_batch.setdefault(e["batch_name"], []).append(_view_live(e, current))
    out = []
    for policy in policies:
        members = by_batch.get(policy["name"], [])
        statuses = [m["status"] for m in members]
        if not members:
            derived = "empty"
        elif policy.get("paused"):
            derived = "paused"
        elif any(s in IN_FLIGHT_STATUSES for s in statuses) or any(s in PRE_DISPATCH_STATUSES for s in statuses):
            derived = "running"
        elif any(s == "blocked" for s in statuses):
            derived = "stalled"
        else:
            derived = "done"
        out.append({
            **policy, "status": derived, "experiment_count": len(members),
            "done": sum(1 for s in statuses if s == "done"),
            "failed": sum(1 for s in statuses if s == "failed"),
        })
    return out


def set_batch_paused(name: str, paused: bool) -> Dict[str, Any]:
    with _lock:
        data = _load()
        policy = data["batches"].get(name)
        if policy is None:
            raise ExperimentError(f"Unknown batch '{name}'")
        policy["paused"] = paused
        _save(data)
    if not paused:
        _dispatch_tick()
    return next(b for b in list_batches() if b["name"] == name)


# --------------------------------------------------------------------------- capacity + feasibility
def _local_free_slots() -> int:
    """Identical accounting to batch_runner.py's own (XDASH_V2_PLAN.md D9's
    fix, ported) — deliberately reads scheduler.list_items() fresh rather
    than any dashboard-local mirror, so this and the old dispatcher always
    see the same shared, live number."""
    data = scheduler.list_items()
    items = data["items"]
    if data.get("paused"):
        return 0
    running = sum(1 for i in items if i["status"] == "running")
    by_id = {i["id"]: i for i in items}

    def launch_eligible(item: Dict[str, Any]) -> bool:
        dep_id = item.get("depends_on")
        if not dep_id:
            return True
        dep = by_id.get(dep_id)
        return dep is not None and dep["status"] == "completed"

    pending = sum(1 for i in items if i["status"] == "pending" and launch_eligible(i))
    return max(0, data["max_concurrent"] - running - pending)


def _kaggle_account_busy(account: Dict[str, Any]) -> bool:
    """Is *account*'s one real slot occupied? Attempts are now the only thing
    that can occupy it — the worker registry this also had to check is gone
    (XDASH_V2_PLAN.md §3.7), so this no longer reads any second system's
    state."""
    with _lock:
        data = _load()
    slot = f"kaggle:{account['name']}"
    for attempt in data["attempts"].values():
        if attempt.get("slot") == slot and attempt["status"] in IN_FLIGHT_STATUSES:
            return True
    return False


def _kaggle_candidate_accounts(est: float, config_path: str) -> List[Dict[str, Any]]:
    """Ported from batch_runner.py's own (same name, same contract) —
    accounts with budget headroom for *est* hours this week, a session cap
    that fits it, and — now that no worker exists to attach a dataset to —
    ANY idle account is a candidate as long as the config's dataset
    resolves at all (§3.4); dataset *attachment* itself happens at push
    time via push_experiment_attempt's own dataset_sources argument, not by
    matching a pre-registered worker's dataset_sources list the way the old
    dispatcher must."""
    required_dataset = dataset_map.resolve_kaggle_dataset(config_path)
    if required_dataset is None:
        return []
    out = []
    for account in kaggle_backend.list_accounts():
        if _kaggle_account_busy(account):
            continue
        session_cap = float(account.get("weekly_budget_hours") or settings.kaggle_default_budget_hours)
        if est > session_cap:
            continue
        usage = account.get("usage_estimate") or {}
        remaining = usage.get("remaining_hours")
        if remaining is not None and est > remaining:
            continue
        out.append(account)
    return out


def _account_last_activity(account: Dict[str, Any]) -> str:
    times = [w.get("pushed_at") for w in account.get("workers", []) if w.get("pushed_at")]
    with _lock:
        data = _load()
    for attempt in data["attempts"].values():
        if attempt.get("slot") == f"kaggle:{account['name']}" and attempt.get("started_at"):
            times.append(attempt["started_at"])
    return max(times) if times else ""


def _pick_kaggle_account(est: float, config_path: str) -> Optional[Dict[str, Any]]:
    """Best-fit, not round robin — same Rule 3 as batch_runner.py's twin."""
    candidates = _kaggle_candidate_accounts(est, config_path)
    if not candidates:
        return None

    def sort_key(account: Dict[str, Any]):
        usage = account.get("usage_estimate") or {}
        remaining = usage.get("remaining_hours")
        remaining_key = remaining if remaining is not None else float("inf")
        return (remaining_key, _account_last_activity(account))

    return min(candidates, key=sort_key)


def _kaggle_block(est: float, config_path: str) -> Dict[str, Any]:
    """§3.5's structured blocked codes, generalized from batch_runner.py's
    _kaggle_block_code() — returns the fuller {code, detail, since,
    clears_at} shape now that this is the real, persisted object model."""
    accounts = kaggle_backend.list_accounts()
    if not accounts:
        return {"code": "no-account", "detail": "No Kaggle accounts are registered"}
    required_dataset = dataset_map.resolve_kaggle_dataset(config_path)
    if required_dataset is None:
        name, _ = dataset_map.config_dataset_identity(config_path)
        return {
            "code": "no-dataset-mapping",
            "detail": f"'{name}' has no Kaggle dataset mapping" if name else "Config declares no dataset",
        }
    idle = [a for a in accounts if not _kaggle_account_busy(a)]
    if not idle:
        return {"code": "pool-busy", "detail": "Every Kaggle account is currently busy"}
    session_ok = [
        a for a in idle
        if est <= float(a.get("weekly_budget_hours") or settings.kaggle_default_budget_hours)
    ]
    if not session_ok:
        return {
            "code": "exceeds-session-cap",
            "detail": f"Estimated {est:.1f}h exceeds every idle account's session cap",
        }

    def _fits_quota(account: Dict[str, Any]) -> bool:
        remaining = (account.get("usage_estimate") or {}).get("remaining_hours")
        return remaining is None or est <= remaining

    quota_ok = [a for a in session_ok if _fits_quota(a)]
    if not quota_ok:
        clears_at = (kaggle_backend._utc_week_start() + timedelta(weeks=1)).isoformat()
        return {
            "code": "quota-exhausted", "detail": "Every session-eligible account is over its weekly budget",
            "clears_at": clears_at,
        }
    return {"code": "pool-busy", "detail": "Feasible, but lost a race with another attempt for the slot"}


# --------------------------------------------------------------------------- dispatch
def _dispatch_tick() -> None:
    if not _dispatch_lock.acquire(blocking=False):
        return
    try:
        _dispatch_tick_locked()
    finally:
        _dispatch_lock.release()


def _dispatch_tick_locked() -> None:
    with _lock:
        data = _load()
        pending_pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for experiment in data["experiments"].values():
            attempt = data["attempts"].get(experiment.get("current_attempt_id") or "")
            if attempt is not None and attempt["status"] in PRE_DISPATCH_STATUSES:
                batch_name = experiment.get("batch_name")
                if batch_name and data["batches"].get(batch_name, {}).get("paused"):
                    continue
                pending_pairs.append((experiment, attempt))
    if not pending_pairs:
        return

    est_cache: Dict[str, Dict[str, Any]] = {}

    def est_for(config_path: str) -> Dict[str, Any]:
        if config_path not in est_cache:
            est_cache[config_path] = estimates.est_hours(config_path)
        return est_cache[config_path]

    local_free = _local_free_slots()

    def feasibility(experiment: Dict[str, Any]) -> Tuple[int, float]:
        pool = experiment.get("pool") or "either"
        est = est_for(experiment["config_path"])["hours"]
        local_ok = pool != "kaggle_only"
        kaggle_ok = pool != "local_only" and bool(
            _kaggle_candidate_accounts(est, experiment["config_path"])
        )
        return (int(local_ok) + int(kaggle_ok), est)

    feasibility_by_id = {e["experiment_id"]: feasibility(e) for e, _ in pending_pairs}
    scored = sorted(
        pending_pairs,
        key=lambda pair: (
            feasibility_by_id[pair[0]["experiment_id"]][0],
            -feasibility_by_id[pair[0]["experiment_id"]][1],
        ),
    )

    for experiment, attempt in scored:
        pool = experiment.get("pool") or "either"
        est = est_for(experiment["config_path"])["hours"]

        target: Optional[Tuple[str, Any]] = None  # ("local", None) | ("kaggle", account)
        if pool != "local_only":
            account = _pick_kaggle_account(est, experiment["config_path"])
            if account:
                target = ("kaggle", account)
        if target is None and pool != "kaggle_only" and local_free > 0:
            target = ("local", None)

        if target is None:
            block = {"code": "pool-busy", "detail": "Nothing free this tick"} if pool == "local_only" \
                else _kaggle_block(est, experiment["config_path"])
            _set_blocked(experiment["experiment_id"], attempt["attempt_id"], block)
            continue

        if target[0] == "local":
            _claim_and_dispatch_local(experiment, attempt)
            local_free -= 1
        else:
            _claim_and_dispatch_kaggle(experiment, attempt, target[1])


def _set_blocked(experiment_id: str, attempt_id: str, block: Dict[str, Any]) -> None:
    with _lock:
        data = _load()
        attempt = data["attempts"].get(attempt_id)
        if attempt is None or attempt["status"] not in PRE_DISPATCH_STATUSES:
            return
        existing = attempt.get("blocked") or {}
        if existing.get("code") != block["code"]:
            block = {**block, "since": _now_iso()}
        else:
            block = {**existing, **{k: v for k, v in block.items() if k != "since"}}
        attempt["status"] = "blocked"
        attempt["blocked"] = block
        attempt["updated_at"] = _now_iso()
        _save(data)


def _claim_attempt(attempt_id: str, expected_status: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Atomic compare-and-swap, same primitive as assignments.claim_row()."""
    with _lock:
        data = _load()
        attempt = data["attempts"].get(attempt_id)
        if attempt is None or attempt["status"] != expected_status:
            return None
        attempt.update(patch)
        attempt["updated_at"] = _now_iso()
        _save(data)
        return attempt


def _update_attempt(attempt_id: str, patch: Dict[str, Any]) -> None:
    with _lock:
        data = _load()
        attempt = data["attempts"].get(attempt_id)
        if attempt is None:
            return
        attempt.update(patch)
        attempt["updated_at"] = _now_iso()
        _save(data)


def _claim_and_dispatch_local(experiment: Dict[str, Any], attempt: Dict[str, Any]) -> None:
    claimed = _claim_attempt(
        attempt["attempt_id"], attempt["status"],
        {"status": "dispatching", "slot": "local", "started_at": _now_iso()},
    )
    if claimed is None:
        return
    try:
        items = scheduler.add_item(experiment["config_path"], "both", experiment.get("extra_args") or "")
        unit_ref = {"train_item_id": items[0]["id"], "eval_item_id": items[1]["id"]}
        _update_attempt(attempt["attempt_id"], {
            "status": "running", "unit_ref": unit_ref,
            "stages": [{"name": "train", "status": "pending"}, {"name": "eval", "status": "pending"}],
        })
    except Exception as e:
        _fail_or_retry(experiment["experiment_id"], attempt["attempt_id"], "dispatching", str(e), "dispatch-failed")


def _claim_and_dispatch_kaggle(experiment: Dict[str, Any], attempt: Dict[str, Any], account: Dict[str, Any]) -> None:
    slot = f"kaggle:{account['name']}"
    # started_at is set here, at claim time, not only on a confirmed push — this is also what
    # lets _account_last_activity() tell two never-yet-succeeded accounts apart, so a failing
    # account isn't picked again on every single retry (see _fail_or_retry's docstring).
    claimed = _claim_attempt(
        attempt["attempt_id"], attempt["status"],
        {"status": "dispatching", "slot": slot, "started_at": _now_iso()},
    )
    if claimed is None:
        return
    try:
        required_dataset = dataset_map.resolve_kaggle_dataset(experiment["config_path"])
        result = kaggle_backend.push_experiment_attempt(
            account["name"], experiment["experiment_id"], experiment["config_path"],
            experiment.get("extra_args") or "", [required_dataset] if required_dataset else [],
        )
        unit_ref = {"account": account["name"], "kernel_slug": result["kernel_slug"], "results_dir": result["results_dir"]}
        _update_attempt(attempt["attempt_id"], {
            "status": "running", "unit_ref": unit_ref,
            "stages": [{"name": "run", "status": "running"}],
        })
    except Exception as e:
        _fail_or_retry(experiment["experiment_id"], attempt["attempt_id"], "dispatching", str(e), "dispatch-failed")


def _fail_or_retry(
    experiment_id: str, attempt_id: str, expected_status: str, detail: str, code: str,
    raw_status: Optional[str] = None,
) -> None:
    """Marks *attempt_id* failed, then starts a fresh Attempt if the Experiment
    hasn't used up max_retries yet — a retry is a new Attempt record
    (XDASH_V2_PLAN.md §3.2), never the same attempt silently reset back to
    "pending". The previous version compared a dispatch failure against
    `attempt["attempt_index"]`, a value fixed at creation and never
    incremented, so `attempt_index <= max_retries` was always true and a
    failing push retried forever — invisibly, since it also discarded the
    error on every "will retry" pass (`attempt["blocked"] = None`). Counting
    `len(experiment.attempt_ids)` instead converges after max_retries+1 real
    attempts, and every failed attempt keeps its own visible error in
    history instead of overwriting the same record."""
    patch = {
        "status": "failed", "ended_at": _now_iso(),
        "blocked": {"code": code, "detail": detail[:300], "since": _now_iso()},
    }
    if raw_status is not None:
        patch["raw_status"] = raw_status
    claimed = _claim_attempt(attempt_id, expected_status, patch)
    if claimed is None:
        return  # already resolved by someone else (e.g. a concurrent cancel) — nothing to retry
    with _lock:
        data = _load()
        experiment = data["experiments"].get(experiment_id)
        if experiment is None:
            return
        max_retries = experiment.get("max_retries", 1)
        if len(experiment.get("attempt_ids", [])) <= max_retries:
            _append_attempt(data, experiment)
            _save(data)
    _dispatch_tick()


# --------------------------------------------------------------------------- completion hooks
def on_scheduler_item_finished(item_id: str) -> None:
    """Called from scheduler._tick() alongside batch_runner's own hook (both
    are wired from the same call site — see scheduler.py) — only the eval
    item is the pair's real terminus, exactly like batch_runner.py's twin."""
    with _lock:
        data = _load()
        attempt = next(
            (a for a in data["attempts"].values() if (a.get("unit_ref") or {}).get("eval_item_id") == item_id),
            None,
        )
    if attempt is None or attempt["status"] != "running":
        return
    item = next((i for i in scheduler.list_items()["items"] if i["id"] == item_id), None)
    if item is None:
        return
    _resolve_attempt(attempt, item["status"] == "completed", item["status"])


def _resolve_attempt(attempt: Dict[str, Any], succeeded: bool, raw_status: str) -> None:
    with _lock:
        data = _load()
        experiment = data["experiments"].get(attempt["experiment_id"])
    if succeeded:
        run_id = _find_run_id_for(cfg.get_experiment_name(experiment["config_path"]), experiment.get("seed")) \
            if experiment is not None else None
        claimed = _claim_attempt(attempt["attempt_id"], "running", {
            "status": "done", "ended_at": _now_iso(), "raw_status": raw_status, "run_id": run_id,
        })
        if claimed is not None and experiment is not None:
            notif.send_all(f"Experiment '{experiment['experiment_id']}' is now done ({raw_status}).")
        _dispatch_tick()
        return
    if experiment is None:
        # Experiment was deleted out from under an in-flight attempt — nothing to retry into.
        _claim_attempt(attempt["attempt_id"], "running", {
            "status": "failed", "ended_at": _now_iso(), "raw_status": raw_status,
        })
        return
    _fail_or_retry(
        experiment["experiment_id"], attempt["attempt_id"], "running",
        f"unit ended: {raw_status}", "attempt-failed", raw_status=raw_status,
    )
    notif.send_all(f"Experiment '{experiment['experiment_id']}' attempt ended: {raw_status}.")


def _poll_kaggle_attempts() -> None:
    """Kaggle attempts have no worker registry entry, so kaggle.py's own
    _tick() poller never sees them — this is the only place that polls a
    worker-less push's status and resolves it on completion."""
    with _lock:
        data = _load()
        in_flight = [
            a for a in data["attempts"].values()
            if a["status"] == "running" and (a.get("unit_ref") or {}).get("kernel_slug")
        ]
    for attempt in in_flight:
        unit_ref = attempt["unit_ref"]
        try:
            result = kaggle_backend.refresh_experiment_status(unit_ref["account"], unit_ref["kernel_slug"])
        except kaggle_backend.KaggleOpsError:
            continue
        status = result.get("status")
        if status not in kaggle_backend.FINAL_STATUSES:
            continue
        try:
            kaggle_backend.download_experiment(unit_ref["account"], unit_ref["kernel_slug"], unit_ref["results_dir"])
        except kaggle_backend.KaggleOpsError:
            pass  # still resolve the attempt below — a download failure shouldn't strand it forever
        _resolve_attempt(attempt, status == "complete", status)


# --------------------------------------------------------------------------- reconciliation + poller
def _reconcile_on_startup() -> None:
    """Mirrors batch_runner.py's own (XDASH_V2_PLAN.md D5) — a crash between
    claiming an attempt and recording its unit_ref leaves it "dispatching"
    with nothing to resolve against; hand it back to the pool. A crash after
    the unit_ref landed is re-resolved against that unit."""
    with _lock:
        data = _load()
        attempts = list(data["attempts"].values())
    for attempt in attempts:
        status = attempt["status"]
        if status not in ({"dispatching"} | IN_FLIGHT_STATUSES):
            continue
        unit_ref = attempt.get("unit_ref") or {}
        if status == "dispatching" and not unit_ref:
            _update_attempt(attempt["attempt_id"], {"status": "pending", "slot": None})
            continue
        if "eval_item_id" in unit_ref:
            item = next((i for i in scheduler.list_items()["items"] if i["id"] == unit_ref["eval_item_id"]), None)
            if item and item["status"] in ("completed", "failed", "cancelled", "skipped"):
                _resolve_attempt({**attempt, "status": "running"}, item["status"] == "completed", item["status"])
        elif unit_ref.get("kernel_slug"):
            try:
                result = kaggle_backend.refresh_experiment_status(unit_ref["account"], unit_ref["kernel_slug"])
            except kaggle_backend.KaggleOpsError:
                continue
            if result.get("status") in kaggle_backend.FINAL_STATUSES:
                try:
                    kaggle_backend.download_experiment(unit_ref["account"], unit_ref["kernel_slug"], unit_ref["results_dir"])
                except kaggle_backend.KaggleOpsError:
                    pass
                _resolve_attempt({**attempt, "status": "running"}, result.get("status") == "complete", result.get("status"))


_poller_started = False
_poller_lock = threading.Lock()


def _poll_loop() -> None:
    while True:
        try:
            _poll_kaggle_attempts()
            _dispatch_tick()
        except Exception:
            pass  # one bad tick must never kill the whole poller
        time.sleep(30)


def ensure_dispatcher_started() -> None:
    global _poller_started
    with _poller_lock:
        if _poller_started:
            return
        try:
            _reconcile_on_startup()
        except Exception:
            pass
        threading.Thread(target=_poll_loop, daemon=True, name="experiment-dispatch-tick").start()
        _poller_started = True


# --------------------------------------------------------------------------- slots
def list_slots() -> List[Dict[str, Any]]:
    """The one capacity concept (§3.3) — `local` (scheduler.max_concurrent)
    plus one entry per Kaggle account (exactly 1 slot, platform-limited)."""
    scheduler_data = scheduler.list_items()
    running_local = sum(1 for i in scheduler_data["items"] if i["status"] == "running")
    slots = [{
        "slot": "local", "kind": "local",
        "used": running_local, "limit": scheduler_data["max_concurrent"],
        "paused": scheduler_data.get("paused", False),
    }]
    for account in kaggle_backend.list_accounts():
        usage = account.get("usage_estimate") or {}
        slots.append({
            "slot": f"kaggle:{account['name']}", "kind": "kaggle", "account": account["name"],
            "used": 1 if _kaggle_account_busy(account) else 0, "limit": 1,
            "hours_this_week": usage.get("hours_this_week"),
            "weekly_budget_hours": usage.get("weekly_budget_hours"),
            "remaining_hours": usage.get("remaining_hours"),
            "clears_at": (kaggle_backend._utc_week_start() + timedelta(weeks=1)).isoformat(),
        })
    return slots


# --------------------------------------------------------------------------- pulse
def get_pulse() -> Dict[str, Any]:
    """Everything the Lab view polls, in one request (§5) — counts, active
    attempts, slot capacity, blocked list and recent events."""
    with _lock:
        pairs = _snapshot_pairs(_load())
    views = [_view_live(e, current) for e, current in pairs]
    running = [v for v in views if v["status"] in IN_FLIGHT_STATUSES]
    blocked = [v for v in views if v["status"] == "blocked"]
    queued = [v for v in views if v["status"] == "pending"]
    done = [v for v in views if v["status"] == "done"]
    failed = [v for v in views if v["status"] == "failed"]
    recent = sorted(
        [v for v in views if v["status"] in ("done", "failed")],
        key=lambda v: (v.get("current_attempt") or {}).get("ended_at") or "", reverse=True,
    )[:10]
    return {
        "slots": list_slots(),
        "running": running,
        "blocked": blocked,
        "queued_count": len(queued),
        "done_count": len(done),
        "failed_count": len(failed),
        "recent": recent,
        "generated_at": _now_iso(),
    }
