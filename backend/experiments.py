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

Identity (XDASH_PLAN.md §3.3.1): `<experiment_name>-s<seed>` for an empty
overlay (no seed -> just the experiment_name), and
`<experiment_name>-s<seed>-<sha1(canonical overlay)[:6]>` otherwise, so two
different overlays never share a record (X8). It is a label, not a path:
where a run's outputs actually live is asked of the framework at dispatch
and stored on the attempt as `run.run_dir` (§4.4) — dissert has written
`<experiment_name>/<hash7>-s<seed>/` since its commit 52477d1, which the old
`<experiment_name>-s<seed>` guess never matched (X2).

The Phase 1 model (§3.2-§3.3, §6):

- **Drafts (X7).** Creating an experiment no longer executes it: no attempt
  means `draft`. Execution is an explicit action — queue, run now, or a
  study's autopilot — through `apply_action()`, the one entry point for a
  single row, a selection, or a whole study (§6.1).
- **Status is derived**, never stored: draft, else the current attempt's
  status (`queued` — formerly `pending` — `blocked`, `dispatching`,
  `running`, `done`, `failed`, `cancelled`). What may be edited follows from
  it (`_EDITABLE`).
- **Studies** (backend/studies.py) live in this same store; membership is a
  list on the experiment (`studies: [{study_id, group}]`), many-to-many.
- **Runtime policy** (§6.3): `{mode: auto, allow, requires}` or
  `{mode: pinned, slot}`; a pinned experiment never falls back.

Completion (XDASH_PLAN.md §6.6): every runner kind resolves through one
path, `_poll_and_resolve()`: poll → collect (logs + the planned run dir) →
canonicalize into the local tree → classify → resolve, or open the next
leg. Machine (local/SSH/Colab) attempts used to be resolved from the exit
code alone by a scheduler hook that never collected anything (X3).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import colab as colab_backend
from . import configs as cfg
from . import datasets as datasets_mod
from . import estimates
from . import framework
from . import kaggle as kaggle_backend
from . import ledger
from . import notifications as notif
from . import results_ingest
from . import runtimes as runtimes_mod
from . import scheduler
from . import snapshot
from . import tb_curves
from . import transport as transport_mod
from .config import background_disabled, settings
from .runners import registry
from .runners.base import Runner, RunnerBlocked
from .store import JsonStore

_lock = threading.RLock()          # guards experiments.json
_dispatch_lock = threading.Lock()  # serializes _dispatch_tick()

# Attempt statuses. An experiment with no attempt at all is a `draft`
# (derived, §3.3.2) — never an attempt status.
PRE_DISPATCH_STATUSES = {"queued", "blocked"}      # not yet claimed by a slot
IN_FLIGHT_STATUSES = {"dispatching", "running"}     # claimed; a unit exists or is being created
TERMINAL_STATUSES = {"done", "failed", "cancelled"}
DRAFT = "draft"

# What PATCH /api/experiments/<id> may change, by derived status (§3.3.2's
# table). `name` and `notes` are display-only and always editable; overlay,
# seed and config_path are identity, so only a draft (which has no history)
# may change them — the draft is re-keyed. `extra_args` is not identity (it
# is the escape hatch for non-config flags) and follows the runtime policy.
_ALWAYS_EDITABLE = {"name", "notes", "studies"}
_POLICY_FIELDS = {"runtime", "priority", "max_retries", "max_legs", "force_on_retry", "extra_args"}
_IDENTITY_FIELDS = {"overlay", "seed", "config_path"}
_EDITABLE = {
    DRAFT: _ALWAYS_EDITABLE | _POLICY_FIELDS | _IDENTITY_FIELDS,
    "queued": _ALWAYS_EDITABLE | _POLICY_FIELDS,
    "blocked": _ALWAYS_EDITABLE | _POLICY_FIELDS,
    "dispatching": set(_ALWAYS_EDITABLE),
    "running": set(_ALWAYS_EDITABLE),
    "done": _ALWAYS_EDITABLE | _POLICY_FIELDS,
    "failed": _ALWAYS_EDITABLE | _POLICY_FIELDS,
    "cancelled": _ALWAYS_EDITABLE | _POLICY_FIELDS,
}

# Priority order for picking the single most-informative blocked reason when
# every candidate runner for an experiment's pool refuses it — lower index
# wins (matches the old, kaggle-only _kaggle_block()'s own ordering). A code
# a runner returns that isn't listed here (forward-compat for a new kind)
# sorts last, never crashes.
_BLOCK_PRIORITY = {
    # DATASETS_PLAN.md §4.2/§9: plan_delivery()'s own codes are the canonical
    # ones now. "no-dataset-binding"/"no-dataset-mapping" are kept as legacy
    # aliases (same priority) for anything still emitting the pre-DATASETS_PLAN
    # spelling.
    "no-account": 0, "runtime-missing": 0,
    "no-dataset-binding": 1, "no-dataset-mapping": 1, "dataset-draft": 1, "dataset-unavailable": 1,
    "no-kaggle-source": 1, "kaggle-no-access": 1, "no-data-account": 1, "dataset-target-occupied": 1,
    "requires-unmet": 1, "pool-busy": 2,
    "exceeds-session-cap": 3, "quota-exhausted": 4, "code-not-pushed": 5,
}

# How long Run now waits for a background dispatch tick that holds the
# dispatcher (a tick can be mid-push to Kaggle) before giving up with a
# reason instead of racing it for the same capacity.
_RUN_NOW_LOCK_TIMEOUT = 60.0

# How many ticks a finished attempt's collection may fail before a
# non-volatile runner's attempt is resolved without its outputs (~5 min at
# the 30 s poll interval). A volatile runner (Colab) never gives up: its VM
# is the only copy, and reap_idle() won't stop it while collection is
# pending (XDASH_PLAN.md §6.6's hard guard).
_COLLECT_MAX_TRIES = 10


def _block_severity(block: Dict[str, Any]) -> int:
    return _BLOCK_PRIORITY.get(block.get("code"), 99)


class ExperimentError(Exception):
    """Expected failure (bad id, bad spec) — routes map this to a 4xx."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- storage
# v1 -> v2 (Phase 0): `extra_args` becomes {"train", "eval"} (X12).
# v2 -> v3 (Phase 1, §3.8): `batches` become studies, `pool` becomes
# `runtime.allow`, attempt status `pending` becomes `queued`.
SCHEMA_VERSION = 3


class ExperimentConflict(ExperimentError):
    """The request is well-formed but the experiment's state forbids it
    (e.g. editing an overlay while queued) — routes map this to a 409."""


def _empty_store() -> Dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "experiments": {}, "attempts": {}, "studies": {}}


def migrated_study_id(batch_name: str) -> str:
    """The study a batch of this name became (§3.8), and the study a legacy
    `batch_name` in POST /api/experiments maps to. Deterministic, never
    random: the migration runs in memory on every load until the next save,
    so two reads before that save must agree on every id."""
    return "st_" + hashlib.sha1(("batch:%s" % batch_name).encode("utf-8")).hexdigest()[:6]


def blank_study(study_id: str, name: str, created_at: Optional[str]) -> Dict[str, Any]:
    """A study record (§3.2) with every field at its default. Its status is
    derived from its members on every read (backend/studies.py), never
    stored — the rule the batch model already had."""
    return {
        "study_id": study_id, "name": name, "question": "", "description": "", "tags": [],
        "primary_metric": None, "baseline": None,
        "defaults": {"seeds": [], "overlay": {}, "runtime": default_runtime()},
        "autopilot": {"enabled": False, "max_parallel": 2},
        "priority": 0, "archived": False,
        "created_at": created_at, "updated_at": created_at,
    }


def _study_from_batch(name: str, batch: Dict[str, Any], archived: bool = False, note: str = "") -> Dict[str, Any]:
    study = blank_study(migrated_study_id(name), name, batch.get("created_at") or batch.get("started_at"))
    study["description"] = note or "Migrated from batch '%s' (XDASH_PLAN.md §3.8)." % name
    try:
        study["defaults"]["runtime"] = normalize_runtime(None, batch.get("pool"))
    except ExperimentError:
        pass
    study["archived"] = archived
    study["migrated_from"] = {"batch": name, **{
        k: batch[k] for k in ("pool", "max_retries", "force_on_retry", "paused", "status", "row_count") if k in batch
    }}
    return study


def _legacy_batches() -> Dict[str, Dict[str, Any]]:
    """data/<profile>/batches.json — the retired batch runner's own file
    (XDASH_V2_PLAN.md §3.7). Read tolerantly and never written: nothing has
    used it since that dispatcher was deleted, and its rows
    (assignments.json) were never experiments, so its batches migrate as
    archived, member-less studies — kept, not lost, and out of the way."""
    try:
        raw = json.loads(_store.path.with_name("batches.json").read_text())
    except (OSError, ValueError):
        return {}
    batches = raw.get("batches") if isinstance(raw, dict) else None
    if not isinstance(batches, dict):
        return {}
    return {str(k): v for k, v in batches.items() if isinstance(v, dict)}


def _migrate_v3(data: Dict[str, Any]) -> None:
    experiments = data.setdefault("experiments", {})
    attempts = data.setdefault("attempts", {})
    studies = data.setdefault("studies", {})
    batches = data.pop("batches", None)
    batches = {str(k): v for k, v in batches.items() if isinstance(v, dict)} if isinstance(batches, dict) else {}
    for name, batch in batches.items():
        studies.setdefault(migrated_study_id(name), _study_from_batch(name, batch))
    for name, batch in _legacy_batches().items():
        if migrated_study_id(name) not in studies:
            rows = batch.get("row_count")
            studies[migrated_study_id(name)] = _study_from_batch(name, batch, archived=True, note=(
                "Imported from the retired batch runner's batches.json. Its %s row(s) were assignments, "
                "not experiments, so it has no members." % ("?" if rows is None else rows)
            ))
    for attempt in attempts.values():
        if isinstance(attempt, dict) and attempt.get("status") == "pending":
            attempt["status"] = "queued"
    for eid, experiment in experiments.items():
        if not isinstance(experiment, dict):
            continue
        memberships = _memberships(experiment)
        batch_name = experiment.pop("batch_name", None)
        if batch_name:
            sid = migrated_study_id(batch_name)
            studies.setdefault(sid, _study_from_batch(batch_name, {}))
            if not any(m["study_id"] == sid for m in memberships):
                memberships.append({"study_id": sid, "group": None})
        experiment["studies"] = memberships
        pool = experiment.pop("pool", None)
        if not isinstance(experiment.get("runtime"), dict):
            try:
                experiment["runtime"] = normalize_runtime(None, pool)
            except ExperimentError:
                # A pool no reader ever understood: keep it verbatim next to
                # the default rather than drop it.
                experiment["runtime"] = default_runtime()
                experiment["legacy_pool"] = pool
        experiment.setdefault("name", experiment.get("experiment_id") or eid)
        experiment.setdefault("overlay", {})
        experiment.setdefault("priority", 0)
        experiment.setdefault("notes", "")
        experiment.setdefault("updated_at", experiment.get("created_at"))
    # A paused batch meant "dispatch none of these": autopilot off (every
    # migrated study starts off) plus hold, i.e. its queued members go back
    # to what they were before they were queued (§3.2).
    for name, batch in batches.items():
        if not batch.get("paused"):
            continue
        sid = migrated_study_id(name)
        for experiment in experiments.values():
            if isinstance(experiment, dict) and _is_member(experiment, sid):
                _dequeue_locked(data, experiment)


def _migrate(data: Any, found: int) -> Dict[str, Any]:
    """Runs in memory on every load of an older file; the next save writes
    the result plus a permanent experiments.json.pre-v<N>.bak of the
    original (backend/store.py), so reading never rewrites the user's data.

    v1 -> v2 (Phase 0): `extra_args` becomes {"train", "eval"} (X12). A
    legacy string goes to train only — see framework.normalize_extra_args()
    for why that is the one safe split — minus the seed flag XDash used to
    bake in at creation, which framework.stage_args() now adds to both
    halves at dispatch from the profile's current seed_arg (X1).

    v2 -> v3 (Phase 1): see _migrate_v3() and §3.8."""
    if not isinstance(data, dict):
        return data
    if found < 2:
        for experiment in (data.get("experiments") or {}).values():
            if isinstance(experiment, dict):
                experiment["extra_args"] = framework.normalize_extra_args(
                    experiment.get("extra_args"), experiment.get("seed"),
                )
    if found < 3:
        _migrate_v3(data)
    return data


_store = JsonStore(
    lambda: settings.experiments_store_file, _empty_store,
    schema_version=SCHEMA_VERSION, migrate=_migrate,
)


def _load() -> Dict[str, Any]:
    data = _store.load()
    data.setdefault("experiments", {})
    data.setdefault("attempts", {})
    data.setdefault("studies", {})
    return data


def _save(data: Dict[str, Any]) -> None:
    _store.save(data)


# --------------------------------------------------------------------------- identity
def experiment_id_for(config_path: str, seed: Optional[Any], overlay: Any = None) -> str:
    """`{experiment_name}-s{seed}` (XDASH_V2_PLAN.md §3.1) — the same string
    dissert's own OUTPUT_LAYOUT.md uses as an output directory name. No seed
    -> just the bare experiment_name. A non-empty *overlay* appends
    `-<sha1(canonical overlay)[:6]>` (XDASH_PLAN.md §3.3.1), so an empty
    overlay keeps every pre-Phase-1 id valid and two different overlays can
    never share a record (X8)."""
    name = cfg.get_experiment_name(config_path)
    base = f"{name}-s{seed}" if seed not in (None, "") else name
    digest = framework.overlay_digest(overlay)
    return f"{base}-{digest}" if digest else base


# --------------------------------------------------------------------------- lifecycle + membership
def derived_status(current: Optional[Dict[str, Any]]) -> str:
    """§3.3.2: no attempt -> draft; otherwise the current attempt's status
    (a legacy `pending` reads as `queued`)."""
    if current is None:
        return DRAFT
    status = current.get("status")
    return "queued" if status == "pending" else status


def _memberships(experiment: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {"study_id": str(m["study_id"]), "group": m.get("group")}
        for m in (experiment.get("studies") or []) if isinstance(m, dict) and m.get("study_id")
    ]


def _is_member(experiment: Dict[str, Any], study_id: str) -> bool:
    return any(m["study_id"] == study_id for m in _memberships(experiment))


def _add_membership(experiment: Dict[str, Any], study_id: str, group: Optional[str] = None) -> bool:
    """Adds *experiment* to *study_id* (a group given for an existing
    membership replaces its group). True when anything changed."""
    memberships = _memberships(experiment)
    for m in memberships:
        if m["study_id"] == study_id:
            if group is None or m.get("group") == group:
                return False
            m["group"] = group
            experiment["studies"] = memberships
            return True
    memberships.append({"study_id": study_id, "group": group})
    experiment["studies"] = memberships
    return True


def _remove_membership(experiment: Dict[str, Any], study_id: str) -> bool:
    memberships = _memberships(experiment)
    kept = [m for m in memberships if m["study_id"] != study_id]
    experiment["studies"] = kept
    return len(kept) != len(memberships)


def _normalize_memberships(value: Any, studies: Dict[str, Any]) -> List[Dict[str, Any]]:
    """`studies` as the API accepts it — a list of study ids or of
    {study_id, group} — checked against the studies that exist."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExperimentError("studies must be a list of study ids or {study_id, group} objects")
    out: List[Dict[str, Any]] = []
    for item in value:
        sid, group = (item, None) if isinstance(item, str) else (
            (item.get("study_id"), item.get("group")) if isinstance(item, dict) else (None, None)
        )
        if not sid:
            raise ExperimentError("Every studies entry needs a study_id, got %r" % (item,))
        if sid not in studies:
            raise ExperimentError(f"Unknown study '{sid}'")
        if not any(m["study_id"] == sid for m in out):
            out.append({"study_id": sid, "group": group if group not in ("",) else None})
    return out


