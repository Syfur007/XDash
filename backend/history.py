"""Read-only recursive browser backing the History page.

Supports multiple named "sources" — currently logs_dir (training logs, eval
reports, anything text-ish) and plots_dir (eval plots, overlays, anything
image-ish) — sharing one tree-walking/path-safety implementation so adding a
third source later is a one-line change, not a new module.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Dict, List

from .config import settings

MAX_PREVIEW_BYTES = 2 * 1024 * 1024  # 2MB — plenty for logs/reports, avoids huge transfers

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"}

SOURCES = {
    "logs": lambda: settings.logs_dir,
    "images": lambda: settings.plots_dir,
}


def _root_for(source: str) -> Path:
    if source not in SOURCES:
        raise ValueError(f"Unknown source '{source}' (expected one of {list(SOURCES)})")
    return SOURCES[source]()


def _relpath(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def _resolve(root: Path, rel_path: str) -> Path:
    candidate = (root / rel_path).resolve()
    if root.resolve() not in candidate.parents and candidate != root.resolve():
        raise ValueError("Path escapes the source directory")
    return candidate


def _matches_source(p: Path, source: str) -> bool:
    is_img = p.suffix.lower() in IMAGE_EXTS
    return is_img if source == "images" else not is_img


def _build_tree(root: Path, dir_path: Path, source: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    try:
        items = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except FileNotFoundError:
        return entries
    for p in items:
        if p.name.startswith("."):
            continue
        if p.is_dir():
            children = _build_tree(root, p, source)
            if children:  # prune directories that end up with nothing matching inside
                entries.append({"name": p.name, "path": _relpath(root, p), "type": "dir", "children": children})
        else:
            if not _matches_source(p, source):
                continue
            stat = p.stat()
            entries.append({
                "name": p.name, "path": _relpath(root, p), "type": "file",
                "size": stat.st_size, "mtime": stat.st_mtime,
                "is_image": p.suffix.lower() in IMAGE_EXTS,
            })
    return entries


def get_tree(source: str) -> List[Dict[str, Any]]:
    root = _root_for(source)
    root.mkdir(parents=True, exist_ok=True)
    return _build_tree(root, root, source)


def read_file(source: str, rel_path: str) -> Dict[str, Any]:
    root = _root_for(source)
    p = _resolve(root, rel_path)
    if not p.is_file():
        raise FileNotFoundError(rel_path)
    if not _matches_source(p, source):
        raise ValueError(f"{rel_path} does not belong to the '{source}' source")
    size = p.stat().st_size
    is_image = p.suffix.lower() in IMAGE_EXTS

    if is_image:
        return {"path": rel_path, "binary": True, "is_image": True, "size": size}

    with open(p, "rb") as f:
        raw = f.read(MAX_PREVIEW_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"path": rel_path, "binary": True, "is_image": False, "size": size}
    return {"path": rel_path, "binary": False, "is_image": False, "size": size, "truncated": size > MAX_PREVIEW_BYTES, "content": text}


def resolve_raw_path(source: str, rel_path: str) -> Path:
    """Used by the /raw/ route to stream a file's actual bytes (images)."""
    root = _root_for(source)
    p = _resolve(root, rel_path)
    if not p.is_file():
        raise FileNotFoundError(rel_path)
    if not _matches_source(p, source):
        raise ValueError(f"{rel_path} does not belong to the '{source}' source")
    return p


def guess_mimetype(p: Path) -> str:
    return mimetypes.guess_type(p.name)[0] or "application/octet-stream"
