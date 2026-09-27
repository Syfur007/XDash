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
from typing import Any, Dict, List, Optional, Sequence

from . import hosts
from .config import DATA_DIR

# Multiplexing is not an optimization here, it is a correctness requirement:
# a second concurrent `colab ssh` returns HTTP 429, so every connection to a
# given host must share one master. ControlPersist keeps it warm between the
# poll loop's frequent short commands.
_CONTROL_DIR = DATA_DIR / ".ssh-control"
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

    def push(self, local_dir, remote_dir, excludes: Sequence[str] = (), delete: bool = False) -> None:
        raise NotImplementedError

    def pull(self, remote_dir, local_dir, excludes: Sequence[str] = ()) -> None:
        raise NotImplementedError

    def pull_file(self, remote_path, local_path) -> None:
        raise NotImplementedError

    def put_text(self, remote_path, text: str, mode: int = 0o600) -> None:
        """Writes *text* to *remote_path* over stdin — never through an
        argument list (DATASETS_PLAN.md §4.6: a Kaggle credentials file
        must never be visible to `ps` on a shared box). *mode* is applied
        with chmod right after the write."""
        raise NotImplementedError

    def exists(self, remote_path, kind: str = "d") -> bool:
        """Is there a directory (*kind* "d") or file ("f") at *remote_path*?
        Raises TransportError when the host can't be asked — "can't tell" must
        never read as "not there", or a collection would record a real run as
        empty."""
        raise NotImplementedError

    def available(self) -> bool:
        raise NotImplementedError


