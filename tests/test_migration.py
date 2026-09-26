"""XDASH_PLAN.md §3.8 (Phase 1): experiments.json v1/v2 -> v3 — batches
become studies, `pool` becomes `runtime.allow`, attempt status `pending`
becomes `queued`, a paused batch becomes autopilot off plus hold, and
Phase 0's split `extra_args` stays. In memory on load; the first save keeps
a permanent experiments.json.pre-v3.bak. The suite never reads the real
data/ dir: REAL_V1 below is a verbatim copy of data/dissert/experiments.json
as of 2026-09-25."""
from __future__ import annotations

import json

import pytest

from backend import experiments, studies
from backend.config import settings

from conftest import FakeRunner

REAL_V1 = json.loads(r'''
{
  "experiments": {
    "gmkunet_t_clinicdb-s42": {
      "experiment_id": "gmkunet_t_clinicdb-s42",
      "config_path": "experiment/gmkunet/gmkunet_t_clinicdb.yaml",
      "seed": 42,
      "batch_name": null,
      "pool": "kaggle_only",
      "extra_args": "--seed 42",
      "max_retries": 1,
      "force_on_retry": false,
      "max_legs": 6,
      "created_at": "2026-09-24T10:43:12+00:00",
      "attempt_ids": [
        "atmpt_0ba9c0780b",
        "atmpt_3589a546b3"
      ],
      "current_attempt_id": "atmpt_3589a546b3"
    }
  },
  "attempts": {
    "atmpt_0ba9c0780b": {
      "attempt_id": "atmpt_0ba9c0780b",
      "experiment_id": "gmkunet_t_clinicdb-s42",
      "attempt_index": 1,
      "leg_index": 1,
      "resume_of": null,
      "resumed_by": null,
      "epochs_completed": null,
      "checkpoint_files": null,
      "slot": "kaggle:tanvir",
      "status": "failed",
      "raw_status": "error",
      "stages": [
        {
          "name": "run",
          "status": "running"
        }
      ],
      "unit_ref": {
        "account": "tanvir",
        "snapshot_slug": null,
        "kernel_slug": "xdash-dissert-slot-tanvir",
        "results_dir": "outputs/kaggle/gmkunet_t_clinicdb-s42"
      },
      "started_at": "2026-09-24T10:43:12+00:00",
      "ended_at": "2026-09-24T10:58:09+00:00",
      "blocked": {
        "code": "attempt-failed",
        "detail": "unit ended: error",
        "since": "2026-09-24T10:58:09+00:00"
      },
      "run_id": null,
      "updated_at": "2026-09-24T10:58:09+00:00"
    },
    "atmpt_3589a546b3": {
      "attempt_id": "atmpt_3589a546b3",
      "experiment_id": "gmkunet_t_clinicdb-s42",
      "attempt_index": 2,
      "leg_index": 1,
      "resume_of": null,
      "resumed_by": null,
      "epochs_completed": null,
      "checkpoint_files": null,
      "slot": "kaggle:emon",
      "status": "failed",
      "raw_status": null,
      "stages": [],
      "unit_ref": null,
      "started_at": "2026-09-24T10:58:09+00:00",
      "ended_at": "2026-09-24T10:58:10+00:00",
      "blocked": {
        "code": "dispatch-failed",
        "detail": "Push failed for experiment 'gmkunet_t_clinicdb-s42': 401 Client Error: Unauthorized for url: https://www.kaggle.com/api/v1/kernels/push",
        "since": "2026-09-24T10:58:10+00:00"
      },
      "run_id": null,
      "updated_at": "2026-09-24T10:58:10+00:00"
    }
  },
  "batches": {}
}
''')


def _write_store(data):
    settings.experiments_store_file.parent.mkdir(parents=True, exist_ok=True)
    settings.experiments_store_file.write_text(json.dumps(data))


@pytest.fixture
def client():
    import server
    return server.app.test_client()


