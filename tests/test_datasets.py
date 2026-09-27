"""DATASETS_PLAN.md — the dataset registry, `plan_delivery()`, placement
(§4.4), `locate_root()` (§4.5), checks (§5) and the v1->v2 migration (§10).
Replaces the old bindings-registry tests (§11/§12 D1 item 10) — this suite
is what actually executes `sh` (`ShellTransport`) or records real argument
lists (`FakeTransport.run_calls`), where the pre-D1 fakes did neither."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend import datasets, framework, hosts
from backend.config import settings
from backend.runners import kaggle as kaggle_runner
from backend.runners import machine

from conftest import FakeTransport, ShellTransport, HOST, XDASH_ROOT

FIXTURES = XDASH_ROOT / "tests" / "fixtures"

# A config with no dataset section at all.
_NO_DATASET_CONFIG = "experiment/no_dataset.yaml"
(HOST / "configs" / "experiment" / "no_dataset.yaml").write_text(
    "logging: {experiment_name: no_dataset_exp}\ntraining: {epochs: 1}\n"
)

# A real, populated local copy of the "demo" dataset — plan_delivery()
# requires an actual directory before it will call a copy "will-transfer";
# this persists for the whole session (HOST/configs and HOST/data are never
# wiped between tests, only HOST/outputs and HOST/.xdash — see conftest's
# `world` fixture).
_DEMO_DATA_DIR = HOST / "data" / "demo"
_DEMO_DATA_DIR.mkdir(parents=True, exist_ok=True)
(_DEMO_DATA_DIR / "sample.txt").write_text("hello\n")

# A second, ColonDB-shaped fragment (DS3's own regression) — the real dissert
# profile isn't available in this harness (see conftest's module docstring),
# so this stands in for it: a fragment with a declared root but no local
# copy, historically blocked everywhere except a `path` SSH binding.
(HOST / "configs" / "dataset" / "colondb.yaml").write_text(
    "dataset: {name: ColonDB, root: data/polyp/ColonDB}\n"
)


def _stub_kaggle_run(tmp_path, stdout="", exit_code=0):
    script = tmp_path / "stub_kaggle.py"
    script.write_text("import sys\nsys.stdout.write(%r)\nsys.exit(%d)\n" % (stdout, exit_code))

    def run(args, account_name, timeout=None):
        return subprocess.run([sys.executable, str(script)] + list(args), capture_output=True, text=True, timeout=timeout)

    return run


# ----------------------------------------------------------------- identity, never typed
def test_dataset_identity_is_read_from_the_fragment_never_typed():
    name, root = datasets.dataset_identity_for_config("experiment/demo.yaml")
    assert (name, root) == ("demo", "data/demo")
    assert datasets.root_for_dataset("demo") == "data/demo"
    assert datasets.root_for_dataset("nope") is None


# ----------------------------------------------------------------- Kaggle slug (§3.3)
def test_kaggle_slug_normalizes_a_pasted_url():
    assert datasets.normalize_kaggle_slug("https://www.kaggle.com/datasets/someone/demo-ds") == "someone/demo-ds"
    assert datasets.normalize_kaggle_slug("https://www.kaggle.com/datasets/someone/demo-ds?x=1") == "someone/demo-ds"
    assert datasets.normalize_kaggle_slug("someone/demo-ds") == "someone/demo-ds"


def test_malformed_kaggle_slug_is_rejected():
    with pytest.raises(datasets.DatasetError):
        datasets.validate_kaggle_slug("a/b/c")
    with pytest.raises(datasets.DatasetError):
        datasets.validate_kaggle_slug("not-a-slug")


def test_patch_with_a_malformed_slug_returns_400_shaped_error():
    with pytest.raises(datasets.DatasetError) as ei:
        datasets.update_dataset("demo", sources={"kaggle": {"slug": "a/b/c"}})
    assert ei.value.status == 400


# ----------------------------------------------------------------- CRUD (§9)
def test_create_draft_then_it_is_listed_and_editable():
    result = datasets.create_draft("Kvasir-SEG", tags=["Polyp", "polyp", " Draft-Import "])
    assert result["identity_source"] == "draft"
    assert result["tags"] == ["polyp", "draft-import"]
    names = [d["name"] for d in datasets.list_datasets()]
    assert "Kvasir-SEG" in names

    datasets.update_dataset("Kvasir-SEG", sources={"kaggle": {"slug": "someone/kvasir-seg"}})
    got = datasets.get_dataset("kvasir-seg")
    assert got["sources"]["kaggle"]["slug"] == "someone/kvasir-seg"


def test_create_draft_conflicts_with_an_existing_record():
    datasets.create_draft("dup-name")
    with pytest.raises(datasets.DatasetError) as ei:
        datasets.create_draft("dup-name")
    assert ei.value.status == 409


def test_create_draft_over_an_existing_fragment_reports_exists_fragment_instead_of_creating():
    # "demo" always migrates in with a record already (the fake profile's
    # own legacy kaggle_dataset_map, see conftest's fake.yaml) — ColonDB has
    # no legacy content, so it's the one that's genuinely record-less here.
    result = datasets.create_draft("ColonDB")
    assert result == {"exists": "fragment", "name": "ColonDB"}
    assert "colondb" not in datasets._load()


def test_delete_dataset_removes_the_xdash_record_only():
    datasets.create_draft("throwaway")
    datasets.delete_dataset("throwaway")
    with pytest.raises(datasets.DatasetError):
        datasets.get_dataset("throwaway")
    # A fragment-backed dataset reappears with no sources once its XDash
    # record is removed — dissert still declares it.
    datasets.update_dataset("demo", tags=["x"])
    datasets.delete_dataset("demo")
    got = datasets.get_dataset("demo")
    assert got["identity_source"] == "fragment" and got["tags"] == []


def test_host_override_crud():
    datasets.set_host_override("demo", "ssh:box", "/data/shared/demo")
    got = datasets.get_dataset("demo")
    assert got["hosts"]["ssh:box"]["path"] == "/data/shared/demo"
    datasets.delete_host_override("demo", "ssh:box")
    assert "ssh:box" not in datasets.get_dataset("demo")["hosts"]
    with pytest.raises(datasets.DatasetError):
        datasets.set_host_override("demo", "kaggle:x", "/nope")


def test_link_fragment_renames_a_draft_to_match_a_differently_named_fragment():
    datasets.create_draft("Kvasir-SEG-old", sources={"kaggle": {"slug": "someone/kvasir"}})
    linked = datasets.link_fragment("Kvasir-SEG-old", "ColonDB")
    assert linked["name"] == "ColonDB"
    assert linked["sources"]["kaggle"]["slug"] == "someone/kvasir"


# ----------------------------------------------------------------- plan_delivery (§4.2)
def test_plan_delivery_blocks_a_draft_with_dataset_draft():
    datasets.create_draft("adraft")
    plan = datasets.plan_delivery("adraft", "ssh:box", "ssh")
    assert plan["state"] == "blocked" and plan["code"] == "dataset-draft"


def test_plan_delivery_local_prefers_the_real_directory_then_a_configured_source():
    plan = datasets.plan_delivery("demo", "local", "local")
    assert plan["state"] == "ready" and plan["strategy"] == "local-existing"


def test_plan_delivery_ssh_will_transfer_from_the_local_copy_when_nothing_else_is_configured():
    plan = datasets.plan_delivery("demo", "ssh:box", "ssh")
    assert plan["state"] == "will-transfer" and plan["strategy"] == "ssh-push"


def test_plan_delivery_ssh_host_override_wins_over_a_local_copy():
    datasets.set_host_override("demo", "ssh:box", "/data/shared/demo")
    plan = datasets.plan_delivery("demo", "ssh:box", "ssh")
    assert plan["strategy"] == "host-override" and plan["source"] == "/data/shared/demo"


def test_plan_delivery_kaggle_needs_a_slug():
    # "demo" always migrates in with the fake profile's own legacy
    # kaggle_dataset_map entry (see conftest's fake.yaml) — ColonDB has none,
    # so it's the one that actually starts with nothing configured.
    plan = datasets.plan_delivery("ColonDB", "kaggle:tanvir", "kaggle")
    assert plan["state"] == "blocked" and plan["code"] == "no-kaggle-source"
    datasets.update_dataset("ColonDB", sources={"kaggle": {"slug": "someone/colondb-ds"}})
    plan = datasets.plan_delivery("ColonDB", "kaggle:tanvir", "kaggle")
    assert plan["strategy"] == "attach" and plan["source"] == "someone/colondb-ds" and plan["state"] != "blocked"


def test_plan_delivery_kaggle_fragment_declared_slug_locks_the_field():
    (HOST / "configs" / "dataset" / "locked.yaml").write_text(
        "dataset: {name: Locked, root: data/locked, kaggle_dataset: someone/locked-ds}\n"
    )
    got = datasets.get_dataset("Locked")
    assert got["kaggle_slug_locked"] is True
    with pytest.raises(datasets.DatasetError):
        datasets.update_dataset("Locked", sources={"kaggle": {"slug": "someone/else"}})
    plan = datasets.plan_delivery("Locked", "kaggle:tanvir", "kaggle")
    assert plan["source"] == "someone/locked-ds"


def test_plan_delivery_colab_downloads_via_kaggle_when_a_data_account_is_set():
    datasets.update_dataset("demo", sources={"kaggle": {"slug": "someone/demo-ds"}})
    datasets.set_data_account("acct1")
    plan = datasets.plan_delivery("demo", "colab:acct1", "colab")
    assert plan["strategy"] == "colab-download" and plan["state"] == "will-transfer"


def test_plan_delivery_colab_pushes_the_local_copy_without_a_data_account():
    plan = datasets.plan_delivery("demo", "colab:acct1", "colab")
    assert plan["strategy"] == "colab-push" and plan["state"] == "will-transfer"


def test_colondb_style_dataset_is_no_longer_blocked_on_kaggle_or_colab():
    """DS3's regression: a dataset with a Kaggle slug configured used to stay
    blocked everywhere except a bare SSH `path` binding, because the
    dataset_map fallback could never fire. plan_delivery() has no such
    fallback to fail — `unknown` is an acceptable answer, `blocked` is not."""
    datasets.update_dataset("ColonDB", sources={"kaggle": {"slug": "syfur007/colondb-train-val-test-images-and-masks"}})
    datasets.set_data_account("acct1")
    assert datasets.plan_delivery("ColonDB", "kaggle:eva", "kaggle")["state"] != "blocked"
    assert datasets.plan_delivery("ColonDB", "colab:acct1", "colab")["state"] != "blocked"


def test_plan_delivery_for_config_reports_no_dataset_binding_for_a_configless_config():
    plan = datasets.plan_delivery_for_config(_NO_DATASET_CONFIG, "ssh:box", "ssh")
    assert plan["state"] == "blocked" and plan["code"] == "no-dataset-binding" and plan["dataset"] is None


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


# ----------------------------------------------------------------- placement (§4.4, executed at dispatch)
def test_stage_ssh_leaves_an_already_populated_target_untouched_no_self_link(tmp_path):
    """DS5's own regression: the old place_dataset() ran
    `ln -sfn <repo>/<root> <repo>/<root>` unconditionally for a `path`
    binding — `ln` treats an existing-directory destination as "link
    *inside* it", producing `.../demo/demo -> .../demo`. This asserts that
    artifact never appears, against a real `sh`/`ln` (ShellTransport), not a
    fake that would have silently agreed with either behavior."""
    remote_root = tmp_path / "remote_repo"
    target_dir = remote_root / "data" / "demo"
    target_dir.mkdir(parents=True)
    (target_dir / "real_file.txt").write_text("real data\n")
    shell = ShellTransport(remote_root, host_id="shellbox")

    result = datasets.stage("demo", "ssh:shellbox", "ssh", shell, remote_root)

    assert result["strategy"] == "repo-path"
    assert target_dir.is_dir() and not target_dir.is_symlink()
    assert (target_dir / "real_file.txt").read_text() == "real data\n"
    assert not (target_dir / "demo").exists()  # the old bug's self-link artifact


def test_stage_ssh_blocks_when_the_target_is_occupied_by_something_else(tmp_path):
    remote_root = tmp_path / "remote_repo2"
    override_source = tmp_path / "override_data"
    override_source.mkdir(parents=True)
    (override_source / "f.txt").write_text("override\n")
    target_dir = remote_root / "data" / "demo"
    target_dir.mkdir(parents=True)
    (target_dir / "unexpected.txt").write_text("not xdash's\n")
    datasets.set_host_override("demo", "ssh:occbox", str(override_source))
    shell = ShellTransport(remote_root, host_id="occbox")

    with pytest.raises(datasets.PlacementError) as ei:
        datasets.stage("demo", "ssh:occbox", "ssh", shell, remote_root)
    assert ei.value.code == datasets.CODE_TARGET_OCCUPIED
    assert (target_dir / "unexpected.txt").is_file()  # never written into


def test_stage_ssh_pushes_the_local_copy_then_links_atomically(tmp_path):
    remote_root = tmp_path / "remote_repo3"
    shell = ShellTransport(remote_root, host_id="pushbox")

    result = datasets.stage("demo", "ssh:pushbox", "ssh", shell, remote_root)

    assert result["strategy"] == "ssh-push"
    target = remote_root / "data" / "demo"
    assert target.is_symlink()
    cache = remote_root / "data" / ".xdash-cache" / "demo"
    assert (cache / "sample.txt").is_file()

    # Re-staging replaces the link atomically rather than erroring or nesting.
    result2 = datasets.stage("demo", "ssh:pushbox", "ssh", shell, remote_root)
    assert result2["strategy"] == "ssh-push"
    assert target.is_symlink()


def test_stage_ssh_host_override_links_to_the_override_path(tmp_path):
    remote_root = tmp_path / "remote_repo4"
    override_source = tmp_path / "override_data2"
    override_source.mkdir(parents=True)
    (override_source / "f.txt").write_text("data\n")
    datasets.set_host_override("demo", "ssh:ovbox", str(override_source))
    shell = ShellTransport(remote_root, host_id="ovbox")

    result = datasets.stage("demo", "ssh:ovbox", "ssh", shell, remote_root)

    assert result["strategy"] == "host-override"
    target = remote_root / "data" / "demo"
    assert target.is_symlink()
    assert (target / "f.txt").read_text() == "data\n"


def test_stage_blocks_a_draft_with_no_declared_root():
    with pytest.raises(datasets.PlacementError) as ei:
        datasets.stage("adraft-nope", "ssh:box", "ssh", FakeTransport("x", Path("/r"), Path("/tmp")), Path("/r"))
    assert ei.value.code == datasets.CODE_DATASET_DRAFT


def test_stage_kaggle_is_a_transport_noop_reporting_attach():
    datasets.update_dataset("demo", sources={"kaggle": {"slug": "someone/demo-ds"}})
    result = datasets.stage("demo", "kaggle:acct", "kaggle", FakeTransport("x", Path("/r"), Path("/tmp")), Path("/r"))
    assert result["strategy"] == "attach" and result["source"] == "someone/demo-ds"


def test_a_nonzero_link_command_blocks_dispatch_instead_of_reporting_success(tmp_path):
    """Fixes DS6: place_dataset() used to never check `run()`'s exit code."""
    remote = tmp_path / "remote"
    remote.mkdir()
    fake = FakeTransport("box5", Path("/remote/repo"), remote)
    fake.run_returncode = 1
    fake.run_stderr = "permission denied"
    with pytest.raises(datasets.PlacementError) as ei:
        datasets.stage("demo", "ssh:box5", "ssh", fake, Path("/remote/repo"))
    assert "permission denied" in ei.value.detail


