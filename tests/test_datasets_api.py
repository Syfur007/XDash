"""DATASETS_PLAN.md §9/§12 D2 acceptance: Flask-test-client coverage for
every new dataset route, including the error paths (bad slug 400, duplicate
409, fragment-exists response, path-escape refusals for /api/fs/list,
/tree, /thumb, /file)."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.config import settings

from conftest import HOST


@pytest.fixture
def client():
    import server
    return server.app.test_client()


# Self-sufficient even if this file runs on its own (not alongside
# test_datasets.py, which sets up the same fixture) — idempotent, so no harm
# if both run in the same session.
(HOST / "data" / "demo").mkdir(parents=True, exist_ok=True)
(HOST / "data" / "demo" / "sample.txt").write_text("hello\n")
(HOST / "configs" / "dataset" / "colondb.yaml").write_text(
    "dataset: {name: ColonDB, root: data/polyp/ColonDB}\n"
)


# ----------------------------------------------------------------- CRUD error paths
def test_bad_slug_returns_400(client):
    r = client.patch("/api/datasets/demo", json={"sources": {"kaggle": {"slug": "not a slug"}}})
    assert r.status_code == 400


def test_duplicate_draft_name_returns_409(client):
    r = client.post("/api/datasets", json={"name": "dup-http"})
    assert r.status_code == 200
    r = client.post("/api/datasets", json={"name": "dup-http"})
    assert r.status_code == 409


def test_create_over_a_fragment_reports_exists_fragment(client):
    r = client.post("/api/datasets", json={"name": "ColonDB"})
    assert r.status_code == 200
    assert r.get_json() == {"exists": "fragment", "name": "ColonDB"}


def test_unknown_dataset_get_is_404(client):
    r = client.get("/api/datasets/does-not-exist-anywhere")
    assert r.status_code == 404


def test_host_override_route_rejects_a_non_ssh_slot(client):
    r = client.put("/api/datasets/demo/hosts/kaggle:x", json={"path": "/nope"})
    assert r.status_code == 400


def test_host_override_crud_over_http(client):
    r = client.put("/api/datasets/demo/hosts/ssh:box", json={"path": "/data/shared/demo"})
    assert r.status_code == 200 and r.get_json()["hosts"]["ssh:box"]["path"] == "/data/shared/demo"
    r = client.delete("/api/datasets/demo/hosts/ssh:box")
    assert r.status_code == 200 and "ssh:box" not in r.get_json()["hosts"]


def test_link_route_requires_a_declared_fragment(client):
    client.post("/api/datasets", json={"name": "loose-draft"})
    r = client.post("/api/datasets/loose-draft/link", json={"fragment": "nope-not-real"})
    assert r.status_code == 400
    r = client.post("/api/datasets/loose-draft/link", json={"fragment": "ColonDB"})
    assert r.status_code == 200 and r.get_json()["name"] == "ColonDB"


# ----------------------------------------------------------------- /api/fs/list (§8.5)
def test_fs_list_refuses_paths_outside_home(client, monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    r = client.get("/api/fs/list?path=/etc")
    assert r.status_code == 400


def test_fs_list_lists_a_real_home_directory(client, monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    (fake_home / "sub").mkdir(parents=True)
    (fake_home / "pic.png").write_bytes(b"\x89PNG")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    r = client.get("/api/fs/list")
    assert r.status_code == 200
    body = r.get_json()
    names = {e["name"] for e in body["entries"]}
    assert "sub" in names and "pic.png" not in names  # dir mode: no files
    r = client.get("/api/fs/list?mode=file")
    names = {e["name"] for e in r.get_json()["entries"]}
    assert "pic.png" in names


def test_fs_list_requires_api_token_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "secret-token")
    try:
        r = client.get("/api/fs/list")
        assert r.status_code == 401
        r = client.get("/api/fs/list", headers={"X-Api-Token": "secret-token"})
        assert r.status_code == 200
    finally:
        monkeypatch.setattr(settings, "api_token", "")


def test_fs_list_refuses_when_not_loopback_and_not_opted_in(client, monkeypatch):
    monkeypatch.setattr(settings, "server_host", "0.0.0.0")
    monkeypatch.setattr(settings, "datasets_allow_fs_browse", False)
    r = client.get("/api/fs/list")
    assert r.status_code == 403
    monkeypatch.setattr(settings, "datasets_allow_fs_browse", True)
    r = client.get("/api/fs/list")
    assert r.status_code == 200


# ----------------------------------------------------------------- /tree, /thumb, /file (§8.4)
def test_tree_refuses_a_path_that_escapes_the_dataset_root(client):
    r = client.get("/api/datasets/demo/tree?path=../../etc")
    assert r.status_code == 400


def test_tree_and_file_serve_real_content(client):
    r = client.get("/api/datasets/demo/tree")
    assert r.status_code == 200
    assert "sample.txt" not in r.get_json()["images"]  # not an image extension, filtered out

    r = client.get("/api/datasets/demo/file?path=sample.txt")
    assert r.status_code == 200
    assert r.data == b"hello\n"

    r = client.get("/api/datasets/demo/file?path=../../../etc/passwd")
    assert r.status_code == 400


def test_thumb_refuses_escaping_paths(client):
    r = client.get("/api/datasets/demo/thumb?path=../../../etc/passwd")
    assert r.status_code == 400


def test_thumb_renders_a_real_image(client, tmp_path):
    from PIL import Image
    img_path = HOST / "data" / "demo" / "pic.png"
    Image.new("RGB", (32, 32), color="red").save(img_path)
    r = client.get("/api/datasets/demo/thumb?path=pic.png&size=16")
    assert r.status_code == 200 and r.mimetype == "image/jpeg"
