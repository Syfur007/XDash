"""Launches (and reuses) a single TensorBoard process for the runs/ directory.

Kept intentionally minimal: one shared TensorBoard instance, started on first
request, embedded via iframe by the frontend. TensorBoard already knows how
to multiplex multiple run subdirectories when pointed at their parent, so we
never need to spawn more than one instance.
"""
from __future__ import annotations

import subprocess
import threading
import time
from typing import Optional

from .config import settings

_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None


def is_running() -> bool:
    return _proc is not None and _proc.poll() is None


class TensorboardLaunchError(Exception):
    pass


def start() -> dict:
    global _proc
    with _lock:
        if is_running():
            return status()
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        try:
            _proc = subprocess.Popen(
                [
                    "tensorboard",
                    "--logdir", str(settings.runs_dir),
                    "--host", settings.tensorboard_host,
                    "--port", str(settings.tensorboard_port),
                ],
                cwd=str(settings.repo_root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            _proc = None
            raise TensorboardLaunchError(
                "'tensorboard' executable not found. Install it with "
                "`pip install tensorboard` in the environment running server.py."
            )
        time.sleep(1.5)  # give it a moment to bind before the frontend polls
        if _proc.poll() is not None:
            # Process already exited (bad port, bad logdir, etc.)
            code = _proc.returncode
            _proc = None
            raise TensorboardLaunchError(f"tensorboard exited immediately (code {code}). Check the port isn't already in use.")
        return status()


def stop() -> dict:
    global _proc
    with _lock:
        if _proc is not None:
            _proc.terminate()
            try:
                _proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _proc.kill()
            _proc = None
        return status()


def status() -> dict:
    return {
        "running": is_running(),
        "port": settings.tensorboard_port,
        "logdir": str(settings.runs_dir),
    }