def test_colab_download_credentials_never_appear_in_any_recorded_argv(tmp_path):
    """Fixes DS7: the credential must travel only via put_text (stdin),
    never as a shell argument FakeTransport.run() would have recorded."""
    datasets.update_dataset("demo", sources={"kaggle": {"slug": "someone/demo-ds"}})
    datasets.set_data_account("acct1")
    remote = tmp_path / "remote"
    remote.mkdir()
    fake = FakeTransport("colab-acct", Path("/remote/repo"), remote)
    creds = {"KAGGLE_USERNAME": "data-bot", "KAGGLE_KEY": "super-secret-key"}

    result = datasets.stage("demo", "colab:acct", "colab", fake, Path("/remote/repo"), data_account_creds=creds)

    assert result["strategy"] == "colab-download"
    for argv in fake.run_calls:
        assert "super-secret-key" not in " ".join(argv)
    put_texts = [text for (op, _p, text) in fake.calls if op == "put_text"]
    assert put_texts and all("super-secret-key" in t for t in put_texts)


def test_colab_download_without_a_data_account_credential_blocks():
    datasets.update_dataset("demo", sources={"kaggle": {"slug": "someone/demo-ds"}})
    datasets.set_data_account("acct1")
    fake = FakeTransport("colab-acct", Path("/remote/repo"), Path("/tmp"))
    with pytest.raises(datasets.PlacementError) as ei:
        datasets.stage("demo", "colab:acct1", "colab", fake, Path("/remote/repo"), data_account_creds=None)
    assert ei.value.code == datasets.CODE_NO_DATA_ACCOUNT


