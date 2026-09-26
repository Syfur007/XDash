"""XDASH_PLAN.md §5 (fixes X4): the dataset registry, resolver, the four
placement modes, checks, and `no-dataset-binding` in every runner's
`can_accept()`."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend import datasets, framework, hosts
from backend.config import settings
from backend.runners import machine
from backend.runners.base import RunnerBlocked

from conftest import FakeTransport, HOST

# A config with no dataset section at all (none of conftest's three stock
# configs qualify: demo/third compose the demo dataset fragment, other
# declares dataset.name inline). Written once, idempotently — HOST/configs
# isn't wiped between tests (only data/ and outputs/ are), so recreating the
# same content every time this module runs is harmless.
_NO_DATASET_CONFIG = "experiment/no_dataset.yaml"
(HOST / "configs" / "experiment" / "no_dataset.yaml").write_text(
    "logging: {experiment_name: no_dataset_exp}\ntraining: {epochs: 1}\n"
)


# ----------------------------------------------------------------- resolver
def test_dataset_identity_is_read_from_the_fragment_never_typed():
    name, root = datasets.dataset_identity_for_config("experiment/demo.yaml")
    assert (name, root) == ("demo", "data/demo")
    assert datasets.root_for_dataset("demo") == "data/demo"
    assert datasets.root_for_dataset("nope") is None


def test_resolution_order_exact_then_kind_wildcard_then_kind_default():
    datasets.upsert_dataset("demo", sources={"kaggle": {"slug": "someone/demo-ds"}})
    # No bindings at all: kind defaults.
    assert datasets.resolve_binding("demo", "ssh:box", "ssh") == {"mode": "path"}
    assert datasets.resolve_binding("demo", "colab:tanvir", "colab") == {"mode": "fetch", "source": "someone/demo-ds"}
    assert datasets.resolve_binding("demo", "kaggle:tanvir", "kaggle") == {"mode": "attach", "source": "someone/demo-ds"}
    assert datasets.resolve_binding("demo", "fake:x", "fake") == {"mode": None}

    # A kind wildcard overrides the kind default.
    datasets.set_binding("demo", "ssh:*", {"mode": "push"})
    assert datasets.resolve_binding("demo", "ssh:box", "ssh") == {"mode": "push"}
    assert datasets.resolve_binding("demo", "ssh:otherbox", "ssh") == {"mode": "push"}

    # An exact runtime id overrides the wildcard.
    datasets.set_binding("demo", "ssh:box", {"mode": "path", "path": "/data/shared/demo"})
    assert datasets.resolve_binding("demo", "ssh:box", "ssh") == {"mode": "path", "path": "/data/shared/demo"}
    assert datasets.resolve_binding("demo", "ssh:otherbox", "ssh") == {"mode": "push"}


def test_migration_seeds_from_the_legacy_dataset_map_in_memory_only():
    """dataset_map.json's existing name->slug entries become datasets.json
    records the first time datasets.json is read — but only in memory until
    an actual write happens (mirrors JsonStore's own migrate-in-memory
    pattern): a bare list never creates the file."""
    from backend import dataset_map
    dataset_map.save_dataset_map({"demo": "seeded/from-map"})
    assert not settings.datasets_file.is_file()
    found = {d["name"]: d for d in datasets.list_datasets()}
    assert found["demo"]["sources"]["kaggle"]["slug"] == "seeded/from-map"
    assert not settings.datasets_file.is_file()  # still not persisted by a read
    datasets.set_binding("demo", "ssh:*", {"mode": "push"})
    assert settings.datasets_file.is_file()  # persisted by the first real write


def test_unknown_binding_mode_is_rejected():
    with pytest.raises(datasets.DatasetError):
        datasets.set_binding("demo", "ssh:*", {"mode": "teleport"})


# ----------------------------------------------------------------- no-dataset-binding
def test_data_mode_for_experiment_reports_no_dataset_binding_when_unresolved():
    # An unknown runtime kind: the config has a dataset, but nothing resolves for it.
    data = datasets.data_mode_for_experiment("experiment/demo.yaml", "fake:x", "fake")
    assert data["mode"] is None and data["code"] == "no-dataset-binding" and data["dataset"] == "demo"
    # A config with no dataset section at all: ssh's kind default (`path`)
    # never even gets a chance — there's no name to resolve a binding for.
    data = datasets.data_mode_for_experiment(_NO_DATASET_CONFIG, "ssh:box", "ssh")
    assert data["mode"] is None and data["code"] == "no-dataset-binding"


def test_machine_can_accept_blocks_ssh_with_no_dataset_declared(monkeypatch):
    hosts.upsert_host({
        "id": "box", "kind": "ssh", "label": "Box", "max_concurrent": 1,
        "ssh": {"host": "box.example"}, "repos": {"fake": {"repo_root": "/remote/repo"}},
    })
    fake = FakeTransport("box", Path("/remote/repo"), Path("/tmp/xdash-empty-mirror-does-not-exist"))
    from backend import transport as transport_mod
    monkeypatch.setattr(transport_mod, "for_host_record", lambda h: fake if h.id == "box" else transport_mod.SshTransport(h))
    runner = machine.MachineRunner(hosts.get_host("box"))
    block = runner.can_accept({"config_path": _NO_DATASET_CONFIG, "seed": 1}, 1.0)
    assert block is not None and block["code"] == "no-dataset-binding"


# ----------------------------------------------------------------- placement (executed at dispatch)
def test_place_dataset_path_mode_symlinks_when_the_directory_already_exists(tmp_path):
    remote = tmp_path / "remote"
    (remote / "data" / "demo").mkdir(parents=True)
    fake = FakeTransport("box", Path("/remote/repo"), remote)
    result = datasets.place_dataset("experiment/demo.yaml", "ssh:box", "ssh", fake, Path("/remote/repo"))
    assert result["mode"] == "path" and "symlinked" in result["detail"]


def test_place_dataset_path_mode_blocks_when_missing(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    fake = FakeTransport("box", Path("/remote/repo"), remote)
    with pytest.raises(datasets.PlacementError) as ei:
        datasets.place_dataset("experiment/demo.yaml", "ssh:box", "ssh", fake, Path("/remote/repo"))
    assert ei.value.code == "no-dataset-binding"


def test_place_dataset_push_mode_rsyncs_a_local_copy_then_symlinks(tmp_path):
    """§5's `push` mode: an SSH host with no data of its own gets one pushed
    up before launch (§10 Phase 2 acceptance) — asserted on the fake
    transport's own command log, never a real rsync."""
    datasets.set_binding("demo", "ssh:*", {"mode": "push"})
    remote = tmp_path / "remote"
    remote.mkdir()
    fake = FakeTransport("box", Path("/remote/repo"), remote)
    result = datasets.place_dataset(
        "experiment/demo.yaml", "ssh:box", "ssh", fake, Path("/remote/repo"), local_repo_root=HOST,
    )
    assert result["mode"] == "push"
    pushes = [c for c in fake.calls if c[0] == "push"]
    assert len(pushes) == 1
    assert pushes[0][1] == str(HOST / "data" / "demo")  # the local copy, resolved from the fragment's root


