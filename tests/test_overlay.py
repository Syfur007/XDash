"""XDASH_PLAN.md §4.3 (Phase 1, closes X12 for good): an experiment's
overrides are an overlay config file, identity v2 includes it (§3.3.1, X8),
and train, eval and locate_run all get the same `--config`."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from backend import experiments, framework, kaggle, scheduler, terminals
from backend.config import settings
from backend.runners import kaggle as kaggle_runner

from conftest import HOST, THESIS_PYTHON, XDASH_ROOT, FakeRunner, needs_thesis

DISSERT_TEMPLATE = XDASH_ROOT / "data" / "dissert" / "kaggle_worker_template.ipynb"
SEGPRIORS_TEMPLATE = XDASH_ROOT / "data" / "segpriors" / "kaggle_worker_template.ipynb"


def _hook_calls():
    path = HOST / "fakefw" / "calls.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []


# ----------------------------------------------------------------- the overlay value
def test_overlay_is_stored_flat_dotted_and_sorted():
    assert framework.normalize_overlay(None) == {}
    assert framework.normalize_overlay({"training": {"lr": 0.1, "epochs": 50}}) == {"training.epochs": 50, "training.lr": 0.1}
    assert framework.normalize_overlay({"training.epochs": 50, "dataset": {"root": "x"}}) == {
        "dataset.root": "x", "training.epochs": 50,
    }
    for bad in ({"training": 5, "training.epochs": 50}, {"training..epochs": 1}, {"compose": ["x.yaml"]},
                {"training.epochs": 1, "training": {"epochs": 2}}, ["training.epochs", 1]):
        with pytest.raises(framework.OverlayError):
            framework.normalize_overlay(bad)


def test_identity_v2_suffix_only_for_a_non_empty_overlay():
    assert experiments.experiment_id_for("experiment/demo.yaml", 42) == "demo_exp-s42"
    assert experiments.experiment_id_for("experiment/demo.yaml", 42, {}) == "demo_exp-s42"
    a = experiments.experiment_id_for("experiment/demo.yaml", 42, {"training.epochs": 50})
    b = experiments.experiment_id_for("experiment/demo.yaml", 42, {"training": {"epochs": 50}})
    c = experiments.experiment_id_for("experiment/demo.yaml", 42, {"training.epochs": 60})
    assert a == b and a != c
    assert a.startswith("demo_exp-s42-") and len(a.rsplit("-", 1)[1]) == 6


def test_rendered_overlay_composes_the_config_relative_to_itself():
    text = framework.render_overlay("experiment/demo.yaml", {"training.epochs": 50, "training.lr": 0.1})
    doc = yaml.safe_load(text)
    assert doc == {"compose": ["../../configs/experiment/demo.yaml"], "training": {"epochs": 50, "lr": 0.1}}
    assert framework.overlay_rel_path("demo_exp-s42-abc123") == ".xdash/overlays/demo_exp-s42-abc123.yaml"


# ----------------------------------------------------------------- validation at composition time
def test_a_typod_overlay_key_fails_at_creation_and_stores_nothing(use_runners):
    use_runners(FakeRunner())
    with pytest.raises(experiments.ExperimentError, match="training.epoch"):
        experiments.create_experiments([{"path": "experiment/demo.yaml", "seeds": [1]}], overlay={"training.epoch": 50})
    assert experiments.list_experiments() == []


def test_overlay_validation_goes_through_the_resolve_config_hook(monkeypatch):
    seen = []
    real = framework.bridge.call_hook
    monkeypatch.setattr(framework.bridge, "call_hook", lambda spec, args=None, **k: seen.append((spec, args)) or real(spec, args, **k))
    assert framework.validate_overlay("experiment/demo.yaml", {"training.epochs": 50}) is None
    spec, args = seen[0]
    assert spec == "fakefw.hooks:load_config" and isinstance(args, list) and len(args) == 1
    assert not (HOST / ".xdash").exists()  # the probe never lands in the host repo


def test_without_a_hook_the_overlay_is_accepted_with_a_warning(monkeypatch, use_runners):
    use_runners(FakeRunner(accept=False))
    monkeypatch.setattr(settings, "hooks", {"locate_run": settings.hooks["locate_run"]})
    out = experiments.create_experiments([{"path": "experiment/demo.yaml", "seeds": [1]}], overlay={"training.epochs": 5})
    assert out["created"] and "not validated" in out["warnings"][0]


def test_a_profile_without_overlay_support_refuses_an_overlay(monkeypatch):
    monkeypatch.setattr(settings, "overlay_compose_key", "")
    with pytest.raises(experiments.ExperimentError, match="overlay_compose_key"):
        experiments.create_experiments([{"path": "experiment/demo.yaml", "seeds": [1]}], overlay={"training.epochs": 5})
    # No overlay: nothing changes for such a profile.
    assert experiments.create_experiments([{"path": "experiment/demo.yaml", "seeds": [1]}])["created"] == ["demo_exp-s1"]


# ----------------------------------------------------------------- dispatch writes it; the plan reads it
def test_dispatch_writes_the_overlay_and_locates_the_run_on_it(use_runners):
    runner = FakeRunner()
    use_runners(runner)
    plain = experiments.create_experiments([{"path": "experiment/demo.yaml", "seeds": [42]}], then="queue")["experiments"][0]
    over = experiments.create_experiments(
        [{"path": "experiment/demo.yaml", "seeds": [42]}], overlay={"training.epochs": 50}, then="queue",
    )["experiments"][0]
    eid = over["experiment_id"]
    rel = ".xdash/overlays/%s.yaml" % eid
    written = (HOST / rel).read_text()
    assert written == framework.render_overlay("experiment/demo.yaml", {"training.epochs": 50})
    assert yaml.safe_load(written)["training"] == {"epochs": 50}

    plan = over["current_attempt"]["run"]
    assert plan["config"] == rel
    assert [c["config"] for c in _hook_calls()] == ["configs/experiment/demo.yaml", rel]
    # A different config, so a different hash and run dir — the one the run will really write.
    assert plan["config_hash"] != plain["current_attempt"]["run"]["config_hash"]
    assert plan["run_dir"].startswith("outputs/experiments/demo_exp/")
    assert runner.dispatched[1]["run"]["run_dir"] == plan["run_dir"]


def test_train_and_eval_get_the_same_overlay_config(ssh_box_overlay, monkeypatch):
    runner, fake, exp = ssh_box_overlay
    captured = {}
    monkeypatch.setattr(scheduler, "add_item", lambda config, mode, extra_args, **k: (
        captured.update(config=config, mode=mode, **k) or [{"id": "t"}, {"id": "e"}]
    ))
    runner.dispatch(exp, {"attempt_id": "atmpt_x"})
    rel = framework.overlay_rel_path(exp["experiment_id"])
    assert captured["config"] == "experiment/demo.yaml" and captured["mode"] == "both"
    assert captured["cli_config"] == rel
    assert (HOST / rel).is_file()
    # Over the transport, as its own step, to the host's repo.
    assert ("push", str(settings.repo_root / ".xdash"), "/remote/repo/.xdash", False) in fake.calls


def test_scheduler_items_carry_cli_config_into_both_commands(monkeypatch):
    """The real scheduler + terminals path, with tmux captured instead of run."""
    typed = []
    monkeypatch.setattr(terminals.tmux, "new_session", lambda name, host_id=None: True)
    monkeypatch.setattr(terminals.tmux, "send_keys", lambda name, keys, host_id=None: typed.append(keys))
    rel = ".xdash/overlays/demo_exp-s42-abc123.yaml"
    train, ev = scheduler.add_item("experiment/demo.yaml", "both", "--seeds 42", train_extra_args="--seeds 42", cli_config=rel)
    assert train["cli_config"] == ev["cli_config"] == rel
    launched = [k for k in typed if "--config" in k]  # add_item's own tick launched the train half
    assert launched and ("--config %s" % rel) in launched[0] and "train.py" in launched[0]
    typed.clear()
    terminals.launch("experiment/demo.yaml", "train", "--seeds 42", cli_config=rel)
    terminals.launch("experiment/demo.yaml", "eval", "--seeds 42", cli_config=rel)
    commands = [k for k in typed if "--config" in k]
    assert len(commands) == 2 and all(("--config %s" % rel) in c for c in commands)
    assert "train.py" in commands[0] and "eval.py" in commands[1]
    # Without an overlay the command line is unchanged.
    typed.clear()
    terminals.launch("experiment/demo.yaml", "train", "")
    assert any("--config configs/experiment/demo.yaml" in k for k in typed)


@pytest.fixture
def ssh_box_overlay(monkeypatch, tmp_path):
    from backend import hosts
    from backend import transport as transport_mod
    from backend.runners import machine
    from conftest import FakeTransport
    hosts.upsert_host({
        "id": "box", "kind": "ssh", "label": "Box", "max_concurrent": 1,
        "ssh": {"host": "box.example"}, "repos": {"fake": {"repo_root": "/remote/repo"}},
    })
    # XDASH_PLAN.md §5, X4: dispatch now places the dataset before launch —
    # this box already has "demo"'s data, so the default `path` binding
    # just symlinks it (this test is about the overlay, not placement).
    (tmp_path / "data" / "demo").mkdir(parents=True)
    fake = FakeTransport("box", Path("/remote/repo"), tmp_path)
    real = transport_mod.for_host_record
    monkeypatch.setattr(transport_mod, "for_host_record", lambda h: fake if h.id == "box" else real(h))
    eid = experiments.experiment_id_for("experiment/demo.yaml", 42, {"training.epochs": 50})
    exp = {"experiment_id": eid, "config_path": "experiment/demo.yaml", "seed": 42,
           "overlay": {"training.epochs": 50}, "extra_args": {"train": "", "eval": ""}}
    return machine.MachineRunner(hosts.get_host("box")), fake, exp


# ----------------------------------------------------------------- Kaggle: OVERLAY_YAML in the launch spec
def _cells(nb_bytes):
    nb = json.loads(nb_bytes)
    return [(c["cell_type"], c["source"] if isinstance(c["source"], str) else "".join(c["source"])) for c in nb["cells"]]


def test_kaggle_template_writes_the_overlay_right_after_the_clone():
    text = framework.render_overlay("experiment/demo.yaml", {"training.epochs": 50})
    rel = ".xdash/overlays/demo_exp-s42-abc123.yaml"
    cells = _cells(kaggle._render_launch_notebook(
        DISSERT_TEMPLATE, rel, "--seeds 42 --repeats 1", eval_extra_args="--seeds 42 --repeats 1",
        code_commit="a" * 40, overlay_yaml=text,
    ))
    spec = next(s for t, s in cells if t == "code" and kaggle.LAUNCH_SPEC_MARKER in s)
    ns = {}
    exec(compile(spec, "spec", "exec"), ns)
    assert ns["CONFIG_PATH"] == rel and ns["OVERLAY_YAML"] == text

    clone_idx = next(i for i, (_t, s) in enumerate(cells) if "git\", \"clone\"" in s)
    clone = cells[clone_idx][1]
    write_at = clone.index("if OVERLAY_YAML:")
    assert clone.index('"git", "checkout"') < write_at  # written into the pinned checkout
    # train and eval get the same file as --config (TRAIN_CMD/EVAL_CMD are pre-rendered
    # strings now, XDASH_PLAN.md §4.5 — not built from a CONFIG_PATH variable at runtime).
    assert ("--config %s" % rel) in ns["TRAIN_CMD"]
    assert ("--config %s" % rel) in ns["EVAL_CMD"]

    # The write block itself, run for real in a scratch "clone".
    block = clone[write_at:]
    with tempfile.TemporaryDirectory() as d:
        cwd = os.getcwd()
        try:
            os.chdir(d)
            exec(compile("import os\n" + block, "write", "exec"), dict(ns))
            assert Path(d, rel).read_text() == text
        finally:
            os.chdir(cwd)


def test_kaggle_without_an_overlay_renders_an_empty_one():
    cells = _cells(kaggle._render_launch_notebook(DISSERT_TEMPLATE, "configs/experiment/demo.yaml", "--seeds 1"))
    spec = next(s for t, s in cells if t == "code" and kaggle.LAUNCH_SPEC_MARKER in s)
    ns = {}
    exec(compile(spec, "spec", "exec"), ns)
    assert ns["OVERLAY_YAML"] == "" and ns["CONFIG_PATH"] == "configs/experiment/demo.yaml"


def test_a_template_without_the_placeholder_refuses_an_overlay():
    with pytest.raises(kaggle.KaggleOpsError, match="OVERLAY_YAML"):
        kaggle._render_launch_notebook(SEGPRIORS_TEMPLATE, ".xdash/overlays/x.yaml", "", overlay_yaml="compose: []\n")


def test_kaggle_dispatch_passes_the_overlay_path_and_text(monkeypatch):
    runner = kaggle_runner.KaggleRunner("acct")
    monkeypatch.setattr(runner, "_account", lambda: {"name": "acct"})
    pushed = {}
    monkeypatch.setattr(kaggle, "push_experiment_attempt", lambda *a, **k: (
        pushed.update(args=a, kwargs=k) or {"kernel_slug": "slug", "results_dir": "outputs/kaggle/x"}))
    overlay = {"training.epochs": 50}
    eid = experiments.experiment_id_for("experiment/demo.yaml", 42, overlay)
    exp = {"experiment_id": eid, "config_path": "experiment/demo.yaml", "seed": 42, "overlay": overlay,
           "extra_args": {"train": "", "eval": ""}}
    runner.dispatch(exp, {"attempt_id": "atmpt_k", "code": {"commit": "c" * 40, "dirty": False, "pushed": True}})
    assert pushed["kwargs"]["cli_config"] == framework.overlay_rel_path(eid)
    assert pushed["kwargs"]["overlay_yaml"] == framework.render_overlay("experiment/demo.yaml", overlay)


# ----------------------------------------------------------------- the real dissert loader (read-only)
@needs_thesis
def test_dissert_composes_the_overlay_as_the_plan_writes_it(dissert_head, tmp_path):
    """§4.3's format against dissert's own loader (utils/config.py) and the
    XDash adapter, from `<root>/.xdash/overlays/` with a relative `compose`
    — on dissert's committed HEAD (the `dissert_head` fixture), copied so
    this test's overlay files never land in it. The overlay's config hash
    equals what train.py's own `--epochs 50` override gives, so train, eval
    and locate_run agree (X12)."""
    shutil.copytree(str(dissert_head), str(tmp_path), dirs_exist_ok=True, symlinks=True)
    overlays = tmp_path / ".xdash" / "overlays"
    overlays.mkdir(parents=True)
    include = "../../configs/experiment/mkunet/mkunet_t_clinicdb.yaml"
    (overlays / "ok.yaml").write_text(framework.render_overlay("x", {"training.epochs": 50}, include=include))
    (overlays / "same.yaml").write_text(framework.render_overlay("x", {"training.epochs": 200}, include=include))
    (overlays / "typo.yaml").write_text(framework.render_overlay("x", {"training.epoch": 50}, include=include))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(tmp_path))
    call = str(XDASH_ROOT / "backend" / "bridge_scripts" / "call.py")

    def run(*argv):
        proc = subprocess.run([str(THESIS_PYTHON), *argv], cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300)
        assert proc.returncode == 0, proc.stderr[-2000:]
        return proc.stdout

    def locate(name):
        out = json.loads(run(call, "xdash:dissert.locate_run", json.dumps({"config": ".xdash/overlays/%s" % name, "seed": 42})))
        assert "__bridge_error__" not in out, out
        return out

    located = locate("ok.yaml")
    # New src/dissert/ package path first, old flat layout as a fallback — see
    # XDASH_PROGRESS.md's Phase 2 section (the dissert-reorg switch-over checklist).
    cli_hash = run("-c", (
        "try:\n"
        "    from dissert.config.loader import load_config\n"
        "    from dissert.orchestration.runid import config_hash\n"
        "except ImportError:\n"
        "    from utils.config import load_config\n"
        "    from orchestration.runid import config_hash\n"
        "c = load_config('configs/experiment/mkunet/mkunet_t_clinicdb.yaml'); c['training']['epochs'] = 50\n"
        "print(config_hash(c))"
    )).strip()
    assert located["config_hash"] == cli_hash and not cli_hash.startswith("f76c81e")
    assert located["run_dir"] == "outputs/experiments/mkunet_t_clinicdb/%s-s42" % cli_hash[:7]
    # An overlay that restates the config's own value is the same run as no overlay.
    assert locate("same.yaml")["config_hash"].startswith("f76c81e")
    # Same hook the profile actually declares (xdash:dissert.resolve_config), not a
    # hardcoded module path — it tries the new package layout first, old as a fallback.
    rejected = json.loads(run(call, "xdash:dissert.resolve_config", json.dumps([".xdash/overlays/typo.yaml"])))
    assert rejected.get("__bridge_error__") and "training.epoch" in rejected["message"]