# ----------------------------------------------------------------- locate_root (§4.5, fixes DS8)
def test_locate_root_agrees_with_the_notebook_embedded_copy_on_five_fixture_trees(tmp_path):
    ns: dict = {}
    exec(datasets.LOCATE_ROOT_PY_SOURCE, ns)
    standalone = ns["locate_root"]

    cases = [
        (["ClinicDB/train", "ClinicDB/val"], "data/polyp/ClinicDB", None, False),
        (["nested/deep/ClinicDB/train"], "data/polyp/ClinicDB", None, False),
        (["train", "val", "test"], "data/x/Whatever", {"train", "val", "test"}, False),
        (["ClinicDB/a", "other/ClinicDB/b"], "data/polyp/ClinicDB", None, True),
        (["somethingelse"], "data/polyp/ClinicDB", None, True),
    ]
    for i, (dirs, root, expect_top, should_raise) in enumerate(cases):
        base = tmp_path / ("case%d" % i)
        for d in dirs:
            (base / d).mkdir(parents=True, exist_ok=True)
        if should_raise:
            with pytest.raises(datasets.DatasetLayoutError):
                datasets.locate_root(base, root, expect_top)
            with pytest.raises(AssertionError):
                standalone(str(base), root, expect_top)
        else:
            real = datasets.locate_root(base, root, expect_top)
            fake = standalone(str(base), root, expect_top)
            assert Path(real).resolve() == Path(fake).resolve()


