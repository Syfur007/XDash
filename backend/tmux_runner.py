"""Runs experiments inside a tmux session instead of a raw subprocess.

The session is driven the same way a person would use it by hand: create a
detached session, `cd` into the repo, optionally activate an environment
(conda/venv), then type the train/eval command. A sentinel line is echoed
after the command so we can detect completion and exit code by reading the
pane's text — no special tmux plumbing needed beyond `capture-pane`.

**Every tmux call in XDash goes through `_run()`**, which is why remoting a
host costs almost nothing: `_run` asks backend/transport.py for that host's
Transport and the same argv either executes here or over ssh. Experiment
launching, live log capture, stop/kill and Machine Stats all become
host-aware at once, and no caller above this file has to know which it is.

`host_id` defaults to "local" on every function, so a call site that has not
been taught about hosts keeps working unchanged.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
from typing import List, Optional

from . import hosts
from . import transport as transport_mod

DONE_MARKER = "__EXPDASH_DONE__"


class TmuxError(Exception):
    pass


def tmux_available(host_id: Optional[str] = None) -> bool:
    """Is tmux usable on *host_id*? Local answers from PATH directly (cheap,
    no subprocess); a remote has to be asked, and an unreachable host is
    simply 'no tmux here' — every caller already degrades gracefully on that."""
    host = hosts.get_host(host_id)
    if host.is_local:
        return shutil.which("tmux") is not None
    return _run(["-V"], host_id=host.id).returncode == 0


def _run(args: List[str], host_id: Optional[str] = None) -> subprocess.CompletedProcess:
    """`tmux <args>` on *host_id*.

    A missing binary, an unreachable host, or a timed-out connection all come
    back as returncode 127 rather than raising, because every caller in this
    module already treats a non-zero returncode as "no session"/"nothing to
    report" (has_session -> False, list_sessions -> [], capture_pane* ->
    None). That means a read-only status route degrades cleanly on a machine
    without tmux installed *and* on a lab box that is currently powered off,
    instead of a raw 500 on first page load.
    """
    try:
        return transport_mod.for_host(host_id).run(["tmux"] + args)
    except (transport_mod.TransportError, hosts.HostError) as e:
        return subprocess.CompletedProcess(args=["tmux"] + args, returncode=127, stdout="", stderr=str(e))


def has_session(session: str, host_id: Optional[str] = None) -> bool:
    return _run(["has-session", "-t", session], host_id=host_id).returncode == 0


def new_session(session: str, host_id: Optional[str] = None):
    host = hosts.get_host(host_id)
    if not tmux_available(host.id):
        raise TmuxError(
            "'tmux' was not found on %s. Install it (e.g. `sudo apt install tmux`) to run experiments."
            % ("PATH" if host.is_local else "host '%s' (or the host is unreachable)" % host.id)
        )
    res = _run([
        "new-session", "-d", "-s", session,
        "-x", str(host.tmux_pane_width),
        "-y", str(host.tmux_pane_height),
    ], host_id=host.id)
    if res.returncode != 0:
        raise TmuxError(f"Failed to create tmux session '{session}': {res.stderr.strip()}")
    _run(["set-option", "-t", session, "history-limit", str(host.tmux_history_limit)], host_id=host.id)
    # Lock the window to this exact size. Without this, tmux's default
    # window-size policy ("latest") silently shrinks the pane to match
    # whatever client last attached to it (e.g. if you `tmux attach` from an
    # ordinary 80-100 column terminal to peek at it) — and it *stays* that
    # size afterwards, which looks exactly like a mysterious fixed per-line
    # character limit in the dashboard's captured output.
    _run(["set-window-option", "-t", session, "window-size", "manual"], host_id=host.id)
    _run(["resize-window", "-t", session, "-x", str(host.tmux_pane_width), "-y", str(host.tmux_pane_height)],
         host_id=host.id)
    _wait_for_shell_ready(session, host_id=host.id)


def _wait_for_shell_ready(session: str, timeout: float = 5.0, host_id: Optional[str] = None):
    """Cold tmux servers (no prior sessions) take a moment to actually spawn
    the pane's shell. Sending keys before that happens silently drops them,
    so poll until the pane has rendered *something* (a prompt) first.
    """
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = capture_pane(session, host_id=host_id)
        if text and text.strip():
            return
        time.sleep(0.1)
    # proceed anyway; worst case the first keystroke is dropped and the
    # dashboard will just see an empty/short run.


def send_keys(session: str, command: str, host_id: Optional[str] = None):
    res = _run(["send-keys", "-t", session, command, "Enter"], host_id=host_id)
    if res.returncode != 0:
        raise TmuxError(f"Failed to send command to tmux session '{session}': {res.stderr.strip()}")


def send_ctrl_c(session: str, host_id: Optional[str] = None):
    _run(["send-keys", "-t", session, "C-c"], host_id=host_id)


def capture_pane(session: str, host_id: Optional[str] = None) -> Optional[str]:
    """Full visible history of the pane, or None if the session is gone."""
    res = _run(["capture-pane", "-p", "-t", session, "-S", "-", "-E", "-"], host_id=host_id)
    if res.returncode != 0:
        return None
    return res.stdout


def capture_pane_tail(session: str, lines: int = 200, host_id: Optional[str] = None) -> Optional[str]:
    """Cheaper capture for status checks — just the last `lines` lines."""
    res = _run(["capture-pane", "-p", "-t", session, "-S", f"-{lines}"], host_id=host_id)
    if res.returncode != 0:
        return None
    return res.stdout


def list_sessions(host_id: Optional[str] = None) -> List[str]:
    res = _run(["list-sessions", "-F", "#{session_name}"], host_id=host_id)
    if res.returncode != 0:
        return []
    return [line for line in res.stdout.splitlines() if line.strip()]


def kill_session(session: str, host_id: Optional[str] = None):
    _run(["kill-session", "-t", session], host_id=host_id)


def build_launch_command(python_exe: str, script: str, config_path: str, extra_flags: List[str], extra_args: str) -> str:
    parts = [python_exe, script, "--config", config_path] + list(extra_flags)
    if extra_args.strip():
        parts += shlex.split(extra_args)
    return " ".join(shlex.quote(p) for p in parts)
