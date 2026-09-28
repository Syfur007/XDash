"""XDASH_FIXES_PLAN.md F1.5 — "Verify environment" (D6): a cheap, cached,
framework-agnostic gate that answers "does this host's interpreter, in this
env, even import the training entrypoint?" *before* dispatch trusts it,
instead of finding out only after a training crash — issue 11's U1 (a stale
conda env missing `timm.layers`) and U3 (mclab's env missing `dissert`
entirely) are exactly the failures this would have caught in seconds.

Runs the profile's `commands.check` template (default `{python}
{train_script} --help`, backend/config.py) over the host's own transport, in
an interactive login shell (`bash -ic`) — the same shell backend/terminals.py's
`_start_session` types `env_activate_cmd` into, since `conda activate` is a
shell *function* sourced from `~/.bashrc`/`~/.bash_profile`, invisible to a
plain non-interactive `bash -c`.

**Caching + the sync/async choice, spelled out (the plan asks for both to be
decided and documented):**

- Cache key: (host id, repo_root, env_activate_cmd, python_executable, the
  local repo's current commit) — the exact tuple that could change what the
  check would report. A code-only commit (no env/host change) still busts
  the cache, which is correct but cheap: framework.code_state() is itself
  cached for 10s, so this adds no new git calls of its own.
- TTL: `_TTL_SECONDS` (5 minutes) — long enough that a dispatch tick (every
  few seconds) never re-runs a live check, short enough that fixing the env
  (U1/U3) is felt on the next dispatch attempt without a restart.
- On a CACHE MISS, `gate_for_dispatch()` (what `MachineRunner.can_accept()`
  calls) does NOT run the check inline and block that tick on it — it kicks
  a background thread to prime the cache and returns "not gated" for *this*
  tick, the asynchronous choice. Rationale: `can_accept()` runs on every
  dispatch tick for every pending experiment; a synchronous first-ever check
  (interpreter startup + import, generally sub-second, but unbounded on a
  slow/hung remote shell) would otherwise stall the *whole* tick's dispatch
  decision the first time any host is asked about, and after every TTL
  expiry. The cost of "asynchronous" is that a truly cold cache never blocks
  the very first dispatch after a restart — that one attempt can still fail
  the old way (caught by F0.4's post-hoc diagnose()) — but every dispatch
  after the (typically sub-second) background check completes is gated.
  Under the test harness (XDASH_DISABLE_BACKGROUND=1) the background prime
  is skipped entirely, so a cold cache is simply "unknown, not gated" and
  never trips a real subprocess mid-test — see the module's own tests for
  how the gating logic itself is still exercised deterministically (prime
  the cache directly with a FakeTransport, then call can_accept()).
- The explicit "Verify environment" button (Settings tab) always calls
  `verify_environment(host, force=True)` — a real, synchronous, on-demand
  check, exactly like the pre-existing "Probe GPU"/"Test connection"
  buttons. That's what actually reports "No module named 'dissert'" (mclab)
  or the `timm.layers` error (local) to the user — not the gate above.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from . import framework
from . import transport as transport_mod
from .config import background_disabled, settings

_TTL_SECONDS = 300.0
_lock = threading.Lock()
_cache: Dict[Tuple, Tuple[float, Dict[str, Any]]] = {}


def check_command(python: str) -> str:
    """The profile's `commands.check` template, rendered for *python* (the
    host's own interpreter — a remote host's python_executable per D5, never
    this machine's)."""
    template = settings.commands.get("check") or "{python} %s --help" % settings.train_script
    return template.format(python=python, train_script=settings.train_script)


def _cache_key(host) -> Tuple:
    commit = framework.code_state().get("commit")
    return (host.id, str(host.repo_root), host.env_activate_cmd, host.python_executable, commit)


def _last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else "(no output captured)"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _run(host) -> Dict[str, Any]:
    """The real check, over *host*'s own transport — never called on the
    dispatch path directly, only from verify_environment() (explicit button,
    or the background prime), so it's fine for this to block its caller."""
    transport = transport_mod.for_host_record(host)
    check_cmd = check_command(host.python_executable)
    parts = ["cd %s" % transport_mod.remote_shell_path(host.repo_root)]
    if host.env_activate_cmd:
        parts.append(host.env_activate_cmd)
    parts.append(check_cmd)
    shell_cmd = " && ".join(parts)
    try:
        proc = transport.run(["bash", "-ic", shell_cmd], timeout=60)
    except transport_mod.TransportError as e:
        return {"ok": False, "checked_at": _now_iso(), "command": shell_cmd, "detail": str(e)}
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "checked_at": _now_iso(),
        "command": shell_cmd,
        "returncode": proc.returncode,
        "detail": None if ok else _last_line(proc.stderr or proc.stdout),
    }


def verify_environment(host, force: bool = False) -> Dict[str, Any]:
    """Cached per (host, repo_root, env_activate, python, code commit). A
    cache hit (force=False, not yet expired) costs nothing; a miss, or
    force=True (the explicit Settings-tab button), runs `_run()` for real
    and re-caches."""
    key = _cache_key(host)
    now = time.monotonic()
    if not force:
        with _lock:
            cached = _cache.get(key)
            if cached is not None and now - cached[0] < _TTL_SECONDS:
                return dict(cached[1])
    result = _run(host)
    with _lock:
        _cache[key] = (now, dict(result))
    return result


def cached_result(host) -> Optional[Dict[str, Any]]:
    """Whatever's cached for *host* right now, without running anything —
    what `capacity()`'s health `extra` and `gate_for_dispatch()` read. None
    for a cold or expired cache, never a fresh check."""
    key = _cache_key(host)
    with _lock:
        cached = _cache.get(key)
    if cached is None or time.monotonic() - cached[0] >= _TTL_SECONDS:
        return None
    return dict(cached[1])


def gate_for_dispatch(host) -> Optional[Dict[str, Any]]:
    """None if *host*'s environment isn't known to be broken —
    `MachineRunner.can_accept()`'s own call. See the module docstring for
    the cold-cache/async trade-off this makes."""
    cached = cached_result(host)
    if cached is None:
        if not background_disabled():
            threading.Thread(target=verify_environment, args=(host,), daemon=True).start()
        return None
    if not cached.get("ok"):
        return {"code": "env-broken", "detail": cached.get("detail") or "Environment check failed"}
    return None
