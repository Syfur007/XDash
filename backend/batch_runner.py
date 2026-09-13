"""Batch dispatcher (EXPERIMENT_AUTOMATION_PLAN.md §4/§4.1): drives a sweep
spec's Assignments rows to completion across the local scheduler and Kaggle
accounts, greedily — on each tick, the most constrained pending/blocked row
is handed to the best resource currently free, no lookahead, no
backtracking.

Deliberately does NOT talk to backend/runners/ (the Runner facade) —
LocalRunner.launch() calls terminals.launch() directly, bypassing
scheduler.py's own queue/concurrency control, which is the opposite of what
this module needs (only dispatch when a scheduler slot is actually free).
This module drives scheduler.add_item() and kaggle.push() the same way the
rest of the dashboard always has.

KNOWN GAP, not silently worked around: a row's Kaggle-feasibility here is
quota/session-cap only (C2/C3) — it does NOT check whether any account's
worker has the config's actual dataset attached (C4). That check needs a
mapping from a config's dataset to a Kaggle dataset slug that doesn't exist
yet anywhere in this codebase (EXPERIMENT_AUTOMATION_PLAN.md §2.5's own
"still open" note). Until that lands, a row can be dispatched to an account
whose worker lacks the right data, and the failure surfaces inside the
Kaggle kernel (the launch template's own dataset-attach assertion) rather
than being caught here first.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import assignments as asg
from . import estimates
from . import kaggle as kaggle_backend
from . import notifications as notif
from . import scheduler
from .config import settings

_lock = threading.RLock()          # guards batches.json
_dispatch_lock = threading.Lock()  # serializes _dispatch_tick() — see its own docstring

TERMINAL_ROW_STATUSES = {"done", "failed", "cancelled"}
IN_FLIGHT_ROW_STATUSES = {"local-queued", "kaggle-pushed"}
CANDIDATE_ROW_STATUSES = {"pending", "blocked"}  # blocked is re-evaluated every tick, not stuck


class BatchError(Exception):
    """Expected failure (unknown batch, bad spec, name collision) — routes
    map this to a 4xx."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load() -> Dict[str, Any]:
    if not settings.batches_file.exists():
        return {"batches": {}}
    try:
        data = json.loads(settings.batches_file.read_text())
    except Exception:
        return {"batches": {}}
    data.setdefault("batches", {})
    return data


def _save(data: Dict[str, Any]) -> None:
    settings.batches_file.write_text(json.dumps(data, indent=2))


def list_batches() -> List[Dict[str, Any]]:
    with _lock:
        return list(_load()["batches"].values())


def get_batch(name: str) -> Dict[str, Any]:
    with _lock:
        batch = _load()["batches"].get(name)
    if batch is None:
        raise BatchError(f"Unknown batch '{name}'")
    return batch


