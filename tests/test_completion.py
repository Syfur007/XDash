"""X2 + X3 — dispatch records the planned run; every runner kind finishes
through one path: collect → canonicalize → classify → resolve or chain."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import experiments, hosts, results_ingest, scheduler
from backend.config import settings

from conftest import HOST, FakeRunner, FakeTransport, make_run_dir, read_runs_csv, write_runs_csv


def _create(runner_ids=None, seed=42, config="experiment/demo.yaml", extra_args="", overlay=None):
    created = experiments.create_experiments(
        [{"path": config, "seeds": [seed]}], extra_args=extra_args, pool=runner_ids or "*",
        overlay=overlay, then="queue",
    )
    return created["experiments"][0]


def _attempt(experiment_id):
    exp = experiments.get_experiment(experiment_id)
    return exp["attempts"][-1]


def _stored_attempt(attempt_id):
    return experiments._get_attempt(attempt_id)


# ----------------------------------------------------------------- dispatch provenance
def test_dispatch_records_run_plan_code_and_log_ref(use_runners):
    runner = FakeRunner()
    use_runners(runner)
    exp = _create()
    a = _attempt(exp["experiment_id"])
    assert a["status"] == "running"
    assert a["run"]["run_dir"].startswith("outputs/experiments/demo_exp/") and a["run"]["run_dir"].endswith("-s42")
    assert a["run"]["run_ids"] and a["run"]["config_hash"]
    assert set(a["code"]) >= {"commit", "dirty", "pushed"}  # the fake host isn't a git repo: all None + error
    assert a["log_ref"].endswith(a["attempt_id"])
    assert a["collect"] == {"state": "pending"}
    # The runner saw the plan before launching.
    assert runner.dispatched[0]["run"]["run_dir"] == a["run"]["run_dir"]


def test_extra_args_are_stored_split_and_no_seed_is_baked_in(use_runners):
    use_runners(FakeRunner(accept=False))
    exp = _create(extra_args="--epochs 5")
    assert exp["extra_args"] == {"train": "--epochs 5", "eval": ""}


def test_v1_store_migrates_on_load_and_keeps_the_original():
    """The real data/dissert/experiments.json shape (one experiment, two
    failed attempts, extra_args "--seed 42"), reproduced here — the suite
    never reads the real file."""
    v1 = {
        "experiments": {"gmkunet_t_clinicdb-s42": {
            "experiment_id": "gmkunet_t_clinicdb-s42", "config_path": "experiment/gmkunet/gmkunet_t_clinicdb.yaml",
            "seed": 42, "batch_name": None, "pool": "kaggle_only", "extra_args": "--seed 42", "max_retries": 1,
            "attempt_ids": ["atmpt_0ba9c0780b"], "current_attempt_id": "atmpt_0ba9c0780b",
        }},
        "attempts": {"atmpt_0ba9c0780b": {
            "attempt_id": "atmpt_0ba9c0780b", "experiment_id": "gmkunet_t_clinicdb-s42", "status": "failed",
            "slot": "kaggle:tanvir", "unit_ref": {"account": "tanvir", "kernel_slug": "xdash-dissert-slot-tanvir"},
        }},
        "batches": {},
    }
    path = settings.experiments_store_file
    path.write_text(json.dumps(v1))
    data = experiments._load()
    assert data["schema_version"] == experiments.SCHEMA_VERSION
    assert data["experiments"]["gmkunet_t_clinicdb-s42"]["extra_args"] == {"train": "", "eval": ""}
    assert data["attempts"]["atmpt_0ba9c0780b"]["status"] == "failed"  # history untouched
    assert json.loads(path.read_text()) == v1  # reading never rewrote the file
    experiments._save(data)
    assert json.loads(path.with_name("experiments.json.pre-v%d.bak" % experiments.SCHEMA_VERSION).read_text()) == v1
    # Legacy attempts carry no `collect` record, so nothing ever tries to re-collect them.
    assert not experiments.slot_has_uncollected("kaggle:tanvir")


# ----------------------------------------------------------------- canonical collection
def _finish(runner, succeeded=True, raw="complete"):
    runner.live = {"raw_status": raw, "stages": [], "finished": True, "succeeded": succeeded}


def _stage_remote_run(attempt, staging_root: Path, status="done", created_at="2026-09-25T00:00:00+00:00", resumable=False):
    run = attempt["run"]
    make_run_dir(staging_root, run["run_dir"], run["run_ids"][0], status=status, created_at=created_at,
                 resumable=resumable, epochs_completed=2 if status == "interrupted" else 3)
    write_runs_csv(staging_root / "outputs" / "ledger" / "runs.csv", [
        {"run_id": run["run_ids"][0], "status": status, "seed": 42, "repeat": "", "experiment_name": "demo_exp",
         "manifest_path": run["run_dir"] + "/checkpoints/manifest.json"},
        {"run_id": "R-someone-else", "status": "done", "manifest_path": "x"},  # another run on that host: not ours
    ])


def test_remote_style_run_is_canonicalized_registered_and_classified(use_runners):
    runner = FakeRunner()
    use_runners(runner)
    exp = _create()
    a = _attempt(exp["experiment_id"])
    staging = HOST / "outputs" / "kaggle" / exp["experiment_id"]
    _stage_remote_run(a, staging)
    runner.staging = str(staging)
    _finish(runner)
    experiments._poll_in_flight_attempts()

    done = _stored_attempt(a["attempt_id"])
    run_dir = HOST / a["run"]["run_dir"]
    assert done["status"] == "done"
    assert done["run_id"] == a["run"]["run_ids"][0]           # from the manifest, via classification
    assert done["collect"]["state"] == "done"
    assert done["run"]["collected_dir"] == a["run"]["run_dir"]
    assert (run_dir / "checkpoints" / "manifest.json").is_file()
    assert not staging.exists()                                 # staging deleted
    rows = read_runs_csv(HOST / "outputs" / "ledger" / "runs.csv")
    assert [r["run_id"] for r in rows] == [a["run"]["run_ids"][0]]  # only this run's row
    assert rows[0]["manifest_path"] == a["run"]["run_dir"] + "/checkpoints/manifest.json"
    assert not experiments.slot_has_uncollected(runner.id)


def test_interrupted_leg_chains_a_second_leg(use_runners):
    """X2's consequence: classification finally finds the run, so resume
    chains (Phase 0 acceptance: an over-budget run chains a second leg)."""
    runner = FakeRunner()
    use_runners(runner)
    exp = _create()
    a = _attempt(exp["experiment_id"])
    staging = HOST / "outputs" / "kaggle" / exp["experiment_id"]
    _stage_remote_run(a, staging, status="interrupted", resumable=True)
    runner.staging = str(staging)
    _finish(runner)
    experiments._poll_in_flight_attempts()

    leg1 = _stored_attempt(a["attempt_id"])
    assert leg1["status"] == "done" and leg1["raw_status"] == "interrupted"
    assert leg1["epochs_completed"] == 2
    assert any(p.endswith("checkpoints/last.pth") for p in leg1["checkpoint_files"])
    assert all(p.startswith("outputs/experiments/demo_exp/") for p in leg1["checkpoint_files"])  # canonical, not staging
    view = experiments.get_experiment(exp["experiment_id"])
    leg2 = view["attempts"][-1]
    assert leg2["resume_of"] == a["attempt_id"] and leg2["leg_index"] == 2
    assert leg2["status"] == "running"  # dispatched on the next tick, to the same fake runner


def test_output_conflict_goes_to_a_side_dir_and_is_flagged(use_runners):
    runner = FakeRunner()
    use_runners(runner)
    exp = _create()
    a = _attempt(exp["experiment_id"])
    # A different run already sits at the canonical path.
    make_run_dir(HOST, a["run"]["run_dir"], "R-old", created_at="2020-01-01T00:00:00+00:00")
    staging = HOST / "outputs" / "remote" / "box" / a["attempt_id"]
    _stage_remote_run(a, staging)
    runner.staging = str(staging)
    _finish(runner)
    experiments._poll_in_flight_attempts()

    done = _stored_attempt(a["attempt_id"])
    side = a["run"]["run_dir"] + "." + a["attempt_id"]
    assert done["run"]["collected_dir"] == side
    assert "output-conflict" in done["flags"]
    assert json.loads((HOST / a["run"]["run_dir"] / "checkpoints" / "manifest.json").read_text())["run_id"] == "R-old"
    assert (HOST / side / "checkpoints" / "manifest.json").is_file()
    row = read_runs_csv(HOST / "outputs" / "ledger" / "runs.csv")[0]
    assert row["manifest_path"] == side + "/checkpoints/manifest.json"


def test_run_mismatch_skips_classification_instead_of_guessing(use_runners):
    runner = FakeRunner()
    use_runners(runner)
    exp = _create()
    a = _attempt(exp["experiment_id"])
    log_dir = settings.attempt_log_dir(a["attempt_id"])
    (log_dir / "train.log").write_text("run_id=R-0000000-s42-f- status=done best_metric=0.9\n")
    staging = HOST / "outputs" / "kaggle" / exp["experiment_id"]
    staging.mkdir(parents=True)
    runner.staging = str(staging)
    _finish(runner)
    experiments._poll_in_flight_attempts()
    done = _stored_attempt(a["attempt_id"])
    assert "run-mismatch" in done["flags"]
    assert done["status"] == "done" and done["run_id"] is None  # trusted the exit code, didn't guess a run


def test_retryable_collection_failure_leaves_the_attempt_unresolved(use_runners):
    from backend.transport import TransportError
    runner = FakeRunner()
    use_runners(runner)
    exp = _create()
    a = _attempt(exp["experiment_id"])
    runner.collect_error = TransportError("host down")
    _finish(runner)
    experiments._poll_in_flight_attempts()
    still = _stored_attempt(a["attempt_id"])
    assert still["status"] == "running"
    assert still["collect"]["state"] == "pending" and still["collect"]["tries"] == 1
    # Host back: the next tick collects and resolves.
    runner.collect_error = None
    staging = HOST / "outputs" / "kaggle" / exp["experiment_id"]
    _stage_remote_run(a, staging)
    runner.staging = str(staging)
    experiments._poll_in_flight_attempts()
    assert _stored_attempt(a["attempt_id"])["status"] == "done"


def test_non_volatile_runner_gives_up_collecting_eventually(use_runners, monkeypatch):
    from backend.transport import TransportError
    monkeypatch.setattr(experiments, "_COLLECT_MAX_TRIES", 2)
    runner = FakeRunner()
    use_runners(runner)
    exp = _create()
    a = _attempt(exp["experiment_id"])
    runner.collect_error = TransportError("host gone for good")
    _finish(runner)
    experiments._poll_in_flight_attempts()
    experiments._poll_in_flight_attempts()
    gave_up = _stored_attempt(a["attempt_id"])
    assert gave_up["collect"]["state"] == "failed"
    assert gave_up["status"] == "done"  # resolved from the exit code, outputs recorded as not collected


def test_volatile_runner_never_gives_up_collecting(use_runners, monkeypatch):
    from backend.transport import TransportError
    monkeypatch.setattr(experiments, "_COLLECT_MAX_TRIES", 2)
    runner = FakeRunner(id="colabish:a", volatile=True)
    use_runners(runner)
    exp = _create()
    a = _attempt(exp["experiment_id"])
    runner.collect_error = TransportError("vm unreachable")
    _finish(runner)
    for _ in range(4):
        experiments._poll_in_flight_attempts()
    held = _stored_attempt(a["attempt_id"])
    assert held["status"] == "running" and held["collect"]["state"] == "pending"
    assert experiments.slot_has_uncollected(runner.id)


def test_failure_diagnosis_sets_its_code_link_and_skips_the_retry(use_runners, world):
    runner = FakeRunner()
    use_runners(runner)
    exp = _create()
    runner.staging = str(HOST / "outputs" / "kaggle" / exp["experiment_id"])
    runner.diagnosis = {"code": "kaggle-secret-missing", "detail": "attach it", "action_url": "https://k/edit", "retry": False}
    _finish(runner, succeeded=False, raw="error")
    experiments._poll_in_flight_attempts()
    view = experiments.get_experiment(exp["experiment_id"])
    assert len(view["attempts"]) == 1  # no automatic retry
    assert view["attempts"][0]["blocked"]["code"] == "kaggle-secret-missing"
    assert view["attempts"][0]["blocked"]["action_url"] == "https://k/edit"
    assert any("https://k/edit" in n for n in world["notifications"])


# ----------------------------------------------------------------- machine runners (X3)
@pytest.fixture
def ssh_box(monkeypatch, tmp_path):
    """A registered SSH host whose 'remote' filesystem is a local dir."""
    from backend import transport as transport_mod
    from backend.runners import machine
    hosts.upsert_host({
        "id": "box", "kind": "ssh", "label": "Box", "max_concurrent": 1,
        "ssh": {"host": "box.example"}, "repos": {"fake": {"repo_root": "/remote/repo"}},
    })
    remote = tmp_path / "remote-repo"
    remote.mkdir()
    # XDASH_PLAN.md §5, X4: dispatch now actually places the dataset before
    # launch. This box already has "demo"'s data (data/demo, per the fake
    # host repo's dataset/demo.yaml) — the default `path` binding just
    # symlinks it, so tests that aren't about dataset placement itself don't
    # need to configure a binding.
    (remote / "data" / "demo").mkdir(parents=True)
    fake = FakeTransport("box", Path("/remote/repo"), remote)
    real_for_record = transport_mod.for_host_record
    monkeypatch.setattr(transport_mod, "for_host_record", lambda h: fake if h.id == "box" else real_for_record(h))
    runner = machine.MachineRunner(hosts.get_host("box"))
    return runner, fake, remote


def _machine_attempt(runner, eval_status="completed", train_status="completed", with_plan=True):
    """A running machine attempt whose scheduler items are already terminal —
    what scheduler._tick() leaves behind when both tmux halves finish."""
    from backend import framework
    data = scheduler._load()
    data["items"] += [
        {"id": "trainitem", "status": train_status, "mode": "train", "session_name": "sess_train", "host_id": runner.host.id,
         "config_path": "experiment/demo.yaml", "extra_args": ""},
        {"id": "evalitem", "status": eval_status, "mode": "eval", "session_name": "sess_eval", "host_id": runner.host.id,
         "config_path": "experiment/demo.yaml", "extra_args": "", "depends_on": "trainitem"},
    ]
    scheduler._save(data)
    store = experiments._load()
    exp = {"experiment_id": "demo_exp-s42", "config_path": "experiment/demo.yaml", "seed": 42, "pool": "*",
           "extra_args": {"train": "", "eval": ""}, "max_retries": 1, "max_legs": 6, "attempt_ids": [],
           "current_attempt_id": None, "created_at": "2026-09-25T00:00:00+00:00"}
    store["experiments"][exp["experiment_id"]] = exp
    attempt = experiments._append_attempt(store, exp)
    attempt.update({
        "status": "running", "slot": runner.id, "started_at": "2026-09-25T00:00:00+00:00",
        "unit_ref": {"train_item_id": "trainitem", "eval_item_id": "evalitem"},
        "run": framework.locate_run("experiment/demo.yaml", 42) if with_plan else None,
        "collect": {"state": "pending"},
    })
    experiments._save(store)
    return attempt


def test_machine_poll_reports_finished_from_the_eval_item(ssh_box):
    runner, _fake, _remote = ssh_box
    a = _machine_attempt(runner, eval_status="running")
    assert runner.poll(a)["finished"] is False
    a2 = _machine_attempt(runner, eval_status="skipped", train_status="failed")
    live = runner.poll(a2)
    assert live["finished"] is True and live["succeeded"] is False


def test_ssh_attempt_is_collected_canonicalized_and_logged(ssh_box, use_runners, monkeypatch):
    runner, fake, remote = ssh_box
    use_runners(runner)
    from backend import terminals
    monkeypatch.setattr(terminals, "get_terminal",
                        lambda name, include_log=False: {"log_text": "pane of %s\nrun_id=%s status=done" % (name, planned[0])})
    a = _machine_attempt(runner)
    planned = a["run"]["run_ids"]
    _stage_remote_run(a, remote)  # the run as it sits on the remote host
    # The scheduler's fast path, now routed through the one completion path.
    experiments.on_scheduler_item_finished("evalitem")

    done = _stored_attempt(a["attempt_id"])
    assert done["status"] == "done"
    assert done["run_id"] == planned[0]
    assert (HOST / a["run"]["run_dir"] / "checkpoints" / "best.pth").is_file()
    assert not (HOST / "outputs" / "remote" / "box" / a["attempt_id"]).exists()
    pulled = [c for c in fake.calls if c[0] == "pull"]
    assert pulled[0][1] == "/remote/repo/" + a["run"]["run_dir"]  # exactly the planned dir, never a guess
    log_dir = settings.attempt_log_dir(a["attempt_id"])
    assert "pane of sess_train" in (log_dir / "train.log").read_text()
    assert "pane of sess_eval" in (log_dir / "eval.log").read_text()


def test_ssh_attempt_with_nothing_on_the_host_collects_empty(ssh_box, use_runners):
    runner, _fake, _remote = ssh_box
    use_runners(runner)
    a = _machine_attempt(runner, eval_status="skipped", train_status="failed")
    experiments._poll_in_flight_attempts()
    done = _stored_attempt(a["attempt_id"])
    assert done["collect"]["state"] == "empty"
    assert done["status"] == "failed"


def test_ssh_host_down_keeps_the_attempt_running(ssh_box, use_runners):
    runner, fake, _remote = ssh_box
    use_runners(runner)
    a = _machine_attempt(runner)
    fake.fail = "ssh: connect to host box.example: Connection refused"
    experiments._poll_in_flight_attempts()
    assert _stored_attempt(a["attempt_id"])["status"] == "running"


def test_local_attempt_reads_the_canonical_tree_in_place(use_runners, monkeypatch):
    from backend.runners import machine
    runner = machine.MachineRunner(hosts.get_host("local"))
    use_runners(runner)
    monkeypatch.setattr(machine.terminals, "get_terminal", lambda name, include_log=False: None)
    a = _machine_attempt(runner)
    make_run_dir(HOST, a["run"]["run_dir"], a["run"]["run_ids"][0], status="done")
    experiments._poll_in_flight_attempts()
    done = _stored_attempt(a["attempt_id"])
    assert done["status"] == "done"
    assert done["run"]["collected_dir"] == a["run"]["run_dir"]
    assert done["run_id"] == a["run"]["run_ids"][0]


def test_machine_dispatch_splits_train_and_eval_args(ssh_box, use_runners, monkeypatch):
    runner, fake, _remote = ssh_box
    captured = {}
    monkeypatch.setattr(scheduler, "add_item", lambda config, mode, extra_args, host_id=None, train_extra_args=None, **k: (
        captured.update(eval=extra_args, train=train_extra_args, **k) or [{"id": "t"}, {"id": "e"}]
    ))
    exp = {"experiment_id": "demo_exp-s42", "config_path": "experiment/demo.yaml", "seed": 42,
           "extra_args": {"train": "--epochs 5", "eval": "--no-vis"}}
    runner.dispatch(exp, {"attempt_id": "atmpt_x", "resume_of": "atmpt_prev"})
    assert captured["train"] == "--seeds 42 --repeats 1 --epochs 5 --resume"
    assert captured["eval"] == "--seeds 42 --repeats 1 --no-vis"
    assert ("push", str(settings.repo_root), "/remote/repo", False) in fake.calls


# ----------------------------------------------------------------- cancel + stragglers
def test_cancelling_a_volatile_attempt_keeps_its_collection_pending(use_runners):
    runner = FakeRunner(id="colabish:a", volatile=True)
    use_runners(runner)
    exp = _create()
    a = _attempt(exp["experiment_id"])
    experiments.cancel_experiment(exp["experiment_id"])
    assert _stored_attempt(a["attempt_id"])["collect"]["state"] == "pending"
    assert experiments.slot_has_uncollected(runner.id)
    staging = HOST / "outputs" / "kaggle" / exp["experiment_id"]
    _stage_remote_run(a, staging, status="interrupted", resumable=True)
    runner.staging = str(staging)
    experiments._collect_stragglers()
    after = _stored_attempt(a["attempt_id"])
    assert after["status"] == "cancelled" and after["collect"]["state"] == "done"
    assert not experiments.slot_has_uncollected(runner.id)


def test_cancelling_a_non_volatile_attempt_skips_collection(use_runners):
    runner = FakeRunner()
    use_runners(runner)
    exp = _create()
    a = _attempt(exp["experiment_id"])
    experiments.cancel_experiment(exp["experiment_id"])
    assert _stored_attempt(a["attempt_id"])["collect"]["state"] == "skipped"
    assert runner.cancelled == [a["attempt_id"]]


# ----------------------------------------------------------------- ledger merge
def test_ledger_upsert_keeps_the_hosts_header_and_other_rows(settings):
    """dissert's own runs.csv may carry columns XDash doesn't know (it grew a
    `repeat` column); registration must never drop them or reorder rows."""
    dest = settings.ledger_dir / "runs.csv"
    header = ["run_id", "status", "weird_future_column", "manifest_path"]
    write_runs_csv(dest, [{"run_id": "R-keep", "status": "done", "weird_future_column": "w", "manifest_path": "m"}], header)
    before = dest.read_text()
    changed = results_ingest._upsert_ledger_rows([{"run_id": "R-new", "status": "done", "manifest_path": "p", "seed": 1}])
    assert changed == ["R-new"]
    text = dest.read_text()
    assert text.startswith(before)  # untouched, appended after
    rows = read_runs_csv(dest)
    assert rows[1] == {"run_id": "R-new", "status": "done", "weird_future_column": "", "manifest_path": "p"}
    # An open row is upserted in place; a finished one is never overwritten.
    results_ingest._upsert_ledger_rows([{"run_id": "R-open", "status": "interrupted"}])
    results_ingest._upsert_ledger_rows([{"run_id": "R-open", "status": "done"}, {"run_id": "R-keep", "status": "failed"}])
    rows = {r["run_id"]: r for r in read_runs_csv(dest)}
    assert rows["R-open"]["status"] == "done" and rows["R-keep"]["status"] == "done"
    assert list(rows) == ["R-keep", "R-new", "R-open"]


def test_ledger_reader_finds_hash_scoped_manifests(settings):
    from backend import ledger
    make_run_dir(HOST, "outputs/experiments/demo_exp/abc1234-s42", "R-abc1234-s42-f-")
    make_run_dir(HOST, "outputs/experiments/old_exp-s7", "R-old-s7")  # the pre-52477d1 flat layout
    ids = {r["run_id"] for r in ledger.list_runs()}
    assert ids == {"R-abc1234-s42-f-", "R-old-s7"}


# ============================================================================
# XDASH_FIXES_PLAN.md F0 — Truthful dispatch
# ============================================================================

# ----------------------------------------------------------------- F0.4: MachineRunner.diagnose()
# The two real crash logs issue 11 turned up, copied verbatim (never read
# from the real data/ dir — see XDASH_FIXES_PLAN.md's own instruction).
TIMM_LAYERS_LOG = """\
syfur@blackbox:~/Workspace/XDash$ cd /home/syfur/Workspace/dissert
syfur@blackbox:~/Workspace/dissert$ conda activate thesis
Traceback (most recent call last):
  File "train.py", line 9, in <module>
    from dissert.cli.train import main
  File "/home/syfur/Workspace/dissert/src/dissert/cli/train.py", line 38, in <module>
    from dissert.models import get_model
  File "/home/syfur/Workspace/dissert/src/dissert/models/__init__.py", line 1, in <module>
    from .blocks import ConvBlock, ResBlock, DoubleConv, EncoderBlock, DecoderBlock, AttentionBlock
  File "/home/syfur/Workspace/dissert/src/dissert/models/blocks.py", line 6, in <module>
    from timm.layers import trunc_normal_tf_