def test_notebook_cell_11_embeds_the_same_locate_root_source():
    nb = json.loads((XDASH_ROOT / "data" / "dissert" / "kaggle_worker_template.ipynb").read_text())
    sources = ["".join(c["source"]) if isinstance(c["source"], list) else c["source"] for c in nb["cells"]]
    joined = "\n".join(sources)
    assert "def locate_root(download_dir, root, expect_top=None):" in joined
    assert "_find_leaf" not in joined


# ----------------------------------------------------------------- checks (§5)
def test_kaggle_access_check_against_a_stub_script_403(tmp_path, monkeypatch):
    from backend import kaggle as kaggle_backend
    kaggle_backend.add_account("tanvir", username="tanvir", key="somekey123")
    datasets.update_dataset("demo", sources={"kaggle": {"slug": "someone/demo-ds"}})
    monkeypatch.setattr(kaggle_backend, "_run_kaggle", _stub_kaggle_run(tmp_path, stdout="403 - Forbidden\n", exit_code=0))

    result = datasets.run_checks("demo", targets=["kaggle:tanvir"])

    check = result["checks"]["kaggle:tanvir"]
    assert check["state"] == "blocked" and check["code"] == "kaggle-no-access"
    plan = datasets.plan_delivery("demo", "kaggle:tanvir", "kaggle")
    assert plan["state"] == "blocked" and plan["code"] == "kaggle-no-access"