# --------------------------------------------------------------------------- runtime policy (§6.3)
# Pool generalizes from a 3-value enum ("either"/"local_only"/"kaggle_only")
# to an allow-list of runner kinds or exact slot ids — "*", ["local", "ssh"],
# ["kaggle:tanvir"]. Since Phase 1 it lives on as `runtime.allow`; a legacy
# `pool` value (in a request, or in an unmigrated record) is read through
# the same normalization.
_LEGACY_POOL_MAP = {"either": ["*"], "local_only": ["local"], "kaggle_only": ["kaggle"]}


def default_runtime() -> Dict[str, Any]:
    return {"mode": "auto", "allow": ["*"], "requires": {"min_vram_gb": None}}


def _validate_pool(pool: Any) -> None:
    # isinstance checks first, deliberately: `pool in _LEGACY_POOL_MAP` below
    # hashes its left operand, which raises on an unhashable list/tuple — so
    # a list must be recognized and returned on before any dict/tuple
    # membership test ever sees it.
    if isinstance(pool, str):
        return  # covers None-like "", "*", every legacy string, and any forward-compat bare kind/slot id
    if pool is None:
        return
    if isinstance(pool, (list, tuple)) and all(isinstance(p, str) for p in pool):
        return
    raise ExperimentError(
        "pool/runtime.allow must be 'either'/'local_only'/'kaggle_only', '*', a runner kind/slot id, "
        f"or a list of them — got {pool!r}"
    )


def _normalize_pool(pool: Any) -> List[str]:
    if pool is None or pool == "":
        return ["*"]
    if isinstance(pool, (list, tuple)):
        out: List[str] = []
        for p in pool:
            for q in _LEGACY_POOL_MAP.get(p, [p]):
                if q not in out:
                    out.append(q)
        return out or ["*"]
    if pool in _LEGACY_POOL_MAP:  # pool is a plain (hashable) string past this point
        return list(_LEGACY_POOL_MAP[pool])
    return [pool]


def normalize_runtime(value: Any = None, pool: Any = None) -> Dict[str, Any]:
    """The runtime policy (§6.3) in its one stored shape:

        {"mode": "auto", "allow": [kind | slot id | "*"], "requires": {"min_vram_gb": float|None}}
        {"mode": "pinned", "slot": "ssh:mclab-gpu2"}

    *value* is a policy dict, or — like *pool*, the pre-Phase-1 field,
    used when *value* is empty — a legacy pool value (a 3-value enum
    string, '*', a kind, a slot id, or a list of those), which becomes an
    auto policy allowing exactly that."""
    if value is None or value == "":
        value = pool
    if value is None or value == "" or isinstance(value, (str, list, tuple)):
        _validate_pool(value)
        return {"mode": "auto", "allow": _normalize_pool(value), "requires": {"min_vram_gb": None}}
    if not isinstance(value, dict):
        raise ExperimentError(f"runtime must be a policy object, got {value!r}")
    mode = str(value.get("mode") or "auto").strip()
    if mode == "pinned":
        slot = str(value.get("slot") or "").strip()
        if not slot:
            raise ExperimentError('A pinned runtime names one slot, e.g. {"mode": "pinned", "slot": "ssh:mclab-gpu2"}')
        return {"mode": "pinned", "slot": slot}
    if mode != "auto":
        raise ExperimentError(f"runtime.mode must be 'auto' or 'pinned', got {mode!r}")
    allow = value.get("allow", "*")
    _validate_pool(allow)
    requires = value.get("requires") or {}
    if not isinstance(requires, dict):
        raise ExperimentError("runtime.requires must be an object, e.g. {\"min_vram_gb\": 24}")
    vram = requires.get("min_vram_gb")
    if vram in (None, "", 0):
        vram = None
    else:
        try:
            vram = float(vram)
        except (TypeError, ValueError):
            raise ExperimentError(f"runtime.requires.min_vram_gb must be a number, got {vram!r}")
        if vram < 0:
            raise ExperimentError("runtime.requires.min_vram_gb can't be negative")
    return {"mode": "auto", "allow": _normalize_pool(allow), "requires": {"min_vram_gb": vram}}


def _runner_allowed(pool_list: List[str], runner: Runner) -> bool:
    return "*" in pool_list or runner.kind in pool_list or runner.id in pool_list