ModuleNotFoundError: No module named 'timm.layers'
(thesis) syfur@blackbox:~/Workspace/dissert$
"""

NO_DISSERT_MODULE_LOG = """\
$ cd /home/syf/dissert
$ conda activate emcadenv
(emcadenv) $ Traceback (most recent call last):
  File "train.py", line 9, in <module>
    from dissert.cli.train import main
ModuleNotFoundError: No module named 'dissert'
(emcadenv) $
"""


def test_classify_failure_marks_both_real_crashes_env_broken_no_retry():
    from backend.runners.machine import _classify_failure
    assert _classify_failure(TIMM_LAYERS_LOG) == ("env-broken", False)
    assert _classify_failure(NO_DISSERT_MODULE_LOG) == ("env-broken", False)


def test_classify_failure_oom_and_data_missing_and_generic():
    from backend.runners.machine import _classify_failure
    assert _classify_failure("RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB") == ("oom", False)
    assert _classify_failure("FileNotFoundError: [Errno 2] No such file or directory: 'data/demo/x.png'") == ("data-missing", False)
    assert _classify_failure("RuntimeError: something unrelated blew up") == ("attempt-failed", True)


def test_classify_failure_env_broken_only_before_any_epoch():
    """A ModuleNotFoundError *after* training got underway is a mid-run crash
    (retryable), not a broken environment — the plan's own "before any
    epoch" qualifier."""
    from backend.runners.machine import _classify_failure
    text = "epoch 1: loss 0.4\nepoch 2: loss 0.3\nModuleNotFoundError: No module named 'plotly'\n"
    assert _classify_failure(text) == ("attempt-failed", True)


