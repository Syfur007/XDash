"""The Flask app against the temp world: smoke of the routes Lab polls,
item 8 (the retired launch route), and fail-loud stores over HTTP."""
from __future__ import annotations

import pytest

from conftest import FakeRunner


@pytest.fixture
def client():
    import server
    return server.app.test_client()


def test_pulse_and_experiments_answer(client):
    r = client.get("/api/pulse")
    assert r.status_code == 200 and {"slots", "running", "blocked", "recent"} <= set(r.get_json())
    r = client.get("/api/experiments")
    assert r.status_code == 200 and r.get_json() == {"experiments": []}


def test_runner_launch_route_is_retired(client):
    r = client.post("/api/runners/local/launch", json={"config_path": "experiment/demo.yaml"})
    assert r.status_code in (404, 405)
    import server
    assert not [rule for rule in server.app.url_map.iter_rules() if rule.rule.endswith("/launch")]


def test_create_experiment_over_http_splits_extra_args(client, use_runners):
    use_runners(FakeRunner(accept=False))
    # The Run Composer's own request shape: flat configs + seeds, legacy pool, then: "queue".
    r = client.post("/api/experiments", json={"configs": ["experiment/demo.yaml"], "seeds": [42], "extra_args": "--epochs 5",
                                              "pool": "either", "then": "queue"})
    assert r.status_code == 200, r.get_json()
    exp = r.get_json()["experiments"][0]
    assert exp["experiment_id"] == "demo_exp-s42"
    assert exp["extra_args"] == {"train": "--epochs 5", "eval": ""}
    assert exp["status"] == "blocked"
    r = client.post("/api/experiments", json={"configs": ["experiment/other.yaml"], "seeds": [7],
                                              "extra_args": {"train": "", "eval": "--no-vis"}})
    assert r.get_json()["experiments"][0]["extra_args"] == {"train": "", "eval": "--no-vis"}


def test_a_corrupt_store_is_a_loud_500_not_an_empty_list(client, settings):
    settings.experiments_store_file.write_text('{"experiments": {"a"')
    r = client.get("/api/experiments")
    assert r.status_code == 500
    assert "experiments.json" in r.get_json()["detail"]


# ----------------------------------------------------------------- Phase 2: /api/runtimes, /api/pulse, datasets
def test_runtimes_lists_every_runtime_kind_in_the_unified_shape(client, use_runners):
    """XDASH_PLAN.md §3.6, closes X10: /api/runtimes covers every kind, not
    just local + kaggle, and every row has the §3.6 shape."""
    use_runners(FakeRunner("ssh:box", kind="ssh"), FakeRunner("colab:acct", kind="colab", volatile=True))
    r = client.get("/api/runtimes")
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.get_json()["runtimes"]}
    assert {"ssh:box", "colab:acct"} <= set(rows)
    for row in rows.values():
        assert {"id", "kind", "label", "state", "accelerator", "capacity", "quota", "capabilities", "running"} <= set(row)


def test_runners_alias_still_answers_but_slots_is_retired(client):
    """§7's retirement note: /api/runners stays as a thin alias (still
    called by static/js/views/compute.js). The standalone /api/slots route
    was retired in Phase 6 — its only caller (the old flat Run Composer,
    spine.js) was itself retired in Phase 3, and nothing else ever called
    it. `list_slots()` itself is unchanged and still backs /api/pulse."""
    assert client.get("/api/runners").status_code == 200
    assert client.get("/api/slots").status_code == 404


def test_pulse_includes_the_unified_runtimes_list(client, use_runners):
    use_runners(FakeRunner("ssh:box", kind="ssh"))
    r = client.get("/api/pulse")
    body = r.get_json()
    assert any(row["id"] == "ssh:box" for row in body["runtimes"])


def test_dataset_registry_routes(client):
    r = client.get("/api/datasets")
    assert r.status_code == 200
    names = {d["name"] for d in r.get_json()["datasets"]}
    assert "demo" in names

    r = client.patch("/api/datasets/demo", json={"sources": {"kaggle": {"slug": "someone/demo-ds"}}})
    assert r.status_code == 200
    assert r.get_json()["sources"]["kaggle"]["slug"] == "someone/demo-ds"

    r = client.patch("/api/datasets/demo", json={"sources": {"kaggle": {"slug": "a/b/c"}}})
    assert r.status_code == 400

    r = client.put("/api/datasets/data-account", json={"name": "acct1"})
    assert r.status_code == 200 and r.get_json()["data_account"] == "acct1"

    r = client.post("/api/datasets", json={"name": "New-Draft"})
    assert r.status_code == 200 and r.get_json()["identity_source"] == "draft"
    r = client.post("/api/datasets", json={"name": "New-Draft"})
    assert r.status_code == 409
    # "demo" already has a record from the PATCH above, so it's not a good
    # "unrecorded fragment" case any more — ColonDB still is.
    r = client.post("/api/datasets", json={"name": "ColonDB"})
    assert r.status_code == 200 and r.get_json() == {"exists": "fragment", "name": "ColonDB"}

    r = client.delete("/api/datasets/New-Draft")
    assert r.status_code == 200
    r = client.get("/api/datasets/New-Draft")
    assert r.status_code == 404


def test_dataset_check_route_runs_synchronously_over_a_real_shell(client, tmp_path, monkeypatch):
    from backend import hosts, transport as transport_mod
    from conftest import ShellTransport

    hosts.upsert_host({
        "id": "checkbox", "kind": "ssh", "label": "Box", "max_concurrent": 1,
        "ssh": {"host": "box.example"}, "repos": {"fake": {"repo_root": str(tmp_path / "remote")}},
    })
    target = tmp_path / "remote" / "data" / "demo"
    target.mkdir(parents=True)
    (target / "f.txt").write_text("x\n")

    shell = ShellTransport(tmp_path / "remote", host_id="checkbox")
    monkeypatch.setattr(transport_mod, "for_host_record", lambda h: shell if h.id == "checkbox" else transport_mod.SshTransport(h))

    r = client.post("/api/datasets/demo/check", json={"targets": ["ssh:checkbox"]})
    assert r.status_code == 200
    assert r.get_json()["checks"]["ssh:checkbox"]["state"] == "ready"

    r = client.get("/api/datasets/demo")
    assert r.get_json()["checks"]["ssh:checkbox"]["state"] == "ready"

    r = client.post("/api/datasets/unknown-dataset-xyz/check")
    assert r.status_code in (200, 404)  # a never-declared name has nothing to check, not a crash
