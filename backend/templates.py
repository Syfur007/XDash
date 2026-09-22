"""Repo-relative notebook template inventory for the dashboard."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .config import settings
from . import kaggle


_DEFAULT_TEMPLATE_LABEL = "(default template)"


def list_templates() -> List[Dict[str, Any]]:
    """Every notebook template this profile could push: the XDash-owned
    default (data/<profile>/, see backend/config.py's kaggle_default_template
    comment for why it moved out of the host repo) plus every *.ipynb the
    host repo itself carries — a worker's notebook_path/template_path
    override is still repo-relative, so those stay found by the rglob below;
    only the *default* template lives outside the repo_root tree it scans."""
    root = settings.repo_root.resolve()
    default_abs = settings.kaggle_default_template_file.resolve()
    workers = []
    for account in kaggle.list_accounts():
        for worker in account.get("workers", []):
            override = worker.get("notebook_path") or worker.get("template_path")
            workers.append({
                "account": account["name"], "worker_id": worker["worker_id"],
                "path": override or _DEFAULT_TEMPLATE_LABEL,
            })
    linked: Dict[str, List[Dict[str, str]]] = {}
    for worker in workers:
        linked.setdefault(worker["path"], []).append(worker)

    result = [{
        "path": f"data/{settings.profile_name}/{default_abs.name}",
        "exists": default_abs.is_file(),
        "size": default_abs.stat().st_size if default_abs.is_file() else 0,
        "linked_workers": linked.get(_DEFAULT_TEMPLATE_LABEL, []),
        "is_default": True,
    }]
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
            "is_default": False,
        })
    for path, linked_workers in linked.items():
        if path == _DEFAULT_TEMPLATE_LABEL:
            continue
        if not (root / path).is_file() and not any(item["path"] == path for item in result):
            result.append({"path": path, "exists": False, "size": 0, "linked_workers": linked_workers, "is_default": False})
    return sorted(result, key=lambda item: (not item["is_default"], item["path"]))
