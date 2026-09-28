"""Cleans up after XDash itself (XDASH_FIXES_PLAN.md F5, closes issue #1).

Nothing prunes anything today: attempt logs, terminal snapshots and finished
tmux sessions all outlive the experiments/terminals that created them
forever, the scheduler's own queue-size cap counts finished items (fixed
separately by F0.7), and neither `delete_experiment()` nor `delete_dataset()`
cascades to the state that belongs to what they just deleted (fixed here, in
those two functions themselves).

**Two entry points**, matching the plan's own shape:

- `inventory()` — read-only, one row per category: how much is there, and
  how much of it is currently eligible for removal (an orphan, over a cap,
  or past its retention window) under today's retention settings. Never
  writes anything the *categories* being described own; it does advance
  this module's own tiny "when did I first notice this was finished"
  bookkeeping the same way a real clean() pass would — see
  `_scan_finished_tmux_sessions()`'s docstring for why that one exception
  exists and why it still can't make `clean(..., dry_run=True)` do anything
  different.
- `clean(categories, dry_run)` — categories are independent; asking for one
  never touches another. `dry_run=True` computes exactly what would be
  removed (freed bytes, item count) without removing anything — every
  action function is only ever *called* when `dry_run` is False.

**Categories** (XDASH_FIXES_PLAN.md's own table, §4/F5.1): thumbnails,
attempt logs, terminal snapshots, finished tmux sessions, terminal records,
scheduler history, remote staging, overlays, and crash temp dirs are
"auto" — safe enough to run unattended, wired into `ensure_worker_started()`
below (startup + once a day, off the request path, skipped entirely when
XDASH_DISABLE_BACKGROUND is set). Kaggle downloads, migration backups/
retired stores, and orphan account dirs are manual-only (`AUTO_CATEGORIES`
never includes them) — each still fully scannable/cleanable through the
Storage panel's own per-category Clean button, just never run unattended,
per D8.

**Safety, non-negotiable** (every action function goes through one of the
two path-removal helpers below, `_act_remove_path`/`_act_remove_path_multi`,
which resolve the target and refuse anything that doesn't land inside its
category's own declared root — no symlink escape, no `..`):

- Never touches host-repo run outputs (`outputs/experiments/**`), the
  ledger, dataset *data* (only cached thumbnails), credentials of a
  *registered* Kaggle/Colab account, `repos/*.yaml`, or any live store file
  itself (`*.json` — only directories/files those stores *reference*, like
  a log dir or a thumbnail, are ever removed; the stores are edited through
  their own module's API — `terminals.kill()`/`scheduler.remove_item()` —
  never by touching `terminals_state.json`/`scheduler.json` on disk here).
- The crash-temp-dir sweep only ever matches XDash's own five `mkdtemp()`
  prefixes, only entries owned by the current user (never another
  account's files that happen to share this machine's `$TMPDIR`), only
  older than a day.
- A tmux session is only ever killed after its pane text has been persisted
  (an attempt log, or a dashboard_logs snapshot — `terminals.
  reap_finished_session()`'s own contract), only for a record whose status
  says finished (never running, never an unmanaged/foreign session, and
  never a monitor/TensorBoard session — those aren't tracked as terminal
  records at all, so they're structurally invisible to this sweep).
- Every host-touching check (the finished-tmux-sessions scan, across every
  registered host) goes through `terminals.list_terminals()`'s own
  transport layer and is wrapped so an unreachable host is skipped, never
  raised — the same tolerance `backend/tmux_runner.py`'s `_run()` already
  built in (a bad host degrades to "nothing found there", not an exception).
"""
from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import colab as colab_backend
from . import config as config_mod
from . import datasets as datasets_mod
from . import experiments as experiments_mod
from . import framework
from . import hosts
from . import kaggle as kaggle_backend
from . import scheduler
from . import terminals as terminals_mod
from . import transport as transport_mod
from .config import background_disabled, settings
from .store import JsonStore

