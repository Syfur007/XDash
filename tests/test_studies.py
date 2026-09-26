"""XDASH_PLAN.md Phase 1 acceptance, over HTTP with fake runners: studies
and membership, drafts (X7), identity v2 (X8), the actions endpoint (§6.1),
execution modes (§6.2), runtime policy (§6.3), ordering (§6.4) and
preflight (§6.5)."""
from __future__ import annotations

import json
import random

import pytest

from backend import experiments

from conftest import HOST, FakeRunner, make_run_dir

CONFIGS = ["experiment/demo.yaml", "experiment/other.yaml", "experiment/third.yaml"]


@pytest.fixture
def client():
    import server
    return server.app.test_client()


def _ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.get_json())
    return r.get_json()


def _study(client, name="Size ablation", **fields):
    return _ok(client.post("/api/studies", json={"name": name, **fields}))


def _post(client, **body):
    return _ok(client.post("/api/experiments", json=body))


def _act(client, action, **body):
    return _ok(client.post("/api/experiments/actions", json={"action": action, **body}))


def _statuses(client, study_id=None):
    qs = "?study=%s" % study_id if study_id else ""
    return {e["experiment_id"]: e["status"] for e in _ok(client.get("/api/experiments" + qs))["experiments"]}


def _in_flight(study_id=None):
    views = experiments.list_experiments(study=study_id)
    return [v["experiment_id"] for v in views if v["status"] in experiments.IN_FLIGHT_STATUSES]


def _finish_all(runner, succeeded=True):
    for a in runner.in_flight():
        runner.finish(a["attempt_id"], succeeded=succeeded)
    experiments._poll_in_flight_attempts()


# ----------------------------------------------------------------- drafts (X7)
def test_a_3x3_study_created_as_drafts_dispatches_nothing(client, use_runners):
    runner = FakeRunner(limit=10)
    use_runners(runner)
    study = _study(client, primary_metric={"key": "dice"})
    assert study["status"] == "planning" and study["study_id"].startswith("st_")
    out = _post(client, configs=CONFIGS, seeds=[1, 2, 3], study_id=study["study_id"])
    assert len(out["created"]) == 9 and out["matched"] == [] and out["then"] is None
    for _ in range(3):
        experiments._dispatch_tick()
    assert runner.dispatched == []
    assert set(_statuses(client, study["study_id"]).values()) == {"draft"}
    got = _ok(client.get("/api/studies/%s" % study["study_id"]))
    assert got["status"] == "planning" and got["counts"]["draft"] == 9 and len(got["experiments"]) == 9
    assert all(e["studies"][0]["name"] == "Size ablation" for e in got["experiments"])


# ----------------------------------------------------------------- the acceptance scenario
def test_queue_one_run_now_one_pinned_then_autopilot_never_exceeds_max_parallel(client, use_runners):
    """§10 Phase 1: queue one, Run-now one pinned, autopilot max_parallel=2
    — at most 2 are ever in flight, across many ticks, until all 9 finish."""
    auto = FakeRunner("fake:auto", limit=10)
    pinned_box = FakeRunner("fake:pinned", limit=10)
    use_runners(auto, pinned_box)
    sid = _study(client)["study_id"]
    ids = _post(client, configs=CONFIGS, seeds=[1, 2, 3], study_id=sid)["created"]

    queued = _act(client, "queue", ids=[ids[0]])
    assert queued["ok"] == [ids[0]] and len(auto.dispatched) == 1

    _ok(client.patch("/api/experiments/%s" % ids[1], json={"runtime": {"mode": "pinned", "slot": "fake:pinned"}}))
    ran = _act(client, "run_now", ids=[ids[1]])
    assert ran["ok"] == [ids[1]] and ran["placed"] == {ids[1]: "fake:pinned"}
    assert [a["experiment_id"] for a in pinned_box.dispatched] == [ids[1]]  # synchronously, on its pin

    ap = _ok(client.post("/api/studies/%s/autopilot" % sid, json={"enabled": True, "max_parallel": 2}))
    assert ap["autopilot"] == {"enabled": True, "max_parallel": 2}
    assert sorted(_in_flight(sid)) == sorted(ids[:2])  # already 2 in flight: nothing promoted

    rng = random.Random(7)
    peak = 0
    for tick in range(60):
        running = auto.in_flight() + pinned_box.in_flight()
        if running and rng.random() < 0.6:
            a = rng.choice(running)
            (auto if a["slot"] == auto.id else pinned_box).finish(a["attempt_id"])
            experiments._poll_in_flight_attempts()
        experiments._dispatch_tick()
        statuses = _statuses(client, sid)
        active = [e for e, s in statuses.items() if s in ("queued", "blocked", "dispatching", "running")]
        peak = max(peak, len(_in_flight(sid)))
        assert len(_in_flight(sid)) <= 2 and len(active) <= 2, (tick, statuses)
        if set(statuses.values()) == {"done"}:
            break
    assert set(_statuses(client, sid).values()) == {"done"} and peak == 2
    assert _ok(client.get("/api/studies/%s" % sid))["status"] == "complete"


