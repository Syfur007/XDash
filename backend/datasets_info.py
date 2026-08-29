"""Reads configs/dataset/*.yaml fragments — the dataset-identity fragments
experiment configs compose (see CODE_REVIEW.md's H1 / CHANGELOG.md's Phase 1
config-composition fix) — directly, so the Data Studio view can show every
registered dataset's configured shape without needing a full, resolved
experiment config for each one.

Plain YAML reads, same as backend/configs.py — no bridge call needed here,
since this only reads a fragment's own literal keys, not anything requiring
schema validation or the host repo's Python packages.
"""
from __future__ import annotations

from typing import Any, Dict, List

import yaml

from .config import settings

FRAGMENT_SUBDIR = "dataset"


def list_dataset_fragments() -> List[Dict[str, Any]]:
    frag_dir = settings.configs_dir / FRAGMENT_SUBDIR
    if not frag_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for p in sorted(frag_dir.glob("*.y*ml")):
        try:
            parsed = yaml.safe_load(p.read_text())
        except yaml.YAMLError:
            continue
        ds = parsed.get("dataset") if isinstance(parsed, dict) else None
        if not isinstance(ds, dict):
            continue
        out.append({"fragment": p.stem, **ds})
    return out
