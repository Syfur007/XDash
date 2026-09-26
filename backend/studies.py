"""Studies (XDASH_PLAN.md §3.2) — the backend for /api/studies.

A study groups experiments around one question: its primary metric, a
baseline to compare against, defaults that pre-fill the Composer (never
overriding an experiment), a priority, and an autopilot. It replaces the
batch (§3.8): a batch's name became a membership, its `paused` became
"autopilot off plus hold".

- **Membership is many-to-many** and lives on the experiment
  (`studies: [{study_id, group}]`), so one baseline can sit in two studies
  without being run twice. Deleting a study removes memberships only; its
  experiments, their attempts and outputs are untouched.
- **Status is derived** from the members on every read, never stored
  (the rule the batch model already had): `archived`, else `attention` (a
  member failed, or is blocked for a reason other than waiting its turn),
  else `running` (anything queued or in flight), else `planning` (drafts
  left, or no members yet), else `complete`.
- **Autopilot** (§6.2) is set here; the promotion itself runs on every
  dispatch tick (experiments._autopilot_promote).

Studies share experiments.json and its lock with the experiments: every
membership change is one atomic write with the experiment it touches.
"""
from __future__ import annotations

import json
import statistics
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import estimates
from . import experiments as ex
from . import framework
from .config import settings
from .reports import LOWER_IS_BETTER

StudyError = ex.ExperimentError

_EDITABLE = ("name", "question", "description", "tags", "primary_metric", "baseline", "defaults",
             "autopilot", "priority", "archived")
_COUNT_KEYS = ("draft", "queued", "blocked", "running", "done", "failed", "cancelled")


# --------------------------------------------------------------------------- derived status
def _is_attention(view: Dict[str, Any]) -> bool:
    if view["status"] == "failed":
        return True
    if view["status"] != "blocked":
        return False
    # Waiting for a free slot is the queue working, not something to fix.
    return ((view.get("current_attempt") or {}).get("blocked") or {}).get("code") != "pool-busy"


def study_status(study: Dict[str, Any], members: List[Dict[str, Any]]) -> str:
    if study.get("archived"):
        return "archived"
    statuses = [m["status"] for m in members]
    if any(_is_attention(m) for m in members):
        return "attention"
    if any(s in ex.PRE_DISPATCH_STATUSES or s in ex.IN_FLIGHT_STATUSES for s in statuses):
        return "running"
    if not members or ex.DRAFT in statuses:
        return "planning"
    return "complete"


def _member_run_dir(member: Dict[str, Any]) -> Optional[Path]:
    run = (member.get("current_attempt") or {}).get("run") or {}
    rel = run.get("collected_dir") or run.get("run_dir")
    return (settings.repo_root / rel) if rel else None