def test_autopilot_picks_up_later_drafts_and_off_stops_promotion(client, use_runners):
    runner = FakeRunner(limit=10)
    use_runners(runner)
    sid = _study(client, autopilot={"enabled": True, "max_parallel": 1})["study_id"]
    first = _post(client, configs=["experiment/demo.yaml"], seeds=[1], study_id=sid)["created"][0]
    assert _statuses(client)[first] == "running"  # promoted on create, then dispatched
    later = _post(client, configs=["experiment/other.yaml"], seeds=[1], study_id=sid)["created"][0]
    assert _statuses(client)[later] == "draft"  # max_parallel=1 is taken
    _ok(client.post("/api/studies/%s/autopilot" % sid, json={"enabled": False}))
    _finish_all(runner)
    experiments._dispatch_tick()
    assert _statuses(client)[later] == "draft"  # off: no more promotion
    _ok(client.post("/api/studies/%s/autopilot" % sid, json={"enabled": True}))
    assert _statuses(client)[later] == "running"
    promoted = experiments._get_attempt(experiments.get_experiment(later)["current_attempt_id"])
    assert promoted["queued_by"] == "autopilot"


def test_hold_turns_autopilot_off_and_dequeues(client, use_runners):
    use_runners(FakeRunner(accept=False))
    sid = _study(client, autopilot={"enabled": True, "max_parallel": 3})["study_id"]
    ids = _post(client, configs=CONFIGS, seeds=[1], study_id=sid)["created"]
    assert set(_statuses(client, sid).values()) == {"blocked"}  # promoted, nothing free
    held = _ok(client.post("/api/studies/%s/autopilot" % sid, json={"hold": True}))
    assert held["autopilot"]["enabled"] is False and sorted(held["dequeued"]) == sorted(ids)
    assert set(_statuses(client, sid).values()) == {"draft"}
    assert all(experiments.get_experiment(i)["attempts"] == [] for i in ids)  # never-run attempts leave no trace


# ----------------------------------------------------------------- membership (U2)
def test_move_an_experiment_between_studies(client, use_runners):
    use_runners(FakeRunner(accept=False))
    a, b = _study(client, "A")["study_id"], _study(client, "B")["study_id"]
    eid = _post(client, configs=["experiment/demo.yaml"], seeds=[1], study_id=a, group="tiny")["created"][0]
    moved = _act(client, "move_to_study", study_id=a, params={"study_id": b})
    assert moved["ok"] == [eid]
    exp = _ok(client.get("/api/experiments/%s" % eid))
    assert [(m["study_id"], m["group"], m["name"]) for m in exp["studies"]] == [(b, "tiny", "B")]
    assert _ok(client.get("/api/studies/%s" % a))["experiment_count"] == 0
    # By ids with an explicit source; a non-member is skipped with its reason.
    back = _act(client, "move_to_study", ids=[eid], params={"study_id": a, "from_study_id": a})
    assert back["ok"] == [] and back["skipped"][0]["reason"] == "not in study '%s'" % a
    assert _act(client, "move_to_study", ids=[eid], params={"study_id": a})["ok"] == [eid]
    assert [m["study_id"] for m in _ok(client.get("/api/experiments/%s" % eid))["studies"]] == [a]