_RETENTION_DEFAULTS: Dict[str, float] = {
    "retention_days": 14,               # terminal records + scheduler history
    "thumbnail_cap_mb": 200,
    "adhoc_session_grace_hours": 24,
}
# Deployment-level, sibling to backend/tools.py's data/tools.json (D2's own
# reasoning: retention policy is a deployment preference, not a repo one) —
# a SEPARATE file rather than reusing tools.json itself, so each stays
# scoped to one concern (tool paths vs. cleanup policy) the way
# hosts.json/monitors.json/tools.json already each own exactly one thing.
HOUSEKEEPING_FILE = config_mod.DATA_DIR / "housekeeping.json"
_settings_store = JsonStore(HOUSEKEEPING_FILE, lambda: dict(_RETENTION_DEFAULTS))

# Crash-temp-dir sweep: XDash's own mkdtemp() prefixes only (grepped across
# backend/ — kaggle.py's kaggle_push_/kaggle_download_, framework.py's
# xdash_overlay_, runners/machine.py's xdash_seed_, snapshot.py's
# xdash_snapshot_ — nothing else in this codebase calls mkdtemp()).
CRASH_TEMP_PREFIXES = ("kaggle_push_", "kaggle_download_", "xdash_seed_", "xdash_snapshot_", "xdash_overlay_")
CRASH_TEMP_MAX_AGE_DAYS = 1.0  # fixed, not a retention setting — matches the plan's own safety rule verbatim

_MANAGED_STATUSES = ("completed", "failed", "stopped", "lost")
_MIGRATION_BACKUP_NAMES = (
    "datasets.v1.json.bak", "dataset_map.json.migrated", "kaggle_state.json",
    "assignments.json", "batches.json",
)


class HousekeepingError(Exception):
    """Raised by an action function that refused to touch a path — always
    caught by clean() and reported per-item, never let to propagate out of
    a whole category's sweep."""


# --------------------------------------------------------------------------- retention settings (F5.4)
def get_settings() -> Dict[str, Any]:
    data = _settings_store.load()
    merged = dict(_RETENTION_DEFAULTS)
    merged.update({k: v for k, v in (data or {}).items() if k in _RETENTION_DEFAULTS})
    return merged


def set_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    current = get_settings()
    for key in _RETENTION_DEFAULTS:
        if key not in patch or patch[key] is None:
            continue
        try:
            value = float(patch[key])
        except (TypeError, ValueError):
            raise ValueError("%s must be a number" % key)
        if value < 0:
            raise ValueError("%s must be >= 0" % key)
        current[key] = value
    _settings_store.save(current)
    return current


# --------------------------------------------------------------------------- this module's own bookkeeping
# "When did I first notice this session was finished?" — needed because an
# ordinary terminal record carries no end timestamp (only a scheduler item
# does, via `ended_at`), so age-based retention for a plain finished/lost
# terminal record, and the ad-hoc-session grace period, both anchor off the
# moment *this sweep* first observed it, not off when it actually finished.
# Per-profile (state_dir), like every other per-profile store — a session
# name is only meaningful within the profile that launched it.
def _seen_state_path() -> Path:
    return settings.state_dir / "housekeeping_state.json"


_seen_store = JsonStore(_seen_state_path, lambda: {"session_finished_seen_at": {}})


def _load_seen() -> Dict[str, str]:
    data = _seen_store.load()
    return dict((data or {}).get("session_finished_seen_at") or {})


def _save_seen(seen: Dict[str, str]) -> None:
    _seen_store.save({"session_finished_seen_at": seen})


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _age_seconds(iso_text: str) -> float:
    """Elapsed time since *iso_text*, tolerant of both the naive-local
    timestamps scheduler.py/terminals.py write and the UTC-aware ones
    experiments.py writes — comparing against a matching 'now' either way
    instead of raising on a naive/aware subtraction."""
    dt = _parse_iso(iso_text)
    now = datetime.now(timezone.utc) if dt.tzinfo is not None else datetime.now()
    return (now - dt).total_seconds()


