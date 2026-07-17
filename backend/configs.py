"""Discover and edit YAML configs under the repo's configs/ directory."""
from __future__ import annotations

from pathlib import Path
from typing import List, Dict, Any
import yaml

from .config import settings

YAML_EXTS = (".yaml", ".yml")


def _relpath(p: Path) -> str:
    return p.relative_to(settings.configs_dir).as_posix()


def _resolve(rel_path: str) -> Path:
    """Resolve a relative path safely inside configs_dir (no path escape)."""
    candidate = (settings.configs_dir / rel_path).resolve()
    if settings.configs_dir.resolve() not in candidate.parents and candidate != settings.configs_dir.resolve():
        raise ValueError("Path escapes configs directory")
    return candidate


def list_configs() -> List[Dict[str, Any]]:
    """Return configs grouped by category (top-level sub-directory name).

    Files directly under configs/ are grouped under category "general".
    """
    settings.ensure_dirs()
    groups: Dict[str, List[Dict[str, str]]] = {}

    for p in sorted(settings.configs_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in YAML_EXTS:
            rel = _relpath(p)
            parts = rel.split("/")
            category = parts[0] if len(parts) > 1 else "general"
            groups.setdefault(category, []).append({
                "name": p.name,
                "path": rel,
                "category": category,
            })

    return [
        {"category": cat, "configs": sorted(items, key=lambda x: x["name"])}
        for cat, items in sorted(groups.items())
    ]


def read_config(rel_path: str) -> Dict[str, Any]:
    fp = _resolve(rel_path)
    if not fp.is_file():
        raise FileNotFoundError(rel_path)
    text = fp.read_text()
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as e:
        parsed = None
    return {"path": rel_path, "raw": text, "valid": parsed is not None or text.strip() == "", "parsed": parsed}


def write_config(rel_path: str, raw_text: str) -> Dict[str, Any]:
    fp = _resolve(rel_path)
    # Validate before writing so a typo never clobbers a working config.
    yaml.safe_load(raw_text)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(raw_text)
    return {"path": rel_path, "saved": True}


def repo_relative_path(rel_path: str) -> str:
    """Path to a config as it should appear on the command line, relative to
    repo_root (e.g. "mkunet/foo.yaml" -> "configs/mkunet/foo.yaml")."""
    fp = _resolve(rel_path)
    return fp.relative_to(settings.repo_root).as_posix()


def get_experiment_name(rel_path: str) -> str:
    """Best-effort experiment name for display / log-file matching."""
    try:
        parsed = read_config(rel_path)["parsed"] or {}
        name = (parsed.get("logging") or {}).get("experiment_name")
        if name:
            return name
    except Exception:
        pass
    return Path(rel_path).stem
