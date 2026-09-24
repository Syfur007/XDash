"""How a command reaches a machine, and how files get to and from it.

**This is the only place in XDash that branches on local-vs-remote.**
Everything above it — launching an experiment, capturing a pane, stopping a
run, seeding a resume, collecting results, machine stats — is written once
against this interface and works on any host.

Deliberately OpenSSH-over-subprocess rather than paramiko: requirements.txt
is four packages and its header is a hard rule against growing that, and the
codebase already drives `tmux` and `kaggle` exactly this way. It also means
ssh_config, agent forwarding, ControlMaster and ProxyCommand all work
untouched — which is what lets a Colab VM (reached via
`colab ssh --proxy-mode`) be an ordinary SSH host with no special casing.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence

from . import hosts

# Multiplexing is not an optimization here, it is a correctness requirement:
# a second concurrent `colab ssh` returns HTTP 429, so every connection to a
# given host must share one master. ControlPersist keeps it warm between the
# poll loop's frequent short commands.
_CONTROL_DIR = hosts.DASHBOARD_DIR / "data" / ".ssh-control"
_SSH_BASE_OPTS = [
    "-o", "ControlMaster=auto",
    "-o", "ControlPersist=10m",
    "-o", "BatchMode=yes",           # never prompt; a missing key must fail fast
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
]

# Never synced to a remote checkout: outputs are pulled back deliberately
# (and --delete would destroy the remote's own in-flight run), .git is large
# and useless there, and data/ is the training corpus which the remote
# either already has or fetches itself.
DEFAULT_PUSH_EXCLUDES = (
    "outputs/", ".git/", "data/", "__pycache__/", "*.pyc",
    "*.pth", "*.pt", "*.ckpt", ".venv/", "node_modules/",
)


class TransportError(Exception):
    """The transport itself failed — host unreachable, rsync missing, a
    refused connection. Distinct from the command running and exiting
    non-zero, which is an ordinary CompletedProcess."""


class Transport:
    """Run a command on, and move files to/from, one machine."""

    host_id: str

    def argv(self, argv: Sequence[str]) -> List[str]:
        raise NotImplementedError

    def run(self, argv: Sequence[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess:
        raise NotImplementedError

    def push(self, local_dir, remote_dir, excludes: Sequence[str] = ()) -> None:
        raise NotImplementedError

    def pull(self, remote_dir, local_dir, excludes: Sequence[str] = ()) -> None:
        raise NotImplementedError

    def available(self) -> bool:
        raise NotImplementedError


class LocalTransport(Transport):
    """Runs argv as-is. push/pull are no-ops because source and destination
    are the same filesystem — which is exactly why the local machine needs no
    runner class of its own."""

    def __init__(self, host_id: str = hosts.LOCAL_HOST_ID):
        self.host_id = host_id

    def argv(self, argv: Sequence[str]) -> List[str]:
        return list(argv)

    def run(self, argv: Sequence[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            # Same shape every caller already handles for a missing binary —
            # see tmux_runner._run's own 127 comment.
            return subprocess.CompletedProcess(
                args=list(argv), returncode=127, stdout="", stderr="%s not found" % (argv[0] if argv else "command"),
            )
        except subprocess.TimeoutExpired:
            raise TransportError("Command timed out after %ss: %s" % (timeout, " ".join(argv)))

    def push(self, local_dir, remote_dir, excludes: Sequence[str] = ()) -> None:
        return None

    def pull(self, remote_dir, local_dir, excludes: Sequence[str] = ()) -> None:
        return None

    def available(self) -> bool:
        return True


class SshTransport(Transport):
    """OpenSSH + rsync against one host record."""

    def __init__(self, host):
        self.host_id = host.id
        self._host = host

    # -- connection ------------------------------------------------------
    def _target(self) -> str:
        ssh = self._host.ssh
        user = (ssh.get("user") or "").strip()
        return "%s@%s" % (user, ssh["host"]) if user else ssh["host"]

    def _opts(self) -> List[str]:
        ssh = self._host.ssh
        _CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        opts = list(_SSH_BASE_OPTS)
        opts += ["-o", "ControlPath=%s/%%r@%%h-%%p" % _CONTROL_DIR]
        if ssh.get("port"):
            opts += ["-p", str(ssh["port"])]
        if ssh.get("identity_file"):
            opts += ["-i", str(Path(ssh["identity_file"]).expanduser())]
        if ssh.get("proxy_command"):
            # Colab's `colab ssh --proxy-mode` lands here, which is the whole
            # reason a Colab VM needs no transport of its own.
            opts += ["-o", "ProxyCommand=%s" % ssh["proxy_command"]]
        return opts

    def argv(self, argv: Sequence[str]) -> List[str]:
        """ssh joins its trailing arguments with spaces and hands the result
        to the REMOTE login shell, which expands it a second time. Quoting
        each element and passing ONE string is what stops a config path with
        a space — or an extra_arg containing quotes — from being re-split
        there. Never interpolate into this string.

        `shlex.join` would be the obvious call; requirements.txt targets 3.8
        (the server runs under python3.8), where it does not exist.
        """
        remote_cmd = " ".join(shlex.quote(a) for a in argv)
        return ["ssh"] + self._opts() + [self._target(), remote_cmd]

    def run(self, argv: Sequence[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(self.argv(argv), capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return subprocess.CompletedProcess(args=list(argv), returncode=127, stdout="", stderr="ssh not found")
        except subprocess.TimeoutExpired:
            raise TransportError("ssh to %s timed out after %ss" % (self.host_id, timeout))

    # -- files -----------------------------------------------------------
    def _rsh(self) -> str:
        return " ".join(["ssh"] + [shlex.quote(o) for o in self._opts()])

    def _rsync(self, src: str, dst: str, excludes: Sequence[str], delete: bool, timeout: float):
        argv = ["rsync", "-az", "--partial", "-e", self._rsh()]
        if delete:
            argv.append("--delete")
        for pattern in excludes:
            argv += ["--exclude", pattern]
        argv += [src, dst]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise TransportError("rsync not found on PATH — required to drive host '%s'" % self.host_id)
        except subprocess.TimeoutExpired:
            raise TransportError("rsync to/from %s timed out after %ss" % (self.host_id, timeout))
        if proc.returncode != 0:
            raise TransportError("rsync failed for '%s': %s" % (self.host_id, (proc.stderr or proc.stdout).strip()[-500:]))
        return proc

    def push(self, local_dir, remote_dir, excludes: Sequence[str] = (), timeout: float = 900.0) -> None:
        """Trailing slashes are load-bearing: 'src/' means *contents of src*,
        so the tree lands at remote_dir rather than remote_dir/src."""
        self._rsync(
            "%s/" % str(local_dir).rstrip("/"),
            "%s:%s/" % (self._target(), str(remote_dir).rstrip("/")),
            excludes, delete=False, timeout=timeout,
        )

    def pull(self, remote_dir, local_dir, excludes: Sequence[str] = (), timeout: float = 900.0) -> None:
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        self._rsync(
            "%s:%s/" % (self._target(), str(remote_dir).rstrip("/")),
            "%s/" % str(local_dir).rstrip("/"),
            excludes, delete=False, timeout=timeout,
        )

    def available(self) -> bool:
        try:
            return self.run(["true"], timeout=15).returncode == 0
        except TransportError:
            return False


def for_host_record(host) -> Transport:
    """The transport for an already-resolved host object, without a second
    hosts.get_host() lookup — what a ColabRunner needs (Multi_runner_XDash.md
    Phase 4) before its VM has a persisted host record to be looked up by."""
    return LocalTransport(host.id) if host.is_local else SshTransport(host)


def for_host(host_id: Optional[str] = None) -> Transport:
    """The transport for *host_id* (None/"local" -> LocalTransport)."""
    return for_host_record(hosts.get_host(host_id))
