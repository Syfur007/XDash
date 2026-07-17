"""Discover and read evaluation report JSON files (written by eval.py) from
the logs/ directory, and support comparing several of them at once.

A file is treated as a report if it parses as JSON and has a "metrics" key —
we don't rely on a strict filename convention beyond that, since the exact
suffix (`_report.json`, `_ensemble_report.json`, ...) can vary.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import settings

# Metrics where a HIGHER value is better vs. LOWER is better — used only to
# decide which cell to highlight when comparing reports; purely cosmetic.
HIGHER_IS_BETTER = {"dice", "miou", "precision", "recall", "specificity", "f2", "accuracy", "throughput_fps"}
LOWER_IS_BETTER = {"hd95", "asd", "mean_ms", "median_ms", "std_ms", "p95_ms", "params_m", "flops"}


def _iter_report_files():
    if not settings.logs_dir.exists():
        return
    for p in sorted(settings.logs_dir.rglob("*_report.json")):
        if p.is_file():
            yield p


def _relpath(p: Path) -> str:
    return p.relative_to(settings.logs_dir).as_posix()


def _resolve(rel_path: str) -> Path:
    candidate = (settings.logs_dir / rel_path).resolve()
    if settings.logs_dir.resolve() not in candidate.parents and candidate != settings.logs_dir.resolve():
        raise ValueError("Path escapes logs directory")
    return candidate


def _safe_load(p: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    if not isinstance(data, dict) or "metrics" not in data:
        return None
    return data


def list_reports() -> List[Dict[str, Any]]:
    """Reports grouped by category (top-level sub-directory under logs/),
    mirroring how configs are grouped."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for p in _iter_report_files():
        data = _safe_load(p)
        if data is None:
            continue
        rel = _relpath(p)
        parts = rel.split("/")
        category = parts[0] if len(parts) > 1 else "general"
        metrics = data.get("metrics", {}) or {}
        groups.setdefault(category, []).append({
            "path": rel,
            "name": p.name,
            "category": category,
            "experiment": data.get("experiment"),
            "timestamp": data.get("timestamp"),
            "is_ensemble": data.get("is_ensemble"),
            "is_multiclass": data.get("is_multiclass"),
            "dice": metrics.get("dice"),
            "miou": metrics.get("miou"),
            "model_name": (data.get("model") or {}).get("name"),
        })
    return [
        {"category": cat, "reports": sorted(items, key=lambda r: r.get("timestamp") or "", reverse=True)}
        for cat, items in sorted(groups.items())
    ]


def find_latest_report_for_experiment(experiment_name: str) -> Optional[Dict[str, Any]]:
    """Best-effort lookup used by the terminals feature to decide whether an
    experiment reached completion."""
    best = None
    for p in _iter_report_files():
        data = _safe_load(p)
        if not data or data.get("experiment") != experiment_name:
            continue
        if best is None or (data.get("timestamp") or "") > (best.get("timestamp") or ""):
            best = data
    return best


def get_report(rel_path: str) -> Dict[str, Any]:
    fp = _resolve(rel_path)
    if not fp.is_file():
        raise FileNotFoundError(rel_path)
    data = _safe_load(fp)
    if data is None:
        raise ValueError(f"{rel_path} is not a valid report file")
    data["_path"] = rel_path
    return data


def flatten_dict(d: Any, parent_key: str = "") -> Dict[str, Any]:
    """{"a": {"b": 1}} -> {"a.b": 1}. Lists are kept as-is (not recursed into)
    so e.g. `channels: [8, 16, 32]` shows as one comparable value."""
    items: Dict[str, Any] = {}
    if isinstance(d, dict):
        for k, v in d.items():
            key = f"{parent_key}.{k}" if parent_key else str(k)
            items.update(flatten_dict(v, key))
    else:
        items[parent_key] = d
    return items


def compare_reports(paths: List[str]) -> Dict[str, Any]:
    reports = []
    for path in paths:
        data = get_report(path)
        reports.append({
            "path": path,
            "experiment": data.get("experiment"),
            "timestamp": data.get("timestamp"),
            "metrics": data.get("metrics", {}) or {},
            "model": data.get("model", {}) or {},
            "efficiency": flatten_dict(data.get("efficiency", {}) or {}),
            "config_flat": flatten_dict(data.get("config", {}) or {}),
        })
    return {"reports": reports}