def test_share_a_baseline_between_two_studies(client, use_runners):
    use_runners(FakeRunner(accept=False))
    a, b = _study(client, "A", baseline={"config_path": "experiment/demo.yaml"})["study_id"], _study(client, "B")["study_id"]
    base = _post(client, configs=["experiment/demo.yaml"], seeds=[42], study_id=a)["created"][0]
    again = _post(client, configs=["experiment/demo.yaml"], seeds=[42], study_id=b)
    assert again["created"] == [] and again["matched"] == [base]  # one record, two studies
    assert {m["study_id"] for m in _ok(client.get("/api/experiments/%s" % base))["studies"]} == {a, b}
    assert _act(client, "add_to_study", ids=[base], params={"study_id": b, "group": "baseline"})["ok"] == [base]
    # Deleting a study removes memberships only.
    gone = _ok(client.delete("/api/studies/%s" % a))
    assert gone["memberships_removed"] == [base]
    exp = _ok(client.get("/api/experiments/%s" % base))
    assert [(m["study_id"], m["group"]) for m in exp["studies"]] == [(b, "baseline")]
    assert client.get("/api/studies/%s" % a).status_code == 404
    assert _statuses(client, "unfiled") == {}


# ----------------------------------------------------------------- identity v2 (X8)
def test_reposting_with_a_different_overlay_creates_a_new_id(client, use_runners):
    use_runners(FakeRunner(accept=False))
    plain = _post(client, configs=["experiment/demo.yaml"], seeds=[42])
    assert plain["created"] == ["demo_exp-s42"]
    o50 = _post(client, configs=["experiment/demo.yaml"], seeds=[42], overlay={"training.epochs": 50})
    o60 = _post(client, configs=["experiment/demo.yaml"], seeds=[42], overlay={"training": {"epochs": 60}})
    assert len({plain["created"][0], o50["created"][0], o60["created"][0]}) == 3
    assert o50["created"][0].startswith("demo_exp-s42-")
    assert o50["experiments"][0]["overlay"] == {"training.epochs": 50}
    same = _post(client, configs=["experiment/demo.yaml"], seeds=[42], overlay={"training": {"epochs": 50}})
    assert same["created"] == [] and same["matched"] == o50["created"]
    typo = client.post("/api/experiments", json={"configs": ["experiment/demo.yaml"], "seeds": [1],
                                                 "overlay": {"training.epochz": 1}})
    assert typo.status_code == 400 and "training.epochz" in typo.get_json()["detail"]


# ----------------------------------------------------------------- lifecycle + editability (§3.3.2)
def test_editability_follows_the_derived_status(client, use_runners):
    runner = FakeRunner(accept=False)
    use_runners(runner)
    eid = _post(client, configs=["experiment/demo.yaml"], seeds=[1])["created"][0]
    # Draft: identity edits re-key the draft.
    renamed = _ok(client.patch("/api/experiments/%s" % eid, json={"overlay": {"training.epochs": 9}, "notes": "n"}))
    new_id = renamed["experiment_id"]
    assert renamed["renamed_from"] == eid and new_id.startswith("demo_exp-s1-") and renamed["name"] == new_id
    assert client.get("/api/experiments/%s" % eid).status_code == 404
    # Queued (blocked here): policy yes, identity no.
    _act(client, "queue", ids=[new_id])
    assert _statuses(client)[new_id] == "blocked"
    assert client.patch("/api/experiments/%s" % new_id, json={"seed": 2}).status_code == 409
    ok = _ok(client.patch("/api/experiments/%s" % new_id, json={"priority": 5, "runtime": {"allow": ["fake"]}}))
    assert ok["priority"] == 5 and ok["runtime"]["allow"] == ["fake"]
    # Running: studies and notes only.
    runner.accept = True
    experiments._dispatch_tick()
    assert _statuses(client)[new_id] == "running"
    assert client.patch("/api/experiments/%s" % new_id, json={"runtime": {"mode": "pinned", "slot": "x"}}).status_code == 409
    assert _ok(client.patch("/api/experiments/%s" % new_id, json={"notes": "still going"}))["notes"] == "still going"
    # Terminal: identity frozen; duplicate is the way.
    _finish_all(runner)
    assert client.patch("/api/experiments/%s" % new_id, json={"overlay": {}}).status_code == 409
    dup = _act(client, "duplicate", ids=[new_id], params={"seed": 7})
    copy = dup["created"][new_id]
    assert _statuses(client)[copy] == "draft"
    assert _ok(client.get("/api/experiments/%s" % copy))["overlay"] == {"training.epochs": 9}
    assert _act(client, "duplicate", ids=[new_id])["skipped"][0]["existing"] == new_id


