"""Repo-relative notebook template inventory for the dashboard."""
from __future__ import annotations

from typing import Any, Dict, List

from .config import settings


def list_templates() -> List[Dict[str, Any]]:
    """Every notebook template this profile could push: the XDash-owned
    default (data/<profile>/, see backend/config.py's kaggle_default_template
    comment for why it moved out of the host repo) plus every *.ipynb the
    host repo itself carries.

    No per-template "linked workers" any more — the worker registry that
    provided notebook_path/template_path overrides is retired
    (XDASH_V2_PLAN.md §3.7), and every push now renders the one default
    template (backend/kaggle.py's push_experiment_attempt). The repo's own
    .ipynb files are still listed because they remain the candidates for the
    hand-authored-notebook escape hatch (§3.3).
    """
    root = settings.repo_root.resolve()
    default_abs = settings.kaggle_default_template_file.resolve()

    result = [{
        "path": "data/%s/%s" % (settings.profile_name, default_abs.name),
        "exists": default_abs.is_file(),
        "size": default_abs.stat().st_size if default_abs.is_file() else 0,
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
            "is_default": False,
        })
    return sorted(result, key=lambda item: (not item["is_default"], item["path"]))