def test_machine_diagnose_reports_stage_exit_code_and_last_exception_line():
    from backend.runners import machine
    runner = machine.MachineRunner(hosts.get_host("local"))
    data = scheduler._load()
    data["items"] += [
        {"id": "trainitem", "status": "failed", "return_code": 1, "session_name": "sess_train"},
        {"id": "evalitem", "status": "skipped", "return_code": None, "session_name": "sess_eval"},
    ]
    scheduler._save(data)
    attempt = {"attempt_id": "atmpt_diag", "unit_ref": {"train_item_id": "trainitem", "eval_item_id": "evalitem"}}
    live = {"stages": [{"name": "train", "status": "failed"}, {"name": "eval", "status": "skipped"}]}
    failure = runner.diagnose(attempt, live, [TIMM_LAYERS_LOG])
    assert failure["code"] == "env-broken" and failure["retry"] is False
    assert failure["detail"] == "train exited 1: ModuleNotFoundError: No module named 'timm.layers'"
    assert failure["action"].startswith("Fix 'train' on This machine:")  # hosts.py's local default label


def test_machine_diagnose_returns_none_for_empty_logs():
    from backend.runners import machine
    runner = machine.MachineRunner(hosts.get_host("local"))
    attempt = {"attempt_id": "atmpt_x", "unit_ref": {}}
    assert runner.diagnose(attempt, {"stages": []}, []) is None
    assert runner.diagnose(attempt, {"stages": []}, [""]) is None