def test_the_real_store_migrates_without_loss(client):
    _write_store(REAL_V1)
    before = settings.experiments_store_file.read_bytes()
    data = experiments._load()
    assert data["schema_version"] == experiments.SCHEMA_VERSION == 3
    assert "batches" not in data and data["studies"] == {}
    exp = data["experiments"]["gmkunet_t_clinicdb-s42"]
    src = REAL_V1["experiments"]["gmkunet_t_clinicdb-s42"]
    assert exp["runtime"] == {"mode": "auto", "allow": ["kaggle"], "requires": {"min_vram_gb": None}}  # was kaggle_only
    assert "pool" not in exp and "batch_name" not in exp
    assert exp["studies"] == [] and exp["overlay"] == {} and exp["priority"] == 0 and exp["name"] == exp["experiment_id"]
    assert exp["extra_args"] == {"train": "", "eval": ""}  # Phase 0's split stays ("--seed 42" was the baked seed)
    for key in ("config_path", "seed", "max_retries", "force_on_retry", "max_legs", "created_at", "attempt_ids",
                "current_attempt_id"):
        assert exp[key] == src[key], key
    assert data["attempts"] == REAL_V1["attempts"]  # both failed: history byte-for-byte
    assert settings.experiments_store_file.read_bytes() == before  # reading never writes

    # Over HTTP: it reads as failed, in no study, and can be retried.
    listed = client.get("/api/experiments").get_json()["experiments"]
    assert [(e["experiment_id"], e["status"], e["studies"]) for e in listed] == [("gmkunet_t_clinicdb-s42", "failed", [])]
    assert settings.experiments_store_file.read_bytes() == before

    experiments._save(data)
    keep = settings.experiments_store_file.with_name("experiments.json.pre-v3.bak")
    assert json.loads(keep.read_text()) == REAL_V1
    assert experiments._load() == data  # v3 on disk reloads unchanged


def _v2_with_batches():
    def exp(eid, batch, pool, attempt_ids, current):
        return {"experiment_id": eid, "config_path": "experiment/demo.yaml", "seed": 1, "batch_name": batch,
                "pool": pool, "extra_args": {"train": "", "eval": ""}, "max_retries": 1, "force_on_retry": True,
                "max_legs": 6, "created_at": "2026-09-20T00:00:00+00:00", "attempt_ids": attempt_ids,
                "current_attempt_id": current}

    def att(aid, eid, status, **kw):
        return {"attempt_id": aid, "experiment_id": eid, "attempt_index": 1, "leg_index": 1, "status": status,
                "slot": None, "resume_of": None, "resumed_by": None, **kw}

    return {
        "schema_version": 2,
        "experiments": {
            "a-s1": exp("a-s1", "abl", "either", ["at_a"], "at_a"),
            "b-s1": exp("b-s1", "abl", ["local", "kaggle:tanvir"], ["at_b"], "at_b"),
            "c-s1": exp("c-s1", "held", "local_only", ["at_c1", "at_c2"], "at_c2"),
            "d-s1": exp("d-s1", "held", "*", ["at_d"], "at_d"),
            "e-s1": exp("e-s1", None, "kaggle_only", [], None),
        },
        "attempts": {
            "at_a": att("at_a", "a-s1", "pending"),
            "at_b": att("at_b", "b-s1", "done"),
            "at_c1": att("at_c1", "c-s1", "failed"),
            "at_c2": att("at_c2", "c-s1", "blocked", blocked={"code": "pool-busy"}),
            "at_d": att("at_d", "d-s1", "pending"),
        },
        "batches": {
            "abl": {"name": "abl", "pool": "either", "max_retries": 1, "force_on_retry": True, "paused": False,
                    "created_at": "2026-09-19T00:00:00+00:00"},
            "held": {"name": "held", "pool": "local_only", "max_retries": 2, "force_on_retry": False, "paused": True,
                     "created_at": "2026-09-19T01:00:00+00:00"},
        },
    }


def test_batches_become_studies_and_a_paused_batch_becomes_hold(client):
    _write_store(_v2_with_batches())
    first = experiments._load()
    assert experiments._load() == first  # deterministic: two reads before any save agree on every id
    abl, held = experiments.migrated_study_id("abl"), experiments.migrated_study_id("held")
    assert set(first["studies"]) == {abl, held}
    assert first["studies"][held]["migrated_from"] == {
        "batch": "held", "pool": "local_only", "max_retries": 2, "force_on_retry": False, "paused": True,
    }
    assert first["studies"][held]["autopilot"] == {"enabled": False, "max_parallel": 2}
    assert first["studies"][abl]["defaults"]["runtime"]["allow"] == ["*"]

    e = first["experiments"]
    assert e["a-s1"]["studies"] == [{"study_id": abl, "group": None}]
    assert e["b-s1"]["runtime"]["allow"] == ["local", "kaggle:tanvir"]
    assert e["c-s1"]["runtime"]["allow"] == ["local"] and e["e-s1"]["runtime"]["allow"] == ["kaggle"]
    assert e["e-s1"]["studies"] == []
    # pending -> queued; the non-paused batch's queue is untouched.
    assert first["attempts"]["at_a"]["status"] == "queued"
    # The paused batch's queued/blocked members are dequeued: back to what they were.
    assert "at_c2" not in first["attempts"] and e["c-s1"]["current_attempt_id"] == "at_c1"
    assert "at_d" not in first["attempts"] and e["d-s1"]["current_attempt_id"] is None
    assert first["attempts"]["at_c1"]["status"] == "failed"

    by_id = {s["study_id"]: s for s in client.get("/api/studies").get_json()["studies"]}
    assert by_id[held]["counts"]["draft"] == 1 and by_id[held]["counts"]["failed"] == 1
    assert by_id[held]["status"] == "attention"
    assert by_id[abl]["counts"] == {"draft": 0, "queued": 1, "blocked": 0, "running": 0, "done": 1, "failed": 0,
                                    "cancelled": 0}
    statuses = {x["experiment_id"]: x["status"] for x in client.get("/api/experiments").get_json()["experiments"]}
    assert statuses == {"a-s1": "queued", "b-s1": "done", "c-s1": "failed", "d-s1": "draft", "e-s1": "draft"}


