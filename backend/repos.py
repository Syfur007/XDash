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
import re
from typing import Any, Dict, List, Optional

import yaml

from . import hosts
from . import monitors
from . import tmux_runner as tmux
from . import terminals as terminals_mod
from .config import REPOS_DIR, Settings, active_repo_store, list_profile_names, settings
from .store import JsonStore, StoreCorruptError, atomic_write_text


class RepoProfileError(Exception):
    """Expected failure (unknown profile) — routes map this to a 4xx."""


class RepoProfileBusyError(RepoProfileError):
    """The active profile cannot switch while a batch is dispatching."""


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


# --------------------------------------------------------------- new-profile wizard (§8.6)
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def detect_repo(repo_root: str) -> Dict[str, Any]:
    """Read-only preview for the "+ New profile" wizard (XDASH_PLAN.md §8.6):
    *repo_root* is resolved the same way a profile's own `framework.repo_root`
    is (relative to `repos/` itself, MULTI_REPO_PLAN.md §3), and this reports
    what's findable inside it — a configs directory, `train.py`/`eval.py`, and
    an output-layout guess — without writing anything. Every field degrades
    to a sane guess rather than raising, since this is only ever a preview a
    user can override before Create."""
    repo_root = (repo_root or "").strip()
    root = (REPOS_DIR / (repo_root or ".")).resolve()
    exists = root.is_dir()
    configs_dir = None
    if exists:
        for candidate in ("configs", "config", "cfgs"):
            if (root / candidate).is_dir():
                configs_dir = candidate
                break
    return {
        "repo_root": repo_root,
        "resolved": str(root),
        "exists": exists,
        "configs_dir": configs_dir,
        "train_py": exists and (root / "train.py").is_file(),
        "eval_py": exists and (root / "eval.py").is_file(),
        "manifest_layout": "experiments" if exists and (root / "outputs" / "experiments").is_dir() else "legacy",
    }


def create_profile(name: str, display_name: str, repo_root: str) -> Dict[str, Any]:
    """Writes a new `repos/<name>.yaml` from the §4.1 sectioned template,
    pre-filled from `detect_repo()` — the wizard's write path. Validated the
    same way `profile_ops`'s PATCH validates a patch (a throwaway `Settings`
    load against the rendered text) *before* anything is written, so a bad
    repo root or a name collision never leaves a half-created profile file
    behind. Doesn't switch the active profile — the caller does that through
    the existing `set_active_profile()`/"Use profile" control, same as any
    other profile."""
    name = (name or "").strip()
    if not name or not _NAME_RE.match(name):
        raise RepoProfileError("Profile name must be a plain slug (letters, digits, '-' or '_')")
    if name in list_profile_names():
        raise RepoProfileError(f"A profile named '{name}' already exists")
    repo_root = (repo_root or "").strip()
    if not repo_root:
        raise RepoProfileError("A repo root is required")
    detected = detect_repo(repo_root)
    if not detected["exists"]:
        raise RepoProfileError(f"'{repo_root}' (resolved to {detected['resolved']}) is not a directory")

    # json.dumps of a plain string is a valid YAML double-quoted flow scalar
    # (escapes backslashes/quotes the same way), unlike yaml.safe_dump()
    # here — dumping a *bare string* with PyYAML emits a whole one-scalar
    # *document*, complete with a trailing "\n...\n" end-of-document marker,
    # which corrupts the surrounding template when spliced into a larger
    # file rather than written out on its own.
    quote = json.dumps

    doc = (
        "display_name: %s\n\n"
        "framework:\n"
        "  repo_root: %s\n"
        "  configs_dir: %s\n"
        "  launchable: [\"experiment/**/*.yaml\"]\n"
        "  fragments: {dataset: \"dataset/*.yaml\", model: \"model/**/*.yaml\", training: \"training/*.yaml\"}\n"
        "  dataset_name_key: dataset.name\n"
        "  dataset_root_key: dataset.root\n\n"
        "commands:\n"
        "  python: python\n"
        "  train: \"{python} train.py --config {config} {budget} {resume} {extra}\"\n"
        "  eval: \"{python} eval.py --config {config} {extra}\"\n"
        "  budget: \"--max-hours {hours}\"\n"
        "  resume: \"--resume\"\n\n"
        "manifest_layout: %s\n"
    ) % (
        quote(display_name or name),
        quote(repo_root),
        quote(detected["configs_dir"] or "configs"),
        detected["manifest_layout"],
    )

    from . import profile_ops
    try:
        profile_ops._validate(doc, name)
    except profile_ops.ProfileError as e:
        raise RepoProfileError(str(e))

    path = REPOS_DIR / f"{name}.yaml"
    atomic_write_text(path, doc)
    return {"repos": list_profiles(), "created": name}


def set_active_profile(profile_name: str) -> Dict[str, Any]:
    names = list_profile_names()
    if profile_name not in names:
        raise RepoProfileError(f"Unknown repo profile '{profile_name}' (known: {', '.join(names) or 'none'})")
    if profile_name != settings.profile_name:
        from . import experiments
        for e in experiments.list_experiments():
            if e["status"] in experiments.IN_FLIGHT_STATUSES:
                raise RepoProfileBusyError(
                    f"Cannot switch profiles while experiment '{e['experiment_id']}' is {e['status']}; "
                    "cancel it first"
                )
    settings.reload(profile_name)
    active_repo_store.save({"profile": profile_name})
    return {"profile": profile_name, "display_name": settings.display_name}