# ----------------------------------------------------------------- F0.4 + F0.5, end to end: no
# retry on env-broken, and the final stages get stored on the failed attempt.
def test_env_broken_failure_does_not_retry_and_stores_final_stages(use_runners, monkeypatch):
    from backend import terminals
    from backend.runners import machine
    runner = machine.MachineRunner(hosts.get_host("local"))
    use_runners(runner)
    a = _machine_attempt(runner, eval_status="skipped", train_status="failed")
    # trainitem's return_code isn't set by _machine_attempt — add it here so
    # the diagnosis' "exited <code>" detail has a real number to quote.
    data = scheduler._load()
    for item in data["items"]:
        if item["id"] == "trainitem":
            item["return_code"] = 1
    scheduler._save(data)

    def fake_get_terminal(name, include_log=False):
        return {"log_text": TIMM_LAYERS_LOG if name == "sess_train" else ""}

    monkeypatch.setattr(terminals, "get_terminal", fake_get_terminal)
    experiments._poll_in_flight_attempts()

    done = _stored_attempt(a["attempt_id"])
    assert done["status"] == "failed"
    assert done["blocked"]["code"] == "env-broken"
    assert "ModuleNotFoundError: No module named 'timm.layers'" in done["blocked"]["detail"]
    assert done["blocked"]["action"].startswith("Fix 'train' on This machine:")
    # F0.5 — the terminal attempt keeps the real final stages, not the
    # pending/pending pair dispatch() wrote at launch time.
    assert done["stages"] == [{"name": "train", "status": "failed"}, {"name": "eval", "status": "skipped"}]
    # F0.4 — env-broken never retries: no second attempt was opened even
    # though the experiment's max_retries (1) would otherwise allow one.
    exp = experiments.get_experiment(a["experiment_id"])
    assert len(exp["attempt_ids"]) == 1