def test_dequeue_returns_to_draft_and_queue_ignores_finished(client, use_runners):
    runner = FakeRunner(limit=1)
    use_runners(runner)
    a, b = _post(client, configs=["experiment/demo.yaml"], seeds=[1, 2])["created"]
    _act(client, "queue", ids=[a, b])
    assert _statuses(client) == {a: "running", b: "blocked"}
    out = _act(client, "dequeue", ids=[a, b])
    assert out["ok"] == [b] and out["skipped"][0]["id"] == a
    assert _statuses(client)[b] == "draft"
    _finish_all(runner)
    again = _act(client, "queue", ids=[a])
    assert again["ok"] == [] and "retry" in again["skipped"][0]["reason"]
    assert _act(client, "queue", ids=[a], params={"rerun": True})["ok"] == [a]
    assert _act(client, "cancel", ids=[a, b])["skipped"][0]["id"] == b  # a draft has nothing to cancel


def test_the_legacy_pending_filter_and_composer_rerun_still_work(client, use_runners, monkeypatch):
    runner = FakeRunner(limit=1)
    use_runners(runner)
    body = {"configs": ["experiment/demo.yaml"], "seeds": [1, 2], "pool": "either", "then": "queue"}
    first = _post(client, **body)
    assert first["then"]["ok"] == first["created"]
    _finish_all(runner)
    experiments._dispatch_tick()
    _finish_all(runner)
    again = _post(client, **body)  # the Run Composer re-posting: finished ones get a fresh attempt
    assert again["matched"] == first["created"] and sorted(again["then"]["ok"]) == sorted(first["created"])
    # `status=pending` (the old word) still filters: it means queued.
    monkeypatch.setattr(experiments, "_dispatch_tick", lambda: None)
    q = _post(client, configs=["experiment/third.yaml"], seeds=[1], then="queue")["created"][0]
    listed = _ok(client.get("/api/experiments?status=pending"))["experiments"]
    assert [(e["experiment_id"], e["status"]) for e in listed] == [(q, "queued")]


# ----------------------------------------------------------------- run now (§6.2)
def test_run_now_never_queues_silently(client, use_runners):
    busy = FakeRunner("fake:busy", accept=False)
    use_runners(busy)
    eid = _post(client, configs=["experiment/demo.yaml"], seeds=[1])["created"][0]
    out = _act(client, "run_now", ids=[eid])
    assert out["ok"] == [] and out["skipped"][0]["can_queue"] is True
    assert out["skipped"][0]["runtimes"] == [{"runtime": "fake:busy", "code": "pool-busy", "detail": "fake says no"}]
    assert _statuses(client)[eid] == "draft" and experiments.get_experiment(eid)["attempts"] == []
    _ok(client.patch("/api/experiments/%s" % eid, json={"runtime": {"mode": "pinned", "slot": "fake:nowhere"}}))
    out = _act(client, "run_now", ids=[eid])
    assert out["skipped"][0]["code"] == "runtime-missing"