# --------------------------------------------------------------- global sessions
# Read-only snapshots — a fresh Settings(name) per profile, never the shared
# `settings` singleton, so this is safe to call from any request regardless
# of what else is in flight (see module docstring).
def _snapshot(name: str) -> Settings:
    return Settings(name)


def _read_other_profile(path, default):
    """Another profile's state file, read-only, for this cross-profile view.
    A corrupt file degrades to *default* here on purpose: this is a display
    of every profile at once, and one profile's broken store must not blank
    the others. The owning profile's own reads (backend/store.py) still fail
    loudly the moment that profile is active."""
    try:
        return JsonStore(path, default).load()
    except StoreCorruptError:
        return default()


def _local_sessions_for_profile(name: str, snap: Settings, alive_by_host: Dict[str, set]) -> List[Dict[str, Any]]:
    records = _read_other_profile(snap.state_file, list)
    if not isinstance(records, list):
        records = []

    out = []
    for r in records:
        session_name = r.get("session_name")
        if not session_name:
            continue
        host_id = r.get("host_id") or hosts.LOCAL_HOST_ID
        if host_id not in alive_by_host:
            alive_by_host[host_id] = set(tmux.list_sessions(host_id=host_id))
        alive = session_name in alive_by_host[host_id]
        status = "ended"
        if alive:
            text = tmux.capture_pane_tail(session_name, lines=50, host_id=host_id) or ""
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
            "profile": name, "kind": "local", "unit_id": session_name, "host_id": host_id,
            "label": r.get("experiment_name") or session_name,
            "config_path": r.get("config_path"), "mode": r.get("mode"),
            "status": status, "alive": alive, "created_at": r.get("created_at"),
        })
    return out


def _kaggle_sessions_for_profile(name: str, snap: Settings) -> List[Dict[str, Any]]:
    """In-flight Kaggle Attempts under *name*, read straight out of that
    profile's experiments.json. Attempts replaced the worker registry this
    used to enumerate (XDASH_V2_PLAN.md §3.7); reading the store as plain
    JSON — rather than importing backend/experiments.py — keeps this a
    snapshot of *another* profile, which is the whole point of the function
    (backend/experiments.py only ever reports the active one)."""
    store = _read_other_profile(snap.experiments_store_file, dict) or {}
    attempts = store.get("attempts") if isinstance(store, dict) else None
    if not isinstance(attempts, dict):
        return []

    out = []
    for attempt in attempts.values():
        slot = attempt.get("slot") or ""
        if not slot.startswith("kaggle:") or attempt.get("status") not in ("dispatching", "running"):
            continue
        unit_ref = attempt.get("unit_ref") or {}
        out.append({
            "profile": name, "kind": "kaggle", "unit_id": attempt.get("attempt_id"),
            "label": attempt.get("experiment_id") or attempt.get("attempt_id"),
            "account": slot.split(":", 1)[1],
            "status": attempt.get("raw_status") or attempt.get("status"),
            "kernel_slug": unit_ref.get("kernel_slug"),
            "pushed_at": attempt.get("started_at"),
        })
    return out


def list_global_sessions() -> List[Dict[str, Any]]:
    """Every tmux (local) and Kaggle session across every profile, each
    tagged with which repo it belongs to — Terminals/Runs/Scheduler/Kaggle
    views use this so switching the active profile never hides a live run
    under another one (MULTI_REPO_PLAN.md §6 option B)."""
    names = list_profile_names()
    snapshots = {name: _snapshot(name) for name in names}
    # One tmux query per distinct host actually referenced by some profile's
    # records, populated lazily by _local_sessions_for_profile — never every
    # configured host on every call.
    alive_by_host: Dict[str, set] = {}

    sessions: List[Dict[str, Any]] = []
    for name in names:
        snap = snapshots[name]
        sessions += _local_sessions_for_profile(name, snap, alive_by_host)
        sessions += _kaggle_sessions_for_profile(name, snap)

    # Local tmux sessions alive but not recorded in any profile's own state
    # file: best-effort attribute to whichever profile's tmux_session_prefix
    # is the longest match, else leave profile unset ("unknown"). Scoped to
    # the local host only — same reasoning as terminals.py's own unmanaged
    # detection: this surfaces a session started by hand on *this* machine,
    # not a census of every remote host's shell state.
    alive_local = alive_by_host.setdefault(hosts.LOCAL_HOST_ID, set(tmux.list_sessions(host_id=hosts.LOCAL_HOST_ID)))
    managed_names = {s["unit_id"] for s in sessions if s["kind"] == "local"}
    prefixes = sorted(
        ((name, snapshots[name].tmux_session_prefix) for name in names),
        key=lambda np: -len(np[1]),
    )
    for session_name in sorted(alive_local - managed_names):
        if monitors.is_monitor_session(session_name):
            continue
        owner = next((n for n, p in prefixes if session_name.startswith(p)), None)
        sessions.append({
            "profile": owner, "kind": "local", "unit_id": session_name, "host_id": hosts.LOCAL_HOST_ID,
            "label": session_name, "config_path": None, "mode": None,
            "status": "unmanaged", "alive": True, "created_at": None,
        })
    return sessions