def test_resolve_attempt_stores_final_stages_on_success_too(use_runners):
    """_resolve_attempt's *stages* patch (F0.5) isn't machine-specific — every
    runner kind goes through the same function."""
    runner = FakeRunner()
    use_runners(runner)
    exp = _create()
    a = _attempt(exp["experiment_id"])
    assert a["stages"] == [{"name": "run", "status": "running"}]  # FakeRunner.dispatch()'s own patch
    runner.live = {"raw_status": "complete", "stages": [{"name": "run", "status": "done"}], "finished": True, "succeeded": True}
    experiments._poll_in_flight_attempts()
    done = _stored_attempt(a["attempt_id"])
    assert done["stages"] == [{"name": "run", "status": "done"}]


# ----------------------------------------------------------------- F0.6 (issue 9): session labels
def test_attempt_owned_session_is_labelled_by_experiment_id():
    from backend.runners import machine
    runner = machine.MachineRunner(hosts.get_host("local"))
    a = _machine_attempt(runner)  # wires scheduler items "trainitem"/"evalitem", session "sess_train"/"sess_eval"
    term = {"session_name": "sess_train", "managed": True, "experiment_name": "demo_exp"}
    assert machine._session_label(term) == a["experiment_id"]


def test_ad_hoc_session_keeps_the_config_name_with_a_marker():
    from backend.runners import machine
    term = {"session_name": "sess_never_scheduled", "managed": True, "experiment_name": "demo_exp"}
    assert machine._session_label(term) == "demo_exp (ad hoc)"


