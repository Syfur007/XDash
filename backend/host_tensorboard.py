"""Per-host TensorBoard (XDASH_FIXES_PLAN.md F3.4) — extends the existing
single local instance (backend/tensorboard_manager.py) to also work on an
SSH host, or a live Colab VM (an ordinary SSH host with no special casing,
same as everywhere else in backend/transport.py — see its own module
docstring). Kaggle has no shell, so it never reaches this module at all:
server.py's routes only ever call it for a host_id that resolves through
backend/hosts.py, and a Kaggle account isn't one.

Local: unchanged, delegates straight to tensorboard_manager (already
resolves its binary through backend/tools.py).

Remote (SSH/Colab): starts `tensorboard` in its own tmux session on the
host, after this profile's env activation — the exact same "cd repo_root &&
env_activate && <command>" shape backend/terminals.py's own dispatch types
into a session (a person using tmux by hand would do the same thing) —
bound to the *host's own* 127.0.0.1 so it's never exposed beyond that
machine. A local port forward over the host's existing SSH ControlMaster
(backend/transport.py's SshTransport.open_forward/close_forward) is what
lets a browser here still reach it. The remote command is the bare
"tensorboard" name, resolved on the REMOTE's own PATH once the argv crosses
ssh — never through backend/tools.py's registry, which only ever resolves a
binary for THIS machine (same reasoning as backend/tmux_runner.py's own
_tmux_exe() comment).

The tmux session shares backend/monitors.py's own naming convention
(MONITOR_SESSION_INFIX, via its _session_name()) so it's never mistaken for
an unmanaged terminal or an experiment session
(backend/terminals.py's list_terminals() -> monitors.is_monitor_session()).

Local port allocation: one free ephemeral port per host (the OS picks it),
remembered in memory for the lifetime of that host's forward — so two hosts
open at once never collide on this machine even though every remote side
always uses the *same* fixed port (each remote's own 127.0.0.1 is a
separate network namespace; only the local side can collide).
"""
from __future__ import annotations

import os
import socket
import threading
from pathlib import PurePosixPath
from typing import Any, Dict, Optional

from . import hosts
from . import monitors
from . import tensorboard_manager
from . import tmux_runner as tmux
from . import transport as transport_mod
from .config import settings

TOOL_ID = "tensorboard"

_lock = threading.Lock()
# host_id -> {"local_port": int, "remote_port": int} — only ever set for a
# non-local host with an open forward; absent means "not forwarded" (either
# never started, or the process restarted since — see module docstring's
# own note in XDASH_FIXES_PLAN.md's build log about this not surviving a
# restart).
_forwards: Dict[str, Dict[str, int]] = {}


def _session_name(host_id: str) -> str:
    return monitors._session_name(TOOL_ID, host_id)


def _remote_logdir(host: "hosts._Host") -> str:
    """The host's own repo_root for the active profile, plus runs_dir's path
    *relative* to repo_root — the local absolute runs_dir means nothing on
    another machine."""
    rel = os.path.relpath(str(settings.runs_dir), str(settings.repo_root))
    return str(PurePosixPath(str(host.repo_root)) / rel)


def _free_local_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def status(host_id: Optional[str] = None) -> Dict[str, Any]:
    host = hosts.get_host(host_id)  # raises HostError on an unknown host
    if host.is_local:
        return tensorboard_manager.status()
    running = tmux.has_session(_session_name(host.id), host_id=host.id)
    fwd = _forwards.get(host.id)
    return {
        "running": running,
        "port": fwd["local_port"] if (running and fwd) else None,
        "logdir": _remote_logdir(host),
    }


def start(host_id: Optional[str] = None) -> Dict[str, Any]:
    host = hosts.get_host(host_id)
    if host.is_local:
        return tensorboard_manager.start()
    with _lock:
        session = _session_name(host.id)
        remote_port = settings.tensorboard_port
        if not tmux.has_session(session, host_id=host.id):
            tmux.new_session(session, host_id=host.id)
            tmux.send_keys(session, f"cd {host.repo_root}", host_id=host.id)
            if host.env_activate_cmd:
                tmux.send_keys(session, host.env_activate_cmd, host_id=host.id)
            tmux.send_keys(
                session,
                f"tensorboard --logdir {_remote_logdir(host)} --host 127.0.0.1 --port {remote_port}",
                host_id=host.id,
            )
        if host.id not in _forwards:
            transport = transport_mod.for_host_record(host)
            if not isinstance(transport, transport_mod.SshTransport):
                raise transport_mod.TransportError(
                    "host '%s' has no SSH transport to forward TensorBoard over" % host.id
                )
            local_port = _free_local_port()
            transport.open_forward(local_port, remote_port)
            _forwards[host.id] = {"local_port": local_port, "remote_port": remote_port}
        return status(host.id)


def stop(host_id: Optional[str] = None) -> Dict[str, Any]:
    host = hosts.get_host(host_id)
    if host.is_local:
        return tensorboard_manager.stop()
    with _lock:
        session = _session_name(host.id)
        if tmux.has_session(session, host_id=host.id):
            tmux.kill_session(session, host_id=host.id)
        fwd = _forwards.pop(host.id, None)
        if fwd:
            transport = transport_mod.for_host_record(host)
            if isinstance(transport, transport_mod.SshTransport):
                transport.close_forward(fwd["local_port"], fwd["remote_port"])
        return status(host.id)


def reset_for_tests() -> None:
    with _lock:
        _forwards.clear()
