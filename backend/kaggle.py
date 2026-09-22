"""Multi-account Kaggle fleet ops: push/status/download for notebook-based
training kernels across several Kaggle accounts, plus registering a
downloaded run into the host repo's own orchestration ledger.

Drives the official `kaggle` CLI via subprocess — never imports the `kaggle`
pip package in-process. Two reasons: (1) it's already installed wherever
training itself runs (see env_activate_cmd in dashboard_config.yaml), so this
adds no new dependency; (2) per-account credential switching is done via
per-account env vars (KAGGLE_CONFIG_DIR / KAGGLE_API_TOKEN / KAGGLE_USERNAME
/ KAGGLE_KEY), all process-global — a subprocess call gets its own isolated
`env=`, so concurrent bulk operations across accounts (see
push_all/refresh_all/download_all) can't race the way an in-process client
switching a shared os.environ would.

An account can hold a classic username/key pair, a newer access token, or
both — see the module comment above _validate_legacy_pair for why, and
_run_kaggle for how the right one gets used without this module having to
know which `kaggle` CLI version is actually installed.

Account/worker registry and credentials are dashboard-owned state (data/
kaggle_accounts.json, data/kaggle_accounts/<name>/{kaggle.json,access_token}
— all gitignored), independent of any config already used to plan work in
the host repo. Downloaded results land in the host repo's own results/ dir
(wherever each worker's `results_dir` points) and get registered into its
artifacts/ledger — mirroring orchestration/ledger.py's schema stdlib-only,
the same way backend/ledger.py already reads it without importing that
package.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import settings, Settings, SYSTEM_KAGGLE_ACCOUNTS_FILE, SYSTEM_KAGGLE_CREDS_DIR
from . import configs as cfg
from . import notifications as notif

_lock = threading.Lock()          # guards kaggle_accounts.json / kaggle_state.json
_ledger_lock = threading.Lock()   # guards concurrent appends to the host repo's runs.csv

STATUS_RE = re.compile(r'has status "([^"]+)"')
_ENUM_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.")
# Live-verified 2026-09 against the actually-installed `kaggle` CLI (pip package "kaggle" 1.7.4.5,
# github.com/Kaggle/kaggle-api): `kernels status` prints the Python enum's own repr — e.g.
# `has status "KernelWorkerStatus.RUNNING"` — not the bare lowercase string
# IN_PROGRESS_STATUSES/FINISHED_STATUSES/FINAL_STATUSES below expect. Confirmed via a real push +
# status poll during this feature's own testing (DASHBOARD_REDESIGN_PLAN.md §2.1's fact-check).
# Without this normalization, over-budget detection, the poller's final-status/notification
# trigger, and download_all()'s "only download finished workers" filter all silently never fire
# against this CLI version — every comparison below is an exact-match against a lowercase set.
_STATUS_ALIASES = {"cancelacknowledged": "cancelAcknowledged"}


def _normalize_kaggle_status(raw: str) -> str:
    value = _ENUM_PREFIX_RE.sub("", (raw or "").strip()).strip().lower()
    return _STATUS_ALIASES.get(value, value)
IN_PROGRESS_STATUSES = {"queued", "preparing", "running"}
FINISHED_STATUSES = {"complete"}
FINAL_STATUSES = {"complete", "error", "cancelAcknowledged"}  # tick() stops polling/chains past these
HISTORY_LIMIT = 50  # per-worker event log cap in kaggle_state.json — a rolling window, not an audit archive

# Template-notebook launch (dashboard redesign, 2026-09): one push == one config, same shape as
# a local-device launch, instead of the notebook itself hand-authoring a whole batch of configs. The
# dashboard renders this cell's three placeholders into a copy of the worker's template notebook
# (settings.kaggle_default_template, or a per-worker override) before every push — see
# _render_launch_notebook(). A worker that instead sets its own `notebook_path` (the original,
# still-supported shape — e.g. the existing iccit-kaggle-worker3/4 notebooks) is pushed verbatim,
# unrendered, exactly as before; the two modes are mutually exclusive per worker, checked in
# push() below.
LAUNCH_SPEC_MARKER = "# DASHBOARD:LAUNCH_SPEC"
_LAUNCH_PLACEHOLDERS = {
    "config_path": "__DASHBOARD_CONFIG_PATH__",
    "extra_args": "__DASHBOARD_EXTRA_ARGS__",
    # Resolved server-side from settings.train_script/eval_script, never hardcoded in the
    # template — a deployment may point these at a wrapper (e.g. this study's own
    # scripts/run_iccit_sweep.py, which loops the pre-registered seeds and writes the
    # manifest/ledger rows train.py's own CLI never does) rather than train.py/eval.py
    # directly. Mirrors tmux_runner.build_launch_command()'s own settings.train_script /
    # settings.eval_script choice exactly, so a Kaggle-launched run and a local-device-launched run
    # of the same config actually run the *same command* — not just a structurally similar one.
    #
    # No "mode" placeholder (EXPERIMENT_AUTOMATION_PLAN.md §2.4): a Kaggle push covers the
    # whole experiment, train then eval, inside one kernel execution — there is no second push
    # to chain a separate eval half onto the way scheduler.add_item(mode="both") chains two
    # local tmux sessions. Both scripts are always resolved and always run.
    "train_script": "__DASHBOARD_TRAIN_SCRIPT__",
    "eval_script": "__DASHBOARD_EVAL_SCRIPT__",
    # settings.eval_default_args, space-joined and shell-quoted — mirrors terminals.py's own
    # `extra_flags = eval_default_args if mode == "eval" else []` merge into
    # tmux_runner.build_launch_command() exactly, so a Kaggle-run eval gets the same flags
    # (e.g. segpriors' --ensemble) a local eval of the same config always gets. Applied only
    # to the eval command, never the train one.
    "eval_extra_flags": "__DASHBOARD_EVAL_EXTRA_FLAGS__",
    "max_hours": "__DASHBOARD_MAX_HOURS__",
    # First entry of the same dataset_sources list that goes into kernel-metadata.json's
    # declarative attach (see _kernel_metadata()) -- handed to the template too so its
    # dataset-attach cell can fall back to a direct `kagglehub.dataset_download()` when the
    # declarative attach didn't take (e.g. a worker whose dataset_sources drifted out of sync,
    # or Kaggle simply not mounting it under /kaggle/input/ for this kernel). Empty string when
    # nothing resolved, in which case the template cell keeps its old attach-only behavior.
    "dataset_source": "__DASHBOARD_DATASET_SOURCE__",
}

# Mirrors orchestration/ledger.py's RUNS_FIELDS in the host repo exactly —
# a downloaded worker's own artifacts/ledger/runs.csv already has these
# columns, so registration is a straight copy-and-append, not a re-derivation.
RUNS_FIELDS = [
    "run_id", "config_hash", "experiment_name", "model_name", "dataset_name",
    "seed", "fold", "status", "start_time", "end_time", "gpu_hours",
    "best_metric", "monitor_metric", "git_commit", "git_dirty", "manifest_path",
]


class KaggleOpsError(Exception):
    """Expected failure (bad credentials, subprocess error, unknown account/worker) —
    routes map this to a 4xx, not a stack trace."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- storage
# Two scopes (see config.SYSTEM_KAGGLE_ACCOUNTS_FILE for why the system one
# has to exist): "system" accounts are shared by every repo profile, "repo"
# accounts belong to the active profile alone.
#
# The split runs along account/worker, not account alone: an *account* is a
# person's Kaggle login — credentials and one weekly quota — while a *worker*
# is a kernel running a specific repo's code, with a repo-relative
# results_dir, its own kernel_slug and (per the automation plan §2.5) its own
# attached datasets. So a system-wide account carries workers for several
# profiles, tagged with which profile each belongs to.
#
# _load_accounts() hides that: it returns the merged list with each account's
# `workers` already filtered to the active profile, so every existing caller
# (push, list_accounts, _tick, _find_worker_and_account, ...) keeps reading
# `account["workers"]` and means the same thing it always did.
SCOPE_SYSTEM = "system"
SCOPE_REPO = "repo"


def _scope_paths(scope: str):
    if scope == SCOPE_SYSTEM:
        return SYSTEM_KAGGLE_ACCOUNTS_FILE, SYSTEM_KAGGLE_CREDS_DIR
    return settings.kaggle_accounts_file, settings.kaggle_creds_dir


def _load_scope(scope: str) -> Dict[str, Any]:
    path, _ = _scope_paths(scope)
    if not path.exists():
        return {"accounts": []}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {"accounts": []}
    data.setdefault("accounts", [])
    return data


