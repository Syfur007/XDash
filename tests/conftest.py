"""XDash's permanent test harness (XDASH_PLAN.md Phase 0 item 9).

Every test runs against a throwaway world, built once per session here
*before* any `backend` module is imported:

- XDASH_DATA_DIR  -> <tmp>/data           (never the real data/)
- XDASH_REPOS_DIR -> <tmp>/repos/fake.yaml (a profile for the fake host repo)
- <tmp>/host      -> a fake host repo: a few configs, a `fakefw.hooks`
  locate_run hook, and orchestration/status.py (a read-only *copy* of
  dissert's, so classification runs real dissert logic; a stub otherwise)
- XDASH_DISABLE_BACKGROUND=1: no scheduler/dispatcher threads

and, per test, the data dir and the fake repo's outputs/ are wiped, module
caches reset, and every outward-facing call (tmux, ssh/rsync, kaggle, colab)
is replaced by a fake that refuses. A test that wants a runtime to answer
installs its own fake (FakeRunner, FakeTransport, a scripted colab/kaggle
subprocess).

Tests that need dissert's own interpreter (the `thesis` env) to run real
dissert code read-only are marked `needs_thesis` and skip when it's absent.
They run on dissert's **committed HEAD**, extracted once per session with
`git archive` (the `dissert_head` fixture), never on its working tree: that
reads only the object store, and a dissert mid-edit (Phase 1 ran while its
src/ reorganization was half-staged) can't make XDash's suite flaky. When
dissert commits a change XDash depends on, these tests fail on purpose.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

XDASH_ROOT = Path(__file__).resolve().parent.parent
REAL_DATA_DIR = XDASH_ROOT / "data"
DISSERT_ROOT = (XDASH_ROOT / ".." / "dissert").resolve()
THESIS_PYTHON = Path(os.environ.get("XDASH_TEST_THESIS_PYTHON", "/home/syfur/miniconda3/envs/thesis/bin/python"))

BASE = Path(tempfile.mkdtemp(prefix="xdash-tests-"))
DATA = BASE / "data"
REPOS = BASE / "repos"
HOST = BASE / "host"

FAKE_HOOKS = '''\
"""Pure-Python hooks for the fake host repo, same contracts as dissert's:
load_config (compose includes resolved relative to the including file,
deep-merged, then a strict key check like dissert's schema) and locate_run
(on the loaded config). No dependencies beyond PyYAML."""
import hashlib, json, os, yaml

KNOWN = {"logging": {"experiment_name"}, "training": {"epochs", "lr", "seed"}, "dataset": {"name", "root"}}
CALLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.log")


def _merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    return out


def _load(path):
    path = os.path.abspath(path)
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    merged = {}
    for inc in raw.pop("compose", None) or []:
        merged = _merge(merged, _load(inc if os.path.isabs(inc) else os.path.join(os.path.dirname(path), inc)))
    return _merge(merged, raw)


def load_config(config_path):
    cfg = _load(config_path)
    for section, value in cfg.items():
        if section not in KNOWN:
            raise ValueError("Extra inputs are not permitted: %s" % section)
        for key in (value or {}):
            if key not in KNOWN[section]:
                raise ValueError("Extra inputs are not permitted: %s.%s" % (section, key))
    return cfg


def locate_run(config, seed, repeat=None):
    with open(CALLS, "a") as f:
        f.write(json.dumps({"hook": "locate_run", "config": config, "seed": seed}) + "\\n")
    raw = _load(config)
    name = (raw.get("logging") or {}).get("experiment_name") or "experiment"
    body = {k: v for k, v in raw.items() if k != "logging"}
    h = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return {
        "config_hash": h, "experiment_name": name,
        "run_dir": os.path.join("outputs/experiments", name, "%s-s%s" % (h[:7], int(seed))),
        "run_ids": ["R-%s-s%s-f-" % (h[:7], int(seed))],
    }
'''

STATUS_STUB = '''\
import glob, json, os

def describe_run(experiment_dir, verify=False):
    run_dir = os.path.abspath(experiment_dir)
    folds = []
    for d in sorted(glob.glob(os.path.join(run_dir, "checkpoints"))):
        m = json.load(open(os.path.join(d, "manifest.json")))
        folds.append(m)
    m = folds[0] if folds else {}
    return {"run_id": m.get("run_id"), "status": m.get("status", "pending"),
            "resumable": os.path.isfile(os.path.join(run_dir, "checkpoints", "last.pth")),
            "epochs_completed": m.get("epochs_completed", 0), "total_epochs": m.get("total_epochs"),
            "checkpoint_files": [os.path.relpath(os.path.join(run_dir, "checkpoints", "last.pth"))]}
'''


def _git_dissert(*args: str) -> subprocess.CompletedProcess:
    # Read-only plumbing (show / cat-file / archive): no index refresh, no lock.
    return subprocess.run(["git", "--no-optional-locks", "-C", str(DISSERT_ROOT), *args], capture_output=True)


def dissert_head_text(rel: str) -> Optional[str]:
    """*rel* as committed at dissert's HEAD, or None."""
    try:
        proc = _git_dissert("show", "HEAD:" + rel)
    except OSError:
        return None
    return proc.stdout.decode("utf-8") if proc.returncode == 0 else None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _build_world() -> None:
    _write(HOST / "configs" / "experiment" / "demo.yaml", textwrap.dedent("""\
        compose: ["../dataset/demo.yaml"]
        logging: {experiment_name: demo_exp}
        training: {epochs: 3}
        """))
    _write(HOST / "configs" / "experiment" / "other.yaml", textwrap.dedent("""\
        logging: {experiment_name: other_exp}
        training: {epochs: 5}
        dataset: {name: demo}
        """))
    _write(HOST / "configs" / "experiment" / "third.yaml", textwrap.dedent("""\
        compose: ["../dataset/demo.yaml"]
        logging: {experiment_name: third_exp}
        training: {epochs: 7, lr: 0.01}
        """))
    _write(HOST / "configs" / "dataset" / "demo.yaml", "dataset: {name: demo, root: data/demo}\n")
    _write(HOST / "fakefw" / "__init__.py", "")
    _write(HOST / "fakefw" / "hooks.py", FAKE_HOOKS)
    _write(HOST / "orchestration" / "__init__.py", "")
    _write(HOST / "orchestration" / "status.py", dissert_head_text("orchestration/status.py") or STATUS_STUB)
    _write(REPOS / "fake.yaml", textwrap.dedent("""\
        display_name: fake
        repo_root: "%s"
        configs_dir: configs
        logs_dir: outputs/experiments
        runs_dir: outputs/experiments
        checkpoints_dir: outputs/experiments
        plots_dir: outputs/experiments
        reports_dir: outputs/experiments
        ledger_dir: outputs/ledger
        manifest_layout: experiments
        python_executable: python
        bridge_python_executable: "%s"
        train_script: train.py
        eval_script: eval.py
        seed_arg: "--seeds {seed} --repeats 1"
        hooks:
          locate_run: "fakefw.hooks:locate_run"
          resolve_config: "fakefw.hooks:load_config"
        overlay_compose_key: compose
        eval_default_args: ["--allow-test-eval"]
        kaggle_dataset_map:
          demo: "someone/demo-ds"
        colab_default_gpu: T4
        """) % (HOST, sys.executable))


_build_world()
os.environ["XDASH_DATA_DIR"] = str(DATA)
os.environ["XDASH_REPOS_DIR"] = str(REPOS)
os.environ["XDASH_DISABLE_BACKGROUND"] = "1"
sys.path.insert(0, str(XDASH_ROOT))

from backend import config as config_mod  # noqa: E402

assert config_mod.DATA_DIR == DATA.resolve(), "the harness must never run against the real data/ dir"
assert config_mod.settings.profile_name == "fake"


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(BASE, ignore_errors=True)


def _dissert_head_has(rel: str) -> bool:
    try:
        return _git_dissert("cat-file", "-e", "HEAD:" + rel).returncode == 0
    except OSError:
        return False


needs_thesis = pytest.mark.skipif(
    not THESIS_PYTHON.is_file() or not _dissert_head_has("train.py"),
    reason="needs the dissert repo (a committed train.py at HEAD) and its `thesis` interpreter",
)


@pytest.fixture(scope="session")
def dissert_head(tmp_path_factory) -> Path:
    """dissert's committed HEAD as a scratch directory (see the module
    docstring) — cwd and PYTHONPATH for every test that runs real dissert
    code."""
    root = tmp_path_factory.mktemp("dissert-head")
    archive = _git_dissert("archive", "HEAD")
    assert archive.returncode == 0, archive.stderr[-500:]
    subprocess.run(["tar", "-x", "-C", str(root)], input=archive.stdout, check=True)
    return root


# --------------------------------------------------------------------------- per-test world
@pytest.fixture(autouse=True)
def world(monkeypatch):
    """A clean data dir and fake-repo outputs for every test, fresh module
    caches, and every outward call replaced by a refusal."""
    from backend import bridge, colab, experiments, framework, kaggle, transport, tmux_runner
    from backend.runners import machine

    shutil.rmtree(DATA, ignore_errors=True)
    shutil.rmtree(HOST / "outputs", ignore_errors=True)
    shutil.rmtree(HOST / ".xdash", ignore_errors=True)
    (HOST / "fakefw" / "calls.log").unlink(missing_ok=True)
    DATA.mkdir(parents=True)
    config_mod.settings.reload("fake")

    for cache in (machine._availability_cache, colab._sessions_cache, kaggle._usage_cache, bridge._cache,
                  framework._code_cache):
        cache.clear()
    experiments._resolving.clear()

    # Resolution the scheduler hook would hand to a thread runs inline here.
    monkeypatch.setattr(experiments, "_spawn", lambda fn, *a: fn(*a))
    # No tmux, ever.
    tmux_calls: List[List[str]] = []

    def fake_tmux(args, host_id=None):
        tmux_calls.append(list(args))
        return subprocess.CompletedProcess(args=["tmux"] + list(args), returncode=127, stdout="", stderr="no tmux in tests")

    monkeypatch.setattr(tmux_runner, "_run", fake_tmux)
    # No ssh/rsync, ever.
    def refuse(*a, **k):
        raise transport.TransportError("tests never ssh/rsync")

    monkeypatch.setattr(transport.SshTransport, "run", refuse)
    monkeypatch.setattr(transport.SshTransport, "_rsync", refuse)
    # No kaggle / colab CLI unless a test scripts one.
    def refuse_kaggle(*a, **k):
        raise kaggle.KaggleOpsError("tests never call the kaggle CLI")

    monkeypatch.setattr(kaggle, "_run_kaggle", refuse_kaggle)

    def refuse_subprocess(*a, **k):
        raise AssertionError("tests never run the colab CLI unless they script it: %r" % (a,))

    # colab's own `subprocess` name only — patching subprocess.run itself
    # would reach every module (the bridge, git) too.
    monkeypatch.setattr(colab, "subprocess", SubprocessProxy(refuse_subprocess))
    # Notifications go nowhere.
    from backend import notifications
    sent: List[str] = []
    monkeypatch.setattr(notifications, "send_all", lambda text: sent.append(text))
    yield {"tmux_calls": tmux_calls, "notifications": sent}


@pytest.fixture
def settings():
    return config_mod.settings


# --------------------------------------------------------------------------- fakes
class SubprocessProxy:
    """Stands in for one module's `subprocess` import: a scripted run(),
    everything else (DEVNULL, CompletedProcess, TimeoutExpired) real."""

    def __init__(self, run):
        self.run = run

    def __getattr__(self, name):
        return getattr(subprocess, name)


class FakeRunner:
    """A runner whose every answer the test scripts. Kind-agnostic on
    purpose: the dispatcher and completion path must not care what it is."""

    def __init__(self, id="fake:one", kind="fake", volatile=False, accept=True, limit=None, vram_gb=None):
        from backend.runners.base import RunnerCapabilities
        self.id, self.kind, self.label = id, kind, id
        self.capabilities = RunnerCapabilities(
            direct_launch=False, live_log=False, stop=False, kill=False, restart=False, queue=True,
            volatile=volatile,
        )
        self.accept = accept
        self.limit = limit                               # None: unlimited; else in-flight attempts it takes
        self.vram_gb = vram_gb
        self.live: Optional[Dict[str, Any]] = None       # what poll() returns
        self.finished: Dict[str, Dict[str, Any]] = {}    # per attempt, overrides `live` (finish())
        self.staging: Optional[str] = None               # what collect() returns
        self.collect_error: Optional[Exception] = None
        self.diagnosis: Optional[Dict[str, Any]] = None
        self.dispatched: List[Dict[str, Any]] = []
        self.collected: List[str] = []
        self.cancelled: List[str] = []
        self.dispatch_error: Optional[Exception] = None

    def accelerator(self):
        return {"name": "fake-gpu", "vram_gb": self.vram_gb} if self.vram_gb is not None else None

    def capacity(self):
        # Only needed by backend/runtimes.py's §3.6 view (Phase 2) — the
        # dispatcher itself never calls this.
        from backend.runners.base import CapacitySnapshot
        used = len(self.in_flight()) if self.limit is not None else 0
        return CapacitySnapshot(unit="slots", used=used, limit=self.limit)

    def list_units(self):
        return []

    def in_flight(self):
        from backend import experiments
        return [a for a in experiments.attempts_for_slot(self.id) if a["status"] in experiments.IN_FLIGHT_STATUSES]

    def can_accept(self, experiment, est_hours):
        if not self.accept:
            return {"code": "pool-busy", "detail": "fake says no"}
        if self.limit is not None and len(self.in_flight()) >= self.limit:
            return {"code": "pool-busy", "detail": "fake is full"}
        return None

    def dispatch_priority(self, experiment, est_hours):
        return (0,)

    def dispatch(self, experiment, attempt):
        if self.dispatch_error is not None:
            raise self.dispatch_error
        self.dispatched.append(dict(attempt))
        return {"unit_ref": {"fake_unit": attempt["attempt_id"]}, "stages": [{"name": "run", "status": "running"}]}

    def finish(self, attempt_id, succeeded=True, raw="complete"):
        self.finished[attempt_id] = {"raw_status": raw, "stages": [], "finished": True, "succeeded": succeeded}

    def poll(self, attempt):
        if not (attempt.get("unit_ref") or {}).get("fake_unit"):
            return None
        if attempt["attempt_id"] in self.finished:
            return self.finished[attempt["attempt_id"]]
        return self.live or {"raw_status": "running", "stages": [], "finished": False, "succeeded": None}

    def collect(self, attempt):
        self.collected.append(attempt["attempt_id"])
        if self.collect_error is not None:
            raise self.collect_error
        return self.staging

    def diagnose(self, attempt, live, log_texts):
        return self.diagnosis

    def cancel(self, attempt):
        self.cancelled.append(attempt["attempt_id"])
        return True

    def seed(self, experiment, attempt, checkpoint_files):
        return {}

    def reap_idle(self):
        return None


@pytest.fixture
def use_runners(monkeypatch):
    """use_runners(r1, r2, ...) makes the registry return exactly these."""
    from backend.runners import registry

    def install(*runners):
        monkeypatch.setattr(registry, "list_runners", lambda: list(runners))
        return runners

    return install


class FakeTransport:
    """A remote host that is really a local directory: `remote_root` stands
    for the host's repo_root. Only the calls MachineRunner makes."""

    def __init__(self, host_id: str, remote_repo_root: Path, local_mirror: Path):
        self.host_id = host_id
        self.remote_repo_root = Path(remote_repo_root)
        self.mirror = Path(local_mirror)
        self.calls: List[tuple] = []
        self.fail: Optional[str] = None

    def _local(self, remote_path) -> Path:
        rel = Path(remote_path).relative_to(self.remote_repo_root)
        return self.mirror / rel

    def _check(self):
        if self.fail:
            from backend.transport import TransportError
            raise TransportError(self.fail)

    def exists(self, remote_path, kind="d"):
        self._check()
        p = self._local(remote_path)
        return p.is_dir() if kind == "d" else p.is_file()

    def pull(self, remote_dir, local_dir, excludes=()):
        self._check()
        self.calls.append(("pull", str(remote_dir), str(local_dir)))
        shutil.copytree(str(self._local(remote_dir)), str(local_dir), dirs_exist_ok=True)

    def pull_file(self, remote_path, local_path):
        self._check()
        self.calls.append(("pull_file", str(remote_path), str(local_path)))
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(self._local(remote_path)), str(local_path))

    def push(self, local_dir, remote_dir, excludes=()):
        self._check()
        self.calls.append(("push", str(local_dir), str(remote_dir)))

    def run(self, argv, timeout=None):
        self._check()
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout="", stderr="")

    def available(self):
        return not self.fail


