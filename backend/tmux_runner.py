"""Runs experiments inside a tmux session instead of a raw subprocess.

The session is driven the same way a person would use it by hand: create a
detached session, `cd` into the repo, optionally activate an environment
(conda/venv), then type the train/eval command. A sentinel line is echoed
after the command so we can detect completion and exit code by reading the
pane's text — no special tmux plumbing needed beyond `capture-pane`.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
from typing import List, Optional

from .config import settings

DONE_MARKER = "__EXPDASH_DONE__"


class TmuxError(Exception):
    pass


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _run(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux"] + args, capture_output=True, text=True)


def has_session(session: str) -> bool:
    res = _run(["has-session", "-t", session])
    return res.returncode == 0


def new_session(session: str):
    if not tmux_available():
        raise TmuxError("'tmux' was not found on PATH. Install it (e.g. `sudo apt install tmux`) to run experiments.")
    res = _run([
        "new-session", "-d", "-s", session,
        "-x", str(settings.tmux_pane_width),
        "-y", str(settings.tmux_pane_height),
    ])
    if res.returncode != 0:
        raise TmuxError(f"Failed to create tmux session '{session}': {res.stderr.strip()}")
    _run(["set-option", "-t", session, "history-limit", str(settings.tmux_history_limit)])
    # Lock the window to this exact size. Without this, tmux's default
    # window-size policy ("latest") silently shrinks the pane to match
    # whatever client last attached to it (e.g. if you `tmux attach` from an
    # ordinary 80-100 column terminal to peek at it) — and it *stays* that
    # size afterwards, which looks exactly like a mysterious fixed per-line
    # character limit in the dashboard's captured output.
    _run(["set-window-option", "-t", session, "window-size", "manual"])
    _run(["resize-window", "-t", session, "-x", str(settings.tmux_pane_width), "-y", str(settings.tmux_pane_height)])
    _wait_for_shell_ready(session)


def _wait_for_shell_ready(session: str, timeout: float = 5.0):
    """Cold tmux servers (no prior sessions) take a moment to actually spawn
    the pane's shell. Sending keys before that happens silently drops them,
    so poll until the pane has rendered *something* (a prompt) first.
    """
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = capture_pane(session)
        if text and text.strip():
            return
        time.sleep(0.1)
    # proceed anyway; worst case the first keystroke is dropped and the
    # dashboard will just see an empty/short run.


def send_keys(session: str, command: str):
    res = _run(["send-keys", "-t", session, command, "Enter"])
    if res.returncode != 0:
        raise TmuxError(f"Failed to send command to tmux session '{session}': {res.stderr.strip()}")


def send_ctrl_c(session: str):
    _run(["send-keys", "-t", session, "C-c"])


def capture_pane(session: str) -> Optional[str]:
    """Full visible history of the pane, or None if the session is gone."""
    res = _run(["capture-pane", "-p", "-t", session, "-S", "-", "-E", "-"])
    if res.returncode != 0:
        return None
    return res.stdout


def capture_pane_tail(session: str, lines: int = 200) -> Optional[str]:
    """Cheaper capture for status checks — just the last `lines` lines."""
    res = _run(["capture-pane", "-p", "-t", session, "-S", f"-{lines}"])
    if res.returncode != 0:
        return None
    return res.stdout


def list_sessions() -> List[str]:
    res = _run(["list-sessions", "-F", "#{session_name}"])
    if res.returncode != 0:
        return []
    return [line for line in res.stdout.splitlines() if line.strip()]


def kill_session(session: str):
    _run(["kill-session", "-t", session])


def build_launch_command(python_exe: str, script: str, config_path: str, extra_flags: List[str], extra_args: str) -> str:
    parts = [python_exe, script, "--config", config_path] + list(extra_flags)
    if extra_args.strip():
        parts += shlex.split(extra_args)
    return " ".join(shlex.quote(p) for p in parts)