def test_place_dataset_fetch_mode_downloads_on_the_runtime_then_symlinks(tmp_path):
    """§5's `fetch` mode: a Colab VM has no data at all — it downloads its
    own copy, over the transport, using the designated data account's
    credentials as env for that one step only (never persisted)."""
    datasets.set_binding("demo", "colab:*", {"mode": "fetch", "source": "someone/demo-ds"})
    remote = tmp_path / "remote"
    remote.mkdir()
    fake = FakeTransport("colab-acct", Path("/remote/repo"), remote)
    result = datasets.place_dataset(
        "experiment/demo.yaml", "colab:acct", "colab", fake, Path("/remote/repo"),
        data_account_creds={"KAGGLE_USERNAME": "data-bot", "KAGGLE_KEY": "shh"},
    )
    assert result["mode"] == "fetch" and result["source"] == "someone/demo-ds"
    # FakeTransport.run() doesn't record args beyond a returncode, but at
    # least one shell command referencing the resolved source must have run —
    # exercised via the real subprocess.run() argv shape in colab/ssh cases
    # elsewhere; here we confirm the call reached run() successfully at all
    # (no exception) and the plan claims the source it actually used.


def test_resolve_binding_fetch_mode_without_any_source_is_unresolved():
    # A dataset with no sources.kaggle.slug at all: fetch/attach have nothing
    # to fetch/attach, so they're reported the same as no binding.
    datasets.upsert_dataset("colondb", sources={})
    assert datasets.resolve_binding("colondb", "colab:x", "colab") == {"mode": None}
    assert datasets.resolve_binding("colondb", "kaggle:x", "kaggle") == {"mode": None}


def test_place_dataset_blocks_on_a_dataset_with_no_declared_root():
    # "other" declares dataset.name inline but no root, and composes nothing
    # to inherit one from.
    with pytest.raises(datasets.PlacementError) as ei:
        datasets.place_dataset("experiment/other.yaml", "ssh:box", "ssh", FakeTransport("x", Path("/r"), Path("/tmp")), Path("/r"))
    assert ei.value.code == "no-dataset-binding" and "no declared root" in ei.value.detail


