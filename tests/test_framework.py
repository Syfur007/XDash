"""X1 (seed flag), X12 (split extra_args) and X2's locate_run hook through
the generic bridge entry point (backend/framework.py, bridge_scripts/call.py)."""
from __future__ import annotations

import json
import os
import subprocess

import pytest
import yaml

from backend import bridge, framework

from conftest import HOST, THESIS_PYTHON, XDASH_ROOT, needs_thesis


# ----------------------------------------------------------------- X1
def test_dissert_profile_uses_the_run_sweep_seed_flags():
    # repos/dissert.yaml was migrated for real in Phase 2 (backend/
    # migrate_profile.py) — seed_arg now lives baked into commands.train/
    # eval's own text (§4.1), not as its own flat key.
    raw = yaml.safe_load((XDASH_ROOT / "repos" / "dissert.yaml").read_text())
    assert "--seeds {seed} --repeats 1" in raw["commands"]["train"]
    assert raw["hooks"]["locate_run"] == "xdash:dissert.locate_run"
    assert "--allow-test-eval" in raw["eval_default_args"]


PARSE_ARGS = r"""
import json, sys
sys.argv = ["x"] + json.loads(sys.argv[1])
which = sys.argv.pop(1)
if which == "train":
    import train
    a = train.parse_args()
    print(json.dumps({"seed": a.seed, "seeds": a.seeds, "repeats": a.repeats}))
else:
    import eval as ev
    a = ev._parse_args()
    print(json.dumps({"seed": a.seed, "seeds": a.seeds, "repeats": a.repeats, "allow": a.allow_test_eval}))
"""


def _dissert_parse(root, argv):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(root))
    return subprocess.run([str(THESIS_PYTHON), "-c", PARSE_ARGS, json.dumps(argv)], cwd=str(root),
                          env=env, capture_output=True, text=True, timeout=300)


@needs_thesis
def test_dissert_train_and_eval_take_the_sweep_path_for_one_seed(dissert_head):
    """`--seeds 42 --repeats 1` leaves args.seed None — train.py's single-run
    bypass is keyed on args.seed — so run_sweep runs one seed with no repeat
    axis and writes a manifest + ledger row (X1). eval.py accepts the same
    pair plus --allow-test-eval."""
    train = _dissert_parse(dissert_head, ["train", "--config", "c.yaml", "--seeds", "42", "--repeats", "1"])
    assert train.returncode == 0, train.stderr[-2000:]
    assert json.loads(train.stdout.strip().splitlines()[-1]) == {"seed": None, "seeds": [42], "repeats": 1}
    ev = _dissert_parse(dissert_head, ["eval", "--config", "c.yaml", "--seeds", "42", "--repeats", "1", "--allow-test-eval"])
    assert ev.returncode == 0, ev.stderr[-2000:]
    assert json.loads(ev.stdout.strip().splitlines()[-1]) == {"seed": None, "seeds": [42], "repeats": 1, "allow": True}


@needs_thesis
def test_dissert_eval_rejects_a_train_only_flag(dissert_head):
    """Why X12's split exists: eval.py's strict parse_args() exits 2."""
    ev = _dissert_parse(dissert_head, ["eval", "--config", "c.yaml", "--epochs", "5"])
    assert ev.returncode == 2 and "unrecognized arguments" in ev.stderr


# ----------------------------------------------------------------- X12
def test_legacy_string_extra_args_go_to_train_only_minus_the_baked_seed():
    assert framework.normalize_extra_args("--seed 42", 42) == {"train": "", "eval": ""}
    assert framework.normalize_extra_args("--seeds 42 --repeats 1 --epochs 5", 42) == {"train": "--epochs 5", "eval": ""}
    # A seed flag that isn't this experiment's own is the user's, and kept.
    assert framework.normalize_extra_args("--seed 7", 42) == {"train": "--seed 7", "eval": ""}
    assert framework.normalize_extra_args({"train": " --lr 0.1 ", "eval": "--no-vis"}) == {"train": "--lr 0.1", "eval": "--no-vis"}
    assert framework.normalize_extra_args(None) == {"train": "", "eval": ""}


