"""X6 (code provenance + pin), X9 (keep the kernel log), X12 in the
template, and the kaggle-secret-missing / code-not-pushed codes."""
from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from backend import framework, kaggle
from backend.config import settings
from backend.runners import kaggle as kaggle_runner
from backend.runners.base import RunnerBlocked

from conftest import HOST, XDASH_ROOT

DISSERT_TEMPLATE = XDASH_ROOT / "data" / "dissert" / "kaggle_worker_template.ipynb"
SEGPRIORS_TEMPLATE = XDASH_ROOT / "data" / "segpriors" / "kaggle_worker_template.ipynb"


def _cells(nb_bytes):
    nb = json.loads(nb_bytes)
    return [(c["cell_type"], c["source"] if isinstance(c["source"], str) else "".join(c["source"])) for c in nb["cells"]]


# ----------------------------------------------------------------- template
def test_dissert_template_renders_split_args_and_the_pinned_commit():
    rendered = kaggle._render_launch_notebook(
        DISSERT_TEMPLATE, "configs/experiment/demo.yaml", "--seeds 42 --repeats 1 --epochs 5",
        dataset_source="someone/demo-ds", budget_hours=9.5, resume=True, snapshot_source="me/snap",
        eval_extra_args="--seeds 42 --repeats 1", code_commit="a" * 40,
        seed=42, train_extra_only="--epochs 5", eval_extra_only="",
    )
    cells = _cells(rendered)
    text = "\n".join(src for _t, src in cells)
    assert "__DASHBOARD_" not in text.replace('startswith("__DASHBOARD_")', "")
    spec_idx = next(i for i, (t, s) in enumerate(cells) if t == "code" and kaggle.LAUNCH_SPEC_MARKER in s)
    clone_idx = next(i for i, (_t, s) in enumerate(cells) if "git\", \"clone\"" in s)
    run_idx = next(i for i, (_t, s) in enumerate(cells) if "shlex.split(TRAIN_CMD)" in s)
    assert spec_idx < clone_idx < run_idx  # the clone can read CODE_COMMIT

    ns = {}
    exec(compile(cells[spec_idx][1], "spec", "exec"), ns)  # the substituted spec is valid Python
    assert ns["TRAIN_EXTRA_ARGS"] == "--seeds 42 --repeats 1 --epochs 5"
    assert ns["EVAL_EXTRA_ARGS"] == "--seeds 42 --repeats 1"
    assert ns["CODE_COMMIT"] == "a" * 40
    assert ns["RESUME"] is True and ns["MAX_HOURS"] == pytest.approx(8.5)
    # XDASH_PLAN.md §4.5 -- TRAIN_CMD/EVAL_CMD are the single source the run cell below
    # executes verbatim; the older per-piece placeholders above are still filled in (a
    # template that predates the split can use them directly) but no longer assembled here.
    assert "--seeds 42" in ns["TRAIN_CMD"] and "--max-hours" in ns["TRAIN_CMD"] and "--resume" in ns["TRAIN_CMD"]
    assert "--epochs 5" in ns["TRAIN_CMD"]
    assert "--seeds 42" in ns["EVAL_CMD"] and "--max-hours" not in ns["EVAL_CMD"] and "--resume" not in ns["EVAL_CMD"]

    clone = cells[clone_idx][1]
    assert '"git", "checkout", "--quiet", CODE_COMMIT' in clone
    assert "XDASH_CODE_COMMIT_NOT_FOUND" in clone
    run = cells[run_idx][1]
    assert "shlex.split(TRAIN_CMD)" in run and "shlex.split(EVAL_CMD)" in run
    assert "cmd_train" not in run and "cmd_eval" not in run  # nothing rebuilds a command here any more