def _best_metric(study: Dict[str, Any], members: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Lab's "best dice .871 mk_t s42" (XDASH_PLAN.md §8.1) — the study's own
    `primary_metric`, else the profile default, read from each *done*
    member's newest collected eval report. Capped at the first 50 members so
    a large study can't turn its Lab card into an unbounded disk scan on
    every /api/pulse tick (matches _report_metrics' existing per-run cost —
    a handful of small JSON reads, not a training-scale operation)."""
    metric = (study.get("primary_metric") or {}).get("key") or settings.metrics_primary
    if not metric:
        return None
    lower_is_better = metric in LOWER_IS_BETTER
    best: Optional[Dict[str, Any]] = None
    for m in members[:50]:
        if m["status"] != "done":
            continue
        run_dir = _member_run_dir(m)
        if run_dir is None:
            continue
        report = _report_metrics(run_dir)
        if not report or metric not in report:
            continue
        value = report[metric]
        if best is None or (value < best["value"] if lower_is_better else value > best["value"]):
            best = {"key": metric, "value": value, "experiment_id": m["experiment_id"]}
    return best


def _eta(members: List[Dict[str, Any]]) -> Optional[str]:
    """A minimal, honest ETA (XDASH_PLAN.md §8.1's "if computable"): the
    latest projected finish time among this study's *currently running*
    members (`started_at` + a fresh `estimates.est_hours` for that member's
    own config), or None the instant that can't be computed — any running
    member missing a `started_at`, or nothing running at all — never a
    number dressed up from a guess. Deliberately does not project queued or
    draft members' future dispatch order (that is the scheduler's call, not
    this card's), so it under-reports whenever work is still queued behind
    what's running now — the same "if computable" degrade the plan asks
    for, not a wrong answer dressed up as a real one."""
    running = [m for m in members if m["status"] in ex.IN_FLIGHT_STATUSES]
    if not running:
        return None
    latest = None
    for m in running:
        started_at = (m.get("current_attempt") or {}).get("started_at")
        if not started_at:
            return None
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        hours = estimates.est_hours(m["config_path"])["hours"]
        finish = started + timedelta(hours=hours)
        if latest is None or finish > latest:
            latest = finish
    return latest.isoformat() if latest else None


def _summary(study: Dict[str, Any], members: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {k: 0 for k in _COUNT_KEYS}
    for m in members:
        key = "running" if m["status"] in ex.IN_FLIGHT_STATUSES else m["status"]
        counts[key] = counts.get(key, 0) + 1
    return {
        **study,
        "status": study_status(study, members),
        "counts": counts,
        "experiment_count": len(members),
        "attention": [m["experiment_id"] for m in members if _is_attention(m)],
        "best_metric": _best_metric(study, members),
        "eta": _eta(members),
    }


def summaries(studies: Dict[str, Any], views: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every study's summary from already-built experiment views (pulse)."""
    by_study: Dict[str, List[Dict[str, Any]]] = {}
    for v in views:
        for m in v.get("studies") or []:
            by_study.setdefault(m["study_id"], []).append(v)
    out = [_summary(s, by_study.get(sid, [])) for sid, s in studies.items()]
    return sorted(out, key=lambda s: (bool(s.get("archived")), -int(s.get("priority") or 0), s.get("created_at") or ""))


# --------------------------------------------------------------------------- read
def list_studies(include_archived: bool = True) -> List[Dict[str, Any]]:
    views, studies = ex.stored_views()
    out = summaries(studies, views)
    return out if include_archived else [s for s in out if not s.get("archived")]


def get_study(study_id: str) -> Dict[str, Any]:
    with ex._lock:
        study = ex._load()["studies"].get(study_id)
    if study is None:
        raise StudyError(f"Unknown study '{study_id}'")
    members = ex.list_experiments(study=study_id)
    view = _summary(study, members)
    view["experiments"] = members
    return view


# --------------------------------------------------------------------------- write
def _clean(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Validates and normalizes the editable §3.2 fields present in *fields*."""
    out: Dict[str, Any] = {}
    for key, value in fields.items():
        if key == "name":
            name = str(value or "").strip()
            if not name:
                raise StudyError("A study needs a name")
            out[key] = name
        elif key in ("question", "description"):
            out[key] = str(value or "")
        elif key == "tags":
            if value is None:
                value = []
            if not isinstance(value, list):
                raise StudyError("tags must be a list of strings")
            out[key] = [str(t).strip() for t in value if str(t).strip()]
        elif key == "primary_metric":
            if value in (None, "", {}):
                out[key] = None
                continue
            if isinstance(value, str):
                value = {"key": value}
            if not isinstance(value, dict) or not str(value.get("key") or "").strip():
                raise StudyError('primary_metric is {"key": "dice", "direction": "max"|"min"}')
            metric = str(value["key"]).strip()
            direction = value.get("direction") or ("min" if metric in LOWER_IS_BETTER else "max")
            if direction not in ("max", "min"):
                raise StudyError("primary_metric.direction must be 'max' or 'min'")
            out[key] = {"key": metric, "direction": direction}
        elif key == "baseline":
            if value in (None, "", {}):
                out[key] = None
                continue
            if isinstance(value, str):
                value = {"config_path": value}
            if not isinstance(value, dict) or not (value.get("config_path") or value.get("experiment_id")):
                raise StudyError('baseline is {"config_path": ...} (or {"experiment_id": ...})')
            out[key] = {k: value[k] for k in ("config_path", "experiment_id", "overlay") if value.get(k) is not None}
        elif key == "defaults":
            value = value or {}
            if not isinstance(value, dict):
                raise StudyError("defaults must be an object: {seeds, overlay, runtime}")
            seeds = value.get("seeds") or []
            if not isinstance(seeds, list):
                raise StudyError("defaults.seeds must be a list")
            try:
                overlay = framework.normalize_overlay(value.get("overlay"))
            except framework.OverlayError as e:
                raise StudyError(str(e))
            out[key] = {"seeds": seeds, "overlay": overlay, "runtime": ex.normalize_runtime(value.get("runtime"))}
        elif key == "autopilot":
            value = value or {}
            if not isinstance(value, dict):
                raise StudyError("autopilot must be an object: {enabled, max_parallel}")
            out[key] = {
                "enabled": bool(value.get("enabled", False)),
                "max_parallel": ex._as_int(value.get("max_parallel", 2), "autopilot.max_parallel", 1),
            }
        elif key == "priority":
            out[key] = ex._as_int(value, "priority")
        elif key == "archived":
            out[key] = bool(value)
    return out


def create_study(body: Dict[str, Any]) -> Dict[str, Any]:
    """`POST /api/studies`. Only `name` is required."""
    if not isinstance(body, dict):
        raise StudyError("Body must be an object")
    unknown = set(body) - set(_EDITABLE)
    if unknown:
        raise StudyError("Unknown study field(s): %s" % ", ".join(sorted(unknown)))
    fields = _clean({"name": body.get("name"), **body})
    now = ex._now_iso()
    with ex._lock:
        data = ex._load()
        study_id = "st_%s" % uuid.uuid4().hex[:6]
        while study_id in data["studies"]:
            study_id = "st_%s" % uuid.uuid4().hex[:6]
        study = ex.blank_study(study_id, fields["name"], now)
        study.update(fields)
        data["studies"][study_id] = study
        ex._save(data)
    if study["autopilot"]["enabled"]:
        ex._dispatch_tick()
    return get_study(study_id)


def update_study(study_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    """`PATCH /api/studies/<id>`: any editable field; `study_id` never
    changes. An autopilot change here is exactly POST …/autopilot without
    `hold`."""
    if not isinstance(patch, dict) or not patch:
        raise StudyError("PATCH body must be a non-empty object")
    unknown = set(patch) - set(_EDITABLE)
    if unknown:
        raise StudyError("Not editable: %s" % ", ".join(sorted(unknown)))
    fields = _clean(patch)
    with ex._lock:
        data = ex._load()
        study = data["studies"].get(study_id)
        if study is None:
            raise StudyError(f"Unknown study '{study_id}'")
        study.update(fields)
        study["updated_at"] = ex._now_iso()
        ex._save(data)
    ex._dispatch_tick()
    return get_study(study_id)


def delete_study(study_id: str) -> Dict[str, Any]:
    """`DELETE /api/studies/<id>`: the study and every membership in it.
    The experiments themselves (attempts, outputs, other memberships) are
    untouched."""
    with ex._lock:
        data = ex._load()
        if study_id not in data["studies"]:
            raise StudyError(f"Unknown study '{study_id}'")
        removed = []
        for experiment in data["experiments"].values():
            if ex._remove_membership(experiment, study_id):
                removed.append(experiment["experiment_id"])
        del data["studies"][study_id]
        ex._save(data)
    return {"deleted": study_id, "memberships_removed": removed}


def set_autopilot(
    study_id: str, enabled: Optional[bool] = None, max_parallel: Any = None, hold: bool = False,
) -> Dict[str, Any]:
    """`POST /api/studies/<id>/autopilot {enabled, max_parallel, hold}`
    (§6.2). On: the study's drafts are promoted in priority order, never
    more than max_parallel members active. Off: promotion stops; queued
    members are left alone. `hold: true` means off *and* its queued or
    blocked members are dequeued (back to draft, or to their previous
    status) — what a paused batch migrated to."""
    if hold:
        enabled = False
    dequeued: List[str] = []
    with ex._lock:
        data = ex._load()
        study = data["studies"].get(study_id)
        if study is None:
            raise StudyError(f"Unknown study '{study_id}'")
        autopilot = dict(study.get("autopilot") or {"enabled": False, "max_parallel": 2})
        if enabled is not None:
            autopilot["enabled"] = bool(enabled)
        if max_parallel is not None:
            autopilot["max_parallel"] = ex._as_int(max_parallel, "max_parallel", 1)
        study["autopilot"] = autopilot
        study["updated_at"] = ex._now_iso()
        if hold:
            for experiment in data["experiments"].values():
                if ex._is_member(experiment, study_id) and ex._dequeue_locked(data, experiment):
                    dequeued.append(experiment["experiment_id"])
        ex._save(data)
    if autopilot.get("enabled"):
        ex.ensure_dispatcher_started()
        ex._dispatch_tick()
    view = get_study(study_id)
    view["dequeued"] = dequeued
    return view


# --------------------------------------------------------------------------- compare (minimal)
def _report_metrics(run_dir: Path) -> Optional[Dict[str, float]]:
    """The numeric metrics of the eval report under *run_dir* (dissert:
    `eval/report.json`; any `eval/*.json` with a `metrics` object)."""
    eval_dir = run_dir / "eval"
    candidates = [eval_dir / "report.json"] + sorted(p for p in eval_dir.glob("*.json") if p.name != "report.json")
    for path in candidates:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        metrics = data.get("metrics") if isinstance(data, dict) else None
        if isinstance(metrics, dict):
            return {k: float(v) for k, v in metrics.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return None


def _latest_report(data: Dict[str, Any], experiment: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """From the newest finished attempt whose run is on local disk: the
    canonical copy collection landed (§3.5), else the planned run dir."""
    for attempt in reversed(ex._attempts_for(data, experiment)):
        if attempt.get("status") != "done":
            continue
        run = attempt.get("run") or {}
        rel = run.get("collected_dir") or run.get("run_dir")
        if rel:
            metrics = _report_metrics(settings.repo_root / rel)
            if metrics is not None:
                return metrics
    return None


def _group_key(experiment: Dict[str, Any], study_id: str, group_by: str) -> str:
    if group_by == "experiment":
        return experiment["experiment_id"]
    if group_by == "group":
        return next((m.get("group") for m in ex._memberships(experiment) if m["study_id"] == study_id), None) or "(no group)"
    digest = framework.overlay_digest(experiment.get("overlay"))
    return experiment["config_path"] + ("#" + digest if digest else "")


def _group_key_adhoc(experiment: Dict[str, Any], group_by: str) -> str:
    if group_by == "experiment":
        return experiment["experiment_id"]
    if group_by == "group":
        # No single study to read a membership group from an ad-hoc list —
        # falls back to config, same as an experiment with no group set.
        pass
    digest = framework.overlay_digest(experiment.get("overlay"))
    return experiment["config_path"] + ("#" + digest if digest else "")


def _compare_rows(
    rows: List[Any], *, metrics: Optional[List[str]], group_by: str, primary_metric: Optional[Dict[str, Any]],
    baseline_config_path: Optional[str], group_key_fn,
) -> Dict[str, Any]:
    """Shared aggregation for `compare()` (study membership) and
    `compare_ids()` (an arbitrary id list — the ad-hoc "Compare selected"
    action from any bulk-bar selection, §8.2). Both produce the identical
    response shape; only how *rows* and the group key are gathered differs."""
    groups: Dict[str, Dict[str, Any]] = {}
    seen_metrics: List[str] = []
    for experiment, report in sorted(rows, key=lambda r: (r[0]["config_path"], str(r[0].get("seed")))):
        key = group_key_fn(experiment)
        g = groups.setdefault(key, {
            "key": key, "config_path": experiment["config_path"],
            "overlay": framework.normalize_overlay(experiment.get("overlay")),
            "experiments": [], "with_report": 0, "values": {},
        })
        g["experiments"].append(experiment["experiment_id"])
        if report is None:
            continue
        g["with_report"] += 1
        for metric, value in report.items():
            if metric not in seen_metrics:
                seen_metrics.append(metric)
            g["values"].setdefault(metric, []).append(
                {"experiment_id": experiment["experiment_id"], "seed": experiment.get("seed"), "value": value}
            )
    primary = (primary_metric or {}).get("key")
    wanted = [m for m in (metrics or seen_metrics)]
    if primary and primary in wanted:
        wanted.remove(primary)
        wanted.insert(0, primary)
    out_groups = []
    for g in groups.values():
        stats: Dict[str, Any] = {}
        for metric in wanted:
            vals = [v["value"] for v in g["values"].get(metric, [])]
            stats[metric] = {
                "mean": statistics.fmean(vals) if vals else None,
                "std": statistics.stdev(vals) if len(vals) > 1 else None,
                "n": len(vals), "values": g["values"].get(metric, []),
            }
        out_groups.append({
            "key": g["key"], "config_path": g["config_path"], "overlay": g["overlay"],
            "experiments": g["experiments"], "n": len(g["experiments"]), "with_report": g["with_report"],
            "baseline": bool(baseline_config_path) and g["config_path"] == baseline_config_path and not g["overlay"],
            "metrics": stats,
        })
    base = next((g for g in out_groups if g["baseline"]), None)
    if base is not None:
        for g in out_groups:
            for metric, s in g["metrics"].items():
                b = base["metrics"].get(metric, {}).get("mean")
                s["delta_vs_baseline"] = (s["mean"] - b) if s["mean"] is not None and b is not None else None
    return {
        "group_by": group_by, "primary_metric": primary_metric,
        "lower_is_better": sorted(m for m in wanted if m in LOWER_IS_BETTER),
        "metrics": wanted, "groups": out_groups,
    }


def compare(study_id: str, metrics: Optional[List[str]] = None, group_by: str = "config") -> Dict[str, Any]:
    """`GET /api/studies/<id>/compare?metrics=&group_by=config` — members
    grouped by config + overlay (so seeds aggregate), `experiment`, or
    membership `group`; per group and metric: mean, sample std, n and the
    per-seed values, read from each member's eval report. With a baseline
    config, every group gets its delta vs the baseline group's mean."""
    if group_by not in ("config", "experiment", "group"):
        raise StudyError("group_by must be config, experiment or group")
    with ex._lock:
        data = ex._load()
        study = data["studies"].get(study_id)
        if study is None:
            raise StudyError(f"Unknown study '{study_id}'")
        members = [e for e in data["experiments"].values() if ex._is_member(e, study_id)]
        rows = [(e, _latest_report(data, e)) for e in members]
    baseline_cfg = (study.get("baseline") or {}).get("config_path")
    out = _compare_rows(
        rows, metrics=metrics, group_by=group_by, primary_metric=study.get("primary_metric"),
        baseline_config_path=baseline_cfg, group_key_fn=lambda e: _group_key(e, study_id, group_by),
    )
    out["study_id"] = study_id
    return out


def compare_ids(
    ids: List[str], metrics: Optional[List[str]] = None, group_by: str = "config",
    baseline_config_path: Optional[str] = None,
) -> Dict[str, Any]:
    """`POST /api/experiments/compare {ids, metrics?, group_by?,
    baseline_config_path?}` — the ad-hoc twin of `compare()`: any bulk-bar
    selection, from any study (or none at all — "Unfiled"), compared
    exactly like a study's members. `group_by="group"` isn't meaningful
    without a study membership, so it degrades to `config` (documented in
    `_group_key_adhoc`)."""
    if group_by not in ("config", "experiment", "group"):
        raise StudyError("group_by must be config, experiment or group")
    with ex._lock:
        data = ex._load()
        members = []
        missing = []
        for eid in ids:
            e = data["experiments"].get(eid)
            (members if e is not None else missing).append(e if e is not None else eid)
        rows = [(e, _latest_report(data, e)) for e in members]
    out = _compare_rows(
        rows, metrics=metrics, group_by=group_by, primary_metric=None,
        baseline_config_path=baseline_config_path, group_key_fn=lambda e: _group_key_adhoc(e, group_by),
    )
    out["missing_ids"] = missing
    return out
