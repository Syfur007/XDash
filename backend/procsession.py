"""A generic, named, line-buffered interactive subprocess session.

XDASH_PLAN.md §8.4/§10 Phase 5 needs two unrelated features that are really
the same shape: **Colab's copy-paste OAuth** (start `colab ...`, show the
printed sign-in URL, accept a pasted code back on stdin) and **Kaggle's live
`kernels logs -f`** (start it, stream lines, stop it on request). Both are
"start a subprocess, read its stdout in the background so a slow/blocked
child never stalls the request thread, let the caller poll a snapshot, and
optionally write to stdin" — one primitive, used twice, rather than two
half-copies of the same threading dance.

A session is a real, running subprocess: starting one for Colab's OAuth
subcommand genuinely opens a browser-facing Google login the moment it
actually runs, and starting one for `kaggle kernels logs -f` genuinely calls
Kaggle's API. Nothing here is what makes that happen — `backend/colab.py`/
`backend/kaggle.py` decide *what* argv to run — but every test in this
codebase must point that argv at a harmless local stand-in (a small Python
script), never at the real `colab`/`kaggle` binaries, exactly like every
other outward-facing call this test suite refuses by default (see
tests/conftest.py's module docstring).
"""
from __future__ import annotations

import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

_sessions: Dict[str, "LineSession"] = {}
_registry_lock = threading.Lock()


class LineSession:
    """One subprocess, read line-by-line on a background thread so a caller
    never blocks on a child that's waiting for input (or for a slow network
    call) — only `snapshot()`/`send()` are ever called from the request
    thread."""

    def __init__(self, argv: Sequence[str], env: Optional[Dict[str, str]] = None):
        self.argv = list(argv)
        self._lines: List[str] = []
        self._lock = threading.Lock()
        self._last_line_at = time.time()
        self.started_at = time.time()
        self.error: Optional[str] = None
        self.proc: Optional[subprocess.Popen] = None
        try:
            self.proc = subprocess.Popen(
                self.argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except (OSError, ValueError) as e:
            self.error = str(e)
            return
        thread = threading.Thread(target=self._read_loop, daemon=True)
        thread.start()

    def _read_loop(self) -> None:
        try:
            for line in self.proc.stdout:  # type: ignore[union-attr]
                with self._lock:
                    self._lines.append(line.rstrip("\n"))
                    self._last_line_at = time.time()
        except (OSError, ValueError):
            pass

    def send(self, text: str) -> bool:
        """Writes *text* (a newline is appended) to the child's stdin — the
        pasted OAuth code, in Colab's case. False if there's no live process
        to write to."""
        if self.proc is None or self.proc.stdin is None or self.proc.poll() is not None:
            return False
        try:
            self.proc.stdin.write(text if text.endswith("\n") else text + "\n")
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def poll(self) -> Optional[int]:
        return self.proc.poll() if self.proc is not None else -1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            lines = list(self._lines)
            idle_seconds = time.time() - self._last_line_at
        return {
            "lines": lines,
            "output": "\n".join(lines),
            "idle_seconds": idle_seconds,
            "returncode": self.poll(),
            "done": self.poll() is not None,
            "error": self.error,
        }

    def terminate(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass


def start(session_id: str, argv: Sequence[str], env: Optional[Dict[str, str]] = None) -> LineSession:
    """Starts (or restarts, if *session_id* is already running) a session.
    Replaces, never leaks, a still-live prior session under the same id."""
    with _registry_lock:
        existing = _sessions.get(session_id)
        if existing is not None:
            existing.terminate()
        session = LineSession(argv, env=env)
        _sessions[session_id] = session
        return session


def get(session_id: str) -> Optional[LineSession]:
    with _registry_lock:
        return _sessions.get(session_id)


def stop(session_id: str) -> bool:
    with _registry_lock:
        session = _sessions.pop(session_id, None)
    if session is None:
        return False
    session.terminate()
    return True