def test_run_now_goes_ahead_of_the_queue(client, use_runners, monkeypatch):
    runner = FakeRunner(limit=1)
    use_runners(runner)
    first, waiting, urgent = _post(client, configs=["experiment/demo.yaml"], seeds=[1, 2, 3])["created"]
    _act(client, "queue", ids=[first, waiting])
    assert _statuses(client) == {first: "running", waiting: "blocked", urgent: "draft"}
    # The slot frees up; before the next tick hands it to `waiting`, Run now claims it.
    tick = experiments._dispatch_tick
    monkeypatch.setattr(experiments, "_dispatch_tick", lambda: None)
    _finish_all(runner)
    out = _act(client, "run_now", ids=[urgent])
    assert out["ok"] == [urgent] and out["placed"] == {urgent: runner.id}
    tick()
    assert _statuses(client) == {first: "done", waiting: "blocked", urgent: "running"}
    # A queued experiment that can't run now keeps its place in the queue.
    out = _act(client, "run_now", ids=[waiting])
    assert out["skipped"][0]["runtimes"][0]["code"] == "pool-busy" and _statuses(client)[waiting] == "blocked"


def test_a_launch_time_refusal_takes_run_nows_attempt_back_out(client, use_runners):
    from backend.runners.base import RunnerBlocked
    runner = FakeRunner()
    runner.dispatch_error = RunnerBlocked({"code": "code-not-pushed", "detail": "push first"})
    use_runners(runner)
    eid = _post(client, configs=["experiment/demo.yaml"], seeds=[1])["created"][0]
    out = _act(client, "run_now", ids=[eid])
    assert out["skipped"][0]["runtimes"][0]["code"] == "code-not-pushed"
    assert _statuses(client)[eid] == "draft"


# ----------------------------------------------------------------- runtime policy (§6.3)
def test_pinned_never_falls_back(client, use_runners):
    free = FakeRunner("fake:free")
    full = FakeRunner("fake:full", accept=False)
    use_runners(free, full)
    eid = _post(client, configs=["experiment/demo.yaml"], seeds=[1], runtime={"mode": "pinned", "slot": "fake:full"},
                then="queue")["created"][0]
    exp = _ok(client.get("/api/experiments/%s" % eid))
    assert exp["status"] == "blocked" and exp["current_attempt"]["blocked"]["code"] == "pool-busy"
    assert free.dispatched == []


def test_requires_min_vram_is_matched_and_unknown_never_satisfies(client, use_runners):
    small = FakeRunner("fake:small", vram_gb=16)
    unknown = FakeRunner("fake:unknown")
    big = FakeRunner("fake:big", vram_gb=48)
    use_runners(small, unknown)
    policy = {"mode": "auto", "allow": ["*"], "requires": {"min_vram_gb": 24}}
    eid = _post(client, configs=["experiment/demo.yaml"], seeds=[1], runtime=policy, then="queue")["created"][0]
    blocked = _ok(client.get("/api/experiments/%s" % eid))["current_attempt"]["blocked"]
    assert blocked["code"] == "requires-unmet"
    use_runners(small, unknown, big)
    experiments._dispatch_tick()
    assert [a["experiment_id"] for a in big.dispatched] == [eid]


def test_priority_orders_the_queue(client, use_runners):
    runner = FakeRunner(accept=False)
    use_runners(runner)
    hi = _study(client, "hi", priority=5)["study_id"]
    low, mid, top, auto = _post(client, configs=["experiment/demo.yaml"], seeds=[1, 2, 3, 4])["created"]
    _ok(client.patch("/api/experiments/%s" % mid, json={"priority": 3}))
    _act(client, "add_to_study", ids=[top], params={"study_id": hi})
    _act(client, "queue", ids=[low, mid, top])
    _ok(client.post("/api/studies/%s/autopilot" % hi, json={"enabled": True, "max_parallel": 5}))
    _act(client, "add_to_study", ids=[auto], params={"study_id": hi})  # promoted by autopilot, not a user
    order = []
    runner.accept, runner.limit = True, 1
    for _ in range(4):
        experiments._dispatch_tick()
        order += [a["experiment_id"] for a in runner.in_flight()]
        _finish_all(runner)
    # manual first; among manual: study priority, then experiment priority.
    assert order == [top, mid, low, auto]