def test_unmanaged_session_label_is_untouched():
    from backend.runners import machine
    term = {"session_name": "someones-manual-tmux", "managed": False}
    assert machine._session_label(term) == "someones-manual-tmux"


# ----------------------------------------------------------------- F0.6: OCCUPYING_STATUSES
def test_running_labels_ignore_interrupted_and_lost_units():
    from backend import runtimes
    from backend.runners.base import RunUnit

    class _StubRunner:
        id = "local"
        kind = "local"

        def list_units(self):
            return [
                RunUnit(unit_id="a", runner_id="local", label="running-one", status="running", raw_status="running"),
                RunUnit(unit_id="b", runner_id="local", label="ghost", status="interrupted", raw_status="lost"),
                RunUnit(unit_id="c", runner_id="local", label="pending-one", status="pending", raw_status="pending"),
            ]

    assert runtimes._running_labels(_StubRunner()) == ["running-one", "pending-one"]


# ----------------------------------------------------------------- F0.6: the lost-record reconcile
def _ghost_terminal_record(session_name="sess_ghost"):
    return {
        "session_name": session_name, "host_id": "local", "config_path": "experiment/demo.yaml",
        "cli_config": None, "full_command": None, "mode": "train", "extra_args": "",
        "experiment_name": "demo_exp", "command": "python train.py", "created_at": "2026-09-15T00:00:00",
        "restart_count": 0, "return_code": None,
    }