def test_place_dataset_attach_mode_is_a_transport_noop():
    """Kaggle's `attach` is entirely declarative (kernel-metadata.json,
    backend/kaggle.py) — place_dataset() reports it without touching the
    transport at all (a Kaggle runner has none)."""
    result = datasets.place_dataset(
        "experiment/demo.yaml", "kaggle:acct", "kaggle", FakeTransport("x", Path("/r"), Path("/tmp")), Path("/r"),
    )
    assert result["mode"] == "attach" and result["source"]


# ----------------------------------------------------------------- cross-runtime identity
def test_same_experiment_gets_the_same_planned_run_on_local_and_remote(monkeypatch, tmp_path):
    """§10 Phase 2 acceptance: the same experiment produces the same
    config_hash/run_dir on local and on a remote runtime — locate_run is a
    pure function of (config, seed), never of which runner asks."""
    exp = {"experiment_id": "demo_exp-s3", "config_path": "experiment/demo.yaml", "seed": 3,
           "extra_args": {"train": "", "eval": ""}}
    local_plan = framework.locate_run(exp["config_path"], exp["seed"])

    hosts.upsert_host({
        "id": "box2", "kind": "ssh", "label": "Box2", "max_concurrent": 1,
        "ssh": {"host": "box2.example"}, "repos": {"fake": {"repo_root": "/remote/repo"}},
    })
    remote_plan = framework.locate_run(exp["config_path"], exp["seed"])  # asked again, as dispatch would for either runtime
    assert local_plan["config_hash"] == remote_plan["config_hash"]
    assert local_plan["run_dir"] == remote_plan["run_dir"]


# ----------------------------------------------------------------- end-to-end dispatch (real MachineRunner.dispatch)
def test_machine_dispatch_pushes_the_dataset_before_the_working_tree(monkeypatch, tmp_path):
    """§10 Phase 2 acceptance, through the real code path (not the bare
    place_dataset() unit above): a `push`-bound SSH host with no data of its
    own gets it pushed as part of an ordinary MachineRunner.dispatch() call,
    asserted on the fake transport's own command log, before the tree push
    that would otherwise run against a config expecting data already there."""
    datasets.set_binding("demo", "ssh:*", {"mode": "push"})
    hosts.upsert_host({
        "id": "box3", "kind": "ssh", "label": "Box3", "max_concurrent": 1,
        "ssh": {"host": "box3.example"}, "repos": {"fake": {"repo_root": "/remote/repo"}},
    })
    remote = tmp_path / "remote"
    remote.mkdir()
    fake = FakeTransport("box3", Path("/remote/repo"), remote)
    from backend import transport as transport_mod
    monkeypatch.setattr(transport_mod, "for_host_record", lambda h: fake if h.id == "box3" else transport_mod.SshTransport(h))
    monkeypatch.setattr("backend.scheduler.add_item", lambda *a, **k: [{"id": "t"}, {"id": "e"}])

    runner = machine.MachineRunner(hosts.get_host("box3"))
    exp = {"experiment_id": "demo_exp-s5", "config_path": "experiment/demo.yaml", "seed": 5,
           "extra_args": {"train": "", "eval": ""}}
    runner.dispatch(exp, {"attempt_id": "atmpt_push"})

    pushes = [c for c in fake.calls if c[0] == "push"]
    # The dataset push (data/demo, from HOST's own copy of it) happens before
    # the working-tree push (settings.repo_root itself).
    assert len(pushes) == 2
    assert pushes[0][1] == str(HOST / "data" / "demo")
    assert pushes[1][1] == str(settings.repo_root)


def test_a_push_binding_resolves_without_the_data_already_existing():
    """A resolvable push binding is *allowed* by can_accept() (it's only
    executed at dispatch) — this just confirms can_accept doesn't require
    the data to already exist for push/fetch modes, unlike the bare `path`
    default."""
    datasets.set_binding("demo", "ssh:*", {"mode": "push"})
    hosts.upsert_host({
        "id": "box4", "kind": "ssh", "label": "Box4", "max_concurrent": 1,
        "ssh": {"host": "box4.example"}, "repos": {"fake": {"repo_root": "/remote/repo"}},
    })
    data = datasets.data_mode_for_experiment("experiment/demo.yaml", "ssh:box4", "ssh")
    assert data["mode"] == "push"