def test_secret_error_text_only_appears_when_the_clone_fails():
    """Otherwise a public-repo run's log would contain 'No user secrets
    exist' and every later failure would be misread as secret-missing."""
    clone = next(s for _t, s in _cells(DISSERT_TEMPLATE.read_bytes()) if "UserSecretsClient" in s)
    before, after = clone.split("if clone.returncode != 0:")
    prints = [line for line in before.splitlines() if "print(" in line]
    assert prints and all("secret_error" not in line and "str(e)" not in line for line in prints)
    assert "secret_error" in after


def test_legacy_single_args_template_still_renders(tmp_path):
    rendered = kaggle._render_launch_notebook(SEGPRIORS_TEMPLATE, "configs/x.yaml", "--seeds 7", eval_extra_args="--no-vis")
    spec = next(s for t, s in _cells(rendered) if t == "code" and kaggle.LAUNCH_SPEC_MARKER in s)
    assert "'--seeds 7'" in spec and "__DASHBOARD_EXTRA_ARGS__" not in spec


# ----------------------------------------------------------------- code provenance
def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                                   GIT_COMMITTER_EMAIL="t@t"))


@pytest.fixture
def git_repo(tmp_path):
    """A local repo with a local bare 'origin' — no network."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", str(origin))
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", str(repo))
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "one")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git(repo, "fetch", "-q", "origin")
    return repo


def test_code_state_reports_commit_dirty_and_pushed(git_repo):
    s = framework.code_state(git_repo, fresh=True)
    assert len(s["commit"]) == 40 and s["dirty"] is False and s["pushed"] is True
    assert framework.code_is_pinnable(s)
    (git_repo / "a.py").write_text("x = 2\n")
    assert framework.code_state(git_repo, fresh=True)["dirty"] is True
    (git_repo / "untracked.txt").write_text("scratch")  # untracked files don't count
    _git(git_repo, "commit", "-q", "-am", "two")
    s = framework.code_state(git_repo, fresh=True)
    assert s["dirty"] is False and s["pushed"] is False and not framework.code_is_pinnable(s)


def test_code_state_outside_git_is_reported_not_raised(tmp_path):
    s = framework.code_state(tmp_path, fresh=True)
    assert s["commit"] is None and "error" in s


@pytest.fixture
def kaggle_account(monkeypatch):
    monkeypatch.setattr(kaggle, "_load_accounts", lambda: {"accounts": [
        {"name": "acct", "kaggle_username": "someone", "workers": [], "scope": "system"}]})
    monkeypatch.setattr(kaggle, "list_accounts", lambda: [
        {"name": "acct", "kaggle_username": "someone", "usage_estimate": {"remaining_hours": 30.0}}])
    monkeypatch.setattr(kaggle, "estimate_usage", lambda name: {"remaining_hours": 30.0})
    return kaggle_runner.KaggleRunner("acct")


def _experiment():
    return {"experiment_id": "demo_exp-s42", "config_path": "experiment/demo.yaml", "seed": 42,
            "extra_args": {"train": "--epochs 5", "eval": ""}}


def test_unpushed_code_blocks_kaggle_and_allow_unpushed_downgrades_it(kaggle_account, monkeypatch):
    runner = kaggle_account
    monkeypatch.setattr(framework, "code_state", lambda *a, **k: {"commit": "b" * 40, "dirty": False, "pushed": False})
    block = runner.can_accept(_experiment(), 1.0)
    assert block["code"] == "code-not-pushed" and "not on any remote branch" in block["detail"]
    # XDASH_FIXES_PLAN.md D7 — a copyable command, not a "click here to push"
    # button (Q1 ruled that out): the action is the exact git invocation.
    assert block["action"] == "git -C %s push" % settings.repo_root
    monkeypatch.setattr(framework, "code_state", lambda *a, **k: {"commit": "b" * 40, "dirty": True, "pushed": True})
    assert runner.can_accept(_experiment(), 1.0)["code"] == "code-not-pushed"
    monkeypatch.setattr(settings, "allow_unpushed", True)
    assert runner.can_accept(_experiment(), 1.0) is None


def test_dispatch_pins_the_recorded_commit_and_splits_args(kaggle_account, monkeypatch):
    runner = kaggle_account
    pushed = {}
    monkeypatch.setattr(kaggle, "push_experiment_attempt", lambda *a, **k: (
        pushed.update(args=a, kwargs=k) or {"kernel_slug": "slug", "results_dir": "outputs/kaggle/demo_exp-s42"}))
    code = {"commit": "c" * 40, "dirty": False, "pushed": True}
    patch = runner.dispatch(_experiment(), {"attempt_id": "atmpt_1", "code": code})
    assert pushed["kwargs"]["code_commit"] == "c" * 40
    assert pushed["args"][3] == "--seeds 42 --repeats 1 --epochs 5"
    assert pushed["kwargs"]["eval_extra_args"] == "--seeds 42 --repeats 1"
    assert patch["unit_ref"]["code_commit"] == "c" * 40 and patch["warnings"] == []
    # The tree went dirty between can_accept() and dispatch(): blocked, not launched.
    with pytest.raises(RunnerBlocked) as e:
        runner.dispatch(_experiment(), {"attempt_id": "atmpt_2", "code": {"commit": "c" * 40, "dirty": True, "pushed": True}})
    assert e.value.block["code"] == "code-not-pushed"
    # allow_unpushed: launched, pinned to nothing unpushed, warning recorded.
    monkeypatch.setattr(settings, "allow_unpushed", True)
    patch = runner.dispatch(_experiment(), {"attempt_id": "atmpt_3", "code": {"commit": "d" * 40, "dirty": False, "pushed": False}})
    assert pushed["kwargs"]["code_commit"] == "" and patch["warnings"][0]["code"] == "code-not-pushed"


def test_runner_blocked_puts_the_attempt_back_to_blocked(use_runners):
    from conftest import FakeRunner
    from backend import experiments
    runner = FakeRunner()
    runner.dispatch_error = RunnerBlocked({"code": "code-not-pushed", "detail": "push first"})
    use_runners(runner)
    exp = experiments.create_experiments([{"path": "experiment/demo.yaml", "seeds": [42]}], then="queue")["experiments"][0]
    a = exp["current_attempt"]
    assert a["status"] == "blocked" and a["blocked"]["code"] == "code-not-pushed" and a["slot"] is None
    assert len(exp["attempts"]) == 1  # no retry consumed


# ----------------------------------------------------------------- X9: keep the log
def _fake_output(monkeypatch, with_zip=True, with_log=True, returncode=0):
    def run(args, account_name, timeout=None):
        dest = Path(args[args.index("-p") + 1])
        if with_zip:
            with zipfile.ZipFile(dest / "results.zip", "w") as zf:
                zf.writestr("outputs/xdash_status.json", json.dumps({"stage": "done", "returncode": 0}))
                zf.writestr("outputs/experiments/demo_exp/abc1234-s42/checkpoints/manifest.json", "{}")
        if with_log:
            (dest / "xdash-fake-slot-acct.log").write_text('[{"data": "No user secrets exist for kernel id 7 and label GITHUB_TOKEN"}]')
        return subprocess.CompletedProcess(args=args, returncode=returncode, stdout="", stderr="")
    monkeypatch.setattr(kaggle, "_run_kaggle", run)


def test_download_keeps_the_kernel_log_and_no_longer_registers(kaggle_account, monkeypatch):
    _fake_output(monkeypatch)
    log_dir = settings.attempt_log_dir("atmpt_k")
    out = kaggle.download_experiment("acct", "slug", "outputs/kaggle/demo_exp-s42", log_dir=log_dir)
    assert Path(out["log_file"]).read_text().startswith('[{"data"')
    assert (HOST / "outputs/kaggle/demo_exp-s42/outputs/experiments/demo_exp/abc1234-s42/checkpoints/manifest.json").is_file()
    assert not (HOST / "outputs" / "ledger").exists()  # canonicalization registers, not the download


def test_log_only_output_is_an_empty_collection_not_an_error(kaggle_account, monkeypatch):
    _fake_output(monkeypatch, with_zip=False)
    attempt = {"attempt_id": "atmpt_l", "unit_ref": {"account": "acct", "kernel_slug": "slug",
                                                     "results_dir": "outputs/kaggle/demo_exp-s42"}}
    staging = kaggle_account.collect(attempt)
    assert Path(staging) == HOST / "outputs/kaggle/demo_exp-s42"
    assert (settings.attempt_log_dir("atmpt_l") / "kaggle.log").is_file()


def test_status_failure_message_is_read(kaggle_account, monkeypatch):
    monkeypatch.setattr(kaggle, "_run_kaggle", lambda args, name, timeout=None: subprocess.CompletedProcess(
        args=args, returncode=0, stderr="",
        stdout='someone/slug has status "KernelWorkerStatus.ERROR"\nFailure message: "No user secrets exist for kernel id 1"\n'))
    out = kaggle.refresh_experiment_status("acct", "slug")
    assert out == {"status": "error", "last_error": "No user secrets exist for kernel id 1"}


def test_diagnose_maps_secret_missing_with_a_deep_link(kaggle_account):
    attempt = {"unit_ref": {"account": "acct", "kernel_slug": "xdash-fake-slot-acct"}}
    d = kaggle_account.diagnose(attempt, {"failure_message": None}, ["...No user secrets exist for kernel id 7..."])
    assert d["code"] == "kaggle-secret-missing" and d["retry"] is False
    assert d["action_url"] == "https://www.kaggle.com/code/someone/xdash-fake-slot-acct/edit"
    d = kaggle_account.diagnose(attempt, {"failure_message": None}, ["RuntimeError: XDASH_CODE_COMMIT_NOT_FOUND: ..."])
    assert d["code"] == "code-not-pushed"
    assert kaggle_account.diagnose(attempt, {"failure_message": "CUDA OOM"}, ["boom"]) is None


# ----------------------------------------------------------------- XDASH_FIXES_PLAN.md F0.3
def test_kaggle_runtime_needs_attention_with_no_credentials_stored(kaggle_account):
    """kaggle_account's own list_accounts() stub (above) reports neither
    has_legacy_key nor has_api_token — real shape for a freshly-added
    account nobody has attached credentials to yet."""
    from backend import runtimes
    view = runtimes.runtime_view(kaggle_account)
    assert view["state"] == "attention"
    assert view["health"]["error"] == "No credentials stored for this account"


def test_kaggle_runtime_needs_attention_when_code_is_not_pinnable(monkeypatch):
    monkeypatch.setattr(kaggle, "_load_accounts", lambda: {"accounts": [
        {"name": "acct2", "kaggle_username": "someone", "workers": [], "scope": "system"}]})
    monkeypatch.setattr(kaggle, "list_accounts", lambda: [
        {"name": "acct2", "kaggle_username": "someone", "has_api_token": True,
         "usage_estimate": {"remaining_hours": 30.0}}])
    monkeypatch.setattr(kaggle, "estimate_usage", lambda name: {"remaining_hours": 30.0})
    monkeypatch.setattr(framework, "code_state", lambda *a, **k: {"commit": "b" * 40, "dirty": False, "pushed": False})
    from backend import runtimes
    runner = kaggle_runner.KaggleRunner("acct2")
    view = runtimes.runtime_view(runner)
    assert view["state"] == "attention"
    assert view["health"]["error"] == "Code isn't pinnable to a pushed commit (code-not-pushed)"


def test_kaggle_runtime_is_not_attention_with_credentials_and_pinnable_code(monkeypatch):
    monkeypatch.setattr(kaggle, "_load_accounts", lambda: {"accounts": [
        {"name": "acct3", "kaggle_username": "someone", "workers": [], "scope": "system"}]})
    monkeypatch.setattr(kaggle, "list_accounts", lambda: [
        {"name": "acct3", "kaggle_username": "someone", "has_api_token": True,
         "usage_estimate": {"remaining_hours": 30.0}}])
    monkeypatch.setattr(kaggle, "estimate_usage", lambda name: {"remaining_hours": 30.0})
    monkeypatch.setattr(framework, "code_state", lambda *a, **k: {"commit": "c" * 40, "dirty": False, "pushed": True})
    from backend import runtimes
    runner = kaggle_runner.KaggleRunner("acct3")
    view = runtimes.runtime_view(runner)
    assert view["health"]["error"] is None
    assert view["state"] != "attention"


# ----------------------------------------------------------------- XDASH_FIXES_PLAN.md F2 (tools registry, measured quota)
def test_kaggle_runtime_needs_attention_when_cli_is_missing(kaggle_account, monkeypatch):
    from backend import runtimes, tools
    monkeypatch.setattr(framework, "code_state", lambda *a, **k: {"commit": "d" * 40, "dirty": False, "pushed": True})
    monkeypatch.setattr(tools, "status", lambda name, refresh=False: tools.ToolStatus(
        name=name, path=name, source="not-found", exists=False, executable=False,
        version=None, min_version="2.2.1", version_ok=None, required=True,
        error="not found (checked an override, /fake/bin, and PATH)",
    ) if name == "kaggle" else tools._compute_status(name))
    monkeypatch.setattr(kaggle_account, "_account", lambda: {
        "name": "acct", "kaggle_username": "someone", "has_api_token": True,
        "usage_estimate": {"remaining_hours": 30.0}, "usage_history": [],
    })
    view = runtimes.runtime_view(kaggle_account)
    assert view["state"] == "attention"
    assert "Kaggle CLI" in view["health"]["error"]


def test_kaggle_can_accept_uses_measured_quota_first(kaggle_account, monkeypatch):
    """XDASH_FIXES_PLAN.md F2.6 — measured quota (once available) gates
    dispatch instead of the self-tracked estimate, with weekly_budget_hours
    narrowing it as an optional cap."""
    monkeypatch.setattr(framework, "code_is_pinnable", lambda *a, **k: True)
    monkeypatch.setattr(kaggle, "get_measured_quota", lambda name, force=False: {
        "available": True, "used": 29.0, "limit": 30.0, "unit": "h/week",
        "resets_at": "2026-10-05", "source": "measured",
    })
    monkeypatch.setattr(kaggle_account, "_account", lambda: {
        "name": "acct", "kaggle_username": "someone", "has_api_token": True,
        "usage_estimate": {"remaining_hours": 30.0},  # self-tracked would allow this — measured must win
    })
    from backend import datasets
    monkeypatch.setattr(datasets, "plan_delivery_for_config", lambda *a, **k: {"state": "ok"})
    block = kaggle_account.can_accept(_experiment(), est_hours=2.0)
    assert block == {
        "code": "quota-exhausted", "detail": "This account is over its weekly budget (measured)",
        "clears_at": block["clears_at"],
    }


def test_kaggle_can_accept_falls_back_to_self_tracked_with_reason(kaggle_account, monkeypatch):
    monkeypatch.setattr(framework, "code_is_pinnable", lambda *a, **k: True)
    monkeypatch.setattr(kaggle, "get_measured_quota", lambda name, force=False: {
        "available": False, "detail": "no credentials",
    })
    monkeypatch.setattr(kaggle_account, "_account", lambda: {
        "name": "acct", "kaggle_username": "someone", "has_api_token": True,
        "usage_estimate": {"remaining_hours": 1.0},
    })
    from backend import datasets
    monkeypatch.setattr(datasets, "plan_delivery_for_config", lambda *a, **k: {"state": "ok"})
    block = kaggle_account.can_accept(_experiment(), est_hours=2.0)
    assert block["code"] == "quota-exhausted"
    assert "self-tracked estimate (no credentials)" in block["detail"]