def test_a_dead_record_reconciles_to_lost_once_and_stays_lost():
    from backend import terminals
    terminals._save([_ghost_terminal_record()])
    first = terminals.get_terminal("sess_ghost")
    assert first["status"] == "lost" and first["restart_available"] is True
    stored = terminals._load()[0]
    assert stored.get("status") == "lost" and stored.get("lost_at")
    lost_at = stored["lost_at"]
    # Never re-probed: a second look doesn't touch lost_at or re-derive.
    again = terminals.get_terminal("sess_ghost")
    assert again["status"] == "lost"
    assert terminals._load()[0]["lost_at"] == lost_at


def test_restart_clears_a_lost_reconciliation(monkeypatch):
    from backend import terminals
    terminals._save([_ghost_terminal_record()])
    terminals.get_terminal("sess_ghost")
    assert terminals._load()[0].get("status") == "lost"
    # _start_session is the one place restart() would touch a real tmux —
    # stubbed so this test exercises only the record bookkeeping around it.
    # The report lookup is stubbed too: restart()'s own trailing
    # _status_for(record) call re-derives a status immediately (still no
    # live tmux session in this fake world), and without a "found" report
    # that re-derivation would call _reconcile_lost() right back — exactly
    # correct behaviour for a restart that never actually started anything,
    # but it would defeat this test's ability to see the cleared fields.
    monkeypatch.setattr(terminals, "_start_session", lambda *a, **k: "python train.py")
    monkeypatch.setattr(terminals.reports, "find_latest_report_for_experiment", lambda name: {"experiment": name, "timestamp": "z"})
    terminals.restart("sess_ghost")
    stored = terminals._load()[0]
    assert "status" not in stored and "lost_at" not in stored
    assert stored["restart_count"] == 1


# ----------------------------------------------------------------- F0.3: runtime health/attention
def test_ssh_runtime_needs_attention_when_no_repo_root_configured():
    from backend import runtimes
    from backend.runners import machine
    hosts.upsert_host({"id": "bare", "kind": "ssh", "label": "Bare box", "max_concurrent": 1, "ssh": {"host": "bare.example"}})
    runner = machine.MachineRunner(hosts.get_host("bare"))
    view = runtimes.runtime_view(runner)
    assert view["state"] == "attention"
    assert view["health"]["error"] == "No repo_root configured for this profile"


def test_ssh_runtime_needs_attention_when_unreachable(ssh_box):
    from backend import runtimes
    runner, fake, _remote = ssh_box
    fake.fail = "ssh: connect to host box.example: Connection refused"
    view = runtimes.runtime_view(runner)
    assert view["state"] == "attention"
    assert view["health"]["error"] == "Host is not reachable"


# ----------------------------------------------------------------- F0.7: scheduler cap
def test_add_item_cap_counts_only_non_terminal_items(settings, monkeypatch):
    monkeypatch.setattr(settings, "scheduler_max_queue_size", 2)
    data = scheduler._load()
    data["items"] = [
        {"id": "old1", "status": "completed", "config_path": "experiment/demo.yaml", "mode": "train",
         "extra_args": "", "host_id": "local", "session_name": None, "depends_on": None},
        {"id": "old2", "status": "failed", "config_path": "experiment/demo.yaml", "mode": "train",
         "extra_args": "", "host_id": "local", "session_name": None, "depends_on": None},
    ]
    scheduler._save(data)
    # Both existing items are terminal — the old cap (every item ever
    # created, finished or not) would have refused this at max size 2; the
    # fix counts only non-terminal items, so a third item is still allowed.
    created = scheduler.add_item("experiment/demo.yaml", "train", "")
    assert len(created) == 1
    assert len(scheduler._load()["items"]) == 3


# ----------------------------------------------------------------- F1.3: PATCH /api/hosts merge
def test_patch_host_merges_and_preserves_accelerator():
    hosts.upsert_host({
        "id": "gpubox", "kind": "ssh", "label": "GPU box", "max_concurrent": 2,
        "ssh": {"host": "gpubox.example", "user": "syf"},
        "accelerator": {"name": "Tesla T4", "vram_gb": 15.0, "source": "nvidia-smi"},
    })
    host = hosts.patch_host("gpubox", {"max_concurrent": 4})
    assert host.max_concurrent == 4
    assert host.accelerator == {"name": "Tesla T4", "vram_gb": 15.0, "source": "nvidia-smi"}
    assert host.ssh["host"] == "gpubox.example"  # an untouched nested key survives too


def test_patch_host_deep_merges_nested_repos_without_touching_other_profiles():
    hosts.upsert_host({
        "id": "multi", "kind": "ssh", "ssh": {"host": "x"},
        "repos": {"other-profile": {"repo_root": "/keep/me"}},
    })
    host = hosts.patch_host("multi", {"repos": {"fake": {"repo_root": "/new/root"}}})
    as_dict = host.as_dict()
    assert as_dict["repos"]["other-profile"]["repo_root"] == "/keep/me"
    assert as_dict["repos"]["fake"]["repo_root"] == "/new/root"


def test_patch_host_unknown_id_raises():
    with pytest.raises(hosts.HostError):
        hosts.patch_host("does-not-exist", {"max_concurrent": 2})


def test_patch_host_synthesizes_local_record_first():
    host = hosts.patch_host("local", {"max_concurrent": 3})
    assert host.id == "local"
    assert host.max_concurrent == 3