def test_kaggle_access_check_ok(tmp_path, monkeypatch):
    from backend import kaggle as kaggle_backend
    kaggle_backend.add_account("tanvir", username="tanvir", key="somekey123")
    datasets.update_dataset("demo", sources={"kaggle": {"slug": "someone/demo-ds"}})
    monkeypatch.setattr(kaggle_backend, "_run_kaggle", _stub_kaggle_run(tmp_path, stdout="file.png 10 bytes\n", exit_code=0))

    result = datasets.run_checks("demo", targets=["kaggle:tanvir"])
    assert result["checks"]["kaggle:tanvir"]["state"] == "ready"


def test_local_check_fingerprints_the_real_directory():
    result = datasets.run_checks("demo", targets=["local"])
    assert result["checks"]["local"]["state"] == "ready"
    assert datasets.get_dataset("demo")["fingerprints"]["local"]["files"] == 1


def test_ssh_check_reports_ready_over_a_real_shell(tmp_path):
    remote_root = tmp_path / "remote_repo5"
    target = remote_root / "data" / "demo"
    target.mkdir(parents=True)
    (target / "f.txt").write_text("x\n")
    shell = ShellTransport(remote_root, host_id="checkbox")
    from backend.runners.base import RunnerCapabilities

    class _FakeRunnerForCheck:
        kind = "ssh"
        def __init__(self):
            self._transport = shell
            self.host = type("H", (), {"repo_root": remote_root})()

    import backend.runners.registry as runner_registry
    orig = runner_registry.get_runner
    runner_registry.get_runner = lambda slot: _FakeRunnerForCheck() if slot == "ssh:checkbox" else orig(slot)
    try:
        result = datasets.run_checks("demo", targets=["ssh:checkbox"])
    finally:
        runner_registry.get_runner = orig
    assert result["checks"]["ssh:checkbox"]["state"] == "ready"