def _save_scope(scope: str, data: Dict[str, Any]) -> None:
    path, _ = _scope_paths(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _load_accounts() -> Dict[str, Any]:
    """Merged, active-profile view of both scopes. Each account gains a
    "scope" key; a system account's workers are filtered to those tagged for
    the active profile (an untagged worker is treated as this profile's, so
    a registry written before this split keeps working)."""
    profile = settings.profile_name
    merged: List[Dict[str, Any]] = []
    for account in _load_scope(SCOPE_SYSTEM)["accounts"]:
        view = dict(account, scope=SCOPE_SYSTEM)
        view["workers"] = [
            w for w in account.get("workers", [])
            if w.get("profile", profile) == profile
        ]
        merged.append(view)
    for account in _load_scope(SCOPE_REPO)["accounts"]:
        merged.append(dict(account, scope=SCOPE_REPO))
    return {"accounts": merged}


def _save_accounts(data: Dict[str, Any]) -> None:
    """Routes each account in a merged view back to the store it came from.
    For a system account only the active profile's workers are replaced —
    other profiles' workers are read from disk and preserved, since the view
    handed to the caller never contained them."""
    profile = settings.profile_name
    system_out: List[Dict[str, Any]] = []
    repo_out: List[Dict[str, Any]] = []
    stored_system = {a["name"]: a for a in _load_scope(SCOPE_SYSTEM)["accounts"]}

    for view in data["accounts"]:
        record = {k: v for k, v in view.items() if k != "scope"}
        if view.get("scope") == SCOPE_SYSTEM:
            others = [
                w for w in stored_system.get(view["name"], {}).get("workers", [])
                if w.get("profile", profile) != profile
            ]
            record["workers"] = others + [
                dict(w, profile=profile) for w in view.get("workers", [])
            ]
            system_out.append(record)
        else:
            repo_out.append(record)

    _save_scope(SCOPE_SYSTEM, {"accounts": system_out})
    _save_scope(SCOPE_REPO, {"accounts": repo_out})


def _load_state() -> Dict[str, Any]:
    if not settings.kaggle_state_file.exists():
        return {}
    try:
        return json.loads(settings.kaggle_state_file.read_text())
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    settings.kaggle_state_file.write_text(json.dumps(state, indent=2))


def _update_worker_state(worker_id: str, patch: Dict[str, Any], event: Optional[str] = None) -> None:
    """Merges *patch* into the worker's state record. *event*, if given, also
    appends a timestamped entry to that record's rolling history log (capped
    at HISTORY_LIMIT) — not every patch is history-worthy (e.g. a routine
    status poll that didn't change anything), so callers opt in explicitly."""
    with _lock:
        state = _load_state()
        rec = state.get(worker_id, {})
        rec.update(patch)
        if event:
            history = rec.setdefault("history", [])
            history.append({"at": _now_iso(), "event": event})
            del history[:-HISTORY_LIMIT]
        state[worker_id] = rec
        _save_state(state)


def _find_account(data: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    return next((a for a in data["accounts"] if a["name"] == name), None)


def _find_worker(account: Dict[str, Any], worker_id: str) -> Optional[Dict[str, Any]]:
    return next((w for w in account.get("workers", []) if w["worker_id"] == worker_id), None)


def _find_worker_and_account(data: Dict[str, Any], worker_id: str):
    for account in data["accounts"]:
        w = _find_worker(account, worker_id)
        if w is not None:
            return account, w
    return None, None


# --------------------------------------------------------------------------- accounts
# Kaggle now issues two incompatible credential shapes: the classic
# username/key pair (kaggle.json) and a newer bearer access token. Which one
# an installed `kaggle` CLI actually understands depends on its version — see
# the comment on _run_kaggle for how that's resolved without this module
# having to sniff a version string itself. An account can store either credential,
# or both (e.g. a token for everyday use plus the classic pair as a fallback
# that still works if the token is later revoked).
CREDS_FILENAME = "kaggle.json"
TOKEN_FILENAME = "access_token"


def _validate_legacy_pair(username: str, key: str) -> Dict[str, str]:
    username, key = (username or "").strip(), (key or "").strip()
    if not username or not key:
        raise KaggleOpsError("Classic auth needs both a username and a key")
    if key.upper().startswith("KGAT"):
        # A newer-format token doesn't authenticate the same way as a classic
        # key with this CLI's Basic Auth — redirect to the token field instead
        # of storing something that will only fail later.
        raise KaggleOpsError(
            "That looks like a new-format API token, not a classic key — paste it into "
            "the API Token field instead."
        )
    return {"username": username, "key": key}


def _validate_access_token(raw_token: str) -> str:
    token = (raw_token or "").strip()
    if not token:
        raise KaggleOpsError("API token is empty")
    if token.startswith("{"):
        raise KaggleOpsError(
            "That looks like a kaggle.json payload, not a bare token — paste the "
            "username/key into the classic fields instead."
        )
    if "\n" in token or " " in token:
        raise KaggleOpsError("API token should be a single unbroken string, with no whitespace")
    return token


def _account_scope(name: str) -> str:
    """Which store holds *name*. Names are unique across both scopes
    (add_account refuses a cross-scope collision), so resolving by name alone
    is unambiguous — which keeps _run_kaggle(args, account_name) and every
    one of its call sites unchanged."""
    if any(a["name"] == name for a in _load_scope(SCOPE_SYSTEM)["accounts"]):
        return SCOPE_SYSTEM
    return SCOPE_REPO


def _creds_dir(name: str) -> Path:
    _, creds_root = _scope_paths(_account_scope(name))
    return creds_root / name


def _source_notebook_path(worker: Dict[str, Any]) -> Path:
    """Absolute path to the notebook this worker's next push is rendered from
    (template-backed) or copied from verbatim (notebook-backed) — see
    add_worker()'s docstring. A worker's own notebook_path/template_path is
    repo-relative (a file the operator explicitly chose inside the host
    repo); the profile-wide default template is XDash-owned
    (settings.kaggle_default_template_file, under data/<profile>/ — never
    the host repo, see that setting's own comment for why)."""
    override = worker.get("notebook_path") or worker.get("template_path")
    if override:
        return settings.repo_root / override
    return settings.kaggle_default_template_file


def _notebook_changed(worker: Dict[str, Any], worker_state: Dict[str, Any]) -> Optional[bool]:
    """True if the worker's on-disk *source* notebook/template differs from
    the one in effect at the last push (by content hash) — None if it's
    never been pushed, or the file is currently missing, since "changed"
    isn't a meaningful answer in either case. Compared against
    `pushed_template_hash` (the source file's own hash), not
    `pushed_notebook_hash` (the exact, possibly-rendered bytes actually
    pushed) — for a template-backed worker those two differ on every push by
    design (config/extra_args get baked in), which would otherwise make this
    always report "changed" even when the template itself is untouched."""
    pushed_hash = worker_state.get("pushed_template_hash") or worker_state.get("pushed_notebook_hash")
    if not pushed_hash:
        return None
    notebook_abs = _source_notebook_path(worker)
    if not notebook_abs.is_file():
        return None
    try:
        current_hash = hashlib.sha1(notebook_abs.read_bytes()).hexdigest()
    except OSError:
        return None
    return current_hash != pushed_hash


def list_accounts() -> List[Dict[str, Any]]:
    """Accounts + workers, each worker enriched with its last known status
    (from kaggle_state.json), a self-tracked usage estimate/history, and
    whether its notebook has changed since the last push. Never touches the
    network — see refresh_status/refresh_all for that."""
    data = _load_accounts()
    state = _load_state()
    result = []
    for account in data["accounts"]:
        workers = []
        for w in account.get("workers", []):
            w_state = state.get(w["worker_id"], {})
            workers.append({**w, **w_state, "notebook_changed": _notebook_changed(w, w_state)})
        creds_dir = _creds_dir(account["name"])
        result.append({
            "name": account["name"],
            "kaggle_username": account.get("kaggle_username"),
            "scope": account.get("scope", SCOPE_REPO),
            "has_legacy_key": (creds_dir / CREDS_FILENAME).is_file(),
            "has_api_token": (creds_dir / TOKEN_FILENAME).is_file(),
            "workers": workers,
            "usage_estimate": estimate_usage(account["name"]),
            "usage_history": usage_history(account["name"]),
        })
    return result


def add_account(
    name: str, username: str = "", key: str = "", api_token: str = "",
    scope: str = SCOPE_REPO,
) -> Dict[str, Any]:
    """*scope* "system" registers the account for every repo profile (one set
    of credentials, one quota); "repo" (the default) keeps it to the active
    profile. A name may exist in only one scope — an account visible twice
    under one label would be ambiguous everywhere it's referenced by name,
    including credential lookup."""
    name = (name or "").strip()
    if not name:
        raise KaggleOpsError("Missing account name")
    if scope not in (SCOPE_SYSTEM, SCOPE_REPO):
        raise KaggleOpsError(f"scope must be '{SCOPE_SYSTEM}' or '{SCOPE_REPO}', got {scope!r}")
    username, key, api_token = (username or ""), (key or ""), (api_token or "")

    # Gate legacy validation on `key` alone, not `username` — username is
    # required regardless of which credential type is used (it's also how a
    # token-only account identifies itself), so branching on it here would
    # wrongly demand a classic key whenever a username was typed.
    legacy = _validate_legacy_pair(username, key) if key.strip() else None
    token = _validate_access_token(api_token) if api_token.strip() else None
    if not legacy and not token:
        raise KaggleOpsError("Provide a classic username/key pair, an API token, or both")

    resolved_username = legacy["username"] if legacy else (username or "").strip()
    if not resolved_username:
        raise KaggleOpsError("Kaggle username is required (Kaggle gives no way to derive it from a bare token)")

    with _lock:
        data = _load_accounts()
        if _find_account(data, name) is not None:
            raise KaggleOpsError(f"Account '{name}' already exists")
        _, creds_root = _scope_paths(scope)
        creds_dir = creds_root / name
        creds_dir.mkdir(parents=True, exist_ok=True)
        if legacy:
            _write_secret(creds_dir / CREDS_FILENAME, json.dumps(legacy))
        if token:
            _write_secret(creds_dir / TOKEN_FILENAME, token)
        data["accounts"].append({
            "name": name, "kaggle_username": resolved_username, "workers": [], "scope": scope,
        })
        _save_accounts(data)
    return {"name": name, "kaggle_username": resolved_username, "scope": scope}


def _write_secret(path: Path, text: str) -> None:
    path.write_text(text)
    os.chmod(path, 0o600)


def update_credentials(
    name: str, username: str = "", key: str = "", api_token: str = "",
) -> Dict[str, Any]:
    """Rotates one or both stored credentials for an existing account, and/or
    just relabels its Kaggle username, without touching its workers — the
    account-delete flow wipes worker assignments too, which is the wrong
    tool for "my key expired, swap it in" or "I typo'd the username".

    A username with no key is a pure relabel (keeps a stored legacy
    kaggle.json's own username field in sync too, so KAGGLE_USERNAME — read
    from that file by _run_kaggle — never disagrees with what's shown here);
    a username with a key rotates the key and takes the username that came
    with it, exactly like before."""
    username, key, api_token = (username or "").strip(), (key or "").strip(), (api_token or "").strip()
    token = _validate_access_token(api_token) if api_token else None
    if not (username or key) and not token:
        raise KaggleOpsError("Provide a new username, key, API token, or some combination")

    with _lock:
        data = _load_accounts()
        account = _find_account(data, name)
        if account is None:
            raise KaggleOpsError(f"Unknown account '{name}'")
        creds_dir = _creds_dir(name)
        creds_dir.mkdir(parents=True, exist_ok=True)

        if key:
            legacy = _validate_legacy_pair(username or account.get("kaggle_username", ""), key)
            _write_secret(creds_dir / CREDS_FILENAME, json.dumps(legacy))
            account["kaggle_username"] = legacy["username"]
        elif username:
            account["kaggle_username"] = username
            legacy_path = creds_dir / CREDS_FILENAME
            if legacy_path.is_file():
                try:
                    pair = json.loads(legacy_path.read_text())
                    pair["username"] = username
                    _write_secret(legacy_path, json.dumps(pair))
                except Exception:
                    pass
        if token:
            _write_secret(creds_dir / TOKEN_FILENAME, token)
        _save_accounts(data)
    return {"name": name, "kaggle_username": account["kaggle_username"]}


def rename_account(old_name: str, new_name: str) -> Dict[str, Any]:
    """Renames an account's own dashboard-facing label (its registry key and
    creds-dir name) — separate from its Kaggle username (see
    update_credentials for that). Its nested workers move with it for free
    since they live inside the same registry entry; kaggle_state.json is
    keyed by worker_id only, so nothing there needs touching."""
    new_name = (new_name or "").strip()
    if not new_name:
        raise KaggleOpsError("Account name can't be empty")
    with _lock:
        data = _load_accounts()
        account = _find_account(data, old_name)
        if account is None:
            raise KaggleOpsError(f"Unknown account '{old_name}'")
        if new_name != old_name and _find_account(data, new_name) is not None:
            raise KaggleOpsError(f"Account '{new_name}' already exists")
        if new_name != old_name:
            old_dir = _creds_dir(old_name)
            if old_dir.is_dir():
                _creds_dir(new_name).parent.mkdir(parents=True, exist_ok=True)
                old_dir.rename(_creds_dir(new_name))
            account["name"] = new_name
        _save_accounts(data)
    return {"name": new_name}


def remove_credential(name: str, kind: str) -> Dict[str, Any]:
    """Deletes just one of an account's two credential slots. Refuses to
    remove the last one — an account with neither can't authenticate at all,
    and that's a worse state than just telling the caller to add a
    replacement first."""
    if kind not in ("legacy", "token"):
        raise KaggleOpsError("kind must be 'legacy' or 'token'")
    with _lock:
        data = _load_accounts()
        account = _find_account(data, name)
        if account is None:
            raise KaggleOpsError(f"Unknown account '{name}'")
        creds_dir = _creds_dir(name)
        legacy_path, token_path = creds_dir / CREDS_FILENAME, creds_dir / TOKEN_FILENAME
        target_path = legacy_path if kind == "legacy" else token_path
        other_path = token_path if kind == "legacy" else legacy_path
        if not target_path.is_file():
            raise KaggleOpsError(f"'{name}' has no {kind} credential stored")
        if not other_path.is_file():
            raise KaggleOpsError(f"Can't remove '{name}'s only stored credential — add a replacement first")
        target_path.unlink()
    return {"name": name, "removed": kind}


def remove_account(name: str) -> bool:
    with _lock:
        data = _load_accounts()
        if _find_account(data, name) is None:
            return False
        data["accounts"] = [a for a in data["accounts"] if a["name"] != name]
        _save_accounts(data)
    shutil.rmtree(settings.kaggle_creds_dir / name, ignore_errors=True)
    return True


def _validate_notebook_path(notebook_path: str) -> Path:
    notebook_abs = (settings.repo_root / notebook_path).resolve()
    repo_root = settings.repo_root.resolve()
    if repo_root not in notebook_abs.parents and notebook_abs != repo_root:
        raise KaggleOpsError("notebook_path escapes the repo root")
    if not notebook_abs.is_file():
        raise KaggleOpsError(f"Notebook not found: {notebook_path}")
    return notebook_abs


# {username}/{slug} or {username}/{slug}/{version} — mirrors the installed kaggle CLI's own
# validate_dataset_string() exactly (kaggle/api/kaggle_api_extended.py), so a malformed entry is
# rejected here with a clear message instead of surfacing as a less legible failure from
# `kaggle kernels push` itself (EXPERIMENT_AUTOMATION_PLAN.md §2.5).
def _validate_dataset_source(source: str) -> str:
    source = (source or "").strip()
    if not source:
        raise KaggleOpsError("Dataset source may not be empty")
    parts = source.split("/")
    if len(parts) < 2 or len(parts) > 3 or not parts[0] or not parts[1]:
        raise KaggleOpsError(
            f"Invalid dataset source {source!r} — expected '{{username}}/{{dataset-slug}}' or "
            "'{username}/{dataset-slug}/{version}'"
        )
    return source


def add_worker(
    account_name: str, worker_id: str, kernel_slug: str, results_dir: str,
    budget_hours: Optional[float] = None, notebook_path: str = "", template_path: str = "",
    dataset_sources: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """A worker is either **notebook-backed** (`notebook_path` set — the original shape: a fixed,
    hand-authored notebook pushed verbatim every time, e.g. the existing
    `iccit-kaggle-worker3/4.ipynb`) or **template-backed** (`notebook_path` left blank — the
    default going forward: `push()` renders a config/extra_args into a shared template,
    settings.kaggle_default_template unless `template_path` overrides it, per-launch — see
    LAUNCH_SPEC_MARKER / _render_launch_notebook()). The two are mutually exclusive so a worker's
    launch behavior is never ambiguous.

    *dataset_sources* (EXPERIMENT_AUTOMATION_PLAN.md §2.5) is the list of Kaggle datasets
    (`{username}/{slug}` strings) this worker's pushed kernel declares in kernel-metadata.json —
    previously hardcoded to `[]` in _kernel_metadata(), which meant every push produced a kernel
    with no data attached at all. A worker's template still asserts at runtime that the config's
    dataset shows up under /kaggle/input/ (see the launch template's own dataset-attach cell) —
    this only controls what Kaggle attaches *for* the kernel, not whether a given config's
    dataset happens to be among what's listed here. NOT YET verified whether pushing overwrites
    a dataset a human attached by hand through the Kaggle web UI (kernel-metadata.json is
    declarative, so that's the expectation, but confirm with one real push before relying on
    manual attachment as a fallback)."""
    worker_id = (worker_id or "").strip()
    notebook_path, template_path = (notebook_path or "").strip(), (template_path or "").strip()
    if not worker_id or not kernel_slug or not results_dir:
        raise KaggleOpsError("worker_id, kernel_slug and results_dir are all required")
    if notebook_path and template_path:
        raise KaggleOpsError("A worker takes either notebook_path (a fixed notebook) or template_path "
                              "(a launch template), not both")
    if notebook_path:
        _validate_notebook_path(notebook_path)
    elif template_path:
        _validate_notebook_path(template_path)  # same validation: repo-relative, must exist
    dataset_sources = [_validate_dataset_source(s) for s in (dataset_sources or [])]

    with _lock:
        data = _load_accounts()
        account = _find_account(data, account_name)
        if account is None:
            raise KaggleOpsError(f"Unknown account '{account_name}'")
        if _find_worker(account, worker_id) is not None:
            raise KaggleOpsError(f"Worker '{worker_id}' already exists under '{account_name}'")
        worker = {
            "worker_id": worker_id,
            "account_name": account_name,
            "profile_name": settings.profile_name,
            "kernel_slug": kernel_slug,
            "results_dir": str(results_dir),
            "budget_hours": float(budget_hours) if budget_hours else settings.kaggle_default_budget_hours,
            "dataset_sources": dataset_sources,
        }
        if notebook_path:
            worker["notebook_path"] = notebook_path
        if template_path:
            worker["template_path"] = template_path
        account.setdefault("workers", []).append(worker)
        _save_accounts(data)
    return worker


def set_worker_datasets(account_name: str, worker_id: str, dataset_sources: List[str]) -> Dict[str, Any]:
    """Replaces a worker's dataset_sources wholesale — the data a config needs changes over a
    worker's life (it's reused across many pushes/configs), so this needs to be editable without
    deleting and re-adding the worker, which would also lose its budget_hours/kernel_slug/state
    history for no reason."""
    dataset_sources = [_validate_dataset_source(s) for s in (dataset_sources or [])]
    with _lock:
        data = _load_accounts()
        account = _find_account(data, account_name)
        if account is None:
            raise KaggleOpsError(f"Unknown account '{account_name}'")
        worker = _find_worker(account, worker_id)
        if worker is None:
            raise KaggleOpsError(f"Unknown worker '{worker_id}'")
        worker["dataset_sources"] = dataset_sources
        _save_accounts(data)
    return {"worker_id": worker_id, "dataset_sources": dataset_sources}


def remove_worker(account_name: str, worker_id: str) -> bool:
    with _lock:
        data = _load_accounts()
        account = _find_account(data, account_name)
        if account is None:
            return False
        before = len(account.get("workers", []))
        account["workers"] = [w for w in account.get("workers", []) if w["worker_id"] != worker_id]
        if len(account["workers"]) == before:
            return False
        _save_accounts(data)
    return True


# --------------------------------------------------------------------------- CLI subprocess
def _run_kaggle(args: List[str], account_name: str, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    creds_dir = _creds_dir(account_name)
    legacy_path, token_path = creds_dir / CREDS_FILENAME, creds_dir / TOKEN_FILENAME
    has_legacy, has_token = legacy_path.is_file(), token_path.is_file()
    if not has_legacy and not has_token:
        raise KaggleOpsError(f"No credentials stored for account '{account_name}'")

    # Hand over whatever credentials this account has, in both env-var forms,
    # rather than the dashboard picking one itself. The installed `kaggle`
    # CLI's own auth() already tries an access token first and falls back to
    # the legacy username/key pair (confirmed against its source: token ->
    # legacy -> OAuth -> anonymous) — an older CLI that predates token support
    # just doesn't recognize KAGGLE_API_TOKEN and uses the legacy pair. That
    # makes the CLI's own version-aware priority order do the "which key is
    # right for this install" decision, instead of this module guessing at a
    # `kaggle --version` string.
    env = {**os.environ, "KAGGLE_CONFIG_DIR": str(creds_dir)}
    if has_token:
        # KAGGLE_API_TOKEN accepts either the literal token or a path to a
        # file containing it; passing the path keeps the secret itself out of
        # the subprocess's env block.
        env["KAGGLE_API_TOKEN"] = str(token_path)
    if has_legacy:
        try:
            pair = json.loads(legacy_path.read_text())
            env["KAGGLE_USERNAME"], env["KAGGLE_KEY"] = pair["username"], pair["key"]
        except Exception:
            pass
    try:
        return subprocess.run(
            [settings.kaggle_executable, *args],
            env=env, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise KaggleOpsError(
            f"'{settings.kaggle_executable}' was not found on PATH. Set kaggle_executable in "
            f"repos/{settings.profile_name}.yaml to wherever it's installed."
        )
    except subprocess.TimeoutExpired:
        raise KaggleOpsError(f"kaggle {' '.join(args)} timed out after {timeout}s")


def validate_account(account_name: str) -> Dict[str, Any]:
    """Cheapest authenticated call available as a stand-in for a real
    whoami — the CLI has no dedicated credential-check command."""
    proc = _run_kaggle(["kernels", "list", "-m", "--page-size", "1"], account_name, timeout=30)
    ok = proc.returncode == 0
    detail = (proc.stdout if ok else (proc.stderr or proc.stdout)).strip()
    return {"ok": ok, "detail": detail}


# --------------------------------------------------------------------------- push
def _kernel_metadata(account: Dict[str, Any], worker: Dict[str, Any], notebook_name: str) -> Dict[str, Any]:
    return {
        "id": f"{account['kaggle_username']}/{worker['kernel_slug']}",
        "title": worker["kernel_slug"],
        "code_file": notebook_name,
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": True,
        "enable_internet": True,
        "keywords": [],
        # Previously hardcoded to [] (EXPERIMENT_AUTOMATION_PLAN.md §2.5) — every push produced
        # a kernel with no data attached, which the launch template's own dataset-attach cell
        # then failed on. Now sourced from the worker record (set_worker_datasets()/add_worker()).
        "dataset_sources": list(worker.get("dataset_sources") or []),
        "competition_sources": [],
        "kernel_sources": [],
    }


def _render_launch_notebook(
    template_abs: Path,
    config_path: str,
    extra_args: str,
    dataset_source: str = "",
) -> bytes:
    """Loads *template_abs* (nbformat JSON), finds the single cell carrying
    LAUNCH_SPEC_MARKER, and substitutes its `__DASHBOARD_*__` placeholders
    with real values — stdlib json/re only, no Papermill (see
    DASHBOARD_REDESIGN_PLAN.md §2.2: dependency-minimalism is load-bearing
    for this project, and a plain marker-cell substitution needs nothing
    Papermill's tagged-parameter-cell convention would). Each value is
    inserted as a Python string literal (`repr()`), so it's automatically
    escaped against quote-breaking — the same shell-safety posture
    tmux_runner.py already applies to CLI arguments applies here to
    notebook source text. Returns the rendered notebook re-serialized as
    bytes."""
    try:
        notebook = json.loads(template_abs.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise KaggleOpsError(f"Could not read template notebook {template_abs}: {e}")

    marker_cells = [
        c for c in notebook.get("cells", [])
        if c.get("cell_type") == "code" and LAUNCH_SPEC_MARKER in "".join(c.get("source") or [])
    ]
    if len(marker_cells) != 1:
        raise KaggleOpsError(
            f"Template notebook {template_abs} must contain exactly one code cell with the "
            f"'{LAUNCH_SPEC_MARKER}' marker (found {len(marker_cells)}) — see "
            "notebooks/kaggle_worker_template.ipynb for the expected shape."
        )
    cell = marker_cells[0]
    values = {
        _LAUNCH_PLACEHOLDERS["config_path"]: config_path,
        _LAUNCH_PLACEHOLDERS["extra_args"]: extra_args or "",
        _LAUNCH_PLACEHOLDERS["train_script"]: settings.train_script,
        _LAUNCH_PLACEHOLDERS["eval_script"]: settings.eval_script,
        # Joined into one shell-quoted string, same shape as EXTRA_ARGS, so the template's
        # runner cell treats both identically (shlex.split before use) rather than needing
        # a second, list-shaped substitution convention just for this one field.
        _LAUNCH_PLACEHOLDERS["eval_extra_flags"]: " ".join(shlex.quote(a) for a in settings.eval_default_args),
        _LAUNCH_PLACEHOLDERS["max_hours"]: max(
            0.1,
            float(settings.kaggle_default_budget_hours)
            - float(settings.kaggle_setup_reserve_hours)
            - float(settings.kaggle_teardown_reserve_hours),
        ),
        _LAUNCH_PLACEHOLDERS["dataset_source"]: dataset_source or "",
    }

    def substitute(line: str) -> str:
        for token, real_value in values.items():
            line = line.replace(f'"{token}"', repr(real_value))
        return line

    cell["source"] = [substitute(line) for line in cell["source"]]
    return json.dumps(notebook, indent=1).encode()



def push(worker_id: str, config_path: str = "", extra_args: str = "") -> Dict[str, Any]:
    """Pushes *worker_id*'s kernel. A notebook-backed worker (`notebook_path`
    set) is pushed verbatim — *config_path*/*extra_args* are ignored,
    matching the original behavior exactly. A template-backed worker (the
    default) requires *config_path*: the launch spec is rendered into its
    template (settings.kaggle_default_template, or `template_path` if set)
    before push, running train then eval sequentially inside the one kernel
    (EXPERIMENT_AUTOMATION_PLAN.md §2.4).

    Every push is fresh — there is no resume contract (XDASH_V2_PLAN.md §4.A1
    deleted it: it validated thoroughly and then silently ran a fresh
    training job anyway, see the plan's D1-D4). Phase D re-specifies resume
    against Kaggle-dataset-versioned legs once the host repo supports
    ``--max-hours`` self-limiting; nothing here should be extended to fake it
    sooner."""
    data = _load_accounts()
    account, worker = _find_worker_and_account(data, worker_id)
    if worker is None:
        raise KaggleOpsError(f"Unknown worker '{worker_id}'")

    notebook_path = worker.get("notebook_path")
    if notebook_path:
        source_abs = settings.repo_root / notebook_path
        if not source_abs.is_file():
            raise KaggleOpsError(f"Notebook not found: {notebook_path}")
        push_bytes = source_abs.read_bytes()
        push_name = source_abs.name
    else:
        config_path = (config_path or "").strip()
        if not config_path:
            raise KaggleOpsError(
                f"Worker '{worker_id}' is template-backed — a config_path is required to push "
                "(pick a config the same way you would to launch it on the local device)"
            )
        try:
            cfg.read_config(config_path)  # raises if the config doesn't exist / isn't valid YAML
            cli_config_path = cfg.repo_relative_path(config_path)  # e.g. "configs/mkunet/foo.yaml"
        except (FileNotFoundError, ValueError) as e:
            raise KaggleOpsError(f"Config not found: {config_path} ({e})")
        source_abs = _source_notebook_path(worker)
        if not source_abs.is_file():
            raise KaggleOpsError(f"Template notebook not found: {source_abs}")
        dataset_source = next(iter(worker.get("dataset_sources") or []), "")
        push_bytes = _render_launch_notebook(source_abs, cli_config_path, extra_args, dataset_source)
        push_name = source_abs.name

    source_hash = hashlib.sha1(source_abs.read_bytes()).hexdigest()

    tmpdir = tempfile.mkdtemp(prefix="kaggle_push_")
    try:
        (Path(tmpdir) / push_name).write_bytes(push_bytes)
        metadata = _kernel_metadata(account, worker, push_name)
        (Path(tmpdir) / "kernel-metadata.json").write_text(json.dumps(metadata, indent=2))
        push_args = ["kernels", "push", "-p", tmpdir]

        budget_hours = worker.get("budget_hours") or settings.kaggle_default_budget_hours
        timeout_args = ["--timeout", str(int(float(budget_hours) * 3600))]
        proc = _run_kaggle(push_args + timeout_args, account["name"], timeout=120)
        if proc.returncode != 0 and _looks_like_unrecognized_option(proc.stderr or proc.stdout, "--timeout"):
            # The installed `kaggle` CLI predates the --timeout flag (confirmed present as of the
            # official kaggle-cli's 2026 release, per DASHBOARD_REDESIGN_PLAN.md's fact-check, but
            # never verified against whatever version is actually installed on this host) — retry
            # without it rather than hard-failing every push over one optional enforcement flag.
            proc = _run_kaggle(push_args, account["name"], timeout=120)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        _update_worker_state(worker_id, {"status": "push_failed", "last_error": detail}, event="push failed")
        raise KaggleOpsError(f"Push failed for '{worker_id}': {detail}")

    notebook_hash = hashlib.sha1(push_bytes).hexdigest()
    event = "pushed" if notebook_path else f"pushed — {config_path}"
    state_patch = {
        "status": "pushed", "pushed_at": _now_iso(), "last_error": None, "over_budget": False,
        "notified_final": False, "pushed_notebook_hash": notebook_hash, "pushed_template_hash": source_hash,
        "last_config_path": config_path or None,
        "last_extra_args": extra_args or None,
        "xdash_status": None,  # cleared here — download() sets it from this push's own status file
    }
    _update_worker_state(worker_id, state_patch, event=event)
    warning = _concurrent_push_warning(account, worker_id)
    result = {"worker_id": worker_id, "status": "pushed"}
    if warning:
        result["concurrent_warning"] = warning
    return result


def restart(worker_id: str) -> Dict[str, Any]:
    """Re-pushes a template-backed worker with the config/extra_args from
    its last push — the Kaggle-side counterpart of terminals.restart()
    (the local device's restart re-runs the same config/args in a fresh tmux session;
    this re-renders and re-pushes the same launch spec). Not meaningful for
    a notebook-backed worker (push() already ignores config_path for those)
    — just calls push() again with no spec, which is a plain re-push,
    matching that worker's pre-existing behavior."""
    data = _load_accounts()
    _, worker = _find_worker_and_account(data, worker_id)
    if worker is None:
        raise KaggleOpsError(f"Unknown worker '{worker_id}'")
    if worker.get("notebook_path"):
        return push(worker_id)
    state = _load_state().get(worker_id, {})
    config_path = state.get("last_config_path")
    if not config_path:
        raise KaggleOpsError(f"Worker '{worker_id}' has never been pushed with a config — nothing to restart")
    return push(worker_id, config_path, state.get("last_extra_args") or "")


def _looks_like_unrecognized_option(output: str, flag: str) -> bool:
    text = (output or "").lower()
    return flag.lower() in text and any(
        phrase in text for phrase in ("no such option", "unrecognized", "unexpected argument", "unknown option")
    )


def _concurrent_push_warning(account: Dict[str, Any], worker_id: str) -> Optional[str]:
    """Kaggle accounts typically run one kernel at a time — a second push
    under the same account usually just queues (or bumps) the first rather
    than running in parallel. Non-blocking: this only annotates the push
    response so the caller can warn, since Kaggle's own behavior here isn't
    something worth guessing at and hard-blocking on."""
    state = _load_state()
    siblings = [
        w["worker_id"] for w in account.get("workers", [])
        if w["worker_id"] != worker_id
        and state.get(w["worker_id"], {}).get("status") in IN_PROGRESS_STATUSES
    ]
    if not siblings:
        return None
    return (
        f"Account '{account['name']}' already has {', '.join(siblings)} in progress — "
        "Kaggle typically runs one kernel per account at a time, so this push may just queue."
    )


# --------------------------------------------------------------------------- worker-less push (Experiments)
# XDASH_V2_PLAN.md §3.3 originally read: "There is no worker. Kernel slug and results dir are
# derived per experiment, not stored per worker." Still true for *results dir* — see
# results_dir_for_experiment() — but *kernel slug* reverted to per-account on 2026-09-22 (see
# kernel_slug_for_account()'s own docstring for why: Kaggle Secrets can't be attached via the
# API, only per-kernel through the web UI, so a fresh kernel per experiment would need that
# manual step redone forever). Still no persisted worker record and no kaggle_state.json entry —
# the functions below share the low-level CLI mechanics above (_render_launch_notebook,
# _kernel_metadata, _run_kaggle, register_ledger, _read_xdash_status) with the worker-based
# push() but keep their own state entirely on the caller's Attempt record, so the old
# Assignments/Kaggle tab (still worker-based, per §6.8's strangler migration) and the new
# dispatcher can never step on each other's bookkeeping even though both may push to the same
# account.
_KERNEL_SLUG_MAX = 48


def kernel_slug_for_account(account_name: str) -> str:
    """Stable, long-lived kernel slug for *account_name* — one Kaggle notebook per account,
    reused across every experiment that account ever runs.

    XDASH_V2_PLAN.md §3.3 originally specified one kernel *per experiment* instead; reverted
    2026-09-22 after a real push hit Kaggle's "No user secrets exist for kernel id ... and label
    GITHUB_TOKEN" error. A Kaggle Secret can only be attached to a kernel through the web UI —
    there is no API/CLI field for it — so a perpetually-growing set of brand-new per-experiment
    kernels would need that manual step repeated for every single experiment, forever, rather
    than once per account. Results stay isolated per experiment via results_dir_for_experiment()
    below, so this does not bring back D4's "downloads interleaved on disk" problem — only
    D4's *cosmetic* half (a kernel's version history on Kaggle's own site interleaves multiple
    experiments) comes back, which is a fair trade for pushes that actually run.

    Deliberately a different slug shape than the old worker system's `xdash-{profile}-{account}`
    (add_worker()) so this can never collide with a still-live legacy worker push (§6.8's
    strangler migration keeps both systems running side by side) — even though, if those old
    kernels already have GITHUB_TOKEN attached from prior use, this one still needs its own
    one-time manual attachment before its first push. That manual step is unavoidable and not
    something this function — or any code — can do on your behalf."""
    base = re.sub(r"[^a-z0-9-]+", "-", f"{settings.profile_name}-slot-{account_name}").strip("-").lower()
    slug = f"xdash-{base}"
    if len(slug) <= _KERNEL_SLUG_MAX:
        return slug
    digest = hashlib.sha1(slug.encode()).hexdigest()[:6]
    keep = _KERNEL_SLUG_MAX - len(digest) - 1
    return f"{slug[:keep]}-{digest}"


def results_dir_for_experiment(experiment_id: str) -> str:
    return f"outputs/kaggle/{experiment_id}"


def push_experiment_attempt(
    account_name: str, experiment_id: str, config_path: str, extra_args: str,
    dataset_sources: List[str], budget_hours: Optional[float] = None,
) -> Dict[str, Any]:
    """Pushes one Attempt's kernel for *experiment_id* under *account_name*. Always
    template-backed (the escape hatch for a hand-authored notebook stays a worker-only action,
    §3.3) and always fresh — there is no resume contract here either (§4.A1)."""
    data = _load_accounts()
    account = _find_account(data, account_name)
    if account is None:
        raise KaggleOpsError(f"Unknown account '{account_name}'")

    config_path = (config_path or "").strip()
    if not config_path:
        raise KaggleOpsError("config_path is required")
    try:
        cfg.read_config(config_path)  # raises if the config doesn't exist / isn't valid YAML
        cli_config_path = cfg.repo_relative_path(config_path)
    except (FileNotFoundError, ValueError) as e:
        raise KaggleOpsError(f"Config not found: {config_path} ({e})")

    template_abs = settings.kaggle_default_template_file
    if not template_abs.is_file():
        raise KaggleOpsError(f"Template notebook not found: {template_abs}")

    kernel_slug = kernel_slug_for_account(account_name)
    results_dir = results_dir_for_experiment(experiment_id)
    budget = float(budget_hours) if budget_hours else settings.kaggle_default_budget_hours
    # _kernel_metadata only reads kernel_slug/dataset_sources off "worker" — a bare dict with
    # just those two keys is all it needs, no registry record required.
    pseudo_worker = {"kernel_slug": kernel_slug, "dataset_sources": list(dataset_sources or [])}

    dataset_source = next(iter(pseudo_worker["dataset_sources"]), "")
    push_bytes = _render_launch_notebook(template_abs, cli_config_path, extra_args, dataset_source)
    push_name = template_abs.name

    tmpdir = tempfile.mkdtemp(prefix="kaggle_push_")
    try:
        (Path(tmpdir) / push_name).write_bytes(push_bytes)
        metadata = _kernel_metadata(account, pseudo_worker, push_name)
        (Path(tmpdir) / "kernel-metadata.json").write_text(json.dumps(metadata, indent=2))
        push_args = ["kernels", "push", "-p", tmpdir]
        timeout_args = ["--timeout", str(int(budget * 3600))]
        proc = _run_kaggle(push_args + timeout_args, account["name"], timeout=120)
        if proc.returncode != 0 and _looks_like_unrecognized_option(proc.stderr or proc.stdout, "--timeout"):
            proc = _run_kaggle(push_args, account["name"], timeout=120)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise KaggleOpsError(f"Push failed for experiment '{experiment_id}': {detail}")

    return {
        "account": account_name, "kernel_slug": kernel_slug, "results_dir": results_dir,
        "pushed_at": _now_iso(),
    }


def refresh_experiment_status(account_name: str, kernel_slug: str) -> Dict[str, Any]:
    """Polls Kaggle for *kernel_slug*'s current status. No worker registry lookup — a
    worker-less Attempt already knows its own account+kernel_slug from push_experiment_attempt's
    return value."""
    data = _load_accounts()
    account = _find_account(data, account_name)
    if account is None:
        raise KaggleOpsError(f"Unknown account '{account_name}'")
    kernel_ref = f"{account['kaggle_username']}/{kernel_slug}"
    proc = _run_kaggle(["kernels", "status", kernel_ref], account_name, timeout=30)
    if proc.returncode != 0:
        return {"status": "unknown", "last_error": (proc.stderr or proc.stdout).strip()}
    m = STATUS_RE.search(proc.stdout)
    kaggle_status = _normalize_kaggle_status(m.group(1)) if m else "unknown"
    return {"status": kaggle_status, "last_error": None}


def download_experiment(account_name: str, kernel_slug: str, results_dir: str) -> Dict[str, Any]:
    """Worker-less counterpart to download() — extracts into *results_dir* (already derived by
    the caller from the experiment_id) and reads xdash_status.json for diagnosis exactly like the
    worker path does (§4.A7)."""
    data = _load_accounts()
    account = _find_account(data, account_name)
    if account is None:
        raise KaggleOpsError(f"Unknown account '{account_name}'")
    kernel_ref = f"{account['kaggle_username']}/{kernel_slug}"

    tmpdir = tempfile.mkdtemp(prefix="kaggle_download_")
    try:
        proc = _run_kaggle(["kernels", "output", kernel_ref, "-p", tmpdir], account_name, timeout=900)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise KaggleOpsError(f"Download failed for '{kernel_slug}': {detail}")
        zips = list(Path(tmpdir).glob("*.zip"))
        if not zips:
            raise KaggleOpsError(f"No output files found for '{kernel_slug}' — has the kernel finished?")
        results_dir_abs = (settings.repo_root / results_dir).resolve()
        repo_root = settings.repo_root.resolve()
        if repo_root not in results_dir_abs.parents and results_dir_abs != repo_root:
            raise KaggleOpsError("results_dir escapes the repo root")
        results_dir_abs.mkdir(parents=True, exist_ok=True)
        for zip_path in zips:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(results_dir_abs)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    registered = register_ledger(results_dir_abs)
    xdash_status = _read_xdash_status(results_dir_abs)
    return {"results_dir": str(results_dir_abs), "registered_runs": registered, "xdash_status": xdash_status}


# --------------------------------------------------------------------------- status
def refresh_status(worker_id: str) -> Dict[str, Any]:
    data = _load_accounts()
    account, worker = _find_worker_and_account(data, worker_id)
    if worker is None:
        raise KaggleOpsError(f"Unknown worker '{worker_id}'")
    kernel_ref = f"{account['kaggle_username']}/{worker['kernel_slug']}"
    proc = _run_kaggle(["kernels", "status", kernel_ref], account["name"], timeout=30)

    if proc.returncode != 0:
        patch = {"status": "unknown", "last_error": (proc.stderr or proc.stdout).strip()}
        _update_worker_state(worker_id, patch)
        return {"worker_id": worker_id, **patch}

    m = STATUS_RE.search(proc.stdout)
    kaggle_status = _normalize_kaggle_status(m.group(1)) if m else "unknown"

    state = _load_state()
    prior = state.get(worker_id, {})
    pushed_at = prior.get("pushed_at")
    over_budget = False
    if kaggle_status in IN_PROGRESS_STATUSES and pushed_at:
        budget_hours = worker.get("budget_hours") or settings.kaggle_default_budget_hours
        elapsed_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(pushed_at)).total_seconds() / 3600.0
        over_budget = elapsed_hours > budget_hours

    patch = {"status": kaggle_status, "last_error": None, "over_budget": over_budget, "checked_at": _now_iso()}
    # Only worth a history line when the status actually moved — a poll that
    # just reconfirms "still running" every tick would otherwise flood the
    # log with duplicate entries.
    event = f"status: {kaggle_status}" if kaggle_status != prior.get("status") else None
    _update_worker_state(worker_id, patch, event=event)
    return {"worker_id": worker_id, **patch}


# --------------------------------------------------------------------------- download + ledger
def _iter_downloaded_manifests(results_dir: Path):
    """Yields (manifest_path, run_id) for every manifest.json under a
    downloaded worker's results_dir, in whichever shape this profile's
    manifest_layout uses — mirrors backend/ledger.py's own
    _iter_manifest_paths() exactly, since the two must agree on where a
    manifest lives or a Kaggle-downloaded run becomes invisible to the Runs/
    Ledger tabs even after registration succeeds."""
    if settings.manifest_layout == "experiments":
        base = results_dir / "outputs" / "experiments"
        if not base.is_dir():
            return
        seen = set()
        for pattern in ("*/checkpoints/manifest.json", "*/checkpoints/fold*/manifest.json"):
            for p in sorted(base.glob(pattern)):
                if p.is_file() and p not in seen:
                    seen.add(p)
                    try:
                        manifest = json.loads(p.read_text())
                    except Exception:
                        continue
                    run_id = manifest.get("run_id")
                    if run_id:
                        yield p, run_id, manifest
        return

    manifests_dir = results_dir / "artifacts" / "runs"
    if not manifests_dir.is_dir():
        return
    for p in sorted(manifests_dir.glob("*/manifest.json")):
        if not p.is_file():
            continue
        try:
            manifest = json.loads(p.read_text())
        except Exception:
            continue
        run_id = manifest.get("run_id") or p.parent.name
        yield p, run_id, manifest


def _downloaded_ledger_rows(results_dir: Path) -> Dict[str, Dict[str, str]]:
    """The downloaded worker's own runs.csv, keyed by run_id — same two
    layouts as _iter_downloaded_manifests(), since ledger_dir sits at a
    different place relative to the manifests in each (nested under
    artifacts/ in "legacy", a sibling of experiments/ in "experiments")."""
    if settings.manifest_layout == "experiments":
        src_runs_csv = results_dir / "outputs" / "ledger" / "runs.csv"
    else:
        src_runs_csv = results_dir / "artifacts" / "ledger" / "runs.csv"
    if not src_runs_csv.is_file():
        return {}
    with open(src_runs_csv, newline="") as f:
        return {row.get("run_id"): row for row in csv.DictReader(f)}


def register_ledger(results_dir: Path) -> List[str]:
    """Copies each newly-downloaded run's manifest.json into the host repo's
    own ledger/manifest layout and appends its row into
    settings.ledger_dir/runs.csv — mirroring orchestration/manifest.py's
    atomic-write style and orchestration/ledger.py's RUNS_FIELDS exactly,
    stdlib-only (no import of that package, same as backend/ledger.py's read
    side). Branches on settings.manifest_layout exactly as backend/ledger.py
    does on the read side (EXPERIMENT_AUTOMATION_PLAN.md §2.2) — the earlier,
    legacy-only version of this function silently registered nothing at all
    under manifest_layout: "experiments" (dissert), since
    results_dir/artifacts/runs/ never exists there.

    Idempotent by run_id, regardless of status (not just "done" — a run
    previously registered as failed/interrupted is not re-appended on a
    later download of the same worker, which the old status=="done"-only
    check let happen). A resumed run's own status transition — the same
    run_id going from "interrupted" to "done" across two chained Kaggle legs
    — needs its row *updated*, not skipped; that is leg-chaining's problem
    (EXPERIMENT_AUTOMATION_PLAN.md §8.2), not this function's, and isn't
    handled here. Returns the run_ids newly registered."""
    src_rows_by_id = _downloaded_ledger_rows(results_dir)
    if not src_rows_by_id:
        return []

    dest_ledger_dir = settings.ledger_dir
    dest_runs_csv = dest_ledger_dir / "runs.csv"

    newly_registered: List[str] = []
    with _ledger_lock:
        known_ids = set()
        if dest_runs_csv.is_file():
            with open(dest_runs_csv, newline="") as f:
                known_ids = {row.get("run_id") for row in csv.DictReader(f)}

        dest_ledger_dir.mkdir(parents=True, exist_ok=True)
        is_new_csv = not dest_runs_csv.is_file()
        with open(dest_runs_csv, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=RUNS_FIELDS)
            if is_new_csv:
                writer.writeheader()
            for manifest_path, run_id, manifest in _iter_downloaded_manifests(results_dir):
                if not run_id or run_id in known_ids:
                    continue
                row = src_rows_by_id.get(run_id)
                if row is None:
                    continue

                if settings.manifest_layout == "experiments":
                    # Mirror the source path's own tail (…/<experiment_id>/checkpoints/[fold*/]manifest.json)
                    # under settings.experiments_dir — this layout doesn't name a manifest's
                    # directory after run_id at all, so there's no run_id-keyed dest path to
                    # target the way the legacy branch below has.
                    rel = manifest_path.relative_to(results_dir / "outputs" / "experiments")
                    dest_manifest_path = settings.experiments_dir / rel
                else:
                    dest_manifest_path = settings.runs_artifacts_dir / run_id / "manifest.json"

                dest_manifest_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = dest_manifest_path.with_suffix(".json.tmp")
                tmp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str))
                os.replace(tmp_path, dest_manifest_path)

                writer.writerow({k: row.get(k, "") for k in RUNS_FIELDS})
                newly_registered.append(run_id)

    return newly_registered


def _read_xdash_status(results_dir: Path) -> Optional[Dict[str, Any]]:
    """The launch template's own {stage, returncode, started_at, ended_at,
    setup_seconds} record (XDASH_V2_PLAN.md §4.A7) — written unconditionally
    by the template's run cell, whether train/eval succeeded or not, so a
    failed attempt is diagnosable from what actually got downloaded instead
    of inferred from zip presence (the previous behavior: a failed run
    produced no zips at all, since the packaging cell never ran after an
    aborting assert)."""
    status_path = results_dir / "outputs" / "xdash_status.json"
    if not status_path.is_file():
        return None
    try:
        return json.loads(status_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def download(worker_id: str) -> Dict[str, Any]:
    data = _load_accounts()
    account, worker = _find_worker_and_account(data, worker_id)
    if worker is None:
        raise KaggleOpsError(f"Unknown worker '{worker_id}'")
    kernel_ref = f"{account['kaggle_username']}/{worker['kernel_slug']}"

    tmpdir = tempfile.mkdtemp(prefix="kaggle_download_")
    try:
        proc = _run_kaggle(["kernels", "output", kernel_ref, "-p", tmpdir], account["name"], timeout=900)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            _update_worker_state(worker_id, {"last_error": detail})
            raise KaggleOpsError(f"Download failed for '{worker_id}': {detail}")

        zips = list(Path(tmpdir).glob("*.zip"))
        if not zips:
            raise KaggleOpsError(f"No output files found for '{worker_id}' — has the kernel finished?")

        results_dir = (settings.repo_root / worker["results_dir"]).resolve()
        repo_root = settings.repo_root.resolve()
        if repo_root not in results_dir.parents and results_dir != repo_root:
            raise KaggleOpsError("results_dir escapes the repo root")
        results_dir.mkdir(parents=True, exist_ok=True)

        for zip_path in zips:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(results_dir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    registered = register_ledger(results_dir)
    xdash_status = _read_xdash_status(results_dir)
    failed_stage = xdash_status and xdash_status.get("returncode") not in (0, None)
    patch = {
        "status": "downloaded", "downloaded_at": _now_iso(), "xdash_status": xdash_status,
        "last_error": (
            f"{xdash_status.get('stage', 'run')} exited with code {xdash_status.get('returncode')} "
            "(see downloaded logs)"
        ) if failed_stage else None,
    }
    event = f"downloaded — {len(registered)} run(s) registered" if registered else "downloaded"
    if failed_stage:
        event = f"downloaded — {xdash_status.get('stage', 'run')} failed (exit {xdash_status.get('returncode')})"
    _update_worker_state(worker_id, patch, event=event)
    return {"worker_id": worker_id, "results_dir": str(results_dir), "registered_runs": registered, "xdash_status": xdash_status}


# --------------------------------------------------------------------------- quota estimate
_profile_snapshot_cache: Dict[str, Optional[Settings]] = {}


def _profile_snapshot(profile_name: str) -> Optional[Settings]:
    """Read-only Settings(name) for any profile, without disturbing the
    active one — the same snapshot trick backend/repos.py uses, never
    mutating the shared singleton, so it's safe to call from any thread.
    Needed for a system-scoped account: its usage/quota must be resolved
    against *every* profile it has workers in, each of which may have its
    own repo_root and manifest_layout. Cached: profiles are files on disk
    that don't change while the server runs, and usage_history() is called
    per account per list_accounts() render (and, once the dispatcher lands,
    per dispatch tick)."""
    if profile_name not in _profile_snapshot_cache:
        try:
            _profile_snapshot_cache[profile_name] = Settings(profile_name)
        except Exception:
            _profile_snapshot_cache[profile_name] = None
    return _profile_snapshot_cache[profile_name]


def _iter_extracted_manifests(base_dir: Path, manifest_layout: str):
    """Yields (manifest_path, run_id, manifest) for every manifest.json
    under *base_dir* in the given manifest_layout shape. Shared by
    register_ledger()'s helpers (base_dir = a temp Kaggle-download
    extraction root, always the active profile's own layout) and
    usage_history() (base_dir = repo_root/worker.results_dir — wherever a
    worker's own past downloads were unpacked, which for a system-scoped
    account may belong to a profile that isn't the active one). Mirrors
    backend/ledger.py's _iter_manifest_paths() exactly, parameterized
    instead of reading the active settings singleton, since it must be
    called against another profile's manifest_layout too."""
    if manifest_layout == "experiments":
        base = base_dir / "outputs" / "experiments"
        if not base.is_dir():
            return
        seen = set()
        for pattern in ("*/checkpoints/manifest.json", "*/checkpoints/fold*/manifest.json"):
            for p in sorted(base.glob(pattern)):
                if p.is_file() and p not in seen:
                    seen.add(p)
                    try:
                        manifest = json.loads(p.read_text())
                    except Exception:
                        continue
                    run_id = manifest.get("run_id")
                    if run_id:
                        yield p, run_id, manifest
        return

    manifests_dir = base_dir / "artifacts" / "runs"
    if not manifests_dir.is_dir():
        return
    for p in sorted(manifests_dir.glob("*/manifest.json")):
        if not p.is_file():
            continue
        try:
            manifest = json.loads(p.read_text())
        except Exception:
            continue
        run_id = manifest.get("run_id") or p.parent.name
        yield p, run_id, manifest


def _utc_week_start(ref: Optional[datetime] = None) -> datetime:
    now = ref or datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


# usage_history() walks every worker's results dir per call, and gets called
# once per account per list_accounts() render — a per-account TTL cache keeps
# that a bounded cost instead of a full filesystem scan on every poll.
_USAGE_CACHE_TTL_SECONDS = 20.0
_usage_cache: Dict[str, tuple] = {}  # account_name -> (monotonic_time, result)


def _usage_history_uncached(account_name: str, weeks: int) -> List[Dict[str, Any]]:
    """Self-tracked GPU-hours per UTC week for *account_name*'s past *weeks*
    weeks (oldest first, current week last), summed from this account's own
    downloaded run manifests. NOT Kaggle's authoritative quota figure —
    Kaggle exposes no API for that (only a logged-in browser session sees
    the real number on the account's Usage page), so this is presented as
    an estimate throughout, and can drift from it (e.g. kernels run outside
    this dashboard aren't counted).

    A system-scoped account is summed across **every** profile it has
    workers in, not just the active one: Kaggle meters one weekly quota per
    account regardless of which repo a kernel was running, so a per-profile
    total would under-count it and any gate built on that number would let
    the dispatcher over-commit the account.

    The current week's bucket also includes an **in-flight reservation**:
    for each of this account's workers currently IN_PROGRESS_STATUSES, add
    min(elapsed_hours, budget_hours). Without this, a kernel that has been
    running for hours counts as 0 until it finishes and is downloaded, so a
    gate built on the bare completed-runs total would keep waving through
    pushes against an account that is, in reality, already near its cap."""
    this_week_start = _utc_week_start()
    buckets = [this_week_start - timedelta(weeks=n) for n in range(weeks - 1, -1, -1)]
    totals = {b: 0.0 for b in buckets}

    account = _find_account(_load_accounts(), account_name)
    if account is None:
        return [{"week_start": b.isoformat(), "hours": 0.0} for b in buckets]

    # (worker, repo_root, manifest_layout, state) tuples. A repo-scoped
    # account only ever has workers under the active profile; a system one
    # resolves each worker against its own profile's snapshot, since
    # results_dir/manifest_layout/kaggle_state_file are all repo-relative or
    # per-profile.
    scoped: List[tuple] = []
    if account.get("scope") == SCOPE_SYSTEM:
        stored = next(
            (a for a in _load_scope(SCOPE_SYSTEM)["accounts"] if a["name"] == account_name), {}
        )
        for worker in stored.get("workers", []):
            snap = _profile_snapshot(worker.get("profile", settings.profile_name))
            if snap is None:
                continue
            state = _load_state() if snap.profile_name == settings.profile_name else _read_state_file(snap.kaggle_state_file)
            scoped.append((worker, snap.repo_root, snap.manifest_layout, state))
    else:
        state = _load_state()
        scoped = [(w, settings.repo_root, settings.manifest_layout, state) for w in account.get("workers", [])]

    earliest = buckets[0]
    now = datetime.now(timezone.utc)
    for worker, repo_root, manifest_layout, state in scoped:
        base_dir = (repo_root / worker["results_dir"]).resolve()
        for _p, _run_id, manifest in _iter_extracted_manifests(base_dir, manifest_layout):
            start_time, gpu_hours = manifest.get("start_time"), manifest.get("gpu_hours")
            if not start_time or not gpu_hours:
                continue
            try:
                started = datetime.fromisoformat(start_time)
            except ValueError:
                continue
            if started < earliest:
                continue
            bucket = _utc_week_start(started)
            if bucket in totals:
                totals[bucket] += float(gpu_hours)

        w_state = state.get(worker["worker_id"], {})
        if w_state.get("status") in IN_PROGRESS_STATUSES and w_state.get("pushed_at"):
            try:
                pushed_at = datetime.fromisoformat(w_state["pushed_at"])
            except ValueError:
                continue
            elapsed_hours = (now - pushed_at).total_seconds() / 3600.0
            budget_hours = float(worker.get("budget_hours") or settings.kaggle_default_budget_hours)
            totals[this_week_start] += min(elapsed_hours, budget_hours)

    return [{"week_start": b.isoformat(), "hours": round(totals[b], 2)} for b in buckets]


def _read_state_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def usage_history(account_name: str, weeks: int = 6) -> List[Dict[str, Any]]:
    """Cached wrapper over _usage_history_uncached() — see its docstring for
    what this computes. Cache key includes *weeks* since callers ask for
    different window sizes (estimate_usage() wants 1, the sparkline wants
    the default 6)."""
    cache_key = f"{account_name}:{weeks}"
    now = time.monotonic()
    cached = _usage_cache.get(cache_key)
    if cached is not None and (now - cached[0]) < _USAGE_CACHE_TTL_SECONDS:
        return cached[1]
    result = _usage_history_uncached(account_name, weeks)
    _usage_cache[cache_key] = (now, result)
    return result


def estimate_usage(account_name: str) -> Dict[str, Any]:
    """This-week slice of usage_history() (including the in-flight
    reservation), plus the account's configured weekly budget so a caller
    can render/gate on remaining headroom without a second lookup."""
    history = usage_history(account_name, weeks=1)
    current = history[-1]
    budget = _weekly_budget_hours(account_name)
    return {
        "hours_this_week": current["hours"],
        "week_start": current["week_start"],
        "weekly_budget_hours": budget,
        "remaining_hours": (round(budget - current["hours"], 2) if budget is not None else None),
    }


def _weekly_budget_hours(account_name: str) -> Optional[float]:
    account = _find_account(_load_accounts(), account_name)
    if account is None:
        return None
    if account.get("weekly_budget_hours") is not None:
        return float(account["weekly_budget_hours"])
    return settings.kaggle_default_weekly_budget_hours


def set_weekly_budget(name: str, hours: Optional[float]) -> Dict[str, Any]:
    """Sets (or, with hours=None, clears back to the profile default) an
    account's own weekly GPU-hour budget — a property of the Kaggle
    account/tier, not of whichever repo happens to be active, so it lives on
    the account record in whichever scope (system/repo) that account is
    already registered under, not in a repo profile's YAML."""
    with _lock:
        data = _load_accounts()
        account = _find_account(data, name)
        if account is None:
            raise KaggleOpsError(f"Unknown account '{name}'")
        account["weekly_budget_hours"] = float(hours) if hours is not None else None
        _save_accounts(data)
    _usage_cache.pop(f"{name}:1", None)
    _usage_cache.pop(f"{name}:6", None)
    return {"name": name, "weekly_budget_hours": account["weekly_budget_hours"]}


# --------------------------------------------------------------------------- bulk / fleet ops
def _all_worker_ids() -> List[str]:
    data = _load_accounts()
    return [w["worker_id"] for a in data["accounts"] for w in a.get("workers", [])]


def _run_bulk(fn, worker_ids: List[str]) -> List[Dict[str, Any]]:
    if not worker_ids:
        return []
    results = []
    with ThreadPoolExecutor(max_workers=settings.kaggle_push_concurrency) as pool:
        futures = {pool.submit(fn, wid): wid for wid in worker_ids}
        for future in as_completed(futures):
            worker_id = futures[future]
            try:
                results.append(future.result())
            except KaggleOpsError as e:
                results.append({"worker_id": worker_id, "error": str(e)})
    return results


def _push_or_restart(worker_id: str) -> Dict[str, Any]:
    """push_all()'s per-worker action: a notebook-backed worker just pushes
    (as always); a template-backed worker re-pushes its *last* config/
    extra_args via restart() — push() alone would fail every time here since it
    has no config_path to work from without one being passed explicitly.
    Raises KaggleOpsError (caught by _run_bulk) for a template-backed worker
    that's never been pushed yet — "push all" bulk-repeats known launches,
    it doesn't guess a first one."""
    data = _load_accounts()
    _, worker = _find_worker_and_account(data, worker_id)
    if worker is None:
        raise KaggleOpsError(f"Unknown worker '{worker_id}'")
    if worker.get("notebook_path"):
        return push(worker_id)
    return restart(worker_id)


def push_all() -> List[Dict[str, Any]]:
    return _run_bulk(_push_or_restart, _all_worker_ids())


def refresh_all() -> List[Dict[str, Any]]:
    return _run_bulk(refresh_status, _all_worker_ids())


def download_all() -> List[Dict[str, Any]]:
    """Downloads every worker whose last known status (from the last
    refresh) is Kaggle's finished state — running/queued workers are
    skipped rather than attempting a download that would just fail."""
    data = _load_accounts()
    state = _load_state()
    worker_ids = [
        w["worker_id"]
        for a in data["accounts"] for w in a.get("workers", [])
        if state.get(w["worker_id"], {}).get("status") in FINISHED_STATUSES
    ]
    return _run_bulk(download, worker_ids)


# --------------------------------------------------------------------------- registry export/import
def export_registry() -> Dict[str, Any]:
    """The account+worker registry, verbatim — no credentials are anywhere
    in this structure (they live in separate files under kaggle_creds_dir,
    never referenced here by content), so it's safe to hand to a teammate
    or save as a file. Pairs with import_registry(), which only ever adds
    workers to accounts that already exist locally — credentials are never
    something an import can supply, by design."""
    return _load_accounts()


def import_registry(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Adds workers from *payload* (the shape export_registry() returns) to
    accounts that already exist locally, matched by name. An account in the
    payload that doesn't exist locally is skipped entirely — creating one
    would need credentials, which an import file never carries — and a
    worker whose worker_id already exists locally, or whose notebook can't
    be found under this repo_root, is skipped too rather than silently
    overwritten or half-added."""
    accounts_in = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts_in, list):
        raise KaggleOpsError('Expected {"accounts": [...]} — the same shape export_registry() produces')

    added: List[Dict[str, str]] = []
    skipped_accounts: List[str] = []
    skipped_workers: List[Dict[str, str]] = []
    with _lock:
        data = _load_accounts()
        for incoming in accounts_in:
            name = (incoming or {}).get("name")
            account = _find_account(data, name) if name else None
            if account is None:
                if name:
                    skipped_accounts.append(name)
                continue
            for w in incoming.get("workers", []) or []:
                worker_id = (w or {}).get("worker_id")
                if not worker_id:
                    continue
                if _find_worker(account, worker_id) is not None:
                    skipped_workers.append({"account": name, "worker_id": worker_id, "reason": "already exists"})
                    continue
                required = {"kernel_slug", "results_dir"}  # notebook_path/template_path are both optional
                if not required.issubset(w):
                    skipped_workers.append({"account": name, "worker_id": worker_id, "reason": "missing fields"})
                    continue
                source_field = "notebook_path" if w.get("notebook_path") else ("template_path" if w.get("template_path") else None)
                if source_field:
                    try:
                        _validate_notebook_path(w[source_field])
                    except KaggleOpsError as e:
                        skipped_workers.append({"account": name, "worker_id": worker_id, "reason": str(e)})
                        continue
                try:
                    incoming_datasets = [_validate_dataset_source(s) for s in (w.get("dataset_sources") or [])]
                except KaggleOpsError as e:
                    skipped_workers.append({"account": name, "worker_id": worker_id, "reason": str(e)})
                    continue
                new_worker = {
                    "worker_id": worker_id,
                    "account_name": name,
                    "profile_name": settings.profile_name,
                    "kernel_slug": w["kernel_slug"],
                    "results_dir": w["results_dir"],
                    "budget_hours": w.get("budget_hours") or settings.kaggle_default_budget_hours,
                    "dataset_sources": incoming_datasets,
                }
                if source_field:
                    new_worker[source_field] = w[source_field]
                account.setdefault("workers", []).append(new_worker)
                added.append({"account": name, "worker_id": worker_id})
        if added:
            _save_accounts(data)
    return {"workers_added": added, "accounts_skipped": skipped_accounts, "workers_skipped": skipped_workers}


# --------------------------------------------------------------------------- background poller
# The one deliberate background thread in this module (mirroring
# backend/scheduler.py's own ensure_worker_started/_tick — see that module's
# docstring for why a narrow, explicit exception like this beats a second
# thread architecture). It exists for two things a purely on-demand,
# poll-on-click design can't do: firing a webhook when nobody's watching
# the tab, and chaining a worker's next push automatically. Everything else
# in this module still computes state fresh on every call.
_poller_started = False
_poller_lock = threading.Lock()


def _send_webhook(text: str) -> None:
    """Best-effort POST to a Slack/Discord-compatible incoming webhook (both
    accept a bare {"text": ...} JSON body). Never raises — a webhook that's
    unreachable or misconfigured shouldn't take down the poll tick, since
    nothing downstream depends on it succeeding."""
    url = settings.kaggle_webhook_url
    if not url:
        return
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"text": text}).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).close()
    except (urllib.error.URLError, OSError):
        pass


def _tick() -> None:
    data = _load_accounts()
    state = _load_state()
    for account in data["accounts"]:
        for w in account.get("workers", []):
            worker_id = w["worker_id"]
            rec = state.get(worker_id, {})
            if not rec.get("pushed_at") or rec.get("notified_final"):
                continue  # never pushed, or this transition was already handled
            try:
                result = refresh_status(worker_id)
            except KaggleOpsError:
                continue
            new_status = result.get("status")
            if new_status not in FINAL_STATUSES:
                continue

            _update_worker_state(worker_id, {"notified_final": True})
            notif.send_all(f"Kaggle worker '{worker_id}' ({account['name']}) is now {new_status}.")
            _send_webhook(f"Kaggle worker '{worker_id}' ({account['name']}) is now {new_status}.")

            # Batch-dispatcher completion hook (EXPERIMENT_AUTOMATION_PLAN.md §4). Lazy import:
            # batch_runner imports this module to call push(), so a top-level import here would
            # be circular; not held under any lock at this point in _tick() (each call above
            # already acquired and released _lock independently), so no deadlock risk calling
            # back into batch_runner's own locking (assignments._lock) from here.
            from . import batch_runner
            batch_runner.on_kaggle_unit_finished(worker_id, new_status)


def _poll_loop() -> None:
    while True:
        try:
            _tick()
        except Exception:
            pass  # one bad tick must never kill the whole poller
        time.sleep(max(30, settings.kaggle_poll_interval_seconds))


def ensure_kaggle_worker_started() -> None:
    global _poller_started
    with _poller_lock:
        if _poller_started:
            return
        threading.Thread(target=_poll_loop, daemon=True).start()
        _poller_started = True