# ----------------------------------------------------------------- F1.4: D5 — remote hosts stop
# inheriting this machine's env/python
def test_remote_host_without_its_own_env_or_python_does_not_inherit_local(settings, monkeypatch):
    monkeypatch.setattr(settings, "env_activate_cmd", "conda activate thesis")
    host = hosts.upsert_host({"id": "plain-ssh", "kind": "ssh", "ssh": {"host": "x"}})
    assert host.env_activate_cmd == ""          # not "conda activate thesis"
    assert host.python_executable == "python"   # not settings.python_executable's value
    # The local host is unaffected — it still reads through to the profile.
    assert hosts.get_host("local").env_activate_cmd == "conda activate thesis"


def test_remote_host_with_its_own_env_and_python_uses_them():
    host = hosts.upsert_host({
        "id": "own-env", "kind": "ssh", "ssh": {"host": "x"},
        "env_activate_cmd": "conda activate dissert-py310",
        "python_executable": "/envs/dissert-py310/bin/python",
    })
    assert host.env_activate_cmd == "conda activate dissert-py310"
    assert host.python_executable == "/envs/dissert-py310/bin/python"


# ----------------------------------------------------------------- F1.5: "Verify environment" (D6)
def test_check_command_renders_the_default_template(settings):
    from backend import envcheck
    assert envcheck.check_command("/envs/x/bin/python") == "/envs/x/bin/python train.py --help"


def test_verify_environment_runs_cd_then_env_activate_then_check_over_the_hosts_transport(ssh_box):
    from backend import envcheck
    runner, fake, _remote = ssh_box
    host = hosts.patch_host("box", {"env_activate_cmd": "conda activate dissert-py310"})
    result = envcheck.verify_environment(host, force=True)
    assert result["ok"] is True
    argv = fake.run_calls[-1]
    assert argv[:2] == ["bash", "-ic"]
    shell_cmd = argv[2]
    assert "cd /remote/repo" in shell_cmd
    assert "conda activate dissert-py310" in shell_cmd
    assert "train.py --help" in shell_cmd
    assert shell_cmd.index("cd ") < shell_cmd.index("conda activate") < shell_cmd.index("train.py")


def test_verify_environment_reports_the_last_exception_line_on_failure(ssh_box):
    from backend import envcheck
    runner, fake, _remote = ssh_box
    fake.run_returncode = 1
    fake.run_stderr = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'dissert'\n"
    result = envcheck.verify_environment(runner.host, force=True)
    assert result["ok"] is False
    assert result["detail"] == "ModuleNotFoundError: No module named 'dissert'"


def test_verify_environment_is_cached_until_a_relevant_field_changes(ssh_box):
    from backend import envcheck
    runner, fake, _remote = ssh_box
    envcheck.verify_environment(runner.host)
    envcheck.verify_environment(runner.host)
    assert len(fake.run_calls) == 1  # second call was a cache hit

    # env_activate_cmd is part of the cache key — busts it even inside the TTL.
    host2 = hosts.patch_host("box", {"env_activate_cmd": "conda activate other"})
    envcheck.verify_environment(host2)
    assert len(fake.run_calls) == 2

    envcheck.verify_environment(host2, force=True)
    assert len(fake.run_calls) == 3  # force always re-runs, cache hit or not


def test_gate_for_dispatch_does_not_block_on_a_cold_cache(ssh_box):
    # XDASH_DISABLE_BACKGROUND=1 (the test harness) skips the background
    # prime entirely, so a never-checked host is "unknown", not "broken" —
    # the documented trade-off in backend/envcheck.py: can_accept() must
    # never stall a dispatch tick on the very first check.
    runner, fake, _remote = ssh_box
    fake.run_returncode = 1  # would fail if ever actually run
    block = runner.can_accept({"config_path": "experiment/demo.yaml", "seed": 1}, 1.0)
    assert block is None
    assert fake.run_calls == []  # nothing ran synchronously


def test_gate_for_dispatch_blocks_on_a_cached_failure(ssh_box):
    from backend import envcheck
    runner, fake, _remote = ssh_box
    fake.run_returncode = 1
    fake.run_stderr = "ImportError: No module named 'timm.layers'"
    envcheck.verify_environment(runner.host, force=True)  # primes the cache
    block = runner.can_accept({"config_path": "experiment/demo.yaml", "seed": 1}, 1.0)
    assert block == {"code": "env-broken", "detail": "ImportError: No module named 'timm.layers'"}


def test_capacity_reports_env_check_failed_from_cache_only_never_triggers_one(ssh_box):
    from backend import envcheck
    runner, fake, _remote = ssh_box
    cap = runner.capacity()
    assert cap.extra["env_check_failed"] is False  # nothing cached yet -> not known-broken
    assert fake.run_calls == []
    fake.run_returncode = 1
    fake.run_stderr = "boom"
    envcheck.verify_environment(runner.host, force=True)
    cap = runner.capacity()
    assert cap.extra["env_check_failed"] is True
    assert cap.extra["env_check_detail"] == "boom"


def test_runtime_health_reports_env_check_failed(ssh_box):
    from backend import envcheck, runtimes
    runner, fake, _remote = ssh_box
    fake.run_returncode = 1
    fake.run_stderr = "ModuleNotFoundError: No module named 'dissert'"
    envcheck.verify_environment(runner.host, force=True)
    view = runtimes.runtime_view(runner)
    assert view["state"] == "attention"
    assert "ModuleNotFoundError: No module named 'dissert'" in view["health"]["error"]
