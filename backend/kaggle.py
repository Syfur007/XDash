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
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import settings

_lock = threading.Lock()          # guards kaggle_accounts.json / kaggle_state.json
_ledger_lock = threading.Lock()   # guards concurrent appends to the host repo's runs.csv

STATUS_RE = re.compile(r'has status "([^"]+)"')
IN_PROGRESS_STATUSES = {"queued", "preparing", "running"}
FINISHED_STATUSES = {"complete"}

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
def _load_accounts() -> Dict[str, Any]:
    if not settings.kaggle_accounts_file.exists():
        return {"accounts": []}
    try:
        data = json.loads(settings.kaggle_accounts_file.read_text())
    except Exception:
        return {"accounts": []}
    data.setdefault("accounts", [])
    return data


def _save_accounts(data: Dict[str, Any]) -> None:
    settings.kaggle_accounts_file.write_text(json.dumps(data, indent=2))


def _load_state() -> Dict[str, Any]:
    if not settings.kaggle_state_file.exists():
        return {}
    try:
        return json.loads(settings.kaggle_state_file.read_text())
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    settings.kaggle_state_file.write_text(json.dumps(state, indent=2))


def _update_worker_state(worker_id: str, patch: Dict[str, Any]) -> None:
    with _lock:
        state = _load_state()
        rec = state.get(worker_id, {})
        rec.update(patch)
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


def _creds_dir(name: str) -> Path:
    return settings.kaggle_creds_dir / name


def list_accounts() -> List[Dict[str, Any]]:
    """Accounts + workers, each worker enriched with its last known status
    (from kaggle_state.json) and a self-tracked usage estimate. Never
    touches the network — see refresh_status/refresh_all for that."""
    data = _load_accounts()
    state = _load_state()
    result = []
    for account in data["accounts"]:
        workers = [{**w, **state.get(w["worker_id"], {})} for w in account.get("workers", [])]
        creds_dir = _creds_dir(account["name"])
        result.append({
            "name": account["name"],
            "kaggle_username": account.get("kaggle_username"),
            "has_legacy_key": (creds_dir / CREDS_FILENAME).is_file(),
            "has_api_token": (creds_dir / TOKEN_FILENAME).is_file(),
            "workers": workers,
            "usage_estimate": estimate_usage(account["name"]),
        })
    return result


def add_account(
    name: str, username: str = "", key: str = "", api_token: str = "",
) -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise KaggleOpsError("Missing account name")
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
        creds_dir = _creds_dir(name)
        creds_dir.mkdir(parents=True, exist_ok=True)
        if legacy:
            _write_secret(creds_dir / CREDS_FILENAME, json.dumps(legacy))
        if token:
            _write_secret(creds_dir / TOKEN_FILENAME, token)
        data["accounts"].append({"name": name, "kaggle_username": resolved_username, "workers": []})
        _save_accounts(data)
    return {"name": name, "kaggle_username": resolved_username}


def _write_secret(path: Path, text: str) -> None:
    path.write_text(text)
    os.chmod(path, 0o600)


def update_credentials(
    name: str, username: str = "", key: str = "", api_token: str = "",
) -> Dict[str, Any]:
    """Rotates one or both stored credentials for an existing account without
    touching its workers — the account-delete flow wipes worker assignments
    too, which is the wrong tool for "my key expired, swap it in"."""
    username, key, api_token = (username or ""), (key or ""), (api_token or "")
    token = _validate_access_token(api_token) if api_token.strip() else None
    if not (username.strip() or key.strip()) and not token:
        raise KaggleOpsError("Provide a new username/key pair, a new API token, or both")

    with _lock:
        data = _load_accounts()
        account = _find_account(data, name)
        if account is None:
            raise KaggleOpsError(f"Unknown account '{name}'")
        # Rotating just the key (the common case — the username doesn't
        # change) shouldn't force the caller to retype a username that's
        # already on file.
        legacy = _validate_legacy_pair(username or account.get("kaggle_username", ""), key) if key.strip() else None
        creds_dir = _creds_dir(name)
        creds_dir.mkdir(parents=True, exist_ok=True)
        if legacy:
            _write_secret(creds_dir / CREDS_FILENAME, json.dumps(legacy))
            account["kaggle_username"] = legacy["username"]
        if token:
            _write_secret(creds_dir / TOKEN_FILENAME, token)
        _save_accounts(data)
    return {"name": name, "kaggle_username": account["kaggle_username"]}


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


