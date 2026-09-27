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
