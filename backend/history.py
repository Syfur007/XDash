"""Read-only recursive browser for whatever ends up under logs_dir — training
logs, eval reports, anything else. This is deliberately separate from
reports.py: reports.py understands report *content*, this module just walks
the filesystem and hands back text, no interpretation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .config import settings

MAX_PREVIEW_BYTES = 2 * 1024 * 1024  # 2MB — plenty for logs/reports, avoids huge transfers


def _relpath(p: Path) -> str:
    return p.relative_to(settings.logs_dir).as_posix()


def _resolve(rel_path: str) -> Path:
    candidate = (settings.logs_dir / rel_path).resolve()
    if settings.logs_dir.resolve() not in candidate.parents and candidate != settings.logs_dir.resolve():
        raise ValueError("Path escapes logs directory")
    return candidate


def _build_tree(dir_path: Path) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    try:
        items = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except FileNotFoundError:
        return entries
    for p in items:
        if p.name.startswith("."):
            continue
        if p.is_dir():
            entries.append({"name": p.name, "path": _relpath(p), "type": "dir", "children": _build_tree(p)})
        else:
            stat = p.stat()
            entries.append({"name": p.name, "path": _relpath(p), "type": "file", "size": stat.st_size, "mtime": stat.st_mtime})
    return entries


def get_tree() -> List[Dict[str, Any]]:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    return _build_tree(settings.logs_dir)


def read_file(rel_path: str) -> Dict[str, Any]:
    p = _resolve(rel_path)
    if not p.is_file():
        raise FileNotFoundError(rel_path)
    size = p.stat().st_size
    with open(p, "rb") as f:
        raw = f.read(MAX_PREVIEW_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"path": rel_path, "binary": True, "size": size}
    return {"path": rel_path, "binary": False, "size": size, "truncated": size > MAX_PREVIEW_BYTES, "content": text}