# ----------------------------------------------------------------- scopes + preflight
def test_actions_take_exactly_one_scope_and_report_per_id(client, use_runners):
    use_runners(FakeRunner(accept=False))
    ids = _post(client, configs=CONFIGS, seeds=[1])["created"]
    r = client.post("/api/experiments/actions", json={"action": "queue", "ids": ids, "study_id": "st_x"})
    assert r.status_code == 400
    out = _act(client, "set_priority", filter={"config": "experiment/other.yaml"}, params={"priority": 2})
    assert out["ok"] == ["other_exp-s1"]
    out = _act(client, "queue", ids=ids + ["nope-s1"])
    assert sorted(out["ok"]) == sorted(ids) and out["skipped"] == [{"id": "nope-s1", "reason": "unknown experiment"}]
    assert client.post("/api/experiments/actions", json={"action": "explode", "ids": ids}).status_code == 400
    out = _act(client, "delete", filter={"q": "third"})
    assert out["ok"] == ["third_exp-s1"] and "third_exp-s1" not in _statuses(client)


def test_preflight_matrix_for_ids_and_specs(client, use_runners):
    ok_box = FakeRunner("fake:ok", vram_gb=24)
    full = FakeRunner("fake:full", accept=False)
    use_runners(ok_box, full)
    eid = _post(client, configs=["experiment/demo.yaml"], seeds=[1], runtime={"allow": ["fake:full"]})["created"][0]
    m = _ok(client.post("/api/experiments/preflight", json={"ids": [eid, "missing-s1"]}))
    assert [r["id"] for r in m["runtimes"]] == ["fake:ok", "fake:full"] and m["missing"] == ["missing-s1"]
    row = m["rows"][0]
    assert row["exists"] and row["status"] == "draft" and row["best"] is None
    assert row["cells"]["fake:ok"]["allowed"] is False and row["cells"]["fake:ok"]["ok"] is True
    assert row["cells"]["fake:full"] == {
        "allowed": True, "ok": False, "code": "pool-busy", "detail": "fake says no",
        "data": {
            "mode": None, "code": "no-dataset-binding", "dataset": "demo",
            "detail": "'demo' has no binding for fake:full and no default source to fall back to",
        },
    }
    assert set(row["estimate"]) >= {"hours", "tier"}
    spec = {"config_path": "experiment/demo.yaml", "seed": 9, "overlay": {"training.epochs": 3}}
    m = _ok(client.post("/api/experiments/preflight", json={"specs": [spec]}))
    assert m["rows"][0]["exists"] is False and m["rows"][0]["best"] == "fake:ok"
    assert m["rows"][0]["experiment_id"] == experiments.experiment_id_for("experiment/demo.yaml", 9, {"training.epochs": 3})
    assert experiments.list_experiments() and len(experiments.list_experiments()) == 1  # a pure read