# ----------------------------------------------------------------- migration (§10)
def test_migration_seeds_in_memory_only_until_the_first_real_write():
    from backend import datasets as datasets_mod
    assert not settings.datasets_file.is_file()
    found = {d["name"]: d for d in datasets.list_datasets()}
    assert found["demo"]["sources"]["kaggle"]["slug"] == "someone/demo-ds"
    assert not settings.datasets_file.is_file()  # a bare read never persists
    datasets.set_host_override("demo", "ssh:box", "/x")
    assert settings.datasets_file.is_file()  # persisted by the first real write


def test_migration_matches_the_section_10_table_for_todays_real_dissert_data(tmp_path, monkeypatch):
    v1 = json.loads((FIXTURES / "dissert_datasets_v1.json").read_text())
    (tmp_path / "dataset_map.json").write_text((FIXTURES / "dissert_dataset_map.json").read_text())
    monkeypatch.setattr(settings, "state_dir", tmp_path)
    monkeypatch.setattr(datasets, "known_dataset_names", lambda: ["ClinicDB", "ColonDB", "BUSI", "ISIC18"])

    migrated = datasets._migrate_v1_to_v2(v1)

    assert migrated["clinicdb"]["sources"]["kaggle"]["slug"] == "syfur007/clinicdb-train-val-test-images-and-masks"
    assert migrated["colondb"]["sources"]["kaggle"]["slug"] == "syfur007/colondb-train-val-test-images-and-masks"
    assert "kaggle" not in migrated["busi"]["sources"]
    assert "isic18" not in migrated  # nothing to migrate for it -- no manufactured empty record
    notes = " ".join(migrated["clinicdb"].get("migration_notes", []))
    assert "clinicdb-images" in notes
    assert "syfur007/syfur007" in notes
    assert "bindings" not in migrated["colondb"] and "checks" not in migrated["colondb"]


# ----------------------------------------------------------------- KaggleRunner (fixes DS2)
def test_kaggle_can_accept_and_dispatch_resolve_the_same_slug(monkeypatch):
    from backend import kaggle as kaggle_backend
    kaggle_backend.add_account("acct1", username="acct1", key="somekey123")
    datasets.update_dataset("ColonDB", sources={"kaggle": {"slug": "syfur007/colondb-train-val-test-images-and-masks"}})
    (HOST / "configs" / "experiment" / "colondb_exp.yaml").write_text(
        "compose: [\"../dataset/colondb.yaml\"]\nlogging: {experiment_name: colondb_exp}\ntraining: {epochs: 1}\n"
    )
    monkeypatch.setattr(framework, "code_state", lambda *a, **k: {"commit": "a" * 40, "dirty": False, "pushed": True})
    runner = kaggle_runner.KaggleRunner("acct1")
    exp = {"experiment_id": "colondb_exp-s1", "config_path": "experiment/colondb_exp.yaml", "seed": 1,
           "extra_args": {"train": "", "eval": ""}}

    accept_plan = datasets.plan_delivery_for_config(exp["config_path"], runner.id, runner.kind)
    assert runner.can_accept(exp, 1.0) is None

    captured = {}

    def fake_push(account_name, experiment_id, config_path, train_args, dataset_sources, **kwargs):
        captured["dataset_sources"] = dataset_sources
        return {"kernel_slug": "acct1/colondb", "results_dir": "outputs/kaggle/colondb"}

    monkeypatch.setattr(kaggle_backend, "push_experiment_attempt", fake_push)
    runner.dispatch(exp, {"attempt_id": "atmpt_x", "unit_ref": {}})

    assert captured["dataset_sources"][0] == accept_plan["source"] == "syfur007/colondb-train-val-test-images-and-masks"
