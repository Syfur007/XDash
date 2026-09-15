"""Safe repo-relative path listings for dashboard pickers."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from .config import settings

_ROOTS = {
    "repo": lambda: settings.repo_root,
    "configs": lambda: settings.configs_dir,
    "logs": lambda: settings.logs_dir,
    "runs": lambda: settings.runs_dir,
}


def _root(scope: str) -> Path:
    try:
        return _ROOTS[scope]().resolve()
    except KeyError:
        raise ValueError(f"Unknown path scope '{scope}'")


def list_paths(scope: str = "repo", kind: str = "file") -> List[Dict[str, str]]:
    if kind not in {"file", "directory"}:
        raise ValueError("kind must be 'file' or 'directory'")
    root = _root(scope)
    if not root.is_dir():
        return []
    entries: List[Dict[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
        if path.name.startswith(".") or path.is_symlink():
            continue
        if (kind == "file" and not path.is_file()) or (kind == "directory" and not path.is_dir()):
            continue
        try:
            relative = path.relative_to(settings.repo_root.resolve()).as_posix()
        except ValueError:
            continue
        entries.append({"path": relative, "name": path.name, "kind": kind})
        if len(entries) >= 5000:
            break
    return entries