def test_stage_args_put_seed_flags_on_both_halves_and_extras_on_their_own():
    train, ev = framework.stage_args({"seed": 42, "extra_args": {"train": "--epochs 5", "eval": "--no-vis"}})
    assert train == "--seeds 42 --repeats 1 --epochs 5"
    assert ev == "--seeds 42 --repeats 1 --no-vis"
    train, ev = framework.stage_args({"seed": 42, "extra_args": "--seed 42 --epochs 5"})  # un-migrated record
    assert (train, ev) == ("--seeds 42 --repeats 1 --epochs 5", "--seeds 42 --repeats 1")
    assert framework.stage_args({"seed": None, "extra_args": ""}) == ("", "")


def test_run_mismatch_flags_only_a_disagreement():
    planned = ["R-abc1234-s42-f-"]
    assert framework.run_mismatch(planned, ["run_id=R-abc1234-s42-f- status=done"]) is None
    assert framework.run_mismatch(planned, ["no run ids printed"]) is None
    m = framework.run_mismatch(planned, ["run_id=R-fff0000-s42-f- status=done best_metric=0.8"])
    assert m["code"] == "run-mismatch" and m["reported"] == ["R-fff0000-s42-f-"]


# ----------------------------------------------------------------- X2: hooks through call.py
def test_locate_run_through_the_generic_bridge_entry_point(settings):
    run = framework.locate_run("experiment/demo.yaml", 42)
    assert run["run_dir"].startswith("outputs/experiments/demo_exp/") and run["run_dir"].endswith("-s42")
    assert run["run_ids"] == ["R-%s-s42-f-" % run["config_hash"][:7]]


def test_locate_run_without_a_hook_or_seed(settings, monkeypatch):
    assert "error" in framework.locate_run("experiment/demo.yaml", None)
    monkeypatch.setattr(settings, "hooks", {})
    assert framework.locate_run("experiment/demo.yaml", 42) is None


def test_bad_hook_specs_are_bridge_errors():
    with pytest.raises(bridge.BridgeError):
        bridge.call_hook("no_colon_here", {})
    with pytest.raises(bridge.BridgeError):
        bridge.call_hook("xdash:nosuchframework.fn", {})
    with pytest.raises(bridge.BridgeError):
        bridge.call_hook("fakefw.hooks:missing", {})


def test_bridge_calls_leave_no_bytecode_in_the_host_repo():
    framework.locate_run("experiment/demo.yaml", 1)
    assert not list((HOST / "fakefw").glob("__pycache__"))


