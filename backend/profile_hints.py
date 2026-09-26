"""Optional per-key hints for the Settings form (XDASH_PLAN.md §4.1/§8.6):
type, enum and any other rendering hint a generic YAML-shape-driven form
(Phase 4) can't infer from the value alone. A key not listed here still
renders and saves fine — see backend/profile_ops.py's `get_profile()`,
which looks this up defensively (`.get(k)`, never assumes every key has an
entry) and degrades to "whatever type-inferred widget the value's own
Python type suggests" (bool -> toggle, number -> numeric, list -> chips,
`*_dir`/`*_path`/`root` suffix -> path picker).

Deliberately tiny for Phase 2 (the form itself is Phase 4 work) — just
enough to prove the mechanism a hint-aware form would use.
"""
from __future__ import annotations

from typing import Any, Dict

HINTS: Dict[str, Dict[str, Any]] = {
    "framework.repo_root": {"type": "path"},
    "framework.configs_dir": {"type": "path"},
    "commands.python": {"type": "path"},
    "commands.train": {"type": "text", "multiline": True},
    "commands.eval": {"type": "text", "multiline": True},
    "metrics.lower_is_better": {"type": "list"},
    "allow_unpushed": {"type": "bool"},
    "manifest_layout": {"type": "enum", "choices": ["legacy", "experiments"]},
    "server_port": {"type": "number", "restart_required": True},
    "server_host": {"type": "text", "restart_required": True},
    "api_token": {"type": "secret", "restart_required": True},
}