def test_a_legacy_batch_name_in_a_request_maps_to_the_same_study(client, use_runners):
    use_runners(FakeRunner(accept=False))
    _write_store(_v2_with_batches())
    body = {"configs": ["experiment/other.yaml"], "seeds": [3], "batch_name": "abl"}
    out = client.post("/api/experiments", json=body).get_json()
    assert out["experiments"][0]["studies"][0]["study_id"] == experiments.migrated_study_id("abl")
    fresh = client.post("/api/experiments", json={**body, "batch_name": "new-one"}).get_json()
    assert fresh["matched"] == ["other_exp-s3"]  # same experiment, now in a second study
    sid = experiments.migrated_study_id("new-one")
    assert [m["study_id"] for m in fresh["experiments"][0]["studies"]] == [experiments.migrated_study_id("abl"), sid]
    assert studies.get_study(sid)["name"] == "new-one"
    # /api/batches* is retired; nothing in the UI called it.
    assert client.get("/api/batches").status_code == 404


def test_the_retired_batch_runners_file_imports_as_an_archived_study():
    """data/<profile>/batches.json (the batch runner's own file, retired in
    XDASH_V2_PLAN.md §3.7): its rows were never experiments, so the batch
    survives as an archived, member-less study rather than being dropped."""
    legacy = {"batches": {"diss-test": {"name": "diss-test", "status": "done", "pool": "kaggle_only",
                                        "max_retries": 1, "force_on_retry": False,
                                        "started_at": "2026-09-15T23:22:45", "ended_at": "2026-09-22T03:49:51",
                                        "row_count": 1}}}
    legacy_path = settings.experiments_store_file.with_name("batches.json")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(json.dumps(legacy))
    _write_store(REAL_V1)
    data = experiments._load()
    study = data["studies"][experiments.migrated_study_id("diss-test")]
    assert study["archived"] is True and study["name"] == "diss-test" and study["created_at"] == "2026-09-15T23:22:45"
    assert study["migrated_from"]["row_count"] == 1 and "assignments" in study["description"]
    assert json.loads(legacy_path.read_text()) == legacy  # never written


def test_a_queued_resume_leg_dequeued_by_hold_is_reopened_by_queue(use_runners):
    """Hold must not break a resume chain: the dequeued leg 2 comes back as
    leg 2 (resume_of leg 1), not as a fresh start."""
    use_runners(FakeRunner(accept=False))
    store = {
        "schema_version": 3, "studies": {"st_x": experiments.blank_study("st_x", "x", None)},
        "experiments": {"demo_exp-s1": {
            "experiment_id": "demo_exp-s1", "config_path": "experiment/demo.yaml", "seed": 1,
            "studies": [{"study_id": "st_x", "group": None}], "runtime": experiments.default_runtime(),
            "extra_args": {"train": "", "eval": ""}, "attempt_ids": ["leg1", "leg2"], "current_attempt_id": "leg2",
            "created_at": "2026-09-20T00:00:00+00:00",
        }},
        "attempts": {
            "leg1": {"attempt_id": "leg1", "experiment_id": "demo_exp-s1", "status": "done", "raw_status": "interrupted",
                     "leg_index": 1, "resumed_by": "leg2", "checkpoint_files": ["outputs/x/last.pth"], "run_id": "R-1"},
            "leg2": {"attempt_id": "leg2", "experiment_id": "demo_exp-s1", "status": "queued", "leg_index": 2,
                     "resume_of": "leg1", "run_id": "R-1"},
        },
    }
    _write_store(store)
    held = studies.set_autopilot("st_x", hold=True)
    assert held["dequeued"] == ["demo_exp-s1"]
    assert experiments._get_attempt("leg1")["resumed_by"] is None
    out = experiments.apply_action("queue", ids=["demo_exp-s1"], params={"rerun": True})
    assert out["ok"] == ["demo_exp-s1"]
    view = experiments.get_experiment("demo_exp-s1")
    leg = view["current_attempt"]
    assert leg["leg_index"] == 2 and leg["resume_of"] == "leg1" and leg["run_id"] == "R-1"
    assert experiments._get_attempt("leg1")["resumed_by"] == leg["attempt_id"]