# ----------------------------------------------------------------- studies CRUD, status, compare
def test_study_crud_and_derived_status(client, use_runners):
    runner = FakeRunner(accept=False)
    use_runners(runner)
    sid = _study(client, question="Does size matter?", tags=["ablation"], primary_metric="hd95")["study_id"]
    got = _ok(client.get("/api/studies/%s" % sid))
    assert got["primary_metric"] == {"key": "hd95", "direction": "min"} and got["tags"] == ["ablation"]
    assert _ok(client.patch("/api/studies/%s" % sid, json={"priority": 3, "defaults": {"seeds": [7, 42]}}))["priority"] == 3
    assert client.patch("/api/studies/%s" % sid, json={"study_id": "x"}).status_code == 400
    ids = _post(client, configs=["experiment/demo.yaml"], seeds=[1, 2], study_id=sid, then="queue")["created"]
    # Waiting for a free slot (pool-busy) is not "attention".
    assert _ok(client.get("/api/studies/%s" % sid))["status"] == "running"
    runner.accept = True
    experiments._dispatch_tick()
    runner.finish(runner.in_flight()[0]["attempt_id"], succeeded=False)
    experiments._poll_in_flight_attempts()
    s = _ok(client.get("/api/studies/%s" % sid))
    assert s["status"] in ("attention", "running")
    pulse = _ok(client.get("/api/pulse"))
    assert [p["study_id"] for p in pulse["studies"]] == [sid] and "draft_count" in pulse
    assert _ok(client.patch("/api/studies/%s" % sid, json={"archived": True}))["status"] == "archived"
    assert [s["study_id"] for s in _ok(client.get("/api/studies?archived=0"))["studies"]] == []


def test_compare_aggregates_seeds_from_eval_reports(client, use_runners):
    runner = FakeRunner()
    use_runners(runner)
    sid = _study(client, primary_metric={"key": "dice"}, baseline={"config_path": "experiment/other.yaml"})["study_id"]
    ids = _post(client, configs=["experiment/demo.yaml", "experiment/other.yaml"], seeds=[1, 2], study_id=sid,
                then="queue")["created"]
    dice = {"demo_exp-s1": 0.80, "demo_exp-s2": 0.84, "other_exp-s1": 0.70, "other_exp-s2": 0.74}
    for a in runner.in_flight():
        plan = a["run"]
        make_run_dir(HOST, plan["run_dir"], plan["run_ids"][0], status="done")
        report = HOST / plan["run_dir"] / "eval" / "report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({"metrics": {"dice": dice[a["experiment_id"]], "hd95": 3.0, "name": "x"}}))
    _finish_all(runner)
    out = _ok(client.get("/api/studies/%s/compare" % sid))
    assert out["metrics"][0] == "dice" and out["lower_is_better"] == ["hd95"]
    by_cfg = {g["config_path"]: g for g in out["groups"]}
    demo = by_cfg["experiment/demo.yaml"]["metrics"]["dice"]
    assert demo["n"] == 2 and demo["mean"] == pytest.approx(0.82) and demo["std"] == pytest.approx(0.028284, abs=1e-5)
    assert demo["delta_vs_baseline"] == pytest.approx(0.10)
    assert by_cfg["experiment/other.yaml"]["baseline"] is True
    only = _ok(client.get("/api/studies/%s/compare?metrics=hd95&group_by=experiment" % sid))
    assert only["metrics"] == ["hd95"] and len(only["groups"]) == len(ids)


def test_compare_ids_is_the_ad_hoc_twin_of_study_compare(client, use_runners):
    """XDASH_PLAN.md §8.2 Phase 6: 'Compare selected' from any bulk-bar
    selection, across studies (or none at all) — POST /api/experiments/compare
    with a bare id list produces the same shape compare() does, no study_id
    required, and reports back any id that didn't resolve."""
    runner = FakeRunner()
    use_runners(runner)
    ids = _post(client, configs=["experiment/demo.yaml", "experiment/other.yaml"], seeds=[1, 2],
                then="queue")["created"]
    dice = {"demo_exp-s1": 0.80, "demo_exp-s2": 0.84, "other_exp-s1": 0.70, "other_exp-s2": 0.74}
    for a in runner.in_flight():
        plan = a["run"]
        make_run_dir(HOST, plan["run_dir"], plan["run_ids"][0], status="done")
        report = HOST / plan["run_dir"] / "eval" / "report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({"metrics": {"dice": dice[a["experiment_id"]]}}))
    _finish_all(runner)

    out = _ok(client.post("/api/experiments/compare", json={
        "ids": ids + ["nonexistent-id"], "baseline_config_path": "experiment/other.yaml",
    }))
    assert "study_id" not in out
    assert out["missing_ids"] == ["nonexistent-id"]
    by_cfg = {g["config_path"]: g for g in out["groups"]}
    assert by_cfg["experiment/demo.yaml"]["metrics"]["dice"]["n"] == 2
    assert by_cfg["experiment/demo.yaml"]["metrics"]["dice"]["delta_vs_baseline"] == pytest.approx(0.10)
    assert by_cfg["experiment/other.yaml"]["baseline"] is True

    assert client.post("/api/experiments/compare", json={"ids": []}).status_code == 400


