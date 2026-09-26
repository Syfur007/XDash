"""Training curves from TensorBoard event files (XDASH_PLAN.md §8.2/§8.3).

Phase 3 built the Experiment page's Metrics tab against the eval report only
(`GET /api/experiments/<id>/report`) — nothing before Phase 6 ever read a
run's TensorBoard event files programmatically (`backend/tensorboard_manager.py`
only starts/stops the shared TensorBoard *server* for the iframe view). dissert
writes scalars under `<run_dir>/tensorboard/events.out.tfevents.*` (verified
against a real collected run: `epoch/dice`, `epoch/loss`, `epoch/train_loss`,
etc.) — this module reads them with the `tensorboard` package's own event
accumulator, which is already installed in the `xdash` env.

Used by the Experiment page's Metrics tab (one run's curves) and by Study
Compare's overlaid-curves section (several runs' curves, fetched one per
experiment and drawn on shared axes client-side — see
`static/js/screens/experiments2.js`'s `loadCompareCurves()`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

TB_SUBDIR = "tensorboard"
DEFAULT_MAX_POINTS = 300


def _downsample(points: List[List[float]], max_points: int) -> List[List[float]]:
    if max_points <= 0 or len(points) <= max_points:
        return points
    step = len(points) / max_points
    out = []
    i = 0.0
    while int(i) < len(points):
        out.append(points[int(i)])
        i += step
    if out[-1] != points[-1]:
        out.append(points[-1])
    return out


def list_tags(run_dir: Path) -> List[str]:
    """Every scalar tag recorded under *run_dir*/tensorboard, or `[]` if
    there's no event data (no run yet, run predates TB logging, or the
    `tensorboard` package isn't installed here)."""
    tb_dir = Path(run_dir) / TB_SUBDIR
    if not tb_dir.is_dir():
        return []
    try:
        from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
    except ImportError:
        return []
    try:
        ea = EventAccumulator(str(tb_dir), size_guidance={"scalars": 0})
        ea.Reload()
        return sorted(ea.Tags().get("scalars", []))
    except Exception:
        return []


def read_scalars(
    run_dir: Path, tags: Optional[Iterable[str]] = None, max_points: int = DEFAULT_MAX_POINTS,
) -> Dict[str, Any]:
    """`{available, tags, series: {tag: [[step, value], ...]}}` for *run_dir*.
    `available` is False (with empty `tags`/`series`) when there's no TB
    event data or the `tensorboard` package can't be imported — never an
    exception, since this backs a "nice to have" chart, not a required one."""
    tb_dir = Path(run_dir) / TB_SUBDIR
    if not tb_dir.is_dir():
        return {"available": False, "tags": [], "series": {}}
    try:
        from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
    except ImportError:
        return {"available": False, "tags": [], "series": {}}
    try:
        ea = EventAccumulator(str(tb_dir), size_guidance={"scalars": 0})
        ea.Reload()
        all_tags = sorted(ea.Tags().get("scalars", []))
    except Exception:
        return {"available": False, "tags": [], "series": {}}
    wanted = [t for t in (tags or all_tags) if t in all_tags]
    series: Dict[str, Any] = {}
    for tag in wanted:
        try:
            points = [[e.step, e.value] for e in ea.Scalars(tag)]
        except Exception:
            continue
        series[tag] = _downsample(points, max_points)
    return {"available": True, "tags": all_tags, "series": series}
