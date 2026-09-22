"""Dataset-name -> Kaggle-dataset-slug resolution (XDASH_V2_PLAN.md §3.4),
used by both the old batch dispatcher (backend/batch_runner.py) and the new
Experiment dispatcher (backend/experiments.py) — extracted here so the two
share exactly one resolution path rather than drifting.

§3.4's precedence, in order:
  1. The config's own (compose-resolved) `dataset.kaggle_dataset` — a repo
     that adopts this key becomes self-describing and always wins.
  2. `data/<profile>/dataset_map.json` — XDash-owned, editable via
     GET/PUT /api/datasets/kaggle-map, so XDash works against a repo it
     doesn't own and never needs write access to that repo's configs.
  3. `repos/<profile>.yaml`'s `kaggle_dataset_map` — the legacy mechanism,
     read straight from Settings (already case-folded at load — see
     backend/config.py) and used only until #2 has its own entry.

Dataset names are matched case-folded on both sides throughout (fixes
XDASH_V2_PLAN.md D11 — the previous version's profile-yaml keys kept their
original case at load, so a mixed-case key never matched the lower-cased
lookup)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from . import configs as cfg
from .config import settings


def _load_config_yaml(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None


def _dataset_identity(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """(dataset name, explicit kaggle_dataset slug) declared by one config or
    fragment dict — either half may be absent."""
    dataset = raw.get("dataset") if isinstance(raw, dict) else None
    if not isinstance(dataset, dict):
        return None, None
    name = dataset.get("name")
    slug = dataset.get("kaggle_dataset")
    name = name.strip() if isinstance(name, str) and name.strip() else None
    slug = slug.strip() if isinstance(slug, str) and slug.strip() else None
    return name, slug


def config_dataset_identity(config_path: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolves (dataset_name, explicit_kaggle_dataset_slug) for *config_path*
    (configs_dir-relative, same convention as backend/configs.py), walking
    its `compose` fragments the same way the host repo's own load_config()
    merges them — a config whose dataset block lives entirely in a fragment
    would otherwise be silently treated as unmapped (XDASH_V2_PLAN.md D6)."""
    try:
        record = cfg.read_config(config_path)
    except (FileNotFoundError, ValueError):
        return None, None
    raw = record.get("parsed")
    if not isinstance(raw, dict):
        return None, None
    name, slug = _dataset_identity(raw)
    if slug:
        return name, slug
    config_dir = settings.configs_dir.resolve()
    config_abs = (config_dir / config_path).resolve()
    for fragment in raw.get("compose") or []:
        fragment_path = (config_abs.parent / str(fragment)).resolve()
        if config_dir not in fragment_path.parents or not fragment_path.is_file():
            continue
        fragment_raw = _load_config_yaml(fragment_path)
        if fragment_raw is None:
            continue
        frag_name, frag_slug = _dataset_identity(fragment_raw)
        name = name or frag_name
        if frag_slug:
            return name, frag_slug
    return name, None


def _normalize_map(data: Any) -> Dict[str, str]:
    if not isinstance(data, dict):
        return {}
    return {
        str(k).strip().casefold(): str(v).strip()
        for k, v in data.items() if str(k).strip() and str(v).strip()
    }


def load_dataset_map() -> Dict[str, str]:
    """XDash-owned name->slug map (§3.4 rule 2), falling back to the profile
    yaml's kaggle_dataset_map (rule 3) until data/<profile>/dataset_map.json
    exists."""
    if settings.dataset_map_file.exists():
        try:
            data = json.loads(settings.dataset_map_file.read_text())
        except (OSError, json.JSONDecodeError):
            data = None
        if data is not None:
            return _normalize_map(data)
    return dict(settings.kaggle_dataset_map)


def save_dataset_map(entries: Dict[str, str]) -> Dict[str, str]:
    """Overwrites data/<profile>/dataset_map.json wholesale — the Data tab's
    editor (Phase C) sends the full map back on every save, same shape
    load_dataset_map() returns, so there's no partial-update ambiguity."""
    normalized = _normalize_map(entries)
    settings.dataset_map_file.write_text(json.dumps(normalized, indent=2, sort_keys=True))
    return normalized


def resolve_kaggle_dataset(config_path: str) -> Optional[str]:
    """Resolve a Kaggle dataset slug for *config_path* per §3.4's precedence.
    Returns None when nothing resolves — nothing here guesses, so an
    unmapped config is reported as `no-dataset-mapping` by the caller
    instead of silently dispatching to an account that may not have the
    right data attached."""
    name, explicit_slug = config_dataset_identity(config_path)
    if explicit_slug:
        return explicit_slug
    if not name:
        return None
    return load_dataset_map().get(name.casefold())


def map_with_provenance() -> List[Dict[str, Any]]:
    """The dataset map as GET /api/datasets/kaggle-map returns it: one entry
    per known dataset name, with where its slug actually came from — the
    XDash-owned override file, or the profile yaml's legacy default (§5:
    "with provenance per entry"). A name present in the profile yaml but
    overridden in dataset_map.json reports "dataset_map", not "profile
    default", since that's the value actually in effect."""
    file_map = {}
    if settings.dataset_map_file.exists():
        try:
            file_map = _normalize_map(json.loads(settings.dataset_map_file.read_text()))
        except (OSError, json.JSONDecodeError):
            file_map = {}
    profile_map = dict(settings.kaggle_dataset_map)
    names = sorted(set(file_map) | set(profile_map))
    out = []
    for name in names:
        if name in file_map:
            out.append({"name": name, "kaggle_dataset": file_map[name], "source": "dataset_map"})
        else:
            out.append({"name": name, "kaggle_dataset": profile_map[name], "source": "profile_default"})
    return out