def _get_or_seed_seen_at(session: str, seen: Dict[str, str], record_observations: bool) -> Optional[str]:
    existing = seen.get(session)
    if existing is not None:
        return existing
    if not record_observations:
        return None
    now_iso = _now_iso()
    seen[session] = now_iso
    return now_iso


# --------------------------------------------------------------------------- path safety
def _within(root: Path, path: Path) -> bool:
    try:
        resolved_root, resolved_path = root.resolve(), path.resolve()
    except OSError:
        return False
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def _file_bytes(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _dir_bytes(p: Path) -> int:
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file() and not f.is_symlink():
                total += _file_bytes(f)
    except OSError:
        pass
    return total


def _item_bytes(p: Path) -> int:
    if p.is_dir() and not p.is_symlink():
        return _dir_bytes(p)
    return _file_bytes(p)


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _remove_path(p: Path) -> None:
    # A real directory (never a symlink to one — that's a single link to
    # unlink, not a tree to walk) gets rmtree'd; everything else — a regular
    # file, a symlink, or a special file like a unix-domain socket (an ssh
    # ControlMaster's, which is neither is_file() nor is_dir()) — is just
    # unlinked.
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    else:
        try:
            p.unlink()
        except OSError:
            pass


def _refuse_if_is_root(p: Path, roots: Sequence[Path]) -> None:
    """Never removes a category root itself, even if some scan bug ever
    handed one back as an "eligible" item — only things *inside* a root are
    ever legitimate targets."""
    resolved = p.resolve()
    for root in roots:
        try:
            if resolved == root.resolve():
                raise HousekeepingError("refusing to remove %s — that's the category root itself" % p)
        except OSError:
            continue


def _act_remove_path(root: Path) -> Callable[[Dict[str, Any]], None]:
    def act(item: Dict[str, Any]) -> None:
        p = Path(item["path"])
        if not _within(root, p):
            raise HousekeepingError("refusing to remove %s — outside its category root %s" % (p, root))
        _refuse_if_is_root(p, [root])
        _remove_path(p)
    return act


def _act_remove_path_multi(roots: Sequence[Path]) -> Callable[[Dict[str, Any]], None]:
    def act(item: Dict[str, Any]) -> None:
        p = Path(item["path"])
        if not any(_within(root, p) for root in roots):
            raise HousekeepingError("refusing to remove %s — outside its category roots" % p)
        _refuse_if_is_root(p, roots)
        _remove_path(p)
    return act


def _act_reap_tmux_session(item: Dict[str, Any]) -> None:
    terminals_mod.reap_finished_session(item["session_name"])


def _act_forget_terminal_record(item: Dict[str, Any]) -> None:
    try:
        terminals_mod.kill(item["session_name"])
    except ValueError:
        pass  # already gone


def _act_remove_scheduler_item(item: Dict[str, Any]) -> None:
    scheduler.remove_item(item["item_id"])


# --------------------------------------------------------------------------- category scans
def _scan_thumbnails(record_observations: bool = False) -> Dict[str, Any]:
    root = settings.state_dir / "thumbs"
    total_count, total_bytes = 0, 0
    eligible: List[Dict[str, Any]] = []
    known_keys = datasets_mod.known_dataset_keys()
    kept_files: List[tuple] = []
    if root.is_dir():
        for key_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            dir_files = [f for f in key_dir.rglob("*") if f.is_file()]
            dir_bytes = sum(_file_bytes(f) for f in dir_files)
            total_count += len(dir_files)
            total_bytes += dir_bytes
            if key_dir.name not in known_keys:
                eligible.append({"path": str(key_dir), "bytes": dir_bytes, "reason": "orphan-dataset"})
            else:
                kept_files.extend((f, _file_bytes(f)) for f in dir_files)
    cap_bytes = int(get_settings()["thumbnail_cap_mb"] * 1024 * 1024)
    kept_bytes = sum(b for _, b in kept_files)
    if kept_bytes > cap_bytes:
        for f, b in sorted(kept_files, key=lambda x: _mtime(x[0])):
            if kept_bytes <= cap_bytes:
                break
            eligible.append({"path": str(f), "bytes": b, "reason": "over-cap"})
            kept_bytes -= b
    return {"label": "Thumbnails", "auto": True, "root": str(root),
            "total_count": total_count, "total_bytes": total_bytes, "eligible": eligible}


def _scan_attempt_logs(record_observations: bool = False) -> Dict[str, Any]:
    root = settings.attempt_logs_root
    total_count, total_bytes = 0, 0
    eligible: List[Dict[str, Any]] = []
    known = experiments_mod.known_attempt_ids()
    if root.is_dir():
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            total_count += 1
            b = _dir_bytes(d)
            total_bytes += b
            if d.name not in known:
                eligible.append({"path": str(d), "bytes": b, "reason": "orphan"})
    return {"label": "Attempt logs", "auto": True, "root": str(root),
            "total_count": total_count, "total_bytes": total_bytes, "eligible": eligible}


def _scan_terminal_snapshots(record_observations: bool = False) -> Dict[str, Any]:
    root = settings.dashboard_log_dir
    total_count, total_bytes = 0, 0
    eligible: List[Dict[str, Any]] = []
    known = terminals_mod.known_session_names()
    if root.is_dir():
        for f in sorted(p for p in root.iterdir() if p.is_file()):
            total_count += 1
            b = _file_bytes(f)
            total_bytes += b
            if f.stem not in known:
                eligible.append({"path": str(f), "bytes": b, "reason": "orphan"})
    return {"label": "Terminal snapshots", "auto": True, "root": str(root),
            "total_count": total_count, "total_bytes": total_bytes, "eligible": eligible}


def _session_is_attempt_owned(session_name: str, items_by_id: Dict[str, Any]) -> bool:
    item = next((i for i in items_by_id.values() if i.get("session_name") == session_name), None)
    if item is None:
        return False
    return experiments_mod.experiment_id_for_scheduler_item(item["id"]) is not None


def _scan_finished_tmux_sessions(record_observations: bool = False) -> Dict[str, Any]:
    """A managed session whose command has already exited (status
    completed/failed/stopped) but whose tmux session is still alive —
    nothing in the dispatcher ever killed it (only an explicit user
    cancel/remove does). An attempt-owned one (traced through its
    scheduler item to an Experiment, same lookup MachineRunner._session_label
    uses for issue 9's labels) is eligible the moment it's noticed: its logs
    are the attempt's own persisted copy, not this idle shell. An ad-hoc one
    (Configs page "Launch in terminal"/"Add to schedule", or no scheduler
    item at all) gets a grace period instead — someone may come back to
    read it — tracked via this module's own "first noticed finished"
    bookkeeping (_seen_store), since a plain terminal record has no
    finish timestamp of its own.

    *record_observations* is False for inventory() and for
    clean(dry_run=True): the *first* time a finished-but-alive session is
    seen, its "seen at" timestamp is only written on a real (non-dry-run)
    clean pass, so a dry-run can never advance the ad-hoc grace clock —
    the one deliberate exception to "clean() is the only thing that writes
    outside test fixtures," and even so it only ever writes this module's
    own private bookkeeping file, never touches the categories being swept.
    """
    total_count = 0
    eligible: List[Dict[str, Any]] = []
    grace_seconds = float(get_settings()["adhoc_session_grace_hours"]) * 3600.0
    seen = _load_seen()
    changed = False
    items_by_id = scheduler.items_by_id()
    for host in hosts.list_hosts():
        try:
            records = terminals_mod.list_terminals(host_id=host.id)
        except Exception:
            continue  # unreachable/broken host — never blocks the sweep
        for r in records:
            if not r.get("managed") or not r.get("alive") or r.get("status") not in ("completed", "failed", "stopped"):
                continue
            total_count += 1
            session = r["session_name"]
            before = seen.get(session)
            first_seen = _get_or_seed_seen_at(session, seen, record_observations)
            if first_seen is not None and before is None:
                changed = True
            if _session_is_attempt_owned(session, items_by_id):
                eligible.append({"session_name": session, "host_id": host.id, "bytes": 0, "reason": "attempt-owned"})
                continue
            if first_seen is None or _age_seconds(first_seen) < grace_seconds:
                continue
            eligible.append({"session_name": session, "host_id": host.id, "bytes": 0, "reason": "ad-hoc-stale"})
    if changed:
        _save_seen(seen)
    return {"label": "Finished tmux sessions", "auto": True, "root": None,
            "total_count": total_count, "total_bytes": 0, "eligible": eligible}


def _scan_terminal_records(record_observations: bool = False) -> Dict[str, Any]:
    total_count = 0
    eligible: List[Dict[str, Any]] = []
    retention_seconds = float(get_settings()["retention_days"]) * 86400.0
    seen = _load_seen()
    changed = False
    for r in terminals_mod.list_terminals():
        if not r.get("managed") or r.get("status") not in _MANAGED_STATUSES or r.get("alive"):
            continue  # still alive: the finished-tmux-sessions sweep handles that first
        total_count += 1
        session = r["session_name"]
        if r.get("status") == "lost" and r.get("lost_at"):
            first_seen = r["lost_at"]
        else:
            before = seen.get(session)
            first_seen = _get_or_seed_seen_at(session, seen, record_observations)
            if first_seen is not None and before is None:
                changed = True
        if first_seen is None or _age_seconds(first_seen) < retention_seconds:
            continue
        eligible.append({"session_name": session, "bytes": 0, "reason": r.get("status") or "finished"})
    if changed:
        _save_seen(seen)
    return {"label": "Terminal records", "auto": True, "root": None,
            "total_count": total_count, "total_bytes": 0, "eligible": eligible}


def _scan_scheduler_history(record_observations: bool = False) -> Dict[str, Any]:
    total_count = 0
    eligible: List[Dict[str, Any]] = []
    retention_seconds = float(get_settings()["retention_days"]) * 86400.0
    protected = experiments_mod.scheduler_item_ids_for_non_terminal_attempts()
    for item in scheduler.list_items()["items"]:
        if item.get("status") not in scheduler.ITEM_TERMINAL_STATUSES:
            continue
        total_count += 1
        if item["id"] in protected:
            continue
        ended_at = item.get("ended_at")
        if not ended_at or _age_seconds(ended_at) < retention_seconds:
            continue
        eligible.append({"item_id": item["id"], "bytes": 0, "reason": item.get("status") or "finished"})
    return {"label": "Scheduler history", "auto": True, "root": None,
            "total_count": total_count, "total_bytes": 0, "eligible": eligible}


def _scan_remote_staging(record_observations: bool = False) -> Dict[str, Any]:
    root = settings.repo_root / "outputs" / "remote"
    total_count, total_bytes = 0, 0
    eligible: List[Dict[str, Any]] = []
    known = experiments_mod.known_attempt_ids()
    if root.is_dir():
        for host_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            attempt_dirs = [p for p in host_dir.iterdir() if p.is_dir()]
            total_count += len(attempt_dirs)
            host_bytes = sum(_dir_bytes(a) for a in attempt_dirs)
            total_bytes += host_bytes
            orphan_all = all(a.name not in known for a in attempt_dirs)
            if not attempt_dirs or orphan_all:
                # Every attempt dir here is orphaned (or there simply are
                # none) — remove the whole host dir in one go rather than
                # leaving an empty one behind.
                eligible.append({"path": str(host_dir), "bytes": host_bytes,
                                  "reason": "orphan" if attempt_dirs else "empty-host-dir"})
            else:
                for a in attempt_dirs:
                    if a.name not in known:
                        eligible.append({"path": str(a), "bytes": _dir_bytes(a), "reason": "orphan"})
    return {"label": "Remote staging", "auto": True, "root": str(root),
            "total_count": total_count, "total_bytes": total_bytes, "eligible": eligible}


def _scan_overlays(record_observations: bool = False) -> Dict[str, Any]:
    root = settings.repo_root / framework.OVERLAY_DIR
    total_count, total_bytes = 0, 0
    eligible: List[Dict[str, Any]] = []
    known = experiments_mod.known_experiment_ids()
    if root.is_dir():
        for f in sorted(p for p in root.iterdir() if p.is_file()):
            total_count += 1
            b = _file_bytes(f)
            total_bytes += b
            if f.stem not in known:
                eligible.append({"path": str(f), "bytes": b, "reason": "orphan"})
    return {"label": "Overlays", "auto": True, "root": str(root),
            "total_count": total_count, "total_bytes": total_bytes, "eligible": eligible}


def _scan_crash_temp_dirs(record_observations: bool = False) -> Dict[str, Any]:
    root = Path(tempfile.gettempdir())
    total_count, total_bytes = 0, 0
    eligible: List[Dict[str, Any]] = []
    my_uid = os.getuid() if hasattr(os, "getuid") else None
    try:
        entries = list(root.iterdir())
    except OSError:
        entries = []
    for p in entries:
        if not any(p.name.startswith(prefix) for prefix in CRASH_TEMP_PREFIXES):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        total_count += 1
        b = _item_bytes(p)
        total_bytes += b
        if my_uid is not None and st.st_uid != my_uid:
            continue  # never another user's temp entries, even with a matching prefix
        age_days = (time.time() - st.st_mtime) / 86400.0
        if age_days >= CRASH_TEMP_MAX_AGE_DAYS:
            eligible.append({"path": str(p), "bytes": b, "reason": "crash-temp"})
    return {"label": "Crash temp dirs", "auto": True, "root": str(root),
            "total_count": total_count, "total_bytes": total_bytes, "eligible": eligible}


def _scan_kaggle_downloads(record_observations: bool = False) -> Dict[str, Any]:
    root = settings.repo_root / "outputs" / "kaggle"
    total_count, total_bytes = 0, 0
    eligible: List[Dict[str, Any]] = []
    known = experiments_mod.known_experiment_ids()
    if root.is_dir():
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            total_count += 1
            b = _dir_bytes(d)
            total_bytes += b
            if d.name not in known:
                eligible.append({"path": str(d), "bytes": b, "reason": "orphan"})
    return {"label": "Kaggle downloads", "auto": False, "root": str(root),
            "total_count": total_count, "total_bytes": total_bytes, "eligible": eligible}


def _scan_migration_backups(record_observations: bool = False) -> Dict[str, Any]:
    total_count, total_bytes = 0, 0
    eligible: List[Dict[str, Any]] = []
    roots = [config_mod.DATA_DIR, settings.state_dir]
    seen_paths = set()
    for root in roots:
        if not root.is_dir():
            continue
        candidates = list(root.glob("*.pre-v3.bak"))
        for name in _MIGRATION_BACKUP_NAMES:
            p = root / name
            if p.is_file():
                candidates.append(p)
        for p in sorted(candidates):
            resolved = str(p.resolve())
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            total_count += 1
            b = _file_bytes(p)
            total_bytes += b
            eligible.append({"path": str(p), "bytes": b, "reason": "legacy-store"})
    return {"label": "Migration backups / retired stores", "auto": False, "root": None,
            "total_count": total_count, "total_bytes": total_bytes, "eligible": eligible}


def _scan_orphan_account_dirs(record_observations: bool = False) -> Dict[str, Any]:
    total_count, total_bytes = 0, 0
    eligible: List[Dict[str, Any]] = []
    kaggle_known = {a["name"] for a in kaggle_backend.list_accounts()}
    colab_known = {a["name"] for a in colab_backend.list_accounts()}
    groups = [
        (config_mod.SYSTEM_KAGGLE_CREDS_DIR, kaggle_known),
        (settings.kaggle_creds_dir, kaggle_known),
        (config_mod.SYSTEM_COLAB_CREDS_DIR, colab_known),
    ]
    for root, known in groups:
        if not root.is_dir():
            continue
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            total_count += 1
            b = _dir_bytes(d)
            total_bytes += b
            if d.name not in known:
                eligible.append({"path": str(d), "bytes": b, "reason": "orphan-account"})
    return {"label": "Orphan account dirs", "auto": False, "root": None,
            "total_count": total_count, "total_bytes": total_bytes, "eligible": eligible}


def _scan_ssh_control_sockets(record_observations: bool = False) -> Dict[str, Any]:
    root = transport_mod.CONTROL_DIR
    total_count, total_bytes = 0, 0
    eligible: List[Dict[str, Any]] = []
    if root.is_dir():
        for p in sorted(root.iterdir()):
            if not p.is_socket():
                continue
            total_count += 1
            b = _file_bytes(p)
            total_bytes += b
            if _is_stale_socket(p):
                eligible.append({"path": str(p), "bytes": b, "reason": "stale-socket"})
    return {"label": "SSH control sockets", "auto": True, "root": str(root),
            "total_count": total_count, "total_bytes": total_bytes, "eligible": eligible}


def _is_stale_socket(p: Path) -> bool:
    """A live ControlMaster is a listening unix socket — connecting to it
    (and immediately closing) tells stale from alive without ever shelling
    out to ssh. No listener (refused/gone) means whatever created it is
    dead and the file is just litter."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            s.connect(str(p))
            return False
        except OSError:
            return True
        finally:
            s.close()
    except OSError:
        return True


# --------------------------------------------------------------------------- action dispatch (built per-call, roots always fresh)
def _action_for(key: str) -> Callable[[Dict[str, Any]], None]:
    if key == "thumbnails":
        return _act_remove_path(settings.state_dir / "thumbs")
    if key == "attempt_logs":
        return _act_remove_path(settings.attempt_logs_root)
    if key == "terminal_snapshots":
        return _act_remove_path(settings.dashboard_log_dir)
    if key == "finished_tmux_sessions":
        return _act_reap_tmux_session
    if key == "terminal_records":
        return _act_forget_terminal_record
    if key == "scheduler_history":
        return _act_remove_scheduler_item
    if key == "remote_staging":
        return _act_remove_path(settings.repo_root / "outputs" / "remote")
    if key == "overlays":
        return _act_remove_path(settings.repo_root / framework.OVERLAY_DIR)
    if key == "crash_temp_dirs":
        return _act_remove_path(Path(tempfile.gettempdir()))
    if key == "kaggle_downloads":
        return _act_remove_path(settings.repo_root / "outputs" / "kaggle")
    if key == "migration_backups":
        return _act_remove_path_multi([config_mod.DATA_DIR, settings.state_dir])
    if key == "orphan_account_dirs":
        return _act_remove_path_multi(
            [config_mod.SYSTEM_KAGGLE_CREDS_DIR, settings.kaggle_creds_dir, config_mod.SYSTEM_COLAB_CREDS_DIR]
        )
    if key == "ssh_control_sockets":
        return _act_remove_path(transport_mod.CONTROL_DIR)
    raise HousekeepingError("Unknown housekeeping category %r" % key)


_SCANNERS: "Dict[str, Callable[[bool], Dict[str, Any]]]" = {
    "thumbnails": _scan_thumbnails,
    "attempt_logs": _scan_attempt_logs,
    "terminal_snapshots": _scan_terminal_snapshots,
    "finished_tmux_sessions": _scan_finished_tmux_sessions,
    "terminal_records": _scan_terminal_records,
    "scheduler_history": _scan_scheduler_history,
    "remote_staging": _scan_remote_staging,
    "overlays": _scan_overlays,
    "crash_temp_dirs": _scan_crash_temp_dirs,
    "kaggle_downloads": _scan_kaggle_downloads,
    "migration_backups": _scan_migration_backups,
    "orphan_account_dirs": _scan_orphan_account_dirs,
    "ssh_control_sockets": _scan_ssh_control_sockets,
}

# Order matters for a combined clean() call: finished_tmux_sessions must run
# before terminal_records (it's what makes a record's session stop being
# alive, and it seeds the "first noticed finished" timestamp terminal_records
# then reads) — CATEGORY_ORDER is that order; AUTO_CATEGORIES is the subset
# ensure_worker_started() runs unattended.
CATEGORY_ORDER: List[str] = [
    "thumbnails", "attempt_logs", "terminal_snapshots", "finished_tmux_sessions",
    "terminal_records", "scheduler_history", "remote_staging", "overlays",
    "crash_temp_dirs", "kaggle_downloads", "migration_backups", "orphan_account_dirs",
    "ssh_control_sockets",
]
AUTO_CATEGORIES: List[str] = [
    "thumbnails", "attempt_logs", "terminal_snapshots", "finished_tmux_sessions",
    "terminal_records", "scheduler_history", "remote_staging", "overlays",
    "crash_temp_dirs", "ssh_control_sockets",
]


# --------------------------------------------------------------------------- public entry points
def inventory() -> Dict[str, Any]:
    """Read-only: every category's total footprint plus what's currently
    eligible for removal — the Storage panel's own listing, and a dry-run
    preview without asking for one explicitly (clean() with dry_run=True
    recomputes the same numbers, category by category, right before it
    would act on them)."""
    out: Dict[str, Any] = {}
    for key in CATEGORY_ORDER:
        try:
            result = _SCANNERS[key](False)
        except Exception as e:  # noqa: BLE001 — one bad category must not blank the whole panel
            result = {"label": key, "auto": key in AUTO_CATEGORIES, "root": None,
                      "total_count": 0, "total_bytes": 0, "eligible": [], "error": str(e)}
        eligible = result.get("eligible", [])
        out[key] = {
            **{k: v for k, v in result.items() if k != "eligible"},
            "eligible_count": len(eligible),
            "eligible_bytes": sum(i.get("bytes", 0) for i in eligible),
        }
    return out


def clean(categories: Sequence[str], dry_run: bool = True) -> Dict[str, Any]:
    """Removes (or, dry_run=True, just tallies) every eligible item in each
    of *categories* — categories are independent; asking for one never
    touches another. dry_run=True never calls an action function at all."""
    out: Dict[str, Any] = {}
    for key in categories:
        scan = _SCANNERS.get(key)
        if scan is None:
            out[key] = {"error": "Unknown housekeeping category %r" % key, "removed_count": 0, "freed_bytes": 0}
            continue
        result = scan(not dry_run)
        eligible = result.get("eligible", [])
        removed, freed, errors = 0, 0, []
        if dry_run:
            removed, freed = len(eligible), sum(i.get("bytes", 0) for i in eligible)
        else:
            act = _action_for(key)
            for item in eligible:
                try:
                    act(item)
                    removed += 1
                    freed += item.get("bytes", 0)
                except Exception as e:  # noqa: BLE001 — one bad item must not abort the whole category
                    errors.append("%s: %s" % (item.get("path") or item.get("session_name") or item.get("item_id"), e))
        out[key] = {"dry_run": dry_run, "removed_count": removed, "freed_bytes": freed, "errors": errors}
    return out


# --------------------------------------------------------------------------- background trigger (F5.3)
_worker_started = False


def ensure_worker_started() -> None:
    """Runs every AUTO_CATEGORIES sweep once at startup and once a day
    after that, in a background thread — never on the request path, so it
    can never delay server start. XDASH_DISABLE_BACKGROUND=1 (the test
    harness) skips this entirely, same as scheduler.ensure_worker_started()/
    experiments.ensure_dispatcher_started()."""
    global _worker_started
    if _worker_started or background_disabled():
        return
    _worker_started = True

    def loop():
        while True:
            try:
                clean(AUTO_CATEGORIES, dry_run=False)
            except Exception:
                pass
            time.sleep(86400)

    threading.Thread(target=loop, daemon=True, name="housekeeping-sweep").start()