def add_worker(
    account_name: str, worker_id: str, notebook_path: str, kernel_slug: str,
    results_dir: str, budget_hours: Optional[float] = None,
) -> Dict[str, Any]:
    worker_id = (worker_id or "").strip()
    if not worker_id or not kernel_slug or not notebook_path or not results_dir:
        raise KaggleOpsError("worker_id, notebook_path, kernel_slug and results_dir are all required")
    notebook_abs = (settings.repo_root / notebook_path).resolve()
    repo_root = settings.repo_root.resolve()
    if repo_root not in notebook_abs.parents and notebook_abs != repo_root:
        raise KaggleOpsError("notebook_path escapes the repo root")
    if not notebook_abs.is_file():
        raise KaggleOpsError(f"Notebook not found: {notebook_path}")

    with _lock:
        data = _load_accounts()
        account = _find_account(data, account_name)
        if account is None:
            raise KaggleOpsError(f"Unknown account '{account_name}'")
        if _find_worker(account, worker_id) is not None:
            raise KaggleOpsError(f"Worker '{worker_id}' already exists under '{account_name}'")
        worker = {
            "worker_id": worker_id,
            "notebook_path": str(notebook_path),
            "kernel_slug": kernel_slug,
            "results_dir": str(results_dir),
            "budget_hours": float(budget_hours) if budget_hours else settings.kaggle_default_budget_hours,
        }
        account.setdefault("workers", []).append(worker)
        _save_accounts(data)
    return worker


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
            "dashboard_config.yaml to wherever it's installed."
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
        "dataset_sources": [],
        "competition_sources": [],
        "kernel_sources": [],
    }


def push(worker_id: str) -> Dict[str, Any]:
    data = _load_accounts()
    account, worker = _find_worker_and_account(data, worker_id)
    if worker is None:
        raise KaggleOpsError(f"Unknown worker '{worker_id}'")
    notebook_abs = settings.repo_root / worker["notebook_path"]
    if not notebook_abs.is_file():
        raise KaggleOpsError(f"Notebook not found: {worker['notebook_path']}")

    tmpdir = tempfile.mkdtemp(prefix="kaggle_push_")
    try:
        shutil.copy(notebook_abs, Path(tmpdir) / notebook_abs.name)
        metadata = _kernel_metadata(account, worker, notebook_abs.name)
        (Path(tmpdir) / "kernel-metadata.json").write_text(json.dumps(metadata, indent=2))
        proc = _run_kaggle(["kernels", "push", "-p", tmpdir], account["name"], timeout=120)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        _update_worker_state(worker_id, {"status": "push_failed", "last_error": detail})
        raise KaggleOpsError(f"Push failed for '{worker_id}': {detail}")

    _update_worker_state(worker_id, {
        "status": "pushed", "pushed_at": _now_iso(), "last_error": None, "over_budget": False,
    })
    return {"worker_id": worker_id, "status": "pushed"}


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
    kaggle_status = m.group(1) if m else "unknown"

    state = _load_state()
    pushed_at = state.get(worker_id, {}).get("pushed_at")
    over_budget = False
    if kaggle_status in IN_PROGRESS_STATUSES and pushed_at:
        budget_hours = worker.get("budget_hours") or settings.kaggle_default_budget_hours
        elapsed_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(pushed_at)).total_seconds() / 3600.0
        over_budget = elapsed_hours > budget_hours

    patch = {"status": kaggle_status, "last_error": None, "over_budget": over_budget, "checked_at": _now_iso()}
    _update_worker_state(worker_id, patch)
    return {"worker_id": worker_id, **patch}


