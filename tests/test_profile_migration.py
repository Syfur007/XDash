"""XDASH_PLAN.md §10 Phase 2 acceptance: the profile migration script
(backend/migrate_profile.py) sections a profile's flat keys, and the
server's *effective* settings — the properties every other module actually
reads — must be identical before and after. Comment/order fidelity is a
different, best-effort concern (see that module's own docstring); this test
is about behavior, not bytes.

repos/dissert.yaml and repos/segpriors.yaml were migrated for real as part
of this phase (XDASH_PROGRESS.md records it) — the equivalence check below
uses an inline flat-profile fixture instead of depending on their current
(already-sectioned) text, so it keeps proving the flat->sectioned bridge
regardless of what state the real files are in by the time this runs."""
from __future__ import annotations

import textwrap

from backend import config as config_mod
from backend.migrate_profile import migrate

from conftest import XDASH_ROOT

_EFFECTIVE_FIELDS = [
    "repo_root", "configs_dir", "dataset_name_key", "dataset_root_key",
    "overlay_compose_key", "run_id_pattern", "python_executable", "env_activate_cmd",
    "commands", "hooks", "allow_unpushed", "eval_default_args", "bridge_python_executable",
    "manifest_layout",
]

# A representative flat profile — everything backend/config.py's legacy
# reads and backend/migrate_profile.py's moves both touch, condensed from
# the shape repos/dissert.yaml had before this phase's real migration.
_FLAT_PROFILE = textwrap.dedent("""\
    display_name: "fixture"
    repo_root: "../../dissert"    # path to the repo root
    configs_dir: "configs"        # where configs live
    manifest_layout: "experiments"
    python_executable: "python"   # interpreter
    train_script: "train.py"
    eval_script: "eval.py"
    seed_arg: "--seeds {seed} --repeats 1"
    bridge_python_executable: "/opt/thesis/bin/python"
    hooks:
      locate_run: "xdash:dissert.locate_run"
      resolve_config: "xdash:dissert.resolve_config"
    overlay_compose_key: "compose"
    eval_default_args: ["--allow-test-eval"]
    allow_unpushed: false
    env_activate_cmd: "conda activate thesis"
    kaggle_dataset_map:
      clinicdb: "syfur007/clinicdb-images"
    run_id_pattern: 'run_id=(\\S+)'
    """)


def _assert_equivalent(monkeypatch, tmp_path, pre_text: str):
    (tmp_path / "pre.yaml").write_text(pre_text)
    post_path = tmp_path / "post.yaml"
    post_path.write_text(pre_text)
    migrate(post_path)

    monkeypatch.setattr(config_mod, "REPOS_DIR", tmp_path)
    pre = config_mod.Settings("pre")
    post = config_mod.Settings("post")
    for field in _EFFECTIVE_FIELDS:
        assert getattr(pre, field) == getattr(post, field), (field, getattr(pre, field), getattr(post, field))
    # metrics.lower_is_better is new in the sectioned profile (moves out of
    # static/app.js) — `pre` only ever sees the same hardcoded default
    # `post` gets written explicitly.
    assert pre.metrics_lower_is_better == post.metrics_lower_is_better


def test_migrated_flat_profile_is_behaviorally_equivalent(monkeypatch, tmp_path):
    _assert_equivalent(monkeypatch, tmp_path, _FLAT_PROFILE)


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "dissert.yaml"
    path.write_text(_FLAT_PROFILE)
    assert migrate(path) is True
    text_once = path.read_text()
    assert migrate(path) is False  # nothing left to move
    assert path.read_text() == text_once


def test_the_real_shipped_profiles_load_and_render_working_commands(monkeypatch):
    """repos/dissert.yaml and repos/segpriors.yaml, as actually committed
    (already migrated) — not a legacy/sectioned comparison, just proof the
    real files load and produce a sane, complete commands section. The test
    harness points config.REPOS_DIR at a fake profile for the whole session
    (conftest.py), so this points it at the real repos/ dir just for itself."""
    monkeypatch.setattr(config_mod, "REPOS_DIR", XDASH_ROOT / "repos")
    for name, script_hint in (("dissert", "train.py"), ("segpriors", "run_iccit_sweep.py")):
        if not (XDASH_ROOT / "repos" / f"{name}.yaml").is_file():
            continue
        s = config_mod.Settings(name)
        assert script_hint in s.commands["train"]
        assert "{config}" in s.commands["train"] and "{config}" in s.commands["eval"]
        assert s.commands["budget"] and s.commands["resume"]
