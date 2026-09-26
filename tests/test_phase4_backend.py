"""XDASH_PLAN.md §10 Phase 4's small backend additions: a path-existence
check for the Settings path widget, "which configs use this dataset" for
the dataset detail page, and the "+ New profile" wizard's detect/create
pair. Everything else Phase 4 needed (the dataset registry, /api/profile)
was already built in Phase 2 — see tests/test_datasets.py and
tests/test_profile_api.py."""
from __future__ import annotations

import pytest

from backend import datasets, paths, repos
from backend.config import REPOS_DIR, settings


@pytest.fixture
def client():
    import server
    return server.app.test_client()


# --------------------------------------------------------------- path_exists
def test_path_exists_finds_a_real_repo_relative_directory():
    result = paths.path_exists("repo", "configs")
    assert result == {"path": "configs", "exists": True, "kind": "directory"}


def test_path_exists_reports_false_for_a_missing_path():
    result = paths.path_exists("repo", "definitely/not/here")
    assert result["exists"] is False and result["kind"] is None


def test_path_exists_route(client):
    r = client.get("/api/paths/exists?scope=repo&path=configs")
    assert r.status_code == 200
    assert r.get_json()["exists"] is True


# --------------------------------------------------------- configs_using_dataset
def test_configs_using_dataset_walks_compose_and_inline_and_skips_the_fragment_itself():
    found = datasets.configs_using_dataset("demo")
    assert found == ["experiment/demo.yaml", "experiment/other.yaml", "experiment/third.yaml"]


def test_configs_using_dataset_route(client):
    r = client.get("/api/datasets/demo/configs")
    assert r.status_code == 200
    assert r.get_json()["configs"] == ["experiment/demo.yaml", "experiment/other.yaml", "experiment/third.yaml"]


def test_configs_using_dataset_is_empty_for_an_unknown_name():
    assert datasets.configs_using_dataset("nope-not-a-real-dataset") == []


# --------------------------------------------------------------- new-profile wizard
def test_detect_repo_reports_real_findings():
    # tests/conftest.py's fake world: REPOS and HOST are both direct
    # children of the same temp BASE dir, so "../host" from REPOS_DIR is
    # exactly HOST — the same fake repo repos/fake.yaml itself points at
    # (absolute, there) — has configs/ but no train.py/eval.py of its own
    # (only fakefw's hooks).
    detected = repos.detect_repo("../host")
    assert detected["exists"] is True
    assert detected["configs_dir"] == "configs"
    assert detected["train_py"] is False


def test_detect_repo_missing_root_reports_not_exists():
    detected = repos.detect_repo("../this/path/does/not/exist")
    assert detected["exists"] is False


def test_create_profile_writes_a_validated_sectioned_yaml(client):
    path = REPOS_DIR / "wizardtest.yaml"
    try:
        r = client.post("/api/repos", json={"name": "wizardtest", "display_name": "Wizard Test", "repo_root": "../host"})
        assert r.status_code == 200, r.get_json()
        assert path.is_file()
        text = path.read_text()
        assert "framework:" in text and 'repo_root: "../host"' in text
        assert any(p["id"] == "wizardtest" for p in r.get_json()["repos"])
    finally:
        path.unlink(missing_ok=True)


def test_create_profile_rejects_a_duplicate_name(client):
    path = REPOS_DIR / "wizardtest2.yaml"
    try:
        r1 = client.post("/api/repos", json={"name": "wizardtest2", "repo_root": "../host"})
        assert r1.status_code == 200
        r2 = client.post("/api/repos", json={"name": "wizardtest2", "repo_root": "../host"})
        assert r2.status_code == 400
        assert "already exists" in r2.get_json()["detail"]
    finally:
        path.unlink(missing_ok=True)


def test_create_profile_rejects_a_missing_repo_root(client):
    r = client.post("/api/repos", json={"name": "wizardtest3", "repo_root": "../nope-does-not-exist"})
    assert r.status_code == 400
    assert not (REPOS_DIR / "wizardtest3.yaml").exists()


def test_create_profile_rejects_a_bad_name_slug(client):
    r = client.post("/api/repos", json={"name": "not a slug!", "repo_root": "../host"})
    assert r.status_code == 400