# --------------------------------------------------------------------------- download + ledger
def register_ledger(results_dir: Path) -> List[str]:
    """Copies each newly-downloaded run's manifest.json into the host repo's
    own artifacts/runs/<run_id>/ and appends its row into artifacts/ledger/
    runs.csv — mirroring orchestration/manifest.py's atomic-write style and
    orchestration/ledger.py's RUNS_FIELDS exactly, stdlib-only (no import of
    that package, same as backend/ledger.py's read side). Idempotent: a
    run_id already present with status 'done' in the host repo's own
    runs.csv is skipped. Returns the run_ids newly registered."""
    manifests_dir = results_dir / "artifacts" / "runs"
    if not manifests_dir.is_dir():
        return []

    src_runs_csv = results_dir / "artifacts" / "ledger" / "runs.csv"
    src_rows_by_id: Dict[str, Dict[str, str]] = {}
    if src_runs_csv.is_file():
        with open(src_runs_csv, newline="") as f:
            src_rows_by_id = {row.get("run_id"): row for row in csv.DictReader(f)}

    dest_ledger_dir = settings.artifacts_dir / "ledger"
    dest_runs_csv = dest_ledger_dir / "runs.csv"
    dest_runs_dir = settings.runs_artifacts_dir

    newly_registered: List[str] = []
    with _ledger_lock:
        done_ids = set()
        if dest_runs_csv.is_file():
            with open(dest_runs_csv, newline="") as f:
                done_ids = {row.get("run_id") for row in csv.DictReader(f) if row.get("status") == "done"}

        dest_ledger_dir.mkdir(parents=True, exist_ok=True)
        is_new_csv = not dest_runs_csv.is_file()
        with open(dest_runs_csv, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=RUNS_FIELDS)
            if is_new_csv:
                writer.writeheader()
            for manifest_path in sorted(manifests_dir.glob("*/manifest.json")):
                try:
                    manifest = json.loads(manifest_path.read_text())
                except Exception:
                    continue
                run_id = manifest.get("run_id") or manifest_path.parent.name
                if run_id in done_ids:
                    continue
                row = src_rows_by_id.get(run_id)
                if row is None:
                    continue

                dest_manifest_path = dest_runs_dir / run_id / "manifest.json"
                dest_manifest_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = dest_manifest_path.with_suffix(".json.tmp")
                tmp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str))
                os.replace(tmp_path, dest_manifest_path)

                writer.writerow({k: row.get(k, "") for k in RUNS_FIELDS})
                newly_registered.append(run_id)

    return newly_registered


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
    _update_worker_state(worker_id, {"status": "downloaded", "downloaded_at": _now_iso(), "last_error": None})
    return {"worker_id": worker_id, "results_dir": str(results_dir), "registered_runs": registered}


# --------------------------------------------------------------------------- quota estimate
def _utc_week_start() -> datetime:
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def estimate_usage(account_name: str) -> Dict[str, Any]:
    """Self-tracked GPU-hours estimate for *account_name* this UTC week,
    summed from this account's own downloaded run manifests. NOT Kaggle's
    authoritative quota figure — Kaggle exposes no API for that (only a
    logged-in browser session sees the real number on the account's Usage
    page), so this can drift from it and is presented as an estimate."""
    data = _load_accounts()
    account = _find_account(data, account_name)
    if account is None:
        return {"hours_this_week": 0.0, "week_start": None}

    week_start = _utc_week_start()
    total = 0.0
    for worker in account.get("workers", []):
        manifests_dir = (settings.repo_root / worker["results_dir"] / "artifacts" / "runs").resolve()
        if not manifests_dir.is_dir():
            continue
        for manifest_path in manifests_dir.glob("*/manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text())
            except Exception:
                continue
            start_time, gpu_hours = manifest.get("start_time"), manifest.get("gpu_hours")
            if not start_time or not gpu_hours:
                continue
            try:
                started = datetime.fromisoformat(start_time)
            except ValueError:
                continue
            if started >= week_start:
                total += float(gpu_hours)
    return {"hours_this_week": round(total, 2), "week_start": week_start.isoformat()}


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


def push_all() -> List[Dict[str, Any]]:
    return _run_bulk(push, _all_worker_ids())


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
