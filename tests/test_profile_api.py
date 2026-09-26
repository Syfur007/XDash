"""XDASH_PLAN.md §7/§8.6: GET/PATCH /api/profile + PUT /api/profile/raw —
ruamel dotted-path patch, comment-preserving, validated before it's written,
applied live via settings.reload()."""
from __future__ import annotations

import pytest

from backend import profile_ops
from backend.config import REPOS_DIR, settings


@pytest.fixture
def client():
    import server
    return server.app.test_client()


def _profile_path():
    return REPOS_DIR / "fake.yaml"


@pytest.fixture(autouse=True)
def _restore_fake_profile():
    """These tests write the *real* repos/fake.yaml file (PATCH/PUT are
    real, file-backed operations, not something to fake away) — restore it
    after every test so no test here leaks a permanent change into whatever
    other test file happens to run afterward in the same session."""
    original = _profile_path().read_text()
    try:
        yield
    finally:
        _profile_path().write_text(original)
        settings.reload("fake")


def test_get_profile_shape(client):
    r = client.get("/api/profile")
    assert r.status_code == 200
    body = r.get_json()
    assert {"profile_name", "text", "parsed", "comments", "hints", "restart_required", "mtime"} <= set(body)
    assert body["profile_name"] == "fake"
    assert body["parsed"]["hooks"]["locate_run"] == "fakefw.hooks:locate_run"
    assert isinstance(body["restart_required"], list)  # fake.yaml declares none of server_host/port/api_token


def test_restart_required_detects_server_keys():
    assert profile_ops.restart_required("server_host")
    assert profile_ops.restart_required("server.port")
    assert not profile_ops.restart_required("commands.train")


def test_patch_keeps_every_byte_but_the_changed_line(client):
    """§10 Phase 2 acceptance: a PATCH to an *existing* nested key
    (hooks.locate_run here — fake.yaml's own stand-in for commands.train,
    which the fake profile doesn't declare) keeps every other line
    byte-identical."""
    before = _profile_path().read_text()
    r = client.patch("/api/profile", json={"patch": {"hooks.locate_run": "fakefw.hooks:locate_run_v2"}})
    assert r.status_code == 200, r.get_json()
    after = _profile_path().read_text()

    before_lines = before.splitlines()
    after_lines = after.splitlines()
    assert len(before_lines) == len(after_lines)
    diff_lines = [i for i, (b, a) in enumerate(zip(before_lines, after_lines)) if b != a]
    assert len(diff_lines) == 1, diff_lines
    changed = diff_lines[0]
    assert "locate_run" in after_lines[changed]
    assert "fakefw.hooks:locate_run_v2" in after_lines[changed]

    # Took effect live, no restart needed.
    assert settings.hooks["locate_run"] == "fakefw.hooks:locate_run_v2"


def test_patch_validates_before_writing_anything(client):
    before = _profile_path().read_text()
    r = client.patch("/api/profile", json={"patch": {"manifest_layout": "not-a-real-layout"}})
    assert r.status_code == 400
    assert _profile_path().read_text() == before  # untouched


def test_patch_rejects_a_bad_key_shape(client):
    r = client.patch("/api/profile", json={"patch": {"": "x"}})
    assert r.status_code == 400
    r = client.patch("/api/profile", json={"patch": {}})
    assert r.status_code == 400


def test_raw_get_and_put_roundtrip(client):
    r = client.get("/api/profile/raw")
    assert r.status_code == 200
    text = r.get_json()["text"]
    new_text = text + "\nallow_unpushed: true\n"
    r = client.put("/api/profile/raw", json={"text": new_text})
    assert r.status_code == 200
    assert settings.allow_unpushed is True
    assert _profile_path().read_text() == new_text


def test_raw_put_rejects_invalid_yaml_and_leaves_the_file_untouched(client):
    before = _profile_path().read_text()
    r = client.put("/api/profile/raw", json={"text": "repo_root: [unterminated"})
    assert r.status_code == 400
    assert _profile_path().read_text() == before


def test_dotted_patch_helper_creates_missing_sections():
    got = profile_ops.patch_profile({"a_new_section.deep.value": 42})
    assert got["parsed"]["a_new_section"]["deep"]["value"] == 42


def test_patch_to_commands_train_on_the_real_migrated_profile_keeps_every_comment():
    """§10 Phase 2 acceptance, literally: repos/dissert.yaml (migrated for
    real this phase) has a real `commands.train` key with real surrounding
    comments — a PATCH to it keeps every other line byte-identical.

    Manages backend.config.REPOS_DIR itself (save/restore in a plain
    try/finally) rather than via the `monkeypatch` fixture: the autouse
    `_restore_fake_profile` fixture above needs REPOS_DIR back at its real
    value *during its own teardown* (it calls settings.reload("fake")), and
    fixture teardown ordering between an autouse fixture and a fixture the
    test requests directly isn't something to depend on here."""
    from backend import config as config_mod
    from conftest import XDASH_ROOT

    real_repos = XDASH_ROOT / "repos"
    path = real_repos / "dissert.yaml"
    if not path.is_file():
        pytest.skip("repos/dissert.yaml not present")
    before = path.read_text()
    if "commands" not in before or "train:" not in before:
        pytest.skip("repos/dissert.yaml hasn't been migrated to a sectioned commands.train")
    saved_repos_dir = config_mod.REPOS_DIR
    config_mod.REPOS_DIR = real_repos
    try:
        profile_ops.patch_profile(
            {"commands.train": "{python} train.py --config {config} --seeds {seed} --repeats 1 {budget} {resume} {extra} --patched"},
            profile_name="dissert",
        )
        after = path.read_text()
        before_lines, after_lines = before.splitlines(), after.splitlines()
        assert len(before_lines) == len(after_lines)
        diffs = [i for i, (b, a) in enumerate(zip(before_lines, after_lines)) if b != a]
        assert len(diffs) == 1, diffs
        assert "--patched" in after_lines[diffs[0]]
    finally:
        path.write_text(before)
        config_mod.REPOS_DIR = saved_repos_dir