def remote_shell_path(path) -> str:
    """*path* quoted for a remote POSIX shell, keeping a leading `~/`
    unquoted so the remote shell still expands it (a Colab host's repo_root
    is `~/xdash-repo`)."""
    text = str(path)
    if text == "~":
        return "~"
    if text.startswith("~/"):
        return "~/" + shlex.quote(text[2:])
    return shlex.quote(text)


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

    def push(self, local_dir, remote_dir, excludes: Sequence[str] = (), delete: bool = False) -> None:
        return None

    def pull(self, remote_dir, local_dir, excludes: Sequence[str] = ()) -> None:
        return None

    def pull_file(self, remote_path, local_path) -> None:
        return None

    def put_text(self, remote_path, text: str, mode: int = 0o600) -> None:
        from .store import atomic_write_text
        atomic_write_text(Path(remote_path).expanduser(), text, mode=mode)

    def exists(self, remote_path, kind: str = "d") -> bool:
        p = Path(remote_path).expanduser()
        return p.is_dir() if kind == "d" else p.is_file()

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
        # Per-record `-o Key=Value` options go FIRST: ssh keeps the first
        # value it sees for each option, so anything after the base options
        # couldn't override them. A Colab VM needs StrictHostKeyChecking=no +
        # UserKnownHostsFile=/dev/null: every `colab new` is a fresh VM with a
        # fresh host key behind the same alias, which the base accept-new
        # would refuse as a changed identity.
        opts: List[str] = []
        for opt in ssh.get("options") or []:
            opts += ["-o", str(opt)]
        opts += list(_SSH_BASE_OPTS)
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

    def push(self, local_dir, remote_dir, excludes: Sequence[str] = (), delete: bool = False, timeout: float = 900.0) -> None:
        """Trailing slashes are load-bearing: 'src/' means *contents of src*,
        so the tree lands at remote_dir rather than remote_dir/src.
        *delete* (DATASETS_PLAN.md §4.4, fixes DS9) makes the remote side an
        exact mirror of *local_dir* — only dataset staging into the host
        cache passes True; the working-tree push never does (a stale file
        the remote created itself, e.g. a checkpoint, must survive it)."""
        self._rsync(
            "%s/" % str(local_dir).rstrip("/"),
            "%s:%s/" % (self._target(), str(remote_dir).rstrip("/")),
            excludes, delete=delete, timeout=timeout,
        )

    def put_text(self, remote_path, text: str, mode: int = 0o600, timeout: float = 60.0) -> None:
        """Writes *text* to *remote_path* over the ssh command's stdin, never
        an argument (DATASETS_PLAN.md §4.6) — a Kaggle credentials file on a
        Colab VM. `install -m` creates the file (and any parent dir) with
        the right mode atomically enough for a single-tenant VM; the plan's
        `trap ... rm -f` deletion happens in the caller's own command, not
        here."""
        remote = remote_shell_path(remote_path)
        cmd = "mkdir -p $(dirname %s) && umask 077 && cat > %s && chmod %o %s" % (remote, remote, mode, remote)
        argv = ["ssh"] + self._opts() + [self._target(), cmd]
        try:
            proc = subprocess.run(argv, input=text, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise TransportError("ssh not found")
        except subprocess.TimeoutExpired:
            raise TransportError("put_text to %s timed out after %ss" % (self.host_id, timeout))
        if proc.returncode != 0:
            raise TransportError(
                "put_text failed for '%s' (exit %s): %s"
                % (self.host_id, proc.returncode, (proc.stderr or proc.stdout).strip()[-300:])
            )

    def pull(self, remote_dir, local_dir, excludes: Sequence[str] = (), timeout: float = 900.0) -> None:
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        self._rsync(
            "%s:%s/" % (self._target(), str(remote_dir).rstrip("/")),
            "%s/" % str(local_dir).rstrip("/"),
            excludes, delete=False, timeout=timeout,
        )

    def pull_file(self, remote_path, local_path, timeout: float = 300.0) -> None:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        self._rsync("%s:%s" % (self._target(), str(remote_path)), str(local_path), (), delete=False, timeout=timeout)

    def exists(self, remote_path, kind: str = "d") -> bool:
        flag = "-d" if kind == "d" else "-f"
        proc = self.run(["sh", "-c", "test %s %s" % (flag, remote_shell_path(remote_path))], timeout=60)
        if proc.returncode in (0, 1):
            return proc.returncode == 0
        raise TransportError(
            "Could not check %s on '%s' (exit %s): %s"
            % (remote_path, self.host_id, proc.returncode, (proc.stderr or proc.stdout).strip()[-300:])
        )

    def available(self) -> bool:
        try:
            return self.run(["true"], timeout=15).returncode == 0
        except TransportError:
            return False


class _AdHocSshHost:
    """Just enough of hosts._Host's surface (`id`, `is_local`, `ssh`) for
    SshTransport, for a host that doesn't exist as a saved record yet —
    the Add-runtime wizard's live test (XDASH_PLAN.md §8.4), which must be
    able to fail *before* anything is persisted."""

    is_local = False

    def __init__(self, ssh: Dict[str, Any]):
        self.id = "test"
        self._ssh = dict(ssh or {})

    @property
    def ssh(self) -> Dict[str, Any]:
        return self._ssh


def test_ssh_connection(ssh: Dict[str, Any]) -> Dict[str, Any]:
    """Ad-hoc reachability check for a not-yet-saved SSH config — the
    Add-runtime wizard's live test gate. A real `ssh true` over the network
    when actually called; tests patch `SshTransport.run`/`subprocess.run`,
    never point this at a real host."""
    if not (ssh or {}).get("host"):
        return {"ok": False, "detail": "ssh.host is required"}
    t = SshTransport(_AdHocSshHost(ssh))
    reachable = t.available()
    return {
        "ok": reachable,
        "detail": "reachable" if reachable else "ssh failed — check host/user/port/identity_file",
    }


def for_host_record(host) -> Transport:
    """The transport for an already-resolved host object, without a second
    hosts.get_host() lookup — what a ColabRunner needs (Multi_runner_XDash.md
    Phase 4) before its VM has a persisted host record to be looked up by."""
    return LocalTransport(host.id) if host.is_local else SshTransport(host)


def for_host(host_id: Optional[str] = None) -> Transport:
    """The transport for *host_id* (None/"local" -> LocalTransport)."""
    return for_host_record(hosts.get_host(host_id))