# --------------------------------------------------------------------------- run-dir builders
def make_run_dir(root: Path, run_rel: str, run_id: str, status: str = "done", created_at: str = "2026-09-25T00:00:00+00:00",
                 epochs_completed: int = 3, total_epochs: int = 3, resumable: bool = False, gpu_hours: float = 0.5,
                 manifest_path_prefix: Optional[str] = None) -> Path:
    """A dissert-shaped run root under *root*: run_meta.json, checkpoints/
    {manifest.json, best.pth[, last.pth]}, logs/train.log."""
    run = root / run_rel
    (run / "checkpoints").mkdir(parents=True, exist_ok=True)
    (run / "logs").mkdir(parents=True, exist_ok=True)
    (run / "run_meta.json").write_text(
        '{"experiment_name": "demo_exp", "seed": 42, "repeat": null, "config_hash": "abc", "created_at": "%s"}' % created_at
    )
    import json
    (run / "checkpoints" / "manifest.json").write_text(json.dumps({
        "run_id": run_id, "status": status, "epochs_completed": epochs_completed, "total_epochs": total_epochs,
        "start_time": "2026-09-25T01:00:00+00:00", "gpu_hours": gpu_hours, "resumable": resumable,
    }))
    (run / "checkpoints" / "best.pth").write_bytes(b"best")
    if resumable:
        (run / "checkpoints" / "last.pth").write_bytes(b"last")
    (run / "logs" / "train.log").write_text("epoch 1\n")
    return run


def write_runs_csv(path: Path, rows: List[Dict[str, Any]], header: Optional[List[str]] = None) -> None:
    import csv
    from backend.results_ingest import RUNS_FIELDS
    header = header or RUNS_FIELDS
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def read_runs_csv(path: Path) -> List[Dict[str, str]]:
    import csv
    with open(path, newline="") as f:
        return list(csv.DictReader(f))
