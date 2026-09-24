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
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import (
    settings, Settings, list_profile_names,
    SYSTEM_KAGGLE_ACCOUNTS_FILE, SYSTEM_KAGGLE_CREDS_DIR,
)
from . import configs as cfg
from . import results_ingest

_lock = threading.Lock()          # guards kaggle_accounts.json

STATUS_RE = re.compile(r'has status "([^"]+)"')
_ENUM_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.")
# Live-verified 2026-09 against the actually-installed `kaggle` CLI (pip package "kaggle" 1.7.4.5,
# github.com/Kaggle/kaggle-api): `kernels status` prints the Python enum's own repr — e.g.
# `has status "KernelWorkerStatus.RUNNING"` — not the bare lowercase string
# IN_PROGRESS_STATUSES/FINAL_STATUSES below expect. Confirmed via a real push +
# status poll during this feature's own testing (DASHBOARD_REDESIGN_PLAN.md §2.1's fact-check).
# Without this normalization, over-budget detection and the dispatcher's own
# final-status/notification trigger silently never fire against this CLI version — every
# comparison below is an exact-match against a lowercase set.
_STATUS_ALIASES = {"cancelacknowledged": "cancelAcknowledged"}


def _normalize_kaggle_status(raw: str) -> str:
    value = _ENUM_PREFIX_RE.sub("", (raw or "").strip()).strip().lower()
    return _STATUS_ALIASES.get(value, value)


IN_PROGRESS_STATUSES = {"queued", "preparing", "running"}
# experiments.py stops polling an Attempt past one of these.
FINAL_STATUSES = {"complete", "error", "cancelAcknowledged"}

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


def _find_account(data: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    return next((a for a in data["accounts"] if a["name"] == name), None)


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


def list_accounts() -> List[Dict[str, Any]]:
    """Accounts with a self-tracked usage estimate/history. Never touches the
    network. Credentials are reported only as booleans — the secrets
    themselves never round-trip to a caller (see update_credentials)."""
    data = _load_accounts()
    result = []
    for account in data["accounts"]:
        creds_dir = _creds_dir(account["name"])
        result.append({
            "name": account["name"],
            "kaggle_username": account.get("kaggle_username"),
            "scope": account.get("scope", SCOPE_REPO),
            "has_legacy_key": (creds_dir / CREDS_FILENAME).is_file(),
            "has_api_token": (creds_dir / TOKEN_FILENAME).is_file(),
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



def _looks_like_unrecognized_option(output: str, flag: str) -> bool:
    text = (output or "").lower()
    return flag.lower() in text and any(
        phrase in text for phrase in ("no such option", "unrecognized", "unexpected argument", "unknown option")
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

    registered = results_ingest.register_ledger(results_dir_abs)
    xdash_status = results_ingest.read_xdash_status(results_dir_abs)
    return {"results_dir": str(results_dir_abs), "registered_runs": registered, "xdash_status": xdash_status}


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


# Attempt statuses (backend/experiments.py's vocabulary, not Kaggle's) that
# should still reserve quota: the kernel is pushed or about to be, so its
# hours are being spent even though no manifest exists yet.
_RESERVING_STATUSES = frozenset({"dispatching", "running"})


def _attempts_from_store(path: Path) -> List[Dict[str, Any]]:
    """Every Attempt record out of a profile's experiments.json, read as plain
    JSON rather than through backend/experiments.py — that module imports this
    one to push, so a real import would be circular, and the four fields read
    here (slot, status, started_at, unit_ref.results_dir) are a stable part of
    the on-disk shape. Missing/corrupt file reads as "no attempts", never
    raises: quota accounting degrading to 0 is survivable, a 500 on every
    account list is not."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return []
    attempts = data.get("attempts") if isinstance(data, dict) else None
    return list(attempts.values()) if isinstance(attempts, dict) else []


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
    for each of this account's Attempts still in _RESERVING_STATUSES, add
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

    # (attempt, repo_root, manifest_layout) tuples. A repo-scoped account only
    # ever ran under the active profile; a system one accrues hours under every
    # profile it was dispatched from, and results_dir/manifest_layout are both
    # repo-relative or per-profile — which is the whole reason for the
    # per-profile snapshot rather than just reading `settings`.
    scoped: List[tuple] = []
    if account.get("scope") == SCOPE_SYSTEM:
        for profile_name in list_profile_names():
            snap = _profile_snapshot(profile_name)
            if snap is None:
                continue
            for attempt in _attempts_from_store(snap.experiments_store_file):
                scoped.append((attempt, snap.repo_root, snap.manifest_layout))
    else:
        for attempt in _attempts_from_store(settings.experiments_store_file):
            scoped.append((attempt, settings.repo_root, settings.manifest_layout))

    slot = "kaggle:%s" % account_name
    earliest = buckets[0]
    now = datetime.now(timezone.utc)
    for attempt, repo_root, manifest_layout in scoped:
        if attempt.get("slot") != slot:
            continue
        results_dir = (attempt.get("unit_ref") or {}).get("results_dir")
        if results_dir:
            base_dir = (repo_root / results_dir).resolve()
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

        if attempt.get("status") in _RESERVING_STATUSES and attempt.get("started_at"):
            try:
                started_at = datetime.fromisoformat(attempt["started_at"])
            except ValueError:
                continue
            elapsed_hours = (now - started_at).total_seconds() / 3600.0
            totals[this_week_start] += min(elapsed_hours, settings.kaggle_default_budget_hours)

    return [{"week_start": b.isoformat(), "hours": round(totals[b], 2)} for b in buckets]


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