# --------------------------------------------------------------------------- start/pause/cancel
def start_batch(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Expands *spec* (the sweep-spec shape, EXPERIMENT_AUTOMATION_PLAN.md
    §3) into Assignments rows and marks the batch running. Refuses to reuse
    a name that's still running/paused — merging a second spec into an
    active batch under the same name would make "how many rows does this
    batch have" and "is it done" both ambiguous; cancel the old one first,
    or start under a different name."""
    name = (spec.get("name") or "").strip()
    if not name:
        raise BatchError("Batch name is required")
    configs = spec.get("configs") or []
    if not configs:
        raise BatchError("Sweep spec must declare at least one config")
    pool = spec.get("pool") or "either"
    if pool not in ("either", "local_only", "kaggle_only"):
        raise BatchError(f"pool must be 'either', 'local_only' or 'kaggle_only', got {pool!r}")
    max_retries = int(spec.get("max_retries", 1))
    force_on_retry = bool(spec.get("force_on_retry", True))

    with _lock:
        data = _load()
        existing = data["batches"].get(name)
        if existing and existing.get("status") in ("running", "paused"):
            raise BatchError(
                f"Batch '{name}' is already {existing['status']} — cancel it first or pick a different name"
            )

        entries = []
        for entry in configs:
            config_path = (entry.get("path") or "").strip()
            if not config_path:
                raise BatchError("Every config entry needs a path")
            seeds = entry.get("seeds") or [None]
            for seed in seeds:
                base_extra_args = ""
                if seed is not None and settings.seed_arg:
                    base_extra_args = settings.seed_arg.format(seed=seed)
                entries.append({
                    "config_path": config_path, "seed": seed, "batch_name": name, "pool": pool,
                    "extra": {"base_extra_args": base_extra_args},
                })
        if not entries:
            raise BatchError("Sweep spec expanded to zero rows")

        rows = asg.bulk_add(entries)
        data["batches"][name] = {
            "name": name, "status": "running", "pool": pool,
            "max_retries": max_retries, "force_on_retry": force_on_retry,
            "started_at": _now(), "ended_at": None, "row_count": len(rows),
        }
        _save(data)
    ensure_batch_worker_started()
    _dispatch_tick()
    return get_batch(name)


def pause_batch(name: str) -> Dict[str, Any]:
    """Stops new handouts; in-flight rows still finish and get recorded."""
    with _lock:
        data = _load()
        batch = data["batches"].get(name)
        if batch is None:
            raise BatchError(f"Unknown batch '{name}'")
        if batch["status"] == "running":
            batch["status"] = "paused"
            _save(data)
    return get_batch(name)


def resume_batch(name: str) -> Dict[str, Any]:
    with _lock:
        data = _load()
        batch = data["batches"].get(name)
        if batch is None:
            raise BatchError(f"Unknown batch '{name}'")
        if batch["status"] == "paused":
            batch["status"] = "running"
            _save(data)
    _dispatch_tick()
    return get_batch(name)


def cancel_batch(name: str) -> Dict[str, Any]:
    """Marks the batch cancelled and every remaining pending/blocked row
    cancelled too. In-flight units are deliberately left alone — cancel
    stops future handouts, it doesn't kill a session or a pushed kernel."""
    with _lock:
        data = _load()
        batch = data["batches"].get(name)
        if batch is None:
            raise BatchError(f"Unknown batch '{name}'")
        if batch["status"] in ("running", "paused"):
            batch["status"] = "cancelled"
            batch["ended_at"] = _now()
            _save(data)
    for row in asg.list_rows():
        if row.get("batch_name") == name and row.get("status") in CANDIDATE_ROW_STATUSES:
            asg.claim_row(row["row_id"], {"status": "cancelled"}, expected_status=row["status"])
    return get_batch(name)


def _finish_batch(name: str, status: str, reason: str = "") -> None:
    """status is "done" or "stalled" — both are terminal for dispatch
    purposes (no more handouts), distinct in meaning: "done" is every row
    settled cleanly, "stalled" is nothing left runnable this tick even
    though rows remain (EXPERIMENT_AUTOMATION_PLAN.md §4.1 Rule 7) — a
    batch stuck on quota exhaustion must not look identical to one that
    actually finished."""
    with _lock:
        data = _load()
        batch = data["batches"].get(name)
        if batch is None or batch["status"] != "running":
            return
        batch["status"] = status
        batch["ended_at"] = _now()
        _save(data)
    rows = [r for r in asg.list_rows() if r.get("batch_name") == name]
    done_n = sum(1 for r in rows if r["status"] == "done")
    failed_n = sum(1 for r in rows if r["status"] == "failed")
    msg = f"Batch '{name}' is {status} — {done_n} done, {failed_n} failed, {len(rows)} total."
    if reason:
        msg += f" ({reason})"
    notif.send_all(msg)


# --------------------------------------------------------------------------- capacity + feasibility
def _local_free_slots() -> int:
    data = scheduler.list_items()
    items = data["items"]
    running = sum(1 for i in items if i["status"] == "running")
    pending = sum(1 for i in items if i["status"] == "pending")
    if data.get("paused"):
        return 0
    return max(0, data["max_concurrent"] - running - pending)


# kaggle.py's own IN_PROGRESS_STATUSES ({"queued", "preparing", "running"}) is what a
# *polled* Kaggle API response reports — it deliberately excludes "pushed", the status a
# worker carries in the gap between a successful push and the poller's next refresh_status()
# call actually reaching Kaggle's API (up to kaggle_poll_interval_seconds later). Without
# treating "pushed" as busy too, two rows in the *same* dispatch tick could both pick the
# same just-pushed account/worker before anything ever reports it as occupied — this set is
# the dispatcher's own, stricter "is this account's slot actually free" answer, not a
# reimplementation of kaggle.py's poll-status vocabulary.
_KAGGLE_BUSY_STATUSES = kaggle_backend.IN_PROGRESS_STATUSES | {"pushed"}


def _idle_template_workers(account: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Workers under *account* that can take an arbitrary config_path right
    now: template-backed (notebook-backed workers ignore config_path
    entirely and always run their own fixed notebook — dispatching a batch
    row to one would silently run the wrong thing) and not currently
    occupying the account's one real slot (_KAGGLE_BUSY_STATUSES)."""
    return [
        w for w in account.get("workers", [])
        if not w.get("notebook_path")
        and (w.get("status") or "") not in _KAGGLE_BUSY_STATUSES
    ]


def _kaggle_candidate_accounts(est: float) -> List[Dict[str, Any]]:
    """Accounts with at least one idle template-backed worker, budget
    headroom for *est* hours this week (C3), and a per-push session cap
    that fits it too (C2 — approximated by the account's idle workers' own
    budget_hours until EXPERIMENT_AUTOMATION_PLAN.md §8.1's setup/teardown
    split lands and gives a tighter number)."""
    out = []
    for account in kaggle_backend.list_accounts():
        idle = _idle_template_workers(account)
        if not idle:
            continue
        session_cap = max((w.get("budget_hours") or settings.kaggle_default_budget_hours) for w in idle)
        if est > session_cap:
            continue
        usage = account.get("usage_estimate") or {}
        remaining = usage.get("remaining_hours")
        if remaining is not None and est > remaining:
            continue
        out.append(account)
    return out


def _account_last_activity(account: Dict[str, Any]) -> str:
    """Latest pushed_at across the account's workers, "" (sorts first) if
    it has never been pushed — used for Rule 3's least-recently-used
    tie-break."""
    times = [w.get("pushed_at") for w in account.get("workers", []) if w.get("pushed_at")]
    return max(times) if times else ""


def _pick_kaggle_account(est: float) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Rule 3 — best fit, not round robin: among quota/session-cap-eligible
    accounts, the one with the LEAST remaining quota that still fits *est*.
    Round-robin draws every account down evenly and can leave several each
    holding not-quite-enough headroom for the last long job; best-fit packs
    the partially-spent account tighter and preserves an intact block of
    quota elsewhere. Ties broken least-recently-pushed, to spread API-rate
    exposure across accounts rather than hammering the same one."""
    candidates = _kaggle_candidate_accounts(est)
    if not candidates:
        return None

    def sort_key(account: Dict[str, Any]):
        usage = account.get("usage_estimate") or {}
        remaining = usage.get("remaining_hours")
        # None (no configured budget) sorts last -- an unbounded account is the
        # worst fit for "tightest remaining headroom that still fits" scoring,
        # since best-fit's whole point is to prefer the account closest to its cap.
        remaining_key = remaining if remaining is not None else float("inf")
        return (remaining_key, _account_last_activity(account))

    best = min(candidates, key=sort_key)
    worker = _idle_template_workers(best)[0]
    return best, worker


def _row_extra_args(row: Dict[str, Any], batch: Dict[str, Any]) -> str:
    base = (row.get("extra") or {}).get("base_extra_args", "")
    is_retry = row.get("attempt_count", 0) > 0
    if is_retry and batch.get("force_on_retry"):
        return (base + " --force").strip()
    return base


# --------------------------------------------------------------------------- dispatch
def _dispatch_tick() -> None:
    """Serialized (not reentrant): two overlapping ticks scoring against the
    same capacity snapshot would both think the same free slot is available
    and double-dispatch into it — per-row claim_row() only stops two ticks
    from claiming the *same row*, not from claiming two different rows for
    one slot. A tick that's already running skips a concurrent call rather
    than blocking behind it; the next poll/completion-triggered tick will
    pick up whatever was missed."""
    if not _dispatch_lock.acquire(blocking=False):
        return
    try:
        _dispatch_tick_locked()
    finally:
        _dispatch_lock.release()


def _dispatch_tick_locked() -> None:
    batches = {b["name"]: b for b in list_batches() if b["status"] == "running"}
    if not batches:
        return

    rows = [
        r for r in asg.list_rows()
        if r.get("batch_name") in batches and r.get("status") in CANDIDATE_ROW_STATUSES
    ]
    if not rows:
        _settle_finished_batches(batches)
        return

    est_cache: Dict[str, Dict[str, Any]] = {}

    def est_for(row: Dict[str, Any]) -> Dict[str, Any]:
        cp = row["config_path"]
        if cp not in est_cache:
            est_cache[cp] = estimates.est_hours(cp)
        return est_cache[cp]

    local_free = _local_free_slots()

    def feasibility(row: Dict[str, Any]) -> Tuple[int, float]:
        pool = row.get("pool") or "either"
        est = est_for(row)["hours"]
        local_ok = pool != "kaggle_only"
        kaggle_ok = pool != "local_only" and bool(_kaggle_candidate_accounts(est))
        count = int(local_ok) + int(kaggle_ok)
        return (count, est)

    # Rule 1: most-constrained first (fewest feasible resources), longest est_hours first
    # within an equal count — LPT within a tier, so long jobs don't get left for last.
    scored = sorted(rows, key=lambda r: (feasibility(r)[0], -feasibility(r)[1]))

    for row in scored:
        pool = row.get("pool") or "either"
        est = est_for(row)["hours"]
        batch = batches[row["batch_name"]]

        target: Optional[Tuple[str, Any]] = None  # ("local", None) | ("kaggle", (account, worker))
        if pool != "local_only":
            picked = _pick_kaggle_account(est)
            if picked:
                target = ("kaggle", picked)
        if target is None and pool != "kaggle_only" and local_free > 0:
            # Rule 5: never idle a usable slot — local takes it if Kaggle wasn't
            # feasible/available this tick, even for an "either" row that would have
            # preferred Kaggle (Rule 2) had a slot been free there.
            target = ("local", None)

        if target is None:
            if pool == "kaggle_only":
                reason = "quota-exhausted-or-no-idle-worker"
            elif pool == "local_only":
                reason = "local-busy"
            else:
                reason = "quota-exhausted-and-local-busy"
            if row["status"] != "blocked" or row.get("blocked_reason") != reason:
                asg.claim_row(row["row_id"], {"status": "blocked", "blocked_reason": reason}, expected_status=row["status"])
            continue

        new_attempt_count = row.get("attempt_count", 0) + 1
        extra_args = _row_extra_args(row, batch)

        if target[0] == "local":
            claimed = asg.claim_row(
                row["row_id"],
                {"status": "dispatching", "runner_id": "local", "attempt_count": new_attempt_count},
                expected_status=row["status"],
            )
            if claimed is None:
                continue
            try:
                items = scheduler.add_item(row["config_path"], "both", extra_args)
                unit_ref = {"train_item_id": items[0]["id"], "eval_item_id": items[1]["id"]}
                asg.update_row(row["row_id"], {"status": "local-queued", "unit_ref": unit_ref})
            except Exception as e:
                _requeue_or_fail(row["row_id"], batch, new_attempt_count, str(e))
                continue
            local_free -= 1
        else:
            account, worker = target[1]
            claimed = asg.claim_row(
                row["row_id"],
                {
                    "status": "dispatching", "runner_id": f"kaggle:{account['name']}",
                    "attempt_count": new_attempt_count,
                },
                expected_status=row["status"],
            )
            if claimed is None:
                continue
            try:
                kaggle_backend.push(worker["worker_id"], row["config_path"], extra_args)
                unit_ref = {"account": account["name"], "worker_id": worker["worker_id"]}
                asg.update_row(row["row_id"], {"status": "kaggle-pushed", "unit_ref": unit_ref})
            except Exception as e:
                _requeue_or_fail(row["row_id"], batch, new_attempt_count, str(e))
                continue

    _settle_finished_batches(batches)


def _requeue_or_fail(row_id: str, batch: Dict[str, Any], attempt_count: int, error: str) -> None:
    """A dispatch attempt itself failed (before any real unit exists — a
    bad config, a Kaggle CLI error). Never silently retries past
    max_retries — a row that exhausts retries stays failed and visible,
    mirroring the caution the old (now-deleted) auto_chain already
    observed about never re-chaining into a known-broken push forever."""
    if attempt_count < batch.get("max_retries", 1):
        asg.update_row(row_id, {"status": "pending"})
    else:
        asg.update_row(row_id, {"status": "failed", "blocked_reason": error[:300]})


def _settle_finished_batches(running_batches: Dict[str, Any]) -> None:
    rows_by_batch: Dict[str, List[Dict[str, Any]]] = {}
    for r in asg.list_rows():
        bn = r.get("batch_name")
        if bn in running_batches:
            rows_by_batch.setdefault(bn, []).append(r)

    for name, batch in running_batches.items():
        rows = rows_by_batch.get(name, [])
        if not rows:
            continue
        pending_or_inflight = [r for r in rows if r["status"] in CANDIDATE_ROW_STATUSES or r["status"] in IN_FLIGHT_ROW_STATUSES]
        if pending_or_inflight:
            continue
        blocked = [r for r in rows if r["status"] == "blocked"]
        if blocked:
            reasons = sorted({r.get("blocked_reason") or "unknown" for r in blocked})
            _finish_batch(name, "stalled", f"blocked: {', '.join(reasons)}")
        else:
            _finish_batch(name, "done")


# --------------------------------------------------------------------------- completion hooks
def on_scheduler_item_finished(item_id: str) -> None:
    """Called from scheduler._tick() for every item newly landed in
    just_finished — for both the running->terminal transition and the
    dependency-skip transition (EXPERIMENT_AUTOMATION_PLAN.md §4's fix to
    that branch never having appended to just_finished before). A row's
    unit_ref names *two* scheduler items (train+eval); only the eval item
    is the chain's real terminus — train reaching its own terminal state
    doesn't resolve the row, since eval hasn't run yet (or was skipped,
    which is itself a terminal outcome for the pair)."""
    row = next(
        (r for r in asg.list_rows() if (r.get("unit_ref") or {}).get("eval_item_id") == item_id),
        None,
    )
    if row is None or row["status"] != "local-queued":
        return
    item = next((i for i in scheduler.list_items()["items"] if i["id"] == item_id), None)
    if item is None:
        return
    _resolve_row(row, item["status"] in ("completed",), item["status"])


def on_kaggle_unit_finished(worker_id: str, kaggle_status: str) -> None:
    """Called from kaggle._tick() when a worker reaches FINAL_STATUSES. One
    push is one experiment now (EXPERIMENT_AUTOMATION_PLAN.md §2.4), so any
    worker reaching a final status resolves its row directly — no second
    half to wait for the way a local row's eval item is."""
    row = next(
        (r for r in asg.list_rows() if (r.get("unit_ref") or {}).get("worker_id") == worker_id),
        None,
    )
    if row is None or row["status"] != "kaggle-pushed":
        return
    _resolve_row(row, kaggle_status == "complete", kaggle_status)


def _resolve_row(row: Dict[str, Any], succeeded: bool, raw_status: str) -> None:
    batch = None
    try:
        batch = get_batch(row["batch_name"])
    except BatchError:
        pass
    if succeeded:
        asg.claim_row(row["row_id"], {"status": "done"}, expected_status=row["status"])
    else:
        max_retries = (batch or {}).get("max_retries", 1)
        if row.get("attempt_count", 0) < max_retries:
            asg.claim_row(row["row_id"], {"status": "pending"}, expected_status=row["status"])
        else:
            asg.claim_row(
                row["row_id"], {"status": "failed", "blocked_reason": f"unit ended: {raw_status}"},
                expected_status=row["status"],
            )
    _dispatch_tick()


# --------------------------------------------------------------------------- reconciliation + poller
def _reconcile_on_startup() -> None:
    """Rows left local-queued/kaggle-pushed when the process died have no
    path back on their own — the completion hook that would resolve them
    only fires from inside _tick(), and a crashed process never got to run
    it. Re-resolve each in-flight row against its unit before the first
    dispatch tick, so a crash strands a batch's *progress*, never the batch
    itself silently forever."""
    for row in asg.list_rows():
        if row["status"] not in IN_FLIGHT_ROW_STATUSES:
            continue
        unit_ref = row.get("unit_ref") or {}
        if row["status"] == "local-queued":
            eval_id = unit_ref.get("eval_item_id")
            item = next((i for i in scheduler.list_items()["items"] if i["id"] == eval_id), None)
            if item is None:
                continue
            if item["status"] in ("completed", "failed", "cancelled", "skipped"):
                _resolve_row(row, item["status"] == "completed", item["status"])
        elif row["status"] == "kaggle-pushed":
            worker_id = unit_ref.get("worker_id")
            if not worker_id:
                continue
            try:
                result = kaggle_backend.refresh_status(worker_id)
            except kaggle_backend.KaggleOpsError:
                continue
            if result.get("status") in kaggle_backend.FINAL_STATUSES:
                _resolve_row(row, result.get("status") == "complete", result.get("status"))


_poller_started = False
_poller_lock = threading.Lock()


def _poll_loop() -> None:
    while True:
        try:
            _dispatch_tick()
        except Exception:
            pass  # one bad tick must never kill the whole poller
        time.sleep(30)


def ensure_batch_worker_started() -> None:
    global _poller_started
    with _poller_lock:
        if _poller_started:
            return
        try:
            _reconcile_on_startup()
        except Exception:
            pass
        threading.Thread(target=_poll_loop, daemon=True, name="batch-dispatch-tick").start()
        _poller_started = True
