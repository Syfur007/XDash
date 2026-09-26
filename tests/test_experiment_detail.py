"""XDASH_PLAN.md §8.3, Phase 3: the Experiment page's three small read-only
additions — an attempt's console log, its eval report, and its run dir's
file listing — none of which any route served before this phase (Phase 0's
`log_ref` was a path only, never read back)."""
from __future__ import annotations

import json

import pytest

from backend import experiments
from backend.config import settings

from conftest import HOST, FakeRunner


@pytest.fixture
def client():
    import server
    return server.app.test_client()


def _create(seed=42, config="experiment/demo.yaml"):
    created = experiments.create_experiments(
        [{"path": config, "seeds": [seed]}], pool="*", then="queue",
    )
    return created["experiments"][0]


def _attempt(experiment_id):
    exp = experiments.get_experiment(experiment_id)
    return exp["attempts"][-1]


def test_log_report_and_artifacts_are_all_a_clean_4xx_before_any_attempt_exists(client):
    exp = experiments.create_experiments([{"path": "experiment/demo.yaml", "seeds": [1]}])
    eid = exp["experiments"][0]["experiment_id"]
    assert client.get(f"/api/experiments/{eid}/log").status_code == 400
    r = client.get(f"/api/experiments/{eid}/report")
    assert r.status_code == 200 and r.get_json() == {"attempt_id": None, "metrics": None, "report_path": None}
    r = client.get(f"/api/experiments/{eid}/artifacts")
    assert r.status_code == 400  # _attempt_or_error: no attempt yet


def test_log_tails_the_persisted_console_log(client, use_runners):
    use_runners(FakeRunner())
    exp = _create()
    a = _attempt(exp["experiment_id"])
    log_dir = settings.attempt_log_dir(a["attempt_id"])
    (log_dir / "train.log").write_text("epoch 1\nepoch 2\n")

    r = client.get(f"/api/experiments/{exp['experiment_id']}/log")
    assert r.status_code == 200
    body = r.get_json()
    assert body["exists"] and body["stage"] == "train" and "epoch 2" in body["text"]
    assert body["available_stages"] == ["train"]

    # An explicit ?stage= for a file that doesn't exist yet: a clean miss, not an error.
    r = client.get(f"/api/experiments/{exp['experiment_id']}/log?stage=eval")
    assert r.status_code == 200
    assert r.get_json()["exists"] is False


def test_report_reads_the_newest_done_attempts_eval_report(client, use_runners):
    use_runners(FakeRunner())
    exp = _create()
    a = _attempt(exp["experiment_id"])
    run_dir = HOST / a["run"]["run_dir"]
    (run_dir / "eval").mkdir(parents=True)
    (run_dir / "eval" / "report.json").write_text(json.dumps({"metrics": {"dice": 0.9, "hd95": 4.2}}))
    experiments._update_attempt(a["attempt_id"], {"status": "done"})

    r = client.get(f"/api/experiments/{exp['experiment_id']}/report")
    assert r.status_code == 200
    body = r.get_json()
    assert body["metrics"] == {"dice": 0.9, "hd95": 4.2}
    assert body["attempt_id"] == a["attempt_id"]


def test_artifacts_lists_files_and_serves_one_but_refuses_to_escape_the_run_dir(client, use_runners):
    use_runners(FakeRunner())
    exp = _create()
    a = _attempt(exp["experiment_id"])
    run_dir = HOST / a["run"]["run_dir"]
    (run_dir / "checkpoints").mkdir(parents=True)
    (run_dir / "checkpoints" / "last.pt").write_bytes(b"0" * 10)

    r = client.get(f"/api/experiments/{exp['experiment_id']}/artifacts")
    assert r.status_code == 200
    files = r.get_json()["files"]
    assert {"name": "last.pt", "rel_path": "checkpoints/last.pt", "size": 10} in files

    r = client.get(f"/api/experiments/{exp['experiment_id']}/artifacts/checkpoints/last.pt")
    assert r.status_code == 200 and r.data == b"0" * 10

    r = client.get(f"/api/experiments/{exp['experiment_id']}/artifacts/../../../etc/passwd")
    assert r.status_code == 400


# --------------------------------------------------------------------------- curves (Phase 6, §8.2/§8.3)
def _write_tb_scalars(run_dir, scalars):
    """scalars: {tag: [(step, value), ...]}. Writes real TB event files with
    tensorboard's own writer/proto (no tensorflow/torch needed) — exactly
    the format `backend/tb_curves.py` reads back with EventAccumulator."""
    from tensorboard.compat.proto import event_pb2, summary_pb2
    from tensorboard.summary.writer.event_file_writer import EventFileWriter

    tb_dir = run_dir / "tensorboard"
    tb_dir.mkdir(parents=True, exist_ok=True)
    w = EventFileWriter(str(tb_dir))
    for tag, points in scalars.items():
        for step, value in points:
            summary = summary_pb2.Summary(value=[summary_pb2.Summary.Value(tag=tag, simple_value=value)])
            w.add_event(event_pb2.Event(wall_time=0.0, step=step, summary=summary))
    w.close()


def test_curves_reads_real_tb_event_files(client, use_runners):
    use_runners(FakeRunner())
    exp = _create()
    a = _attempt(exp["experiment_id"])
    run_dir = HOST / a["run"]["run_dir"]
    _write_tb_scalars(run_dir, {"epoch/dice": [(0, 0.5), (1, 0.6), (2, 0.7)], "epoch/loss": [(0, 1.0), (1, 0.8)]})

    r = client.get(f"/api/experiments/{exp['experiment_id']}/curves")
    assert r.status_code == 200
    body = r.get_json()
    assert body["available"] is True
    assert set(body["tags"]) == {"epoch/dice", "epoch/loss"}
    steps = [p[0] for p in body["series"]["epoch/dice"]]
    values = [p[1] for p in body["series"]["epoch/dice"]]
    assert steps == [0, 1, 2]
    assert values == pytest.approx([0.5, 0.6, 0.7], abs=1e-6)  # simple_value is float32

    r = client.get(f"/api/experiments/{exp['experiment_id']}/curves?tags=epoch/dice")
    assert set(r.get_json()["series"]) == {"epoch/dice"}


def test_curves_is_a_clean_miss_with_no_tensorboard_dir(client, use_runners):
    use_runners(FakeRunner())
    exp = _create()
    r = client.get(f"/api/experiments/{exp['experiment_id']}/curves")
    assert r.status_code == 200
    body = r.get_json()
    assert body["available"] is False and body["tags"] == [] and body["series"] == {}