@needs_thesis
def test_dissert_adapter_locates_the_hash_scoped_run_dir(dissert_head):
    """The real adapter, in dissert's own interpreter, read-only. f76c81e is
    the hash dissert itself used for this config's existing
    outputs/experiments/mkunet_t_clinicdb/f76c81e-s*-r* dirs."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(dissert_head))
    proc = subprocess.run(
        [str(THESIS_PYTHON), str(XDASH_ROOT / "backend" / "bridge_scripts" / "call.py"), "xdash:dissert.locate_run",
         json.dumps({"config": "configs/experiment/mkunet/mkunet_t_clinicdb.yaml", "seed": 42})],
        cwd=str(dissert_head), env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout)
    assert "__bridge_error__" not in out, out
    assert out["run_dir"] == "outputs/experiments/mkunet_t_clinicdb/%s-s42" % out["config_hash"][:7]
    assert out["config_hash"].startswith("f76c81e")
    assert out["run_ids"] == ["R-f76c81e-s42-f-"]


# ----------------------------------------------------------------- Phase 2: command templates
def test_render_command_matches_the_pre_template_output_for_an_unmigrated_profile(settings):
    """A profile that hasn't added a `commands:` section gets a synthesized
    default (backend/config.py's _default_command_template) built from
    train_script/eval_script/seed_arg — this must render byte-identical to
    what stage_args() + tmux_runner.build_launch_command() produced before
    Phase 2 (modulo shlex-quoting, irrelevant for arguments with no
    whitespace/shell metacharacters)."""
    import shlex
    from backend import tmux_runner as tmux
    experiment = {"seed": 42, "extra_args": {"train": "--epochs 5", "eval": "--no-vis"}}
    train_extra, eval_extra = framework.extra_only(experiment)
    train_cmd = framework.render_command("experiment/demo.yaml", 42, "python", "train", extra=train_extra)
    eval_cmd = framework.render_command("experiment/demo.yaml", 42, "python", "eval", extra=eval_extra)

    legacy_train_extra, legacy_eval_extra = framework.stage_args(experiment)
    legacy_train = tmux.build_launch_command("python", settings.train_script, "experiment/demo.yaml", [], legacy_train_extra)
    legacy_eval = tmux.build_launch_command(
        "python", settings.eval_script, "experiment/demo.yaml", list(settings.eval_default_args), legacy_eval_extra,
    )
    # Train: identical, token order included (the template's {extra} is the trailing slot,
    # exactly where the legacy extra_args string landed).
    assert train_cmd == legacy_train
    assert train_cmd == "python train.py --config experiment/demo.yaml --seeds 42 --repeats 1 --epochs 5"
    # Eval: same tokens, reordered — the legacy path put eval_default_args right after
    # --config (before the seed flags, which were baked into legacy_eval_extra itself);
    # the template puts the seed flags in their own fixed slot before {extra}, so
    # eval_default_args (folded into {extra}) now lands after them. Harmless to argparse
    # (flag order never matters), so this is a deliberate, not a regressive, difference.
    assert sorted(shlex.split(eval_cmd)) == sorted(shlex.split(legacy_eval))
    assert eval_cmd == "python eval.py --config experiment/demo.yaml --seeds 42 --repeats 1 --allow-test-eval --no-vis"


def test_render_command_is_identical_across_runtime_kinds_modulo_the_interpreter():
    """§10 Phase 2 acceptance: the same attempt produces the same command
    line on every runtime kind — local, ssh, colab and kaggle each just pass
    their own interpreter path into the one render_command()."""
    experiment = {"seed": 7, "extra_args": {"train": "--max-hours 0.05", "eval": ""}}
    train_extra, eval_extra = framework.extra_only(experiment)
    interpreters = {
        "local": "python", "ssh": "/home/user/miniconda3/envs/thesis/bin/python",
        "colab": "python3", "kaggle": "/kaggle/working/py38_env/bin/python",
    }
    trains = {
        kind: framework.render_command("experiment/demo.yaml", 7, py, "train", resume=True, extra=train_extra)
        for kind, py in interpreters.items()
    }
    evals = {
        kind: framework.render_command("experiment/demo.yaml", 7, py, "eval", extra=eval_extra)
        for kind, py in interpreters.items()
    }
    for kind, py in interpreters.items():
        assert trains[kind] == "%s train.py --config experiment/demo.yaml --seeds 7 --repeats 1 --resume --max-hours 0.05" % py
        assert evals[kind] == "%s eval.py --config experiment/demo.yaml --seeds 7 --repeats 1 --allow-test-eval" % py
    # Strip each command's own leading interpreter token: everything after it agrees exactly.
    train_tails = {cmd.split(" ", 1)[1] for cmd in trains.values()}
    eval_tails = {cmd.split(" ", 1)[1] for cmd in evals.values()}
    assert len(train_tails) == 1
    assert len(eval_tails) == 1


def test_render_command_uses_a_profiles_own_commands_section(settings, monkeypatch):
    """A profile with a `commands:` section renders from it directly —
    proves the sectioned form isn't just cosmetic."""
    monkeypatch.setitem(settings.commands, "train", "{python} -m dissert.cli.train --config {config} --seeds {seed} {extra}")
    out = framework.render_command("x.yaml", 3, "python3.11", "train", extra="--foo")
    assert out == "python3.11 -m dissert.cli.train --config x.yaml --seeds 3 --foo"


def test_render_command_requires_a_template_for_the_stage(settings, monkeypatch):
    monkeypatch.setitem(settings.commands, "eval", "")
    with pytest.raises(ValueError):
        framework.render_command("x.yaml", 3, "python", "eval")
