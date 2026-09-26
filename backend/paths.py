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


def path_exists(scope: str, rel_path: str) -> Dict[str, object]:
    """Lightweight existence check for a Settings path widget (XDASH_PLAN.md
    §8.6: "a path picker with an existence check") — *rel_path* is resolved
    the same way `list_paths()`'s own entries are (relative to the scope's
    root), so a value copied out of that picker always checks the same
    thing it was picked from. An absolute path is checked as-is (a profile
    key like `framework.repo_root` is itself resolved relative to
    `repos/`, not always repo-relative), never used to escape the scope's
    root for a relative one."""
    rel_path = (rel_path or "").strip()
    if not rel_path:
        return {"path": rel_path, "exists": False, "kind": None}
    candidate = Path(rel_path)
    target = candidate if candidate.is_absolute() else (_root(scope) / candidate)
    if target.is_dir():
        return {"path": rel_path, "exists": True, "kind": "directory"}
    if target.is_file():
        return {"path": rel_path, "exists": True, "kind": "file"}
    return {"path": rel_path, "exists": False, "kind": None}


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