# ----------------------------------------------------------------- XDASH_PLAN.md §8.1 Lab: best-metric-so-far / ETA
def test_study_summary_reports_best_metric_so_far(client, use_runners):
    runner = FakeRunner()
    use_runners(runner)
    sid = _study(client, primary_metric={"key": "dice"})["study_id"]
    _post(client, configs=["experiment/demo.yaml"], seeds=[1, 2], study_id=sid, then="queue")
    dice = {"demo_exp-s1": 0.80, "demo_exp-s2": 0.91}
    for a in runner.in_flight():
        plan = a["run"]
        make_run_dir(HOST, plan["run_dir"], plan["run_ids"][0], status="done")
        report = HOST / plan["run_dir"] / "eval" / "report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({"metrics": {"dice": dice[a["experiment_id"]]}}))
    _finish_all(runner)
    summary = _ok(client.get("/api/studies/%s" % sid))
    assert summary["best_metric"] == {"key": "dice", "value": 0.91, "experiment_id": "demo_exp-s2"}
    pulse_summary = next(s for s in _ok(client.get("/api/pulse"))["studies"] if s["study_id"] == sid)
    assert pulse_summary["best_metric"]["experiment_id"] == "demo_exp-s2"


def test_study_summary_best_metric_prefers_a_lower_value_when_lower_is_better(client, use_runners):
    runner = FakeRunner()
    use_runners(runner)
    sid = _study(client, primary_metric={"key": "hd95"})["study_id"]
    _post(client, configs=["experiment/demo.yaml"], seeds=[1, 2], study_id=sid, then="queue")
    hd95 = {"demo_exp-s1": 5.0, "demo_exp-s2": 2.0}
    for a in runner.in_flight():
        plan = a["run"]
        make_run_dir(HOST, plan["run_dir"], plan["run_ids"][0], status="done")
        report = HOST / plan["run_dir"] / "eval" / "report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({"metrics": {"hd95": hd95[a["experiment_id"]]}}))
    _finish_all(runner)
    summary = _ok(client.get("/api/studies/%s" % sid))
    assert summary["best_metric"] == {"key": "hd95", "value": 2.0, "experiment_id": "demo_exp-s2"}


def test_study_summary_has_no_best_metric_before_anything_finishes(client, use_runners):
    runner = FakeRunner()
    use_runners(runner)
    sid = _study(client, primary_metric={"key": "dice"})["study_id"]
    _post(client, configs=["experiment/demo.yaml"], seeds=[1], study_id=sid, then="queue")
    summary = _ok(client.get("/api/studies/%s" % sid))
    assert summary["best_metric"] is None


def test_study_summary_eta_is_the_latest_running_members_projected_finish(client, use_runners):
    runner = FakeRunner(limit=10)
    use_runners(runner)
    sid = _study(client)["study_id"]
    _post(client, configs=["experiment/demo.yaml"], seeds=[1], study_id=sid, then="queue")
    summary = _ok(client.get("/api/studies/%s" % sid))
    assert summary["eta"] is not None  # a running member always has a started_at


def test_study_summary_eta_is_none_with_nothing_running(client, use_runners):
    runner = FakeRunner(accept=False)
    use_runners(runner)
    sid = _study(client)["study_id"]
    _post(client, configs=["experiment/demo.yaml"], seeds=[1], study_id=sid, then="queue")
    summary = _ok(client.get("/api/studies/%s" % sid))
    assert summary["eta"] is None
