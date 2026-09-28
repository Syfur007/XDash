"""Google Colab account registry + CLI wrapper (Multi_runner_XDash.md
Phase 4, rewritten for XDASH_PLAN.md X5) — provisions/tears down ephemeral
VMs via the `google-colab-cli` (`colab`), mirroring backend/kaggle.py's
per-account subprocess isolation.

Verified against colab-cli-reference.md and the google-colab-cli 0.7.2
source itself (its `commands/session.py`, `auth.py`, `cli.py`,
`consumption.py`), not guessed:

- **Global options go before the command** (`colab --config X ssh ...`).
  They are typer callback options; placed after the subcommand, `new` and
  friends reject them.
- **Per-account isolation is a per-account HOME.** Every path the CLI owns is
  `os.path.expanduser("~/...")`: the OAuth token
  (`~/.config/colab-cli/token.json`, computed at import), sessions, history,
  the update-check settings. There is no XDG_CONFIG_HOME support. So each
  account runs the CLI with HOME=data/colab_accounts/<name>/home/, which
  isolates all of them at once. PYTHONUSERBASE is pinned to the real one so a
  `pip install --user` CLI still imports under the fake HOME.
- **`sessions.json` is session state, not a credential.** `colab new`
  creates it. The credential is the OAuth token (or ADC credentials) under
  the account's HOME; that is what "connected" means here. The old wrapper
  refused to run anything until sessions.json existed — which only `colab
  new` (run through that same wrapper) could create, so a fresh account could
  never provision.
- **`--gpu` is validated** against T4/L4/G4/H100/A100: the CLI maps an
  unknown value to A100 silently.
- **ssh is reached through `colab ssh --proxy-mode`** with an absolute CLI
  path (ssh runs ProxyCommand without the user's PATH), the account's HOME,
  and an explicit ed25519 key (`-i`): under the per-account HOME, the CLI's
  default `~/.ssh/id_ed25519` would point at nothing. RSA is rejected by
  Colab.
- **`status`/`sessions` print text, not JSON**, one line per session:
  `[name] endpoint | Hardware: T4 | Shape: Standard | Variant: GPU[ | Status: IDLE]`
  (`_format_session_line` in the CLI). `status -s NAME` for an unknown
  session prints "Session 'NAME' not found." and still exits 0.
- **`colab usage`** prints the compute-unit balance and burn rate.

Single scope, unlike Kaggle's system/repo split: a Google account's session
limit is a property of the person/tier, shared across every repo profile.
Connecting an account (the one-time OAuth copy-paste login) is still a
manual step until Phase 5's Connect-account flow — see connect_command().
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import settings, SYSTEM_COLAB_ACCOUNTS_FILE, SYSTEM_COLAB_CREDS_DIR
from .store import JsonStore
from . import tools

_lock = threading.Lock()          # guards colab_accounts.json
_store = JsonStore(SYSTEM_COLAB_ACCOUNTS_FILE, lambda: {"accounts": []})

# Written by the pre-X5 wrapper into data/colab_accounts/<name>/. The OAuth
# *client* config is still honoured (passed as `-c`); the old sessions.json
# is ignored — it was never a credential.
LEGACY_OAUTH_CLIENT_FILENAME = "oauth-config.json"

VALID_GPUS = ("T4", "L4", "G4", "H100", "A100")
VALID_AUTH = ("oauth2", "adc")

# `[name] endpoint | Hardware: X | Shape: Y | Variant: Z[ | Status: W]`
_SESSION_LINE_RE = re.compile(
    r"^\[(?P<name>[^\]]+)\]\s+(?P<endpoint>\S+)\s+\|\s+Hardware:\s+(?P<hardware>[^|]+?)\s+"
    r"\|\s+Shape:\s+(?P<shape>[^|]+?)\s+\|\s+Variant:\s+(?P<variant>[^|]+?)"
    r"(?:\s+\|\s+Status:\s+(?P<status>.+?))?\s*$"
)
_BALANCE_RE = re.compile(r"Current balance:\s*([-\d.]+)")
_RATE_RE = re.compile(r"Usage rate:\s*([-\d.]+)\s*/\s*hr")
_ASSIGNMENTS_RE = re.compile(r"Active assignments:\s*(\d+)")


class ColabOpsError(Exception):
    """Expected failure (bad account, subprocess error, CLI absent) — routes
    map this to a 4xx, not a stack trace."""


class ColabNotConnectedError(ColabOpsError):
    """The account has no CLI login under its HOME yet. Running the CLI
    anyway would start the interactive OAuth prompt and hang."""


class ProvisionError(ColabOpsError):
    """`colab new` (or the wait for SSH afterward) failed outright — maps to
    the `provision-failed` blocked code."""


class NoAcceleratorError(ColabOpsError):
    """`colab new` succeeded but `colab status` reports no GPU/TPU actually
    granted (Colab-constraints table: "a Pro+ request for A100 can be handed
    a T4; free tier can be denied a GPU entirely") — maps to the
    `no-accelerator` blocked code, distinct from a bare provisioning failure
    since the fix is different (retry later / different tier), not "broken"."""


# --------------------------------------------------------------------------- storage
def _load() -> Dict[str, Any]:
    data = _store.load()
    data.setdefault("accounts", [])
    return data


def _save(data: Dict[str, Any]) -> None:
    _store.save(data)


def _find(data: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    return next((a for a in data["accounts"] if a["name"] == name), None)


def find_account(name: str) -> Optional[Dict[str, Any]]:
    return _find(_load(), name)


def _creds_dir(name: str) -> Path:
    return SYSTEM_COLAB_CREDS_DIR / name


def account_home(name: str) -> Path:
    """The HOME every `colab` subprocess for *name* runs under."""
    return _creds_dir(name) / "home"


def sessions_path(name: str) -> Path:
    return account_home(name) / ".config" / "colab-cli" / "sessions.json"


def token_path(name: str) -> Path:
    return account_home(name) / ".config" / "colab-cli" / "token.json"


def adc_path(name: str) -> Path:
    return account_home(name) / ".config" / "gcloud" / "application_default_credentials.json"


def has_credentials(name: str, account: Optional[Dict[str, Any]] = None) -> bool:
    account = account if account is not None else (find_account(name) or {})
    if (account.get("auth") or "oauth2") == "adc":
        return adc_path(name).is_file()
    return token_path(name).is_file()


def validate_gpu(gpu: str) -> str:
    """*gpu* normalized to the CLI's spelling, or ColabOpsError — never let an
    unknown value through, since `colab new --gpu <unknown>` silently rents an
    A100."""
    value = (gpu or "").strip().upper()
    if value not in VALID_GPUS:
        raise ColabOpsError("Unknown Colab GPU %r — must be one of %s" % (gpu, ", ".join(VALID_GPUS)))
    return value


def list_accounts() -> List[Dict[str, Any]]:
    """Accounts with whether each is connected (has a CLI login under its
    HOME). Never touches the network — see list_sessions()/session_status()
    for the live VM-state queries."""
    result = []
    for account in _load()["accounts"]:
        result.append({
            "name": account["name"],
            "label": account.get("label") or account["name"],
            "gpu": account.get("gpu") or settings.colab_default_gpu,
            "auth": account.get("auth") or "oauth2",
            "session_limit_hours": account.get("session_limit_hours"),
            "has_credentials": has_credentials(account["name"], account),
            "home": str(account_home(account["name"])),
        })
    return result


def add_account(
    name: str, label: str = "", gpu: str = "", session_limit_hours: Optional[float] = None,
    auth: str = "oauth2", ssh_key: str = "",
) -> Dict[str, Any]:
    """Registers *name* and creates its HOME. Does not log in: a human runs
    connect_command() once (the CLI's copy-paste OAuth), until Phase 5 wires
    that flow into the Compute tab. Registering first is deliberate: it gives
    the login a stable HOME to write its token into."""
    name = (name or "").strip()
    if not name or not re.match(r"^[A-Za-z0-9_.-]+$", name):
        raise ColabOpsError("Account names may use letters, digits, '.', '_' and '-' only")
    auth = (auth or "oauth2").strip().lower()
    if auth not in VALID_AUTH:
        raise ColabOpsError("auth must be one of %s" % ", ".join(VALID_AUTH))
    record = {"name": name, "label": (label or "").strip() or name, "auth": auth}
    if (gpu or "").strip():
        record["gpu"] = validate_gpu(gpu)
    if session_limit_hours is not None:
        record["session_limit_hours"] = float(session_limit_hours)
    if (ssh_key or "").strip():
        record["ssh_key"] = str(Path(ssh_key.strip()).expanduser())
    with _lock:
        data = _load()
        if _find(data, name) is not None:
            raise ColabOpsError(f"Account '{name}' already exists")
        account_home(name).mkdir(parents=True, exist_ok=True)
        data["accounts"].append(record)
        _save(data)
    return record


def remove_account(name: str) -> bool:
    with _lock:
        data = _load()
        before = len(data["accounts"])
        data["accounts"] = [a for a in data["accounts"] if a["name"] != name]
        if len(data["accounts"]) == before:
            return False
        _save(data)
    return True


def set_session_limit(name: str, hours: Optional[float]) -> Dict[str, Any]:
    with _lock:
        data = _load()
        account = _find(data, name)
        if account is None:
            raise ColabOpsError(f"Unknown account '{name}'")
        if hours is None:
            account.pop("session_limit_hours", None)
        else:
            account["session_limit_hours"] = float(hours)
        _save(data)
    return account


def session_limit_hours(name: str) -> float:
    account = find_account(name)
    limit = (account or {}).get("session_limit_hours")
    return float(limit) if limit is not None else settings.colab_default_session_limit_hours


# --------------------------------------------------------------------------- CLI
def colab_path() -> Optional[str]:
    """Absolute path of the `colab` executable, or None. Absolute because the
    ProxyCommand runs without the user's PATH (colab-cli-reference §5.2).
    Resolved through backend/tools.py's registry (XDASH_FIXES_PLAN.md D3: an
    explicit override, then the bin/ dir next to whichever Python is running
    server.py, then PATH) instead of a bare PATH-only lookup."""
    st = tools.status("colab")
    if not st.exists or not st.executable:
        return None
    return os.path.abspath(st.path)


def colab_available() -> bool:
    """Cheap PATH check, same discipline as tmux_runner.tmux_available()'s
    local branch — a missing `colab` binary degrades this kind to
    "unavailable", never a 500."""
    return colab_path() is not None


def _session_name(account_name: str) -> str:
    return "xdash-%s" % account_name


def _real_user_base() -> str:
    return os.environ.get("PYTHONUSERBASE") or os.path.expanduser("~/.local")


def _env(account_name: str) -> Dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = str(account_home(account_name))
    env["PYTHONUSERBASE"] = _real_user_base()
    return env


def _global_args(account_name: str, account: Optional[Dict[str, Any]] = None) -> List[str]:
    """The options that must precede the subcommand."""
    account = account if account is not None else (find_account(account_name) or {})
    args = ["--config", str(sessions_path(account_name))]
    legacy_client = _creds_dir(account_name) / LEGACY_OAUTH_CLIENT_FILENAME
    if legacy_client.is_file():
        args += ["-c", str(legacy_client)]
    if (account.get("auth") or "oauth2") == "adc":
        args += ["--auth", "adc"]
    return args


def colab_argv(account_name: str, args: List[str]) -> List[str]:
    exe = colab_path()
    if exe is None:
        raise ColabOpsError(
            f"'{tools.path('colab')}' was not found. Set an override for 'colab' in "
            f"Settings -> Tools, or make sure it's installed in the environment running server.py."
        )
    return [exe] + _global_args(account_name) + list(args)


def connect_command(account_name: str) -> str:
    """The one-time login for *account_name*, as a shell command a human
    runs: any authenticated command triggers the CLI's copy-paste OAuth flow
    and saves the token under the account's HOME."""
    exe = colab_path() or tools.path("colab")
    argv = [exe] + _global_args(account_name) + ["sessions"]
    return "env HOME=%s PYTHONUSERBASE=%s %s" % (
        shlex.quote(str(account_home(account_name))), shlex.quote(_real_user_base()),
        " ".join(shlex.quote(a) for a in argv),
    )


def test_config(gpu: str = "") -> Dict[str, Any]:
    """The Add-runtime wizard's live test for Colab (XDASH_PLAN.md §8.4) —
    run *before* an account is registered, so there's no HOME/token to log
    in against yet. Real connectivity can only be proven by actually running
    the OAuth flow (begin_connect() below), which this deliberately does not
    do: never touches the network, only checks the GPU choice and that the
    CLI binary itself is present."""
    if gpu:
        try:
            validate_gpu(gpu)
        except ColabOpsError as e:
            return {"ok": False, "detail": str(e)}
    if not colab_available():
        return {"ok": False, "detail": f"'{tools.path('colab')}' was not found on PATH"}
    return {"ok": True, "detail": "CLI found on PATH. Save, then use Connect account to finish sign-in."}


# --------------------------------------------------------------------------- Connect-account OAuth flow (XDASH_PLAN.md §8.4/§10 Phase 5)
# Drives the CLI's copy-paste OAuth from the dashboard instead of the
# out-of-band shell command connect_command() above documents: starts the
# login subprocess with the account's own per-account HOME (X5, already
# isolated by _env() below), captures the sign-in URL it prints, and accepts
# the pasted code back over the same process's stdin.
#
# *** This is the one place in this phase that, when actually invoked (i.e.
# begin_connect() is called against the real `colab` binary), opens a real
# browser-facing Google OAuth flow. That line is the subprocess start inside
# procsession.start() below — everything upstream of it (the route, this
# function's own argv-building) is inert until that call actually runs. No
# test in this suite calls it with a real `colab` executable: they point
# colab.colab_path (via monkeypatch, or tools.set_override("colab", ...)) at a
# local fixture script that mimics the prompt/response shape without
# contacting Google. ***
def _connect_key(account_name: str) -> str:
    return f"colab-connect:{account_name}"


def begin_connect(account_name: str) -> Dict[str, Any]:
    from . import procsession
    account = find_account(account_name)
    if account is None:
        raise ColabOpsError(f"Unknown Colab account '{account_name}'")
    argv = colab_argv(account_name, ["sessions"])  # any authenticated command triggers first-use OAuth
    account_home(account_name).mkdir(parents=True, exist_ok=True)
    procsession.start(_connect_key(account_name), argv, env=_env(account_name))
    return connect_status(account_name)


_URL_RE = re.compile(r"https?://\S+")


def connect_status(account_name: str) -> Dict[str, Any]:
    from . import procsession
    session = procsession.get(_connect_key(account_name))
    if session is None:
        return {"active": False}
    snap = session.snapshot()
    url_match = _URL_RE.search(snap["output"])
    # Heuristic, documented (KAGGLE_API.md's sibling doc has no worked example
    # of the CLI's exact prompt text either): once a sign-in URL has printed
    # and the child has gone quiet for a moment, it is almost certainly
    # blocked on stdin waiting for the pasted code, not mid-print.
    awaiting_code = bool(url_match) and not snap["done"] and snap["idle_seconds"] > 1.0
    return {
        "active": True,
        "output": snap["output"],
        "url": url_match.group(0) if url_match else None,
        "awaiting_code": awaiting_code,
        "done": snap["done"],
        "returncode": snap["returncode"],
        "connected": snap["done"] and snap["returncode"] == 0 and has_credentials(account_name),
    }


def submit_connect_code(account_name: str, code: str) -> Dict[str, Any]:
    from . import procsession
    session = procsession.get(_connect_key(account_name))
    if session is None:
        raise ColabOpsError(f"No Connect-account session in progress for '{account_name}' — click Connect first")
    code = (code or "").strip()
    if not code:
        raise ColabOpsError("Paste the code shown after signing in")
    if not session.send(code):
        raise ColabOpsError("The login process already exited — click Connect to start over")
    return connect_status(account_name)


def cancel_connect(account_name: str) -> bool:
    from . import procsession
    return procsession.stop(_connect_key(account_name))


def _run_colab(args: List[str], account_name: str, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    account = find_account(account_name)
    if account is None:
        raise ColabOpsError(f"Unknown Colab account '{account_name}'")
    if not has_credentials(account_name, account):
        raise ColabNotConnectedError(
            f"Colab account '{account_name}' is not connected (no CLI login under {account_home(account_name)}). "
            f"Run this once, in a terminal, and follow the sign-in link: {connect_command(account_name)}"
        )
    argv = colab_argv(account_name, args)
    account_home(account_name).mkdir(parents=True, exist_ok=True)
    try:
        # stdin=DEVNULL: an expired login makes the CLI prompt for a code;
        # with no stdin it fails at once instead of hanging until timeout.
        return subprocess.run(
            argv, env=_env(account_name), capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise ColabOpsError(f"'{argv[0]}' could not be executed")
    except subprocess.TimeoutExpired:
        raise ColabOpsError(f"colab {' '.join(args)} timed out after {timeout}s")


def _tail(proc: subprocess.CompletedProcess, n: int = 500) -> str:
    return (proc.stderr or proc.stdout or "").strip()[-n:]


def parse_session_lines(text: str) -> List[Dict[str, Any]]:
    """Every `[name] endpoint | Hardware: ... ` line in *text* (the output of
    `colab sessions` or `colab status`); any other line — `[colab] ...`
    notices, an update banner, `Last Execution:` detail — is skipped."""
    rows = []
    for line in (text or "").splitlines():
        m = _SESSION_LINE_RE.match(line.strip())
        if not m:
            continue
        row = {k: (v.strip() if isinstance(v, str) else v) for k, v in m.groupdict().items()}
        row["accelerator"] = row.pop("hardware")
        rows.append(row)
    return rows


# Sessions/status listings are polled once per account per dispatch tick (the
# same shape as kaggle.py's own _usage_cache) — a TTL cache keeps an idle
# fleet of Colab accounts from re-invoking the CLI every ~30s poll tick per
# account for state that doesn't change nearly that often.
_SESSIONS_CACHE_TTL_SECONDS = 20.0
_sessions_cache: Dict[str, tuple] = {}  # account_name -> (monotonic_time, result)


def list_sessions(account_name: str) -> List[Dict[str, Any]]:
    """This account's live VM under XDash's session name (0 or 1 rows), from
    `colab sessions`. Returns [] on any read failure — a poll loop must
    degrade to "unknown, treat as idle" rather than raise, same discipline as
    tmux_runner's own returncode-127 convention."""
    now = time.monotonic()
    cached = _sessions_cache.get(account_name)
    if cached is not None and (now - cached[0]) < _SESSIONS_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        proc = _run_colab(["sessions"], account_name, timeout=60)
    except ColabOpsError:
        return []
    result: List[Dict[str, Any]] = []
    if proc.returncode == 0:
        result = [r for r in parse_session_lines(proc.stdout) if r["name"] == _session_name(account_name)]
    _sessions_cache[account_name] = (now, result)
    return result


def _invalidate_sessions_cache(account_name: str) -> None:
    _sessions_cache.pop(account_name, None)


def session_status(account_name: str) -> Optional[Dict[str, Any]]:
    """Hardware/shape/variant/IDLE-BUSY for this account's named session —
    `colab status -s xdash-<account>`. None if no such session (the CLI
    prints "not found" and exits 0 for that)."""
    try:
        proc = _run_colab(["status", "-s", _session_name(account_name)], account_name, timeout=60)
    except ColabOpsError:
        return None
    if proc.returncode != 0:
        return None
    rows = [r for r in parse_session_lines(proc.stdout) if r["name"] == _session_name(account_name)]
    return rows[0] if rows else None


def usage(account_name: str) -> Dict[str, Any]:
    """Compute-unit balance and burn rate from `colab usage` (XDASH_PLAN.md
    §3.6's Colab quota): {balance, burn_per_h, assignments, unit: "CU",
    source: "measured"}. A real network call — on demand only."""
    proc = _run_colab(["usage"], account_name, timeout=60)
    if proc.returncode != 0:
        raise ColabOpsError("colab usage failed: %s" % _tail(proc))
    balance, rate, count = (_BALANCE_RE.search(proc.stdout), _RATE_RE.search(proc.stdout),
                            _ASSIGNMENTS_RE.search(proc.stdout))
    if balance is None:
        raise ColabOpsError("Unrecognized `colab usage` output: %r" % proc.stdout[-300:])
    return {
        "balance": float(balance.group(1)),
        "burn_per_h": float(rate.group(1)) if rate else None,
        "assignments": int(count.group(1)) if count else None,
        "unit": "CU", "source": "measured",
    }


def ssh_key_for(account_name: str) -> str:
    """The ed25519 private key this account's VM is reached with — its own
    `ssh_key`, else the profile's colab_ssh_key (default ~/.ssh/id_ed25519,
    resolved against the real home). ColabOpsError if missing or not ed25519
    (Colab rejects RSA; XDASH_PLAN.md X5 requires ed25519)."""
    account = find_account(account_name) or {}
    key = Path(account.get("ssh_key") or settings.colab_ssh_key).expanduser()
    if not key.is_file():
        raise ColabOpsError(
            f"No SSH key at {key} for Colab account '{account_name}' — create one with "
            f"`ssh-keygen -t ed25519 -f {key}` (Colab accepts ed25519, not RSA)."
        )
    pub = Path(str(key) + ".pub")
    try:
        if pub.is_file():
            key_type = (pub.read_text().split() or [""])[0]
        else:
            proc = subprocess.run(["ssh-keygen", "-y", "-f", str(key)], capture_output=True, text=True, timeout=15,
                                  stdin=subprocess.DEVNULL)
            key_type = (proc.stdout.split() or [""])[0] if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ColabOpsError(f"Could not read SSH key {key}: {e}")
    if key_type != "ssh-ed25519":
        raise ColabOpsError(
            f"SSH key {key} is {key_type or 'unreadable'}, not ed25519 — Colab requires ed25519 "
            f"(`ssh-keygen -t ed25519`)."
        )
    return str(key)


def proxy_command(account_name: str, key: str) -> str:
    """The ssh ProxyCommand for this account's VM: the absolute CLI path,
    the account's HOME and global options, then `ssh --proxy-mode` with the
    explicit key. `%` is doubled because ssh expands %-tokens in it."""
    env_exe = "/usr/bin/env" if os.path.exists("/usr/bin/env") else "env"
    argv = [env_exe, "HOME=%s" % account_home(account_name), "PYTHONUSERBASE=%s" % _real_user_base()]
    argv += colab_argv(account_name, ["ssh", "--proxy-mode", "-s", _session_name(account_name), "-i", key])
    return " ".join(shlex.quote(a) for a in argv).replace("%", "%%")


def ensure_session(account_name: str, gpu: str = "", timeout: float = 300.0) -> Dict[str, Any]:
    """Returns {"proxy_command", "accelerator", "identity_file"} for a live VM
    for *account_name*, reusing one already up (keeps it warm across
    attempts) or provisioning one via `colab new --gpu <gpu> -s xdash-<acct>`.

    Raises ProvisionError if the CLI/provisioning fails, or NoAcceleratorError
    if the VM came up without a GPU/TPU — in which case it is stopped again
    first, so a CPU VM nobody asked for doesn't keep burning compute units."""
    gpu = validate_gpu(gpu or settings.colab_default_gpu)
    key = ssh_key_for(account_name)
    if not list_sessions(account_name):
        try:
            proc = _run_colab(["new", "--gpu", gpu, "-s", _session_name(account_name)], account_name, timeout=timeout)
        except ColabOpsError as e:
            raise ProvisionError(str(e))
        if proc.returncode != 0:
            raise ProvisionError(_tail(proc) or "colab new failed")
        _invalidate_sessions_cache(account_name)

    status = session_status(account_name)
    if status is None:
        raise ProvisionError(f"'colab new' reported success but 'colab status' can't find session '{_session_name(account_name)}'")

    accelerator = (status.get("accelerator") or "").strip()
    if not accelerator or accelerator.upper() in ("CPU", "NONE"):
        stop_session(account_name)
        raise NoAcceleratorError(
            f"Colab granted no accelerator for account '{account_name}' this attempt "
            f"(requested {gpu}; status: {status})"
        )
    return {"proxy_command": proxy_command(account_name, key), "accelerator": accelerator, "identity_file": key}


def stop_session(account_name: str) -> bool:
    """`colab stop -s xdash-<account>`. True once no such session is tracked
    any more (stopped now, or already gone)."""
    try:
        proc = _run_colab(["stop", "-s", _session_name(account_name)], account_name, timeout=120)
    except ColabOpsError:
        return False
    _invalidate_sessions_cache(account_name)
    return proc.returncode == 0
