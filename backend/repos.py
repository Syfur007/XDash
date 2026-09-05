"""Repo-profile registry + live active-profile switching (MULTI_REPO_PLAN.md
Phases 1/2/4).

Exactly one profile is "active" at a time — `settings` (backend/config.py)
always reflects it, and every write path (launching a terminal, pushing a
Kaggle worker, adding a scheduler item) targets it. But tmux itself is one
global namespace on this machine, and Kaggle accounts are dashboard state
independent of which profile is active — a training run started under
dissert doesn't stop existing just because the UI switches to segpriors. So
list_global_sessions() below deliberately reads every profile's own state
files directly (never by mutating the shared `settings` singleton, which
would race a threaded server's concurrent requests) to give a cross-profile
view: every session, tagged with which repo it belongs to, regardless of
which profile is currently active (§6 option B). New launches never go
through this module — they use the active `settings` as every other route
already does.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import yaml

from . import monitors
from . import tmux_runner as tmux
from . import terminals as terminals_mod
from .config import ACTIVE_REPO_FILE, REPOS_DIR, Settings, list_profile_names, settings


class RepoProfileError(Exception):
    """Expected failure (unknown profile) — routes map this to a 4xx."""


def list_profiles() -> List[Dict[str, Any]]:
    result = []
    for name in list_profile_names():
        path = REPOS_DIR / f"{name}.yaml"
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except Exception:
            raw = {}
        repo_root = (REPOS_DIR / raw.get("repo_root", "..")).resolve()
        result.append({
            "id": name,
            "display_name": raw.get("display_name", name),
            "repo_root": str(repo_root),
            "repo_root_exists": repo_root.is_dir(),
            "active": name == settings.profile_name,
        })
    return result


def set_active_profile(profile_name: str) -> Dict[str, Any]:
    names = list_profile_names()
    if profile_name not in names:
        raise RepoProfileError(f"Unknown repo profile '{profile_name}' (known: {', '.join(names) or 'none'})")
    settings.reload(profile_name)
    ACTIVE_REPO_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_REPO_FILE.write_text(json.dumps({"profile": profile_name}))
    return {"profile": profile_name, "display_name": settings.display_name}


# --------------------------------------------------------------- global sessions
# Read-only snapshots — a fresh Settings(name) per profile, never the shared
# `settings` singleton, so this is safe to call from any request regardless
# of what else is in flight (see module docstring).
def _snapshot(name: str) -> Settings:
    return Settings(name)


def _local_sessions_for_profile(name: str, snap: Settings, alive_sessions: set) -> List[Dict[str, Any]]:
    if not snap.state_file.exists():
        records = []
    else:
        try:
            records = json.loads(snap.state_file.read_text())
        except Exception:
            records = []

    out = []
    for r in records:
        session_name = r.get("session_name")
        if not session_name:
            continue
        alive = session_name in alive_sessions
        status = "ended"
        if alive:
            text = tmux.capture_pane_tail(session_name, lines=50) or ""
            code = terminals_mod._marker_code(text, session_name)
            if code is None:
                status = "running"
            elif code == 0:
                status = "completed"
            elif code == 130:
                status = "stopped"
            else:
                status = "failed"
        out.append({
            "profile": name, "kind": "local", "unit_id": session_name,
            "label": r.get("experiment_name") or session_name,
            "config_path": r.get("config_path"), "mode": r.get("mode"),
            "status": status, "alive": alive, "created_at": r.get("created_at"),
        })
    return out


def _kaggle_sessions_for_profile(name: str, snap: Settings) -> List[Dict[str, Any]]:
    if not snap.kaggle_accounts_file.exists():
        return []
    try:
        accounts = (json.loads(snap.kaggle_accounts_file.read_text()) or {}).get("accounts", [])
    except Exception:
        accounts = []
    state: Dict[str, Any] = {}
    if snap.kaggle_state_file.exists():
        try:
            state = json.loads(snap.kaggle_state_file.read_text()) or {}
        except Exception:
            state = {}

    out = []
    for account in accounts:
        for w in account.get("workers", []):
            w_state = state.get(w["worker_id"], {})
            if not w_state.get("status"):
                continue  # never pushed — nothing to show, same as KaggleRunner.list_units()
            out.append({
                "profile": name, "kind": "kaggle", "unit_id": w["worker_id"],
                "label": w["worker_id"], "account": account.get("name"),
                "status": w_state.get("status"), "over_budget": w_state.get("over_budget", False),
                "pushed_at": w_state.get("pushed_at"),
            })
    return out


def list_global_sessions() -> List[Dict[str, Any]]:
    """Every tmux (local) and Kaggle session across every profile, each
    tagged with which repo it belongs to — Terminals/Runs/Scheduler/Kaggle
    views use this so switching the active profile never hides a live run
    under another one (MULTI_REPO_PLAN.md §6 option B)."""
    names = list_profile_names()
    snapshots = {name: _snapshot(name) for name in names}
    alive_sessions = set(tmux.list_sessions())

    sessions: List[Dict[str, Any]] = []
    for name in names:
        snap = snapshots[name]
        sessions += _local_sessions_for_profile(name, snap, alive_sessions)
        sessions += _kaggle_sessions_for_profile(name, snap)

    # tmux sessions alive but not recorded in any profile's own state file:
    # best-effort attribute to whichever profile's tmux_session_prefix is
    # the longest match, else leave profile unset ("unknown").
    managed_names = {s["unit_id"] for s in sessions if s["kind"] == "local"}
    prefixes = sorted(
        ((name, snapshots[name].tmux_session_prefix) for name in names),
        key=lambda np: -len(np[1]),
    )
    for session_name in sorted(alive_sessions - managed_names):
        if monitors.is_monitor_session(session_name):
            continue
        owner = next((n for n, p in prefixes if session_name.startswith(p)), None)
        sessions.append({
            "profile": owner, "kind": "local", "unit_id": session_name,
            "label": session_name, "config_path": None, "mode": None,
            "status": "unmanaged", "alive": True, "created_at": None,
        })
    return sessions
