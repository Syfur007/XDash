"""Repo-relative notebook template inventory for the dashboard."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .config import settings
from . import kaggle


def list_templates() -> List[Dict[str, Any]]:
    root = settings.repo_root.resolve()
    workers = []
    for account in kaggle.list_accounts():
        for worker in account.get("workers", []):
            path = worker.get("notebook_path") or worker.get("template_path") or settings.kaggle_default_template
            workers.append({"account": account["name"], "worker_id": worker["worker_id"], "path": path})
    linked: Dict[str, List[Dict[str, str]]] = {}
    for worker in workers:
        linked.setdefault(worker["path"], []).append(worker)
    result = []
    for path in sorted(root.rglob("*.ipynb")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        result.append({
            "path": relative,
            "exists": True,
            "size": path.stat().st_size,
            "linked_workers": linked.get(relative, []),
            "is_default": relative == settings.kaggle_default_template,
        })
    for path, linked_workers in linked.items():
        if not (root / path).is_file() and not any(item["path"] == path for item in result):
            result.append({"path": path, "exists": False, "size": 0, "linked_workers": linked_workers, "is_default": path == settings.kaggle_default_template})
    return sorted(result, key=lambda item: (not item["is_default"], item["path"]))