def _experiment_policy(experiment: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return normalize_runtime(experiment.get("runtime"), experiment.get("pool"))
    except ExperimentError:
        return default_runtime()


def _policy_candidates(experiment: Dict[str, Any], runners: List[Runner]) -> Tuple[List[Runner], Optional[Dict[str, Any]]]:
    """The runners *experiment*'s policy lets it use, or ([], why none).
    Pinned is exactly one slot and never falls back to another (§6.3)."""
    policy = _experiment_policy(experiment)
    if policy["mode"] == "pinned":
        pinned = [r for r in runners if r.id == policy["slot"]]
        if not pinned:
            return [], {
                "code": "runtime-missing",
                "detail": "Pinned to '%s', which isn't a configured runtime (a pinned experiment never falls back)"
                          % policy["slot"],
            }
        return pinned, None
    allowed = [r for r in runners if _runner_allowed(policy["allow"], r)]
    if not allowed:
        return [], {"code": "no-account", "detail": "No runtime matches this experiment's allow list %s" % policy["allow"]}
    return allowed, None


def _runner_accelerator(runner: Runner) -> Optional[Dict[str, Any]]:
    fn = getattr(runner, "accelerator", None)
    try:
        acc = fn() if callable(fn) else None
    except Exception:  # noqa: BLE001 — a probe failing means "unknown", never a crashed tick
        acc = None
    return acc if isinstance(acc, dict) else None


def _requirement_block(runner: Runner, experiment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """`requires-unmet` when *runner*'s accelerator doesn't meet the
    policy's `requires` — an unknown accelerator never meets one."""
    need = (_experiment_policy(experiment).get("requires") or {}).get("min_vram_gb")
    if not need:
        return None
    vram = (_runner_accelerator(runner) or {}).get("vram_gb")
    if vram is None:
        return {"code": "requires-unmet",
                "detail": "Needs ≥%g GB VRAM; %s's accelerator is unknown (declare `accelerator` on its host record)"
                          % (need, runner.id)}
    if float(vram) < float(need):
        return {"code": "requires-unmet", "detail": "Needs ≥%g GB VRAM; %s has %g GB" % (need, runner.id, float(vram))}
    return None


def _runner_block(runner: Runner, experiment: Dict[str, Any], est_hours: float) -> Optional[Dict[str, Any]]:
    """None when *runner* could take *experiment* right now; else why not."""
    return _requirement_block(runner, experiment) or runner.can_accept(experiment, est_hours)


# --------------------------------------------------------------------------- experiments (read)
def _attempts_for(data: Dict[str, Any], experiment: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [data["attempts"][aid] for aid in experiment.get("attempt_ids", []) if aid in data["attempts"]]


def _current_attempt(data: Dict[str, Any], experiment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    aid = experiment.get("current_attempt_id")
    return data["attempts"].get(aid) if aid else None


def _status_in(data: Dict[str, Any], experiment: Dict[str, Any]) -> str:
    return derived_status(_current_attempt(data, experiment))


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


def _resolve_attempt_live(attempt: Dict[str, Any]) -> Dict[str, Any]:
    """Attempt as stored, with its `stages`/`raw_status` re-derived by its
    own runner's `poll()` when still in flight — replaces what used to be an
    inline `slot == "local"` vs `unit_ref.get("kernel_slug")` branch here
    with one call through the Runner interface (Multi_runner_XDash.md
    Phase 2). Terminal/pre-dispatch attempts are returned unchanged; there is
    nothing live left to resolve."""
    if attempt["status"] not in IN_FLIGHT_STATUSES:
        return attempt
    try:
        runner = registry.get_runner(attempt.get("slot") or "")
    except KeyError:
        return attempt
    live = runner.poll(attempt)
    if live is None:
        return attempt
    out = dict(attempt)
    out["raw_status"] = live.get("raw_status")
    out["stages"] = live.get("stages") or attempt.get("stages") or []
    return out


def _snapshot(data: Dict[str, Any]) -> Tuple[List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]], Dict[str, Any]]:
    """(experiment, current_attempt) pairs plus the studies — pure dict
    access, safe to call under _lock. Live resolution (which may shell out
    to `kaggle`) must happen only *after* the lock is released — see
    _view_live's docstring."""
    return [(e, _current_attempt(data, e)) for e in data["experiments"].values()], dict(data["studies"])


def _view(experiment: Dict[str, Any], current: Optional[Dict[str, Any]], studies: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **experiment,
        # Membership with each study's display name, for any list that shows it.
        "studies": [{**m, "name": (studies.get(m["study_id"]) or {}).get("name")} for m in _memberships(experiment)],
        "runtime": _experiment_policy(experiment),
        "status": derived_status(current),
        "current_attempt": current,
        "attempt_count": len(experiment.get("attempt_ids", [])),
    }


def _view_live(experiment: Dict[str, Any], current: Optional[Dict[str, Any]], studies: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resolves *current*'s live status/stages (kaggle.py's own `kernels
    status` subprocess can take real wall-clock time) — callers must invoke
    this only after releasing _lock, never while holding it, or a slow
    Kaggle poll stalls every other create/cancel/dispatch call in the
    process."""
    return _view(experiment, _resolve_attempt_live(current) if current else None, studies or {})


def _matches(view: Dict[str, Any], study: Optional[str], status: Optional[str], config: Optional[str],
             slot: Optional[str], runtime: Optional[str], q: Optional[str]) -> bool:
    if study:
        members = {m["study_id"] for m in view["studies"]}
        if (study == "unfiled" and members) or (study != "unfiled" and study not in members):
            return False
    if status and view["status"] != ("queued" if status == "pending" else status):
        return False
    if config and view.get("config_path") != config:
        return False
    placed = (view.get("current_attempt") or {}).get("slot")
    if slot and placed != slot:
        return False
    if runtime and placed != runtime and view["runtime"].get("slot") != runtime:
        return False
    if q:
        needle = q.strip().lower()
        hay = " ".join(str(view.get(k) or "") for k in ("experiment_id", "name", "config_path", "notes")).lower()
        if needle not in hay:
            return False
    return True


def stored_views() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Every experiment's view from the stored state alone (no live runner
    poll — status never needs one), plus the studies: what study summaries
    are built from."""
    with _lock:
        pairs, studies = _snapshot(_load())
    return [_view(e, current, studies) for e, current in pairs], studies


def list_experiments(
    study: Optional[str] = None, status: Optional[str] = None,
    config: Optional[str] = None, slot: Optional[str] = None,
    runtime: Optional[str] = None, q: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """GET /api/experiments?study=&status=&runtime=&config=&q= (§7).
    `study=unfiled` lists experiments in no study; `status=pending` is read
    as `queued`. Filters on the stored state first, then resolves only the
    survivors live."""
    with _lock:
        pairs, studies = _snapshot(_load())
    views = [
        _view_live(e, current, studies) for e, current in pairs
        if _matches(_view(e, current, studies), study, status, config, slot, runtime, q)
    ]
    return sorted(views, key=lambda v: v.get("created_at") or "", reverse=True)


def get_experiment(experiment_id: str) -> Dict[str, Any]:
    with _lock:
        data = _load()
        experiment = data["experiments"].get(experiment_id)
        if experiment is None:
            raise ExperimentError(f"Unknown experiment '{experiment_id}'")
        raw_attempts = list(_attempts_for(data, experiment))
        current_id = experiment.get("current_attempt_id")
        studies = dict(data["studies"])
    attempts = [_resolve_attempt_live(a) for a in raw_attempts]  # outside the lock — see _view_live
    current = next((a for a in attempts if a["attempt_id"] == current_id), None)
    view = _view(experiment, current, studies)
    view["attempts"] = attempts
    if current and current.get("run_id"):
        view["run"] = ledger.get_run(current["run_id"])
    else:
        view["run"] = None
    return view


# --------------------------------------------------------------------------- Experiment page (XDASH_PLAN.md §8.3, Phase 3)
# Three small, read-only additions the Experiment page's Live/Metrics/Artifacts
# tabs need and nothing pre-Phase-3 exposed over HTTP: the per-attempt console
# log (written to settings.attempt_log_dir(), never served before), the eval
# report for whichever attempt's output is actually on disk (studies.py's own
# compare() already solved this exact "which attempt's report" question for a
# study's members — duplicated here in miniature for one experiment, rather
# than importing studies.py, which itself imports this module), and a flat
# listing of that same run dir's files. All three are pure reads; none claims
# or stores anything, and none creates a new store.
MAX_LOG_TAIL_BYTES = 512 * 1024  # plenty for a console log tail; avoids serving a multi-GB file whole
_LOG_STAGE_FILES = ("train", "eval", "kaggle")


def _attempt_or_error(experiment_id: str, attempt_id: Optional[str]) -> Dict[str, Any]:
    with _lock:
        data = _load()
        experiment = data["experiments"].get(experiment_id)
        if experiment is None:
            raise ExperimentError(f"Unknown experiment '{experiment_id}'")
        if attempt_id:
            attempt = data["attempts"].get(attempt_id)
            if attempt is None or attempt.get("experiment_id") != experiment_id:
                raise ExperimentError(f"Unknown attempt '{attempt_id}' for experiment '{experiment_id}'")
        else:
            current_id = experiment.get("current_attempt_id")
            attempt = data["attempts"].get(current_id) if current_id else None
            if attempt is None:
                raise ExperimentError(f"'{experiment_id}' has no attempt yet")
        return dict(attempt)


def get_attempt_log(experiment_id: str, attempt_id: Optional[str] = None, stage: Optional[str] = None) -> Dict[str, Any]:
    """`GET /api/experiments/<id>/log?attempt_id=&stage=train|eval|kaggle`.
    Tails whichever of that attempt's persisted console logs exists
    (data/<profile>/logs/<attempt_id>/{train,eval,kaggle}.log — Phase 0's own
    `log_ref`, a path only, never read back until now). Defaults to the
    experiment's current attempt and whichever stage file exists first."""
    attempt = _attempt_or_error(experiment_id, attempt_id)
    log_dir = settings.attempt_log_dir(attempt["attempt_id"])
    available = [s for s in _LOG_STAGE_FILES if (log_dir / f"{s}.log").is_file()]
    chosen = stage if stage in _LOG_STAGE_FILES else (available[0] if available else "train")
    path = log_dir / f"{chosen}.log"
    text = ""
    exists = path.is_file()
    if exists:
        data = path.read_bytes()
        truncated = len(data) > MAX_LOG_TAIL_BYTES
        if truncated:
            data = data[-MAX_LOG_TAIL_BYTES:]
        text = data.decode("utf-8", errors="replace")
        if truncated:
            text = "…(truncated — showing the tail)…\n" + text
    return {
        "attempt_id": attempt["attempt_id"], "stage": chosen, "available_stages": available,
        "path": settings.display_path(path), "exists": exists, "text": text,
    }


def _run_dir_for_report(attempt: Dict[str, Any]) -> Optional[Path]:
    run = attempt.get("run") or {}
    rel = run.get("collected_dir") or run.get("run_dir")
    return (settings.repo_root / rel) if rel else None


def _eval_report_metrics(run_dir: Path) -> Optional[Dict[str, Any]]:
    """Same resolution studies.py's `_report_metrics` uses (dissert's
    `eval/report.json`, else any `eval/*.json` with a `metrics` object) —
    kept as a short duplicate rather than importing studies.py, which
    imports this module."""
    eval_dir = run_dir / "eval"
    candidates = [eval_dir / "report.json"] + sorted(p for p in eval_dir.glob("*.json") if p.name != "report.json")
    for path in candidates:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        metrics = data.get("metrics") if isinstance(data, dict) else None
        if isinstance(metrics, dict):
            return {"metrics": metrics, "report_path": settings.display_path(path)}
    return None


def get_experiment_report(experiment_id: str) -> Dict[str, Any]:
    """`GET /api/experiments/<id>/report` — the eval metrics of the newest
    *done* attempt whose output actually landed on disk, else null. Every
    attempt is tried newest-first (a later attempt's collected dir may still
    be missing a same-named report an earlier leg wrote)."""
    with _lock:
        data = _load()
        experiment = data["experiments"].get(experiment_id)
        if experiment is None:
            raise ExperimentError(f"Unknown experiment '{experiment_id}'")
        attempts = list(reversed(_attempts_for(data, experiment)))
    for attempt in attempts:
        if attempt.get("status") != "done":
            continue
        run_dir = _run_dir_for_report(attempt)
        if not run_dir:
            continue
        found = _eval_report_metrics(run_dir)
        if found is not None:
            return {"attempt_id": attempt["attempt_id"], **found}
    return {"attempt_id": None, "metrics": None, "report_path": None}


MAX_ARTIFACT_LIST = 500  # a runaway output tree still returns fast and finite


def list_attempt_artifacts(experiment_id: str, attempt_id: Optional[str] = None) -> Dict[str, Any]:
    """`GET /api/experiments/<id>/artifacts` — a flat file listing of
    whichever attempt's run directory is actually on disk (collected, else
    the merely-planned dir). Read-only; the same directory studies.py's
    Compare and this module's own get_experiment_report() read metrics from."""
    attempt = _attempt_or_error(experiment_id, attempt_id)
    run_dir = _run_dir_for_report(attempt)
    files: List[Dict[str, Any]] = []
    if run_dir and run_dir.is_dir():
        for p in sorted(run_dir.rglob("*")):
            if not p.is_file():
                continue
            files.append({
                "name": p.name, "rel_path": p.relative_to(run_dir).as_posix(),
                "size": p.stat().st_size,
            })
            if len(files) >= MAX_ARTIFACT_LIST:
                break
    return {
        "attempt_id": attempt["attempt_id"],
        "run_dir": settings.display_path(run_dir) if run_dir else None,
        "exists": bool(run_dir and run_dir.is_dir()),
        "files": files,
    }


def resolve_attempt_artifact(experiment_id: str, rel_path: str, attempt_id: Optional[str] = None) -> Path:
    """The real path for one file `list_attempt_artifacts()` listed — raises
    `ExperimentError` (400) if *rel_path* would escape the run directory."""
    attempt = _attempt_or_error(experiment_id, attempt_id)
    run_dir = _run_dir_for_report(attempt)
    if not run_dir:
        raise ExperimentError("This attempt has no run directory on record")
    candidate = (run_dir / rel_path).resolve()
    root = run_dir.resolve()
    if root != candidate and root not in candidate.parents:
        raise ExperimentError("Path escapes the run directory")
    if not candidate.is_file():
        raise ExperimentError(f"No such artifact: {rel_path}")
    return candidate


def get_experiment_curves(
    experiment_id: str, attempt_id: Optional[str] = None, tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """`GET /api/experiments/<id>/curves?attempt_id=&tags=a,b` — training
    curves from that attempt's TensorBoard event files (§8.3's Metrics tab,
    finished off in Phase 6; also what Study Compare's overlaid-curves
    section fetches, one call per member experiment). Same run-dir
    resolution as `get_experiment_report()`/`list_attempt_artifacts()`."""
    attempt = _attempt_or_error(experiment_id, attempt_id)
    run_dir = _run_dir_for_report(attempt)
    if not run_dir:
        return {"attempt_id": attempt["attempt_id"], "available": False, "tags": [], "series": {}}
    result = tb_curves.read_scalars(run_dir, tags=tags)
    return {"attempt_id": attempt["attempt_id"], **result}


# --------------------------------------------------------------------------- experiments (write)
def _as_int(value: Any, field: str, minimum: Optional[int] = None) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ExperimentError(f"{field} must be an integer, got {value!r}")
    if minimum is not None and out < minimum:
        raise ExperimentError(f"{field} must be ≥ {minimum}")
    return out


def _check_config(config_path: str) -> None:
    try:
        cfg.read_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        raise ExperimentError(f"Config not found: {config_path} ({e})")


def _check_overlay(config_path: str, overlay: Dict[str, Any], warnings: List[str]) -> None:
    """Validates a non-empty overlay on *config_path* through the
    framework's resolve_config hook (§4.3) — a typo'd key fails here, at
    creation. Outside _lock: it is a bridge subprocess."""
    if not overlay:
        return
    try:
        warning = framework.validate_overlay(config_path, overlay)
    except framework.OverlayError as e:
        raise ExperimentError(str(e))
    if warning:
        warnings.append("%s: %s" % (config_path, warning))


def _new_experiment(
    experiment_id: str, config_path: str, seed: Any, overlay: Dict[str, Any], extra_args: Dict[str, str],
    runtime: Dict[str, Any], priority: int, max_retries: int, force_on_retry: bool, max_legs: int, notes: str = "",
) -> Dict[str, Any]:
    now = _now_iso()
    return {
        "experiment_id": experiment_id, "name": experiment_id,
        "config_path": config_path, "seed": seed, "overlay": dict(overlay),
        "extra_args": dict(extra_args), "studies": [], "runtime": dict(runtime),
        "priority": priority, "max_retries": max_retries, "force_on_retry": force_on_retry,
        "max_legs": max_legs, "notes": notes,
        "created_at": now, "updated_at": now, "attempt_ids": [], "current_attempt_id": None,
    }


def _requested_memberships(
    data: Dict[str, Any], study_id: Optional[str], group: Optional[str], studies: Any, batch_name: Optional[str],
) -> List[Dict[str, Any]]:
    """The memberships a create request asks for. `batch_name` is the
    pre-Phase-1 spelling (the Run Composer still sends it): it names a
    study, created on first use under the same deterministic id a migrated
    batch of that name has. Must be called with _lock held; may add that
    study to *data*."""
    out = _normalize_memberships(studies, data["studies"])
    if study_id:
        if study_id not in data["studies"]:
            raise ExperimentError(f"Unknown study '{study_id}'")
        if not any(m["study_id"] == study_id for m in out):
            out.append({"study_id": study_id, "group": group or None})
    if batch_name:
        existing = next((s for s in data["studies"].values() if s.get("name") == batch_name), None)
        if existing is None:
            existing = blank_study(migrated_study_id(batch_name), batch_name, _now_iso())
            data["studies"][existing["study_id"]] = existing
        if not any(m["study_id"] == existing["study_id"] for m in out):
            out.append({"study_id": existing["study_id"], "group": group or None})
    return out


def create_experiments(
    configs: List[Dict[str, Any]], extra_args: Any = "", runtime: Any = None, pool: Any = None,
    overlay: Any = None, study_id: Optional[str] = None, group: Optional[str] = None, studies: Any = None,
    batch_name: Optional[str] = None, priority: Any = 0, max_retries: Any = 1, force_on_retry: bool = True,
    max_legs: Any = 6, notes: str = "", then: Optional[str] = None, rerun: bool = True,
) -> Dict[str, Any]:
    """`POST /api/experiments` (§7). *configs* is a list of `{"path": str,
    "seeds": [int|None, ...]}`; every (path, seed) pair — with the shared
    *overlay* — is one Experiment.

    Creates **drafts** (X7): nothing is queued, nothing dispatches, unless
    *then* is "queue" or "run_now", which applies that action (§6.1) to
    every experiment this request named, created or matched — with
    *rerun* (default true, the Run Composer's long-standing behaviour) a
    matched experiment that already finished gets a fresh attempt. Drafts
    that join a study with autopilot on are picked up by it (§6.2).

    **Idempotent on identity** (§3.3.1): re-posting an id that exists
    changes nothing about it except adding the requested study memberships
    (which is how one baseline joins a second study), and the response lists
    it under `matched` instead of `created`. Identity includes the overlay,
    so a different overlay is a different experiment (X8).

    A non-empty overlay is validated through the framework's resolve_config
    hook before anything is stored; a rejected key fails the whole request.

    *runtime* is the §6.3 policy; *pool* its legacy spelling. *extra_args*
    ({"train", "eval"}, or a train-only string) is only for flags that are
    not config (e.g. `--max-hours`) — config changes are the overlay.

    Returns {experiments, created, matched, warnings, then}."""
    if then not in (None, "", "queue", "run_now"):
        raise ExperimentError(f"then must be 'queue' or 'run_now', got {then!r}")
    if not configs:
        raise ExperimentError("configs must declare at least one entry")
    policy = normalize_runtime(runtime, pool)
    try:
        flat_overlay = framework.normalize_overlay(overlay)
    except framework.OverlayError as e:
        raise ExperimentError(str(e))
    row_extra_args = framework.normalize_extra_args(extra_args)
    priority = _as_int(priority, "priority")
    max_retries = _as_int(max_retries, "max_retries", 0)
    max_legs = _as_int(max_legs, "max_legs", 1)

    warnings: List[str] = []
    planned: List[Tuple[str, Any, str]] = []
    for entry in configs:
        config_path = (entry.get("path") or "").strip() if isinstance(entry, dict) else ""
        if not config_path:
            raise ExperimentError("Every config entry needs a path")
        _check_config(config_path)
        _check_overlay(config_path, flat_overlay, warnings)
        for seed in entry.get("seeds") or [None]:
            planned.append((config_path, seed, experiment_id_for(config_path, seed, flat_overlay)))

    created: List[str] = []
    matched: List[str] = []
    with _lock:
        data = _load()
        memberships = _requested_memberships(data, study_id, group, studies, batch_name)
        for config_path, seed, eid in planned:
            if eid in created or eid in matched:
                continue
            experiment = data["experiments"].get(eid)
            if experiment is None:
                experiment = _new_experiment(
                    eid, config_path, seed, flat_overlay, row_extra_args, policy,
                    priority, max_retries, bool(force_on_retry), max_legs, notes=str(notes or ""),
                )
                data["experiments"][eid] = experiment
                created.append(eid)
            else:
                matched.append(eid)
            for m in memberships:
                if _add_membership(experiment, m["study_id"], m["group"]):
                    experiment["updated_at"] = _now_iso()
        autopilot = any((data["studies"].get(m["study_id"]) or {}).get("autopilot", {}).get("enabled") for m in memberships)
        _save(data)

    touched = created + matched
    result: Dict[str, Any] = {"created": created, "matched": matched, "warnings": warnings, "then": None}
    if then:
        result["then"] = apply_action(then, ids=touched, params={"rerun": bool(rerun)})
    elif autopilot:
        ensure_dispatcher_started()
        _dispatch_tick()
    result["experiments"] = [get_experiment(eid) for eid in touched]
    return result


_PATCHABLE = _ALWAYS_EDITABLE | _POLICY_FIELDS | _IDENTITY_FIELDS


def _coerce_seed(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ExperimentError(f"seed must be an integer, got {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ExperimentError(f"seed must be an integer, got {value!r}")


def update_experiment(experiment_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    """`PATCH /api/experiments/<id>` under §3.3.2's editability table
    (`_EDITABLE`). Changing identity (overlay, seed, config_path) is a
    draft-only edit and re-keys the draft to its new id — a draft has no
    attempts, logs or outputs that could point at the old one; the response
    carries `renamed_from`. A new id that already exists is refused rather
    than merged. Anything past draft keeps its identity forever: dequeue it,
    or duplicate it (the `duplicate` action)."""
    if not isinstance(patch, dict) or not patch:
        raise ExperimentError("PATCH body must be a non-empty object")
    unknown = set(patch) - _PATCHABLE
    if unknown:
        raise ExperimentError("Not editable: %s (editable: %s)" % (", ".join(sorted(unknown)), ", ".join(sorted(_PATCHABLE))))

    with _lock:
        data = _load()
        experiment = data["experiments"].get(experiment_id)
        if experiment is None:
            raise ExperimentError(f"Unknown experiment '{experiment_id}'")
        status = _status_in(data, experiment)
        snapshot = dict(experiment)
    refused = set(patch) - _EDITABLE.get(status, _ALWAYS_EDITABLE)
    if refused:
        hint = " — dequeue it first" if status in PRE_DISPATCH_STATUSES else (
            " — use the duplicate action to change identity" if status in TERMINAL_STATUSES else ""
        )
        raise ExperimentConflict("Can't change %s while %s%s" % (", ".join(sorted(refused)), status, hint))

    # Identity (and the bridge-validated overlay) is worked out before the
    # write, outside the lock.
    warnings: List[str] = []
    new_config = (patch.get("config_path") or snapshot["config_path"]).strip()
    new_seed = _coerce_seed(patch["seed"]) if "seed" in patch else snapshot.get("seed")
    try:
        new_overlay = framework.normalize_overlay(patch["overlay"] if "overlay" in patch else snapshot.get("overlay"))
    except framework.OverlayError as e:
        raise ExperimentError(str(e))
    new_id = experiment_id
    if _IDENTITY_FIELDS & set(patch):
        if new_config != snapshot["config_path"]:
            _check_config(new_config)
        if "overlay" in patch or new_config != snapshot["config_path"]:
            _check_overlay(new_config, new_overlay, warnings)
        new_id = experiment_id_for(new_config, new_seed, new_overlay)

    with _lock:
        data = _load()
        experiment = data["experiments"].get(experiment_id)
        if experiment is None:
            raise ExperimentError(f"Unknown experiment '{experiment_id}'")
        if _status_in(data, experiment) != status:
            raise ExperimentConflict(f"Experiment '{experiment_id}' changed state meanwhile — reload and retry")
        if "name" in patch:
            name = str(patch["name"] or "").strip()
            if not name:
                raise ExperimentError("name can't be empty")
            experiment["name"] = name
        if "notes" in patch:
            experiment["notes"] = str(patch["notes"] or "")
        if "studies" in patch:
            experiment["studies"] = _normalize_memberships(patch["studies"], data["studies"])
        if "runtime" in patch:
            experiment["runtime"] = normalize_runtime(patch["runtime"])
            experiment.pop("pool", None)
        if "priority" in patch:
            experiment["priority"] = _as_int(patch["priority"], "priority")
        if "max_retries" in patch:
            experiment["max_retries"] = _as_int(patch["max_retries"], "max_retries", 0)
        if "max_legs" in patch:
            experiment["max_legs"] = _as_int(patch["max_legs"], "max_legs", 1)
        if "force_on_retry" in patch:
            experiment["force_on_retry"] = bool(patch["force_on_retry"])
        if "extra_args" in patch:
            experiment["extra_args"] = framework.normalize_extra_args(patch["extra_args"])
        if _IDENTITY_FIELDS & set(patch):
            if new_id != experiment_id and new_id in data["experiments"]:
                raise ExperimentConflict(f"That edit makes it '{new_id}', which already exists")
            experiment.update({"config_path": new_config, "seed": new_seed, "overlay": new_overlay})
            if new_id != experiment_id:
                del data["experiments"][experiment_id]
                if experiment.get("name") == experiment_id:
                    experiment["name"] = new_id
                experiment["experiment_id"] = new_id
                data["experiments"][new_id] = experiment
        experiment["updated_at"] = _now_iso()
        _save(data)
    view = get_experiment(new_id)
    if new_id != experiment_id:
        view["renamed_from"] = experiment_id
    if warnings:
        view["warnings"] = warnings
    return view


def _new_attempt(experiment_id: str, attempt_index: int, queued_by: str = "user") -> Dict[str, Any]:
    return {
        "attempt_id": f"atmpt_{uuid.uuid4().hex[:10]}",
        "experiment_id": experiment_id,
        "attempt_index": attempt_index,
        # Who queued it (§6.4's `manual` ordering key): "user" (queue, run
        # now, retry), "autopilot" (a study promoted a draft), "retry" (an
        # automatic retry after a failure), "chain" (the next resumed leg).
        # Everything but autopilot counts as manual and goes first.
        "queued_by": queued_by,
        "queued_at": _now_iso(),
        # Multi_runner_XDash.md Phase 5 — leg_index counts legs *within* one
        # continuous resumed chain (reset to 1 by every fresh attempt_index,
        # since a manual retry starts over, not a resume); resume_of/resumed_by
        # link a chain's legs both directions. epochs_completed/checkpoint_files
        # are populated only when this leg resolves as interrupted (see
        # _try_open_next_leg) — None otherwise, never guessed at.
        "leg_index": 1,
        "resume_of": None,
        "resumed_by": None,
        "epochs_completed": None,
        "checkpoint_files": None,
        "slot": None,
        "status": "queued",
        "raw_status": None,
        "stages": [],
        "unit_ref": None,
        "started_at": None,
        "ended_at": None,
        "blocked": None,
        "run_id": None,
        # XDASH_PLAN.md §3.4 — filled in at dispatch: `code` {commit, dirty,
        # pushed} (X6), `run` {config_hash, run_dir, run_ids} from the
        # framework's locate_run hook (X2; plus collected_dir etc. once
        # collected), `log_ref` the persisted console logs; `collect` tracks
        # getting the outputs home (X3); `flags`/`warnings` e.g.
        # output-conflict, run-mismatch, code-not-pushed (allowed).
        "code": None,
        "run": None,
        "log_ref": None,
        "collect": None,
        "flags": [],
        "warnings": [],
        "updated_at": _now_iso(),
    }


def _append_attempt(data: Dict[str, Any], experiment: Dict[str, Any], queued_by: str = "user") -> Dict[str, Any]:
    """Must be called with _lock held and *data*/*experiment* the live
    (not copied) dicts, so the caller's own _save(data) persists it."""
    attempt = _new_attempt(experiment["experiment_id"], len(experiment["attempt_ids"]) + 1, queued_by=queued_by)
    data["attempts"][attempt["attempt_id"]] = attempt
    experiment["attempt_ids"].append(attempt["attempt_id"])
    experiment["current_attempt_id"] = attempt["attempt_id"]
    return attempt


def _queue_locked(data: Dict[str, Any], experiment: Dict[str, Any], queued_by: str = "user") -> Dict[str, Any]:
    """Queues *experiment* (draft or terminal): a new queued attempt. The
    one exception is an interrupted leg whose queued continuation was
    dequeued (_dequeue_locked): queueing it again re-opens that
    continuation — the next leg resumes, it doesn't start over."""
    current = _current_attempt(data, experiment)
    if (current is not None and current.get("status") == "done" and current.get("raw_status") == "interrupted"
            and not current.get("resumed_by") and current.get("checkpoint_files")):
        attempt = _append_attempt(data, experiment, queued_by=queued_by)
        attempt.update({
            "leg_index": int(current.get("leg_index") or 1) + 1,
            "resume_of": current["attempt_id"], "run_id": current.get("run_id"),
        })
        current["resumed_by"] = attempt["attempt_id"]
        return attempt
    return _append_attempt(data, experiment, queued_by=queued_by)


def _dequeue_locked(data: Dict[str, Any], experiment: Dict[str, Any]) -> bool:
    """Takes *experiment* out of the queue: its current attempt, if queued
    or blocked (never claimed, so it has no unit, outputs or history worth
    keeping), is removed, and the experiment reads as what it was before —
    a draft, or its previous attempt's terminal status. A queued resume leg
    is removed the same way and its predecessor unlinked, so queueing again
    re-opens it (_queue_locked). False when there was nothing queued."""
    current = _current_attempt(data, experiment)
    if current is None or derived_status(current) not in PRE_DISPATCH_STATUSES:
        return False
    aid = current["attempt_id"]
    data["attempts"].pop(aid, None)
    experiment["attempt_ids"] = [a for a in experiment.get("attempt_ids", []) if a != aid]
    experiment["current_attempt_id"] = experiment["attempt_ids"][-1] if experiment["attempt_ids"] else None
    prev = data["attempts"].get(current.get("resume_of") or "")
    if prev is not None and prev.get("resumed_by") == aid:
        prev["resumed_by"] = None
    return True


def retry_experiment(experiment_id: str) -> Dict[str, Any]:
    """`POST /api/experiments/<id>/retry` — a retry is an Attempt (§3.2),
    never a new Experiment. Refuses while the current attempt is still
    live (the existing one must reach a terminal state first), and for a
    draft, which has nothing to retry — queue it."""
    with _lock:
        data = _load()
        experiment = data["experiments"].get(experiment_id)
        if experiment is None:
            raise ExperimentError(f"Unknown experiment '{experiment_id}'")
        current = data["attempts"].get(experiment.get("current_attempt_id") or "")
        if current is None:
            raise ExperimentConflict(f"Experiment '{experiment_id}' is a draft — queue it instead")
        if current["status"] not in TERMINAL_STATUSES:
            raise ExperimentConflict(f"Experiment '{experiment_id}' already has a live attempt")
        _append_attempt(data, experiment, queued_by="user")
        _save(data)
    ensure_dispatcher_started()
    _dispatch_tick()
    return get_experiment(experiment_id)


def cancel_experiment(experiment_id: str) -> Dict[str, Any]:
    """`POST /api/experiments/<id>/cancel`. Queued/blocked: just marks the
    attempt cancelled (a draft has nothing to cancel and is returned as is). In flight: marks it cancelled, then best-effort asks
    its runner to stop the underlying unit (`Runner.cancel()` — local
    actually stops the tmux session; Kaggle has no cooperative stop, so this
    only stops the dashboard from tracking it further — Kaggle's own
    `kernels status` may keep reporting it running until it finishes or
    times out, matching backend/runners/base.py's documented `stop`/`kill`
    capability gap for Kaggle).

    Collection after a cancel: only a volatile runner's (Colab's) partial
    outputs are still collected — its VM is the only copy, and reap_idle()
    keeps it up until they are home. Everywhere else they are left where
    they are (an SSH host keeps its files; a cancelled Kaggle kernel may
    still be running, so downloading "its" output now could fetch another
    version's)."""
    with _lock:
        data = _load()
        experiment = data["experiments"].get(experiment_id)
        if experiment is None:
            raise ExperimentError(f"Unknown experiment '{experiment_id}'")
        attempt = data["attempts"].get(experiment.get("current_attempt_id") or "")
        if attempt is None or attempt["status"] in TERMINAL_STATUSES:
            return get_experiment(experiment_id)
        try:
            runner = registry.get_runner(attempt.get("slot") or "")
        except KeyError:
            runner = None
        attempt["status"] = "cancelled"
        attempt["ended_at"] = _now_iso()
        attempt["updated_at"] = _now_iso()
        if (attempt.get("collect") or {}).get("state") == "pending":
            if runner is None or not runner.capabilities.volatile:
                attempt["collect"] = {**attempt["collect"], "state": "skipped", "reason": "cancelled"}
        _save(data)
    if runner is not None:
        runner.cancel(attempt)
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

    # Its overlay file (XDash's own, under the host repo's .xdash/) goes too.
    overlay_file = settings.repo_root / framework.overlay_rel_path(experiment_id)
    try:
        overlay_file.unlink()
    except OSError:
        pass
    if remove_results:
        results_dir = (settings.repo_root / kaggle_backend.results_dir_for_experiment(experiment_id)).resolve()
        repo_root = settings.repo_root.resolve()
        if repo_root in results_dir.parents:  # never remove anything outside the repo
            shutil.rmtree(results_dir, ignore_errors=True)
    if remove_ledger:
        for run_id in run_ids:
            ledger.delete_run(run_id)
    return True


# --------------------------------------------------------------------------- capacity
def attempts_for_slot(slot: str) -> List[Dict[str, Any]]:
    """Every Attempt currently on *slot*, regardless of status — the one
    place any runner needs to look at another Attempt's record (its own
    dispatch_priority()'s "last activity" tie-break, e.g.), so it never has
    to reach into this module's storage internals directly."""
    with _lock:
        data = _load()
    return [a for a in data["attempts"].values() if a.get("slot") == slot]


def is_slot_busy(slot: str) -> bool:
    """Is *slot*'s capacity currently claimed by an in-flight Attempt? The
    one occupancy check every 1-slot runner kind (Kaggle today; Colab once
    Phase 4 lands) needs, and the only place that ever reads IN_FLIGHT_STATUSES
    against a slot."""
    return any(a["status"] in IN_FLIGHT_STATUSES for a in attempts_for_slot(slot))


# --------------------------------------------------------------------------- dispatch
def _dispatch_tick() -> None:
    if not _dispatch_lock.acquire(blocking=False):
        return
    try:
        _dispatch_tick_locked()
    finally:
        _dispatch_lock.release()


def _autopilot_promote() -> List[str]:
    """Study autopilot (§6.2): for every non-archived study with autopilot
    on (highest study priority first), promote its drafts to queued in
    priority order while fewer than `max_parallel` of its members are
    active. Active = queued, blocked, dispatching or running — counting the
    queue too is what bounds what can be *in flight* by max_parallel no
    matter how much capacity is free. Drafts added to the study later are
    picked up on a later tick; turning autopilot off just stops this."""
    promoted: List[str] = []
    with _lock:
        data = _load()
        studies = sorted(
            (s for s in data["studies"].values()
             if (s.get("autopilot") or {}).get("enabled") and not s.get("archived")),
            key=lambda s: (-int(s.get("priority") or 0), s.get("created_at") or "", s["study_id"]),
        )
        for study in studies:
            members = [e for e in data["experiments"].values() if _is_member(e, study["study_id"])]
            active = sum(1 for e in members if _status_in(data, e) in (PRE_DISPATCH_STATUSES | IN_FLIGHT_STATUSES))
            room = max(1, int((study.get("autopilot") or {}).get("max_parallel") or 1)) - active
            if room <= 0:
                continue
            drafts = sorted(
                (e for e in members if _status_in(data, e) == DRAFT),
                key=lambda e: (-int(e.get("priority") or 0), e.get("created_at") or "", e["experiment_id"]),
            )
            for experiment in drafts[:room]:
                _queue_locked(data, experiment, queued_by="autopilot")
                promoted.append(experiment["experiment_id"])
        if promoted:
            _save(data)
    return promoted


def _study_priority(experiment: Dict[str, Any], studies: Dict[str, Any]) -> int:
    """An experiment in several studies queues at its highest study's priority."""
    return max((int((studies.get(m["study_id"]) or {}).get("priority") or 0) for m in _memberships(experiment)), default=0)


def _dispatch_tick_locked() -> None:
    """Loops over every registered runner instead of branching on kind — the
    old `("local", None) | ("kaggle", account)` target tuple and its
    dedicated `_claim_and_dispatch_local`/`_claim_and_dispatch_kaggle` pair
    are gone; a runner is a runner regardless of what it's called
    (Multi_runner_XDash.md Phase 2). Adding a kind means registering it in
    backend/runners/registry.py — nothing here changes.

    Phase 1: autopilot promotes first; each experiment's candidates come
    from its runtime policy (§6.3: auto within `allow`, meeting `requires`;
    or exactly its pinned slot, never falling back); and the queue is
    ordered by §6.4's `(-manual, -study.priority, -experiment.priority,
    feasibility_kind_count, -est_hours, created_at)` — the existing greedy
    (constrained first, longest first) with priority in front."""
    _autopilot_promote()
    with _lock:
        data = _load()
        studies = dict(data["studies"])
        pending_pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for experiment in data["experiments"].values():
            attempt = data["attempts"].get(experiment.get("current_attempt_id") or "")
            if attempt is not None and attempt["status"] in PRE_DISPATCH_STATUSES:
                pending_pairs.append((experiment, attempt))
    if not pending_pairs:
        return

    est_cache: Dict[str, Dict[str, Any]] = {}

    def est_for(config_path: str) -> Dict[str, Any]:
        if config_path not in est_cache:
            est_cache[config_path] = estimates.est_hours(config_path)
        return est_cache[config_path]

    runners = registry.list_runners()

    def feasible_kind_count(experiment: Dict[str, Any], est: float) -> int:
        # How many *kinds* (not runner instances) have at least one allowed
        # runner willing to take this experiment right now — used only to
        # order the tick's greedy pass (constrained experiments first), never
        # to pick the actual target.
        candidates, _why = _policy_candidates(experiment, runners)
        return len({r.kind for r in candidates if _runner_block(r, experiment, est) is None})

    order_key: Dict[str, Tuple] = {}
    for e, a in pending_pairs:
        est = est_for(e["config_path"])["hours"]
        order_key[e["experiment_id"]] = (
            -int(a.get("queued_by") != "autopilot"),
            -_study_priority(e, studies),
            -int(e.get("priority") or 0),
            feasible_kind_count(e, est),
            -est,
            e.get("created_at") or "",
        )

    for experiment, attempt in sorted(pending_pairs, key=lambda pair: order_key[pair[0]["experiment_id"]]):
        est = est_for(experiment["config_path"])["hours"]
        candidates, why_none = _policy_candidates(experiment, runners)
        if not candidates:
            _set_blocked(experiment["experiment_id"], attempt["attempt_id"], why_none)
            continue

        eligible: List[Runner] = []
        worst_block: Optional[Dict[str, Any]] = None
        for r in candidates:
            block = _runner_block(r, experiment, est)
            if block is None:
                eligible.append(r)
            elif worst_block is None or _block_severity(block) > _block_severity(worst_block):
                worst_block = block
        if not eligible:
            _set_blocked(
                experiment["experiment_id"], attempt["attempt_id"],
                worst_block or {"code": "pool-busy", "detail": "Nothing free this tick"},
            )
            continue

        chosen = min(eligible, key=lambda r: r.dispatch_priority(experiment, est))
        _claim_and_dispatch(experiment, attempt, chosen)


def _set_blocked(experiment_id: str, attempt_id: str, block: Dict[str, Any]) -> None:
    is_new_reason = False
    with _lock:
        data = _load()
        attempt = data["attempts"].get(attempt_id)
        if attempt is None or attempt["status"] not in PRE_DISPATCH_STATUSES:
            return
        existing = attempt.get("blocked") or {}
        if existing.get("code") != block["code"]:
            is_new_reason = True
            block = {**block, "since": _now_iso()}
        else:
            block = {**existing, **{k: v for k, v in block.items() if k != "since"}}
        attempt["status"] = "blocked"
        attempt["blocked"] = block
        attempt["updated_at"] = _now_iso()
        _save(data)
    # Multi_runner_XDash.md Phase 6 — the one missing trigger the plan's own
    # notification audit flagged: previously nothing ever fired for a
    # dispatch-time block/stall, only for a resolved done/failed attempt.
    # Gated on is_new_reason so re-affirming the same block every ~30s
    # dispatch tick doesn't spam a channel with the identical message —
    # only an actual change (first block, or a different code) notifies.
    if is_new_reason:
        notif.send_all(f"Experiment '{experiment_id}' is blocked: {block.get('code')} — {block.get('detail', '')}")


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


def _get_attempt(attempt_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        data = _load()
    return data["attempts"].get(attempt_id)


def _provenance(fn: Callable[[], Any]) -> Any:
    """Provenance is recorded, never load-bearing for the launch itself: a
    failure computing one field is recorded in its place, and dispatch goes
    on (the attempt must not be stranded in `dispatching`)."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — see docstring
        return {"error": "%s: %s" % (type(e).__name__, str(e)[:300])}


def _claim_and_dispatch(experiment: Dict[str, Any], attempt: Dict[str, Any], runner: Runner) -> None:
    """Replaces the old `_claim_and_dispatch_local`/`_claim_and_dispatch_kaggle`
    pair — claim, then delegate the actual launch to *runner*.dispatch(),
    whatever kind it is."""
    # started_at is set here, at claim time, not only on a confirmed launch —
    # this is also what lets a Kaggle runner's dispatch_priority() tell two
    # never-yet-succeeded accounts apart, so a failing account isn't picked
    # again on every single retry (see _fail_or_retry's docstring).
    claimed = _claim_attempt(
        attempt["attempt_id"], attempt["status"],
        {"status": "dispatching", "slot": runner.id, "started_at": _now_iso()},
    )
    if claimed is None:
        return
    # XDASH_PLAN.md §4.3 — the overlay file goes into the local repo first,
    # whatever the runtime: locate_run below reads it here, and a runner that
    # executes elsewhere carries it over from here (or, Kaggle, embeds it).
    try:
        framework.write_overlay(experiment)
    except (OSError, ValueError) as e:
        _fail_or_retry(experiment["experiment_id"], attempt["attempt_id"], "dispatching", str(e), "overlay-failed")
        return
    # XDASH_PLAN.md §3.4 provenance, recorded before the unit exists so every
    # later reader — the runner's dispatch() (Kaggle pins `code.commit`),
    # collect(), classification — reads it off the attempt instead of
    # re-deriving it: which code (X6), where the framework will write (X2) —
    # located on the exact `--config` train and eval get, the overlay when
    # there is one — and where this attempt's console logs are kept (§3.5).
    provenance = {
        "code": _provenance(lambda: framework.code_state(fresh=True)),
        "run": _provenance(lambda: framework.locate_run(
            experiment["config_path"], experiment.get("seed"), cli_config=framework.cli_config(experiment),
        )),
        "log_ref": _provenance(lambda: settings.display_path(settings.attempt_log_dir(attempt["attempt_id"]))),
    }
    _update_attempt(attempt["attempt_id"], provenance)
    claimed = {**claimed, **provenance}
    try:
        # A resumed leg (Multi_runner_XDash.md Phase 5): seed() first, using
        # the previous leg's own checkpoint_files, and merge whatever it
        # returns (Kaggle: a snapshot_slug) into unit_ref before dispatch()
        # runs — dispatch() itself decides *how* to use that (e.g. attaching
        # the slug, or adding --resume) by reading claimed/attempt directly,
        # never from seed()'s return value alone.
        if claimed.get("resume_of"):
            prev = _get_attempt(claimed["resume_of"])
            checkpoint_files = (prev or {}).get("checkpoint_files") or []
            seed_patch = runner.seed(experiment, claimed, checkpoint_files)
            if seed_patch:
                merged_unit_ref = {**(claimed.get("unit_ref") or {}), **seed_patch}
                _update_attempt(attempt["attempt_id"], {"unit_ref": merged_unit_ref})
                claimed = {**claimed, "unit_ref": merged_unit_ref}
        patch = runner.dispatch(experiment, claimed)
        # `collect: pending` from here until the outputs are home — what
        # keeps a volatile runner's VM up (ColabRunner.reap_idle()).
        _update_attempt(attempt["attempt_id"], {"status": "running", **patch, "collect": {"state": "pending"}})
    except RunnerBlocked as e:
        # Not a failure: the runner refused on a fresh look at launch time
        # for a reason the user fixes (e.g. code-not-pushed). Back to the
        # queue as blocked, slot released, no retry consumed.
        _claim_attempt(attempt["attempt_id"], "dispatching", {
            "status": "blocked", "slot": None, "started_at": None,
            "blocked": {**e.block, "since": _now_iso()},
        })
        notif.send_all(
            f"Experiment '{experiment['experiment_id']}' is blocked: {e.block.get('code')} — {e.block.get('detail', '')}"
        )
    except snapshot.SnapshotError as e:
        _fail_or_retry(experiment["experiment_id"], attempt["attempt_id"], "dispatching", str(e), "snapshot-failed")
    except colab_backend.NoAcceleratorError as e:
        # Provisioned, but Colab granted no GPU/TPU this attempt (Colab-
        # constraints table: "compute units buy a budget, not a GPU") — a
        # different diagnosis than a bare provisioning failure, so it gets
        # its own code (Multi_runner_XDash.md Phase 4).
        _fail_or_retry(experiment["experiment_id"], attempt["attempt_id"], "dispatching", str(e), "no-accelerator")
    except colab_backend.ProvisionError as e:
        _fail_or_retry(experiment["experiment_id"], attempt["attempt_id"], "dispatching", str(e), "provision-failed")
    except transport_mod.TransportError as e:
        # A MachineRunner's push() failed (host went unreachable between
        # can_accept()'s cached check and this dispatch, rsync error, …) —
        # transport.TransportError is a shared, kind-agnostic type (any
        # Transport can raise it), so branching on it here isn't branching on
        # kind. A distinct code from generic dispatch-failed since "the sync
        # failed" is a more specific, more actionable diagnosis than "dispatch
        # failed" (Multi_runner_XDash.md Phase 3).
        _fail_or_retry(experiment["experiment_id"], attempt["attempt_id"], "dispatching", str(e), "sync-failed")
    except Exception as e:
        _fail_or_retry(experiment["experiment_id"], attempt["attempt_id"], "dispatching", str(e), "dispatch-failed")


def _fail_or_retry(
    experiment_id: str, attempt_id: str, expected_status: str, detail: str, code: str,
    raw_status: Optional[str] = None, retry: bool = True, extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Marks *attempt_id* failed, then starts a fresh Attempt if the Experiment
    hasn't used up max_retries yet — a retry is a new Attempt record
    (XDASH_V2_PLAN.md §3.2), never the same attempt silently reset back to
    "queued". The previous version compared a dispatch failure against
    `attempt["attempt_index"]`, a value fixed at creation and never
    incremented, so `attempt_index <= max_retries` was always true and a
    failing push retried forever — invisibly, since it also discarded the
    error on every "will retry" pass (`attempt["blocked"] = None`). Counting
    `len(experiment.attempt_ids)` instead converges after max_retries+1 real
    attempts, and every failed attempt keeps its own visible error in
    history instead of overwriting the same record.

    *retry* False (a diagnosed failure a retry would only repeat, e.g.
    kaggle-secret-missing) fails without opening a new attempt. *extra*
    carries additional block fields (e.g. `action_url`)."""
    patch = {
        "status": "failed", "ended_at": _now_iso(),
        "blocked": {**(extra or {}), "code": code, "detail": detail[:300], "since": _now_iso()},
    }
    if raw_status is not None:
        patch["raw_status"] = raw_status
    claimed = _claim_attempt(attempt_id, expected_status, patch)
    if claimed is None:
        return  # already resolved by someone else (e.g. a concurrent cancel) — nothing to retry
    if not retry:
        return
    with _lock:
        data = _load()
        experiment = data["experiments"].get(experiment_id)
        if experiment is None:
            return
        max_retries = experiment.get("max_retries", 1)
        if len(experiment.get("attempt_ids", [])) <= max_retries:
            _append_attempt(data, experiment, queued_by="retry")
            _save(data)
    _dispatch_tick()


# --------------------------------------------------------------------------- actions (§6.1)
# One endpoint, three scopes: the row button, the selection bar and the
# study header all call apply_action(); only the scope differs, so group
# handling can't drift from individual handling (U3). Every response is
# per id: {ok: [...], skipped: [{id, reason, ...}]}.
ACTIONS = (
    "run_now", "queue", "dequeue", "cancel", "retry", "delete", "set_runtime", "set_priority",
    "add_to_study", "move_to_study", "remove_from_study", "duplicate",
)
_FILTER_KEYS = ("study", "status", "config", "slot", "runtime", "q")


def resolve_scope(
    ids: Optional[List[str]] = None, study_id: Optional[str] = None, filter: Optional[Dict[str, Any]] = None,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """The experiment ids an action applies to — exactly one of *ids*,
    *study_id* (its members) or *filter* (GET /api/experiments' filters) —
    plus a skipped entry for every id that doesn't exist."""
    if sum(x is not None for x in (ids, study_id, filter)) != 1:
        raise ExperimentError("Give exactly one scope: ids, study_id or filter")
    with _lock:
        data = _load()
    if ids is not None:
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ExperimentError("ids must be a list of experiment ids")
        out: List[str] = []
        skipped: List[Dict[str, Any]] = []
        for eid in ids:
            if eid in out or any(s["id"] == eid for s in skipped):
                continue
            if eid in data["experiments"]:
                out.append(eid)
            else:
                skipped.append({"id": eid, "reason": "unknown experiment"})
        return out, skipped
    if study_id is not None:
        if study_id not in data["studies"]:
            raise ExperimentError(f"Unknown study '{study_id}'")
        members = [e for e in data["experiments"].values() if _is_member(e, study_id)]
        return [e["experiment_id"] for e in sorted(members, key=lambda e: e.get("created_at") or "")], []
    if not isinstance(filter, dict):
        raise ExperimentError("filter must be an object with any of: " + ", ".join(_FILTER_KEYS))
    unknown = set(filter) - set(_FILTER_KEYS)
    if unknown:
        raise ExperimentError("Unknown filter key(s): %s" % ", ".join(sorted(unknown)))
    pairs, studies = _snapshot(data)
    return [
        e["experiment_id"] for e, current in pairs
        if _matches(_view(e, current, studies), *(filter.get(k) for k in _FILTER_KEYS))
    ], []


def apply_action(
    action: str, ids: Optional[List[str]] = None, study_id: Optional[str] = None,
    filter: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """`POST /api/experiments/actions {action, ids | study_id | filter,
    params}`. *params* by action:

    - run_now / queue: `rerun` (bool) — also start a fresh attempt on a
      finished experiment (default: skip it; `retry` is the usual verb).
    - delete: `remove_results`, `remove_ledger` (see delete_experiment).
    - set_runtime: `runtime` (§6.3 policy). set_priority: `priority`.
    - add_to_study: `study_id`, `group`. remove_from_study: `study_id`
      (default: the scope's study). move_to_study: `study_id` (target),
      `from_study_id` (default: the scope's study; else every other study),
      `group`.
    - duplicate: any of `config_path`, `seed`, `overlay` (replaces the
      source's), `name` — the copy is a draft with the source's policy,
      priority, extra args and studies."""
    if action not in ACTIONS:
        raise ExperimentError("Unknown action %r (one of: %s)" % (action, ", ".join(ACTIONS)))
    params = params if params is not None else {}
    if not isinstance(params, dict):
        raise ExperimentError("params must be an object")
    scope_ids, skipped = resolve_scope(ids, study_id, filter)
    result: Dict[str, Any] = {"action": action, "ok": [], "skipped": skipped}
    _ACTION_HANDLERS[action](scope_ids, params, result, study_id)
    return result


def _skip(result: Dict[str, Any], eid: str, reason: str, **extra: Any) -> None:
    result["skipped"].append({"id": eid, "reason": reason, **extra})


def _act_queue(ids: List[str], params: Dict[str, Any], result: Dict[str, Any], scope_study: Optional[str]) -> None:
    rerun = bool(params.get("rerun"))
    with _lock:
        data = _load()
        for eid in ids:
            experiment = data["experiments"].get(eid)
            if experiment is None:
                _skip(result, eid, "unknown experiment")
                continue
            status = _status_in(data, experiment)
            if status in PRE_DISPATCH_STATUSES:
                _skip(result, eid, "already queued")
            elif status in IN_FLIGHT_STATUSES:
                _skip(result, eid, f"already {status}")
            elif status in TERMINAL_STATUSES and not rerun:
                _skip(result, eid, f"already {status} — retry it, or queue with rerun")
            else:
                _queue_locked(data, experiment, queued_by="user")
                result["ok"].append(eid)
        if result["ok"]:
            _save(data)
    if result["ok"]:
        ensure_dispatcher_started()
        _dispatch_tick()


def _act_dequeue(ids: List[str], params: Dict[str, Any], result: Dict[str, Any], scope_study: Optional[str]) -> None:
    with _lock:
        data = _load()
        for eid in ids:
            experiment = data["experiments"].get(eid)
            if experiment is not None and _dequeue_locked(data, experiment):
                result["ok"].append(eid)
            else:
                _skip(result, eid, "not queued (%s)" % (_status_in(data, experiment) if experiment else "unknown"))
        if result["ok"]:
            _save(data)


def _act_cancel(ids: List[str], params: Dict[str, Any], result: Dict[str, Any], scope_study: Optional[str]) -> None:
    for eid in ids:
        with _lock:
            data = _load()
            experiment = data["experiments"].get(eid)
            status = _status_in(data, experiment) if experiment else None
        if status is None:
            _skip(result, eid, "unknown experiment")
        elif status == DRAFT:
            _skip(result, eid, "draft — nothing to cancel")
        elif status in TERMINAL_STATUSES:
            _skip(result, eid, f"already {status}")
        else:
            cancel_experiment(eid)
            result["ok"].append(eid)


def _act_retry(ids: List[str], params: Dict[str, Any], result: Dict[str, Any], scope_study: Optional[str]) -> None:
    with _lock:
        data = _load()
        for eid in ids:
            experiment = data["experiments"].get(eid)
            status = _status_in(data, experiment) if experiment else None
            if status is None:
                _skip(result, eid, "unknown experiment")
            elif status == DRAFT:
                _skip(result, eid, "draft — queue it instead")
            elif status not in TERMINAL_STATUSES:
                _skip(result, eid, f"still {status}")
            else:
                _append_attempt(data, experiment, queued_by="user")
                result["ok"].append(eid)
        if result["ok"]:
            _save(data)
    if result["ok"]:
        ensure_dispatcher_started()
        _dispatch_tick()


def _act_delete(ids: List[str], params: Dict[str, Any], result: Dict[str, Any], scope_study: Optional[str]) -> None:
    for eid in ids:
        try:
            deleted = delete_experiment(
                eid, remove_results=bool(params.get("remove_results")), remove_ledger=bool(params.get("remove_ledger")),
            )
        except ExperimentError as e:
            _skip(result, eid, str(e))
            continue
        if deleted:
            result["ok"].append(eid)
        else:
            _skip(result, eid, "unknown experiment")


def _set_field(ids: List[str], field: str, value: Any, result: Dict[str, Any]) -> None:
    """Sets one policy field on every id whose state allows it (_EDITABLE)."""
    with _lock:
        data = _load()
        for eid in ids:
            experiment = data["experiments"].get(eid)
            if experiment is None:
                _skip(result, eid, "unknown experiment")
                continue
            status = _status_in(data, experiment)
            if field not in _EDITABLE.get(status, set()):
                _skip(result, eid, f"can't change {field} while {status}")
                continue
            experiment[field] = value
            experiment.pop("pool", None)
            experiment["updated_at"] = _now_iso()
            result["ok"].append(eid)
        if result["ok"]:
            _save(data)
    if result["ok"]:
        _dispatch_tick()  # a blocked experiment may be placeable under its new policy


def _act_set_runtime(ids: List[str], params: Dict[str, Any], result: Dict[str, Any], scope_study: Optional[str]) -> None:
    if "runtime" not in params:
        raise ExperimentError('set_runtime needs params.runtime, e.g. {"mode": "pinned", "slot": "local"}')
    _set_field(ids, "runtime", normalize_runtime(params["runtime"]), result)


def _act_set_priority(ids: List[str], params: Dict[str, Any], result: Dict[str, Any], scope_study: Optional[str]) -> None:
    if "priority" not in params:
        raise ExperimentError("set_priority needs params.priority")
    _set_field(ids, "priority", _as_int(params["priority"], "priority"), result)


def _target_study(data: Dict[str, Any], study_id: Optional[str], what: str) -> str:
    if not study_id:
        raise ExperimentError(f"{what} needs params.study_id")
    if study_id not in data["studies"]:
        raise ExperimentError(f"Unknown study '{study_id}'")
    return study_id


def _act_add_to_study(ids: List[str], params: Dict[str, Any], result: Dict[str, Any], scope_study: Optional[str]) -> None:
    with _lock:
        data = _load()
        target = _target_study(data, params.get("study_id"), "add_to_study")
        for eid in ids:
            experiment = data["experiments"].get(eid)
            if experiment is None:
                _skip(result, eid, "unknown experiment")
                continue
            if _add_membership(experiment, target, params.get("group") or None):
                experiment["updated_at"] = _now_iso()
            result["ok"].append(eid)
        _save(data)
    _dispatch_tick()  # the target's autopilot may pick new drafts up


def _act_move_to_study(ids: List[str], params: Dict[str, Any], result: Dict[str, Any], scope_study: Optional[str]) -> None:
    """Reassign (§1 U2) = move: out of `from_study_id` (the scope's study
    when the scope is a study; otherwise every study it is in), into the
    target, keeping its group unless one is given."""
    with _lock:
        data = _load()
        target = _target_study(data, params.get("study_id"), "move_to_study")
        source = params.get("from_study_id") or scope_study
        if source and source not in data["studies"]:
            raise ExperimentError(f"Unknown study '{source}'")
        for eid in ids:
            experiment = data["experiments"].get(eid)
            if experiment is None:
                _skip(result, eid, "unknown experiment")
                continue
            memberships = _memberships(experiment)
            if source and not any(m["study_id"] == source for m in memberships):
                _skip(result, eid, f"not in study '{source}'")
                continue
            leaving = [m for m in memberships if m["study_id"] != target and (m["study_id"] == source or not source)]
            group = params.get("group") or next((m.get("group") for m in leaving if m.get("group")), None)
            for m in leaving:
                _remove_membership(experiment, m["study_id"])
            _add_membership(experiment, target, group)
            experiment["updated_at"] = _now_iso()
            result["ok"].append(eid)
        _save(data)
    _dispatch_tick()


def _act_remove_from_study(ids: List[str], params: Dict[str, Any], result: Dict[str, Any], scope_study: Optional[str]) -> None:
    with _lock:
        data = _load()
        target = _target_study(data, params.get("study_id") or scope_study, "remove_from_study")
        for eid in ids:
            experiment = data["experiments"].get(eid)
            if experiment is not None and _remove_membership(experiment, target):
                experiment["updated_at"] = _now_iso()
                result["ok"].append(eid)
            else:
                _skip(result, eid, f"not in study '{target}'")
        if result["ok"]:
            _save(data)


def _act_duplicate(ids: List[str], params: Dict[str, Any], result: Dict[str, Any], scope_study: Optional[str]) -> None:
    """"Duplicate & edit" (§3.3.2): the way to change a non-draft's
    identity. Each copy is a new draft; a copy whose identity already
    exists is skipped (with that id), never merged."""
    result["created"] = {}
    result["warnings"] = []
    with _lock:
        data = _load()
        sources = {eid: dict(data["experiments"][eid]) for eid in ids if eid in data["experiments"]}
    try:
        overlay_param = framework.normalize_overlay(params["overlay"]) if "overlay" in params else None
    except framework.OverlayError as e:
        raise ExperimentError(str(e))
    planned: List[Tuple[str, Dict[str, Any], str]] = []
    validated: Dict[Tuple[str, str], bool] = {}
    for eid in ids:
        src = sources.get(eid)
        if src is None:
            _skip(result, eid, "unknown experiment")
            continue
        config_path = (params.get("config_path") or src["config_path"]).strip()
        seed = _coerce_seed(params["seed"]) if "seed" in params else src.get("seed")
        overlay = overlay_param if overlay_param is not None else framework.normalize_overlay(src.get("overlay"))
        key = (config_path, framework.overlay_digest(overlay))
        if key not in validated:
            if config_path != src["config_path"]:
                _check_config(config_path)
            _check_overlay(config_path, overlay, result["warnings"])
            validated[key] = True
        planned.append((eid, {"config_path": config_path, "seed": seed, "overlay": overlay},
                        experiment_id_for(config_path, seed, overlay)))
    with _lock:
        data = _load()
        for eid, identity, new_id in planned:
            src = data["experiments"].get(eid)
            if src is None:
                _skip(result, eid, "unknown experiment")
                continue
            if new_id in data["experiments"]:
                _skip(result, eid, f"a copy would be '{new_id}', which already exists — change seed, overlay or config",
                      existing=new_id)
                continue
            copy = _new_experiment(
                new_id, identity["config_path"], identity["seed"], identity["overlay"],
                framework.normalize_extra_args(src.get("extra_args")), _experiment_policy(src),
                int(src.get("priority") or 0), int(src.get("max_retries", 1)), bool(src.get("force_on_retry", True)),
                int(src.get("max_legs", 6)),
            )
            if params.get("name"):
                copy["name"] = str(params["name"])
            copy["studies"] = _memberships(src)
            copy["duplicated_from"] = eid
            data["experiments"][new_id] = copy
            result["ok"].append(eid)
            result["created"][eid] = new_id
        if result["ok"]:
            _save(data)
    if result["ok"]:
        _dispatch_tick()  # a copy in an autopilot study is a draft it may promote


# ------------------------------------------------------------------ run now (§6.2)
def _act_run_now(ids: List[str], params: Dict[str, Any], result: Dict[str, Any], scope_study: Optional[str]) -> None:
    result["placed"] = {}
    for eid in ids:
        if not _dispatch_lock.acquire(timeout=_RUN_NOW_LOCK_TIMEOUT):
            _skip(result, eid, "the dispatcher is busy (a tick is mid-launch) — try again")
            continue
        try:
            _run_now_locked(eid, bool(params.get("rerun")), result)
        finally:
            _dispatch_lock.release()


def _run_now_locked(experiment_id: str, rerun: bool, result: Dict[str, Any]) -> None:
    """Run now (§6.2): preflight *experiment_id* against its pinned runtime
    or its auto candidates; if one can take it right now, claim and
    dispatch it synchronously, ahead of the queue. Otherwise report why,
    per runtime (`runtimes`), and change nothing — it never queues
    silently; `can_queue` tells the caller Queue is the fallback. A queued
    experiment keeps its attempt and is simply taken out of turn. Called
    with _dispatch_lock held, so no background tick can claim the same
    capacity in between."""
    with _lock:
        data = _load()
        experiment = data["experiments"].get(experiment_id)
        status = _status_in(data, experiment) if experiment else None
    if experiment is None:
        return _skip(result, experiment_id, "unknown experiment")
    if status in IN_FLIGHT_STATUSES:
        return _skip(result, experiment_id, f"already {status}")
    if status in TERMINAL_STATUSES and not rerun:
        return _skip(result, experiment_id, f"already {status} — retry it, or run now with rerun")

    runners = registry.list_runners()
    est = estimates.est_hours(experiment["config_path"])["hours"]
    candidates, why_none = _policy_candidates(experiment, runners)
    if not candidates:
        return _skip(result, experiment_id, why_none["detail"], code=why_none["code"], runtimes=[], can_queue=False)
    eligible: List[Runner] = []
    refusals: List[Dict[str, Any]] = []
    for r in candidates:
        block = _runner_block(r, experiment, est)
        if block is None:
            eligible.append(r)
        else:
            refusals.append({"runtime": r.id, **block})
    if not eligible:
        return _skip(result, experiment_id, "no runtime can take it right now", runtimes=refusals, can_queue=True)
    chosen = min(eligible, key=lambda r: r.dispatch_priority(experiment, est))

    with _lock:
        data = _load()
        experiment = data["experiments"].get(experiment_id)
        if experiment is None or _status_in(data, experiment) != status:
            return _skip(result, experiment_id, "changed state meanwhile — reload and retry")
        fresh = status not in PRE_DISPATCH_STATUSES
        attempt = _queue_locked(data, experiment, queued_by="user") if fresh else _current_attempt(data, experiment)
        _save(data)
        experiment, attempt = dict(experiment), dict(attempt)
    _claim_and_dispatch(experiment, attempt, chosen)

    after = _get_attempt(attempt["attempt_id"]) or {}
    if after.get("status") == "blocked":
        # The runtime refused on a fresh look at launch (RunnerBlocked, e.g.
        # code-not-pushed). An attempt Run now created is taken back out:
        # nothing is left queued behind the caller's back.
        if fresh:
            with _lock:
                data = _load()
                if experiment_id in data["experiments"]:
                    _dequeue_locked(data, data["experiments"][experiment_id])
                    _save(data)
        block = after.get("blocked") or {}
        return _skip(result, experiment_id, block.get("detail") or "refused at launch",
                     runtimes=[{"runtime": chosen.id, **block}], can_queue=True)
    if after.get("status") == "failed":
        block = after.get("blocked") or {}
        with _lock:
            data = _load()
            exp = data["experiments"].get(experiment_id) or {}
            retried = exp.get("current_attempt_id") not in (None, attempt["attempt_id"])
        return _skip(result, experiment_id, "dispatch failed: %s" % (block.get("detail") or "?"),
                     code=block.get("code"), runtime=chosen.id, retry_queued=retried)
    result["ok"].append(experiment_id)
    result["placed"][experiment_id] = chosen.id


_ACTION_HANDLERS: Dict[str, Callable[..., None]] = {
    "run_now": _act_run_now, "queue": _act_queue, "dequeue": _act_dequeue, "cancel": _act_cancel,
    "retry": _act_retry, "delete": _act_delete, "set_runtime": _act_set_runtime,
    "set_priority": _act_set_priority, "add_to_study": _act_add_to_study,
    "move_to_study": _act_move_to_study, "remove_from_study": _act_remove_from_study,
    "duplicate": _act_duplicate,
}


# --------------------------------------------------------------------------- preflight (§6.5)
def _data_mode(runner: Runner, experiment: Dict[str, Any]) -> Dict[str, Any]:
    """How the runtime would get the experiment's dataset — the real
    resolver (DATASETS_PLAN.md §4.1): backend/datasets.py's
    `plan_delivery_for_config()`, the same one every runner's own
    can_accept() uses, so preflight's matrix and the live block always
    agree."""
    try:
        return datasets_mod.plan_delivery_for_config(experiment["config_path"], runner.id, runner.kind)
    except Exception:  # noqa: BLE001 — a preflight cell never fails the matrix
        return {"strategy": None, "state": "blocked", "code": "dataset-unavailable", "detail": ""}


def preflight(ids: Optional[List[str]] = None, specs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """`POST /api/experiments/preflight {ids | specs}` (§6.5): the
    experiment × runtime matrix every "can this run?" answer renders. A
    spec is a not-yet-created experiment: {config_path, seed, overlay,
    runtime | pool}. Per row: the would-be (or real) id, whether it exists,
    the policy, the estimate with its tier, `best` (what Run now would pick
    right now, or null) and, per runtime, `allowed` (by the policy), `ok`
    (could accept right now) or the block code/detail, and the data mode.
    A pure read: nothing is claimed or stored."""
    if (ids is None) == (specs is None):
        raise ExperimentError("Give ids or specs")
    with _lock:
        data = _load()
    rows_in: List[Tuple[Dict[str, Any], Optional[str]]] = []
    missing: List[str] = []
    if ids is not None:
        for eid in ids:
            experiment = data["experiments"].get(eid)
            if experiment is None:
                missing.append(eid)
            else:
                rows_in.append((dict(experiment), _status_in(data, experiment)))
    else:
        for spec in specs or []:
            if not isinstance(spec, dict):
                raise ExperimentError("Every spec is an object: {config_path, seed, overlay, runtime}")
            config_path = (spec.get("config_path") or spec.get("path") or "").strip()
            _check_config(config_path)
            try:
                overlay = framework.normalize_overlay(spec.get("overlay"))
            except framework.OverlayError as e:
                raise ExperimentError(str(e))
            eid = experiment_id_for(config_path, spec.get("seed"), overlay)
            existing = data["experiments"].get(eid)
            rows_in.append(({
                "experiment_id": eid, "config_path": config_path, "seed": spec.get("seed"), "overlay": overlay,
                "runtime": normalize_runtime(spec.get("runtime"), spec.get("pool")),
            }, _status_in(data, existing) if existing else None))

    runners = registry.list_runners()
    rows = []
    for experiment, status in rows_in:
        estimate = estimates.est_hours(experiment["config_path"])
        candidates, why_none = _policy_candidates(experiment, runners)
        allowed = {r.id for r in candidates}
        cells: Dict[str, Dict[str, Any]] = {}
        for r in runners:
            block = _runner_block(r, experiment, estimate["hours"])
            cell = {"allowed": r.id in allowed, "ok": block is None, "data": _data_mode(r, experiment)}
            if block is not None:
                cell.update({k: block[k] for k in block if k in ("code", "detail", "clears_at", "action")})
            cells[r.id] = cell
        eligible = [r for r in candidates if cells[r.id]["ok"]]
        best = min(eligible, key=lambda r: r.dispatch_priority(experiment, estimate["hours"])).id if eligible else None
        rows.append({
            "experiment_id": experiment["experiment_id"], "config_path": experiment["config_path"],
            "seed": experiment.get("seed"), "overlay": framework.normalize_overlay(experiment.get("overlay")),
            "exists": status is not None, "status": status, "runtime": _experiment_policy(experiment),
            "estimate": estimate, "best": best, "reason": why_none, "cells": cells,
        })
    return {
        "runtimes": [
            {"id": r.id, "kind": r.kind, "label": r.label, "accelerator": _runner_accelerator(r)} for r in runners
        ],
        "rows": rows,
        "missing": missing,
    }


# --------------------------------------------------------------------------- completion hooks
def _spawn(fn: Callable[..., None], *args: Any) -> None:
    """Runs *fn* off the calling thread. A module attribute so the test
    harness can make it synchronous."""
    threading.Thread(target=fn, args=args, daemon=True, name="experiment-resolve").start()


def on_scheduler_item_finished(item_id: str) -> None:
    """Called from scheduler._tick() when a scheduler item reaches a terminal
    state — the fast path for noticing a machine attempt finished, instead of
    waiting for the next 30 s poll. It no longer resolves anything itself
    (XDASH_PLAN.md §6.6): it hands the attempt to the same
    `_poll_and_resolve()` every other kind goes through, so a machine
    attempt is collected, canonicalized and classified exactly like a Kaggle
    one. In a thread of its own: collecting from a remote host is an rsync,
    and scheduler._tick() must not wait on it."""
    with _lock:
        data = _load()
        attempt = next(
            (a for a in data["attempts"].values()
             if item_id in ((a.get("unit_ref") or {}).get("eval_item_id"), (a.get("unit_ref") or {}).get("train_item_id"))),
            None,
        )
    if attempt is None or attempt["status"] != "running":
        return
    _spawn(_poll_and_resolve, attempt)


def _previous_leg_epochs(attempt: Dict[str, Any]) -> Optional[int]:
    prev_id = attempt.get("resume_of")
    if not prev_id:
        return None
    prev = _get_attempt(prev_id)
    return prev.get("epochs_completed") if prev else None


def _try_open_next_leg(attempt: Dict[str, Any], experiment: Dict[str, Any], classification: Dict[str, Any]) -> bool:
    """Attempt-record half of the leg loop (Multi_runner_XDash.md Phase 5c):
    if *classification* (describe_run()'s own verdict) says this leg ended
    interrupted-but-resumable and the loop guards allow it, resolves this
    leg as done-but-continuing and opens a fresh Attempt (leg_index+1,
    resume_of this one, same run_id) for the dispatcher to pick up on its
    next tick. Returns False (opens nothing) when any guard fails — the
    caller then resolves this attempt as a stopped, not finished, failure.

    Guards, carried over from XDASH_V2_PLAN.md §7 (the soundest part of the
    spec Multi_runner_XDash.md otherwise supersedes): not resumable, no
    checkpoint_files, zero epochs_completed, max_legs reached, or no
    progress since the previous leg — each one a real "don't loop forever
    burning compute for nothing" case, not an arbitrary cap."""
    if not classification.get("resumable"):
        return False
    checkpoint_files = classification.get("checkpoint_files") or []
    if not checkpoint_files:
        return False
    epochs_completed = classification.get("epochs_completed") or 0
    if epochs_completed <= 0:
        return False
    leg_index = attempt.get("leg_index", 1)
    max_legs = experiment.get("max_legs", 6)
    if leg_index >= max_legs:
        return False
    prev_epochs = _previous_leg_epochs(attempt)
    if prev_epochs is not None and epochs_completed <= prev_epochs:
        return False  # no progress since the last leg — looping further would just burn compute

    run_id = classification.get("run_id") or attempt.get("run_id")
    with _lock:
        data = _load()
        exp = data["experiments"].get(experiment["experiment_id"])
        cur = data["attempts"].get(attempt["attempt_id"])
        if exp is None or cur is None or cur["status"] != "running":
            return False  # resolved by someone else concurrently (e.g. a cancel)

        cur["status"] = "done"
        cur["ended_at"] = _now_iso()
        cur["raw_status"] = "interrupted"
        cur["epochs_completed"] = epochs_completed
        cur["checkpoint_files"] = checkpoint_files
        cur["run_id"] = run_id
        cur["updated_at"] = _now_iso()

        new_attempt = _new_attempt(exp["experiment_id"], len(exp["attempt_ids"]) + 1, queued_by="chain")
        new_attempt["leg_index"] = leg_index + 1
        new_attempt["resume_of"] = attempt["attempt_id"]
        new_attempt["run_id"] = run_id
        data["attempts"][new_attempt["attempt_id"]] = new_attempt
        exp["attempt_ids"].append(new_attempt["attempt_id"])
        exp["current_attempt_id"] = new_attempt["attempt_id"]
        cur["resumed_by"] = new_attempt["attempt_id"]
        _save(data)

    notif.send_all(
        f"Experiment '{experiment['experiment_id']}' leg {leg_index} interrupted @ "
        f"{epochs_completed} epochs — opening leg {leg_index + 1}/{max_legs}."
    )
    _dispatch_tick()
    return True


def _chain_stop_reason(attempt: Dict[str, Any], experiment: Dict[str, Any], classification: Dict[str, Any]) -> str:
    """Human-readable reason _try_open_next_leg refused, for the blocked
    detail shown on the attempt that stopped the chain — computed
    separately from the guard itself so the guard stays a single early-exit
    pass and this stays purely descriptive."""
    if not classification.get("resumable"):
        return "Manifest reports this leg is not resumable"
    if not classification.get("checkpoint_files"):
        return "No checkpoint files to resume from"
    epochs_completed = classification.get("epochs_completed") or 0
    if epochs_completed <= 0:
        return "No epochs completed this leg"
    leg_index = attempt.get("leg_index", 1)
    max_legs = experiment.get("max_legs", 6)
    if leg_index >= max_legs:
        return f"Reached max_legs ({max_legs})"
    prev_epochs = _previous_leg_epochs(attempt)
    if prev_epochs is not None and epochs_completed <= prev_epochs:
        return f"No progress since the previous leg ({prev_epochs} -> {epochs_completed} epochs)"
    return "Chain stopped"  # guard passed after all (a concurrent resolve) — generic fallback


def _planned_run_id_in_ledger(attempt: Dict[str, Any]) -> Optional[str]:
    """The first run id planned at dispatch (§4.4) that the ledger actually
    has a row for — the join XDash used to guess at by experiment name and
    seed (which matched a *different* config's run whenever two configs
    shared a name, and nothing at all for runs dissert wrote hash-scoped)."""
    planned = (attempt.get("run") or {}).get("run_ids") or []
    if not planned:
        return None
    have = {row.get("run_id") for row in ledger.list_ledger_rows("runs")}
    return next((rid for rid in planned if rid in have), None)


def _resolve_attempt(
    attempt: Dict[str, Any], succeeded: bool, raw_status: str,
    classification: Optional[Dict[str, Any]] = None,
    failure: Optional[Dict[str, Any]] = None,
) -> None:
    with _lock:
        data = _load()
        experiment = data["experiments"].get(attempt["experiment_id"])

    # Multi_runner_XDash.md Phase 5 — a self-limited leg exits 0 exactly like
    # a genuinely finished one (both are "succeeded" from the runner's own
    # poll()), so only classify_run()'s manifest-level verdict can tell them
    # apart. Only reachable when the caller actually classified (currently
    # Kaggle's poll-style path) and got an "interrupted" verdict back.
    if succeeded and classification is not None and classification.get("status") == "interrupted" and experiment is not None:
        if _try_open_next_leg(attempt, experiment, classification):
            return
        _claim_attempt(attempt["attempt_id"], "running", {
            "status": "failed", "ended_at": _now_iso(), "raw_status": raw_status,
            "blocked": {
                "code": "chain-stopped",
                "detail": _chain_stop_reason(attempt, experiment, classification),
                "since": _now_iso(),
            },
        })
        notif.send_all(f"Experiment '{experiment['experiment_id']}' resume chain stopped without finishing.")
        return

    if succeeded:
        run_id = (classification or {}).get("run_id") or _planned_run_id_in_ledger(attempt) or (
            _find_run_id_for(cfg.get_experiment_name(experiment["config_path"]), experiment.get("seed"))
            if experiment is not None and not (attempt.get("run") or {}).get("run_ids") else None
        )
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
    if failure:
        # A runner-diagnosed failure (Runner.diagnose(), e.g. Kaggle's
        # kaggle-secret-missing) — its own code, detail and deep link, and no
        # automatic retry when the runner says one would only repeat it.
        _fail_or_retry(
            experiment["experiment_id"], attempt["attempt_id"], "running",
            failure.get("detail") or failure["code"], failure["code"], raw_status=raw_status,
            retry=bool(failure.get("retry", True)),
            extra={k: v for k, v in failure.items() if k in ("action_url", "action")},
        )
        link = f" Fix: {failure['action_url']}" if failure.get("action_url") else ""
        notif.send_all(f"Experiment '{experiment['experiment_id']}' failed: {failure['code']} — {failure.get('detail', '')}{link}")
        return
    _fail_or_retry(
        experiment["experiment_id"], attempt["attempt_id"], "running",
        f"unit ended: {raw_status}", "attempt-failed", raw_status=raw_status,
    )
    notif.send_all(f"Experiment '{experiment['experiment_id']}' attempt ended: {raw_status}.")


# Attempt ids currently inside _poll_and_resolve()/_collect_attempt(). The
# poll loop, the scheduler hook's thread and startup reconciliation can all
# reach the same attempt at once; collecting it twice would race two rsyncs
# (or downloads) into one staging dir.
_resolving: set = set()
_resolving_lock = threading.Lock()


def _begin_resolving(attempt_id: str) -> bool:
    with _resolving_lock:
        if attempt_id in _resolving:
            return False
        _resolving.add(attempt_id)
        return True


def _end_resolving(attempt_id: str) -> None:
    with _resolving_lock:
        _resolving.discard(attempt_id)


def _attempt_log_texts(attempt_id: str) -> List[str]:
    try:
        log_dir = settings.attempt_log_dir(attempt_id)
    except ValueError:
        return []
    texts = []
    for p in sorted(log_dir.glob("*.log")):
        try:
            texts.append(p.read_text(errors="replace"))
        except OSError:
            continue
    return texts


def _collect_attempt(runner: Runner, attempt: Dict[str, Any]) -> bool:
    """Gets *attempt*'s outputs home (XDASH_PLAN.md §3.5/§6.6): the runner's
    collect() (console logs + the planned run dir into staging), then
    results_ingest.canonicalize() into the local tree and ledger, then the
    §4.4 run-id cross-check. Records the outcome on the attempt: `collect`
    {state: done|empty|legacy|pending|failed, ...}, `run.collected_dir`,
    and the `output-conflict`/`run-mismatch` flags.

    Returns False when collection failed in a way worth retrying (host
    unreachable, download error) and the caller should not resolve yet —
    until _COLLECT_MAX_TRIES for a non-volatile runner; a volatile one
    (Colab) keeps `pending` indefinitely, since its VM holds the only copy."""
    attempt_id = attempt["attempt_id"]
    prior = attempt.get("collect") or {}
    try:
        staging = runner.collect(attempt)
        outcome = results_ingest.canonicalize(attempt, Path(staging) if staging else None)
    except Exception as e:  # noqa: BLE001 — any collection failure is recorded, never raised into the loop
        tries = int(prior.get("tries") or 0) + 1
        give_up = tries >= _COLLECT_MAX_TRIES and not runner.capabilities.volatile
        record = {
            "state": "failed" if give_up else "pending", "tries": tries,
            "error": "%s: %s" % (type(e).__name__, str(e)[:300]), "at": _now_iso(),
        }
        _update_attempt(attempt_id, {"collect": record})
        if tries == _COLLECT_MAX_TRIES:
            held = " Its Colab VM is being kept up until they are." if runner.capabilities.volatile else ""
            notif.send_all(
                f"Couldn't collect the outputs of experiment '{attempt['experiment_id']}' "
                f"({attempt_id}) after {tries} tries: {record['error']}.{held}"
            )
        return give_up

    run = dict(attempt.get("run") or {})
    flags = list(attempt.get("flags") or [])
    if outcome.get("collected_dir"):
        run["collected_dir"] = outcome["collected_dir"]
        run["gpu_hours"] = outcome.get("gpu_hours")
        run["start_time"] = outcome.get("start_time")
    if outcome.get("conflict") and "output-conflict" not in flags:
        flags.append("output-conflict")
    mismatch = framework.run_mismatch(run.get("run_ids") or [], _attempt_log_texts(attempt_id))
    if mismatch is not None:
        run["mismatch"] = mismatch
        if "run-mismatch" not in flags:
            flags.append("run-mismatch")
    record = {
        "state": outcome.get("state"), "at": _now_iso(),
        **{k: outcome[k] for k in ("registered", "conflict") if k in outcome},
    }
    _update_attempt(attempt_id, {"collect": record, "run": run or None, "flags": flags})
    return True


def _classification_root(attempt: Dict[str, Any]) -> Optional[Path]:
    """The one path classification reads (§4.4): the canonical copy this
    attempt's collection landed. Never reconstructed from an experiment id;
    None when there is nothing trustworthy to read — no plan, nothing
    collected, or the logs say the run went somewhere else (run-mismatch)."""
    run = attempt.get("run") or {}
    if run.get("mismatch") or not run.get("collected_dir"):
        return None
    return settings.repo_root / run["collected_dir"]


def _poll_and_resolve(attempt: Dict[str, Any]) -> None:
    """The one completion path for every runner kind (XDASH_PLAN.md §6.6):
    ask *attempt*'s runner whether its unit has finished; if so, collect it
    (_collect_attempt: logs + planned run dir → canonical tree + ledger),
    classify the canonical copy (Multi_runner_XDash.md Phase 5 — done vs.
    interrupted-and-resumable is only answerable from the manifest, never
    from poll() alone), ask the runner to diagnose a failure, and resolve —
    or open the next leg. A collection that failed retryably leaves the
    attempt running; the next poll tick tries again."""
    attempt_id = attempt["attempt_id"]
    if not _begin_resolving(attempt_id):
        return
    try:
        try:
            runner = registry.get_runner(attempt.get("slot") or "")
        except KeyError:
            return
        try:
            live = runner.poll(attempt)
        except Exception:
            return
        if live is None or not live.get("finished"):
            return

        if not _collect_attempt(runner, attempt):
            return
        attempt = _get_attempt(attempt_id) or attempt
        if attempt.get("status") != "running":
            return  # resolved concurrently (e.g. cancelled while collecting)

        succeeded = bool(live.get("succeeded"))
        classification, failure = None, None
        if succeeded:
            root = _classification_root(attempt)
            if root is not None:
                classification = results_ingest.classify_run(root)
        else:
            try:
                failure = runner.diagnose(attempt, live, _attempt_log_texts(attempt_id))
            except Exception:
                failure = None
        _resolve_attempt(attempt, succeeded, live.get("raw_status"), classification, failure)
    finally:
        _end_resolving(attempt_id)


def _collect_stragglers() -> None:
    """Attempts already resolved (cancelled, or a volatile runner's attempt
    whose collection kept failing) whose outputs still need to come home —
    `collect.state == "pending"` on a terminal attempt. Collection only;
    resolution already happened."""
    with _lock:
        data = _load()
        pending = [
            a for a in data["attempts"].values()
            if a["status"] in TERMINAL_STATUSES and (a.get("collect") or {}).get("state") == "pending"
        ]
    for attempt in pending:
        if not _begin_resolving(attempt["attempt_id"]):
            continue
        try:
            try:
                runner = registry.get_runner(attempt.get("slot") or "")
            except KeyError:
                continue
            _collect_attempt(runner, attempt)
        finally:
            _end_resolving(attempt["attempt_id"])


def slot_has_uncollected(slot: str) -> bool:
    """Does *slot* hold an attempt whose outputs aren't home yet — in flight,
    or finished with collection still pending? The hard guard behind
    ColabRunner.reap_idle() (XDASH_PLAN.md §6.6, X3): a Colab VM is the only
    copy of its run until collected, and `colab stop` deletes it."""
    return any(
        a["status"] in ({"dispatching"} | IN_FLIGHT_STATUSES)
        or (a.get("collect") or {}).get("state") == "pending"
        for a in attempts_for_slot(slot)
    )


def _poll_in_flight_attempts() -> None:
    """Every `running` Attempt, regardless of kind — replaces the old
    Kaggle-only `_poll_kaggle_attempts` (local attempts pass through
    _poll_and_resolve() as a no-op; see its docstring)."""
    with _lock:
        data = _load()
        in_flight = [a for a in data["attempts"].values() if a["status"] == "running"]
    for attempt in in_flight:
        _poll_and_resolve(attempt)


def _reap_idle_runners() -> None:
    """Generic loop over every runner's reap_idle() (Multi_runner_XDash.md
    Phase 4) — a no-op for every kind except Colab today, same shape as
    _poll_in_flight_attempts: kind-agnostic here, the one kind that actually
    does something owns that behaviour itself."""
    for r in registry.list_runners():
        try:
            r.reap_idle()
        except Exception:
            pass  # best-effort — see Runner.reap_idle()'s own docstring


# --------------------------------------------------------------------------- reconciliation + poller
def _reconcile_on_startup() -> None:
    """A crash between claiming an attempt and recording its unit_ref leaves
    it "dispatching" with nothing to resolve against; hand it back to the
    pool. A crash after the unit_ref landed is re-resolved against that unit
    via the same generic `_poll_and_resolve` the background loop uses —
    replacing what used to be separate eval_item_id/kernel_slug branches
    here."""
    with _lock:
        data = _load()
        attempts = list(data["attempts"].values())
    for attempt in attempts:
        status = attempt["status"]
        if status not in ({"dispatching"} | IN_FLIGHT_STATUSES):
            continue
        if status == "dispatching":
            # Crashed between the claim and recording the unit. If the runner
            # doesn't recognize a unit on it (poll() -> None — e.g. only a
            # resume seed's snapshot_slug had been recorded), there is nothing
            # to resolve against: back to the pool. Otherwise it is running.
            try:
                live = registry.get_runner(attempt.get("slot") or "").poll(attempt)
            except Exception:
                live = None
            if live is None:
                _update_attempt(attempt["attempt_id"], {"status": "queued", "slot": None})
                continue
            _update_attempt(attempt["attempt_id"], {"status": "running"})
            attempt = _get_attempt(attempt["attempt_id"]) or attempt
        _poll_and_resolve(attempt)


_poller_started = False
_poller_lock = threading.Lock()


def _poll_loop() -> None:
    while True:
        # Each step on its own: a failure in one (e.g. a corrupt store raising
        # StoreCorruptError) must not starve the others, and must never kill
        # the poller.
        for step in (_poll_in_flight_attempts, _collect_stragglers, _dispatch_tick, _reap_idle_runners):
            try:
                step()
            except Exception:
                pass
        time.sleep(30)


def ensure_dispatcher_started() -> None:
    global _poller_started
    with _poller_lock:
        if _poller_started or background_disabled():
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
    plus one entry per Kaggle account (exactly 1 slot, platform-limited).
    Kept as its own dedicated shape (not yet folded into a generic loop over
    Runner.capacity()) since the frontend's Lab/Compute cards read specific
    keys per kind (`paused` for local, `hours_this_week`/`clears_at` for
    Kaggle) that a fully generic merge would rename or lose — Phase 6's
    Compute redesign is where that unification belongs."""
    scheduler_data = scheduler.list_items()
    running_local = sum(1 for i in scheduler_data["items"] if i["status"] == "running")
    slots = [{
        "slot": registry.LOCAL, "kind": "local",
        "used": running_local, "limit": scheduler_data["max_concurrent"],
        "paused": scheduler_data.get("paused", False),
    }]
    for account in kaggle_backend.list_accounts():
        usage = account.get("usage_estimate") or {}
        slot = registry.slot_id("kaggle", account["name"])
        slots.append({
            "slot": slot, "kind": "kaggle", "account": account["name"],
            "used": 1 if is_slot_busy(slot) else 0, "limit": 1,
            "hours_this_week": usage.get("hours_this_week"),
            "weekly_budget_hours": usage.get("weekly_budget_hours"),
            "remaining_hours": usage.get("remaining_hours"),
            "clears_at": (kaggle_backend._utc_week_start() + timedelta(weeks=1)).isoformat(),
        })
    return slots


# --------------------------------------------------------------------------- pulse
def get_pulse() -> Dict[str, Any]:
    """Everything the Lab view polls, in one request (§5) — counts, active
    attempts, slot capacity, blocked list, recent events and (Phase 1) a
    summary of every study."""
    from . import studies as studies_mod  # studies imports this module
    with _lock:
        pairs, studies = _snapshot(_load())
    views = [_view_live(e, current, studies) for e, current in pairs]
    running = [v for v in views if v["status"] in IN_FLIGHT_STATUSES]
    blocked = [v for v in views if v["status"] == "blocked"]
    queued = [v for v in views if v["status"] == "queued"]
    drafts = [v for v in views if v["status"] == DRAFT]
    done = [v for v in views if v["status"] == "done"]
    failed = [v for v in views if v["status"] == "failed"]
    recent = sorted(
        [v for v in views if v["status"] in ("done", "failed")],
        key=lambda v: (v.get("current_attempt") or {}).get("ended_at") or "", reverse=True,
    )[:10]
    return {
        "slots": list_slots(),
        # XDASH_PLAN.md §3.6/X10 — every registered runtime kind (local, every
        # ssh host, every colab account, every kaggle account), not just
        # local + kaggle. `slots` above is kept exactly as it was: it's the
        # capacity-math shape spine.js's Run Composer already reads.
        "runtimes": runtimes_mod.list_runtimes(),
        "running": running,
        "blocked": blocked,
        "queued_count": len(queued),
        "draft_count": len(drafts),
        "done_count": len(done),
        "failed_count": len(failed),
        "recent": recent,
        "studies": studies_mod.summaries(studies, views),
        "generated_at": _now_iso(),
    }
