"""backend/tools.py — the CLI/binary registry (XDASH_FIXES_PLAN.md F2,
issue 7): resolution order (D3), the version gate, the one-time profile
migration (D2, against temp copies only — never the real repos/*.yaml), and
the local-vs-remote split every caller that reaches a *different* host must
still apply itself (tmux, nvidia-smi, ssh/rsync)."""
from __future__ import annotations

import subprocess
import sys

import pytest

from backend import config as config_mod
from backend import tools


@pytest.fixture(autouse=True)
def _clean_tool_cache():
    tools.reset_cache_for_tests()
    yield
    tools.reset_cache_for_tests()


@pytest.fixture
def client():
    import server
    return server.app.test_client()


# --------------------------------------------------------------------------- Settings -> Tools routes
def test_list_tools_route_includes_label_and_used_for(client):
    r = client.get("/api/tools")
    assert r.status_code == 200
    body = r.get_json()["tools"]
    assert set(body) == set(tools.TOOL_SPECS)
    assert body["kaggle"]["used_for"] == "Kaggle runtimes"
    assert body["kaggle"]["required"] is True
    assert body["nvidia-smi"]["required"] is False


def test_set_tool_override_route_round_trips(client, tmp_path):
    fake = tmp_path / "kaggle"
    fake.write_text("#!/bin/sh\necho 'Kaggle CLI 2.2.4'\n")
    fake.chmod(0o755)
    r = client.put("/api/tools/kaggle", json={"path": str(fake)})
    assert r.status_code == 200
    body = r.get_json()
    assert body["source"] == "override" and body["path"] == str(fake) and body["ok"] is True

    r = client.put("/api/tools/kaggle", json={"path": None})  # clears it
    assert r.get_json()["source"] != "override"


def test_set_tool_override_route_unknown_tool_is_404(client):
    r = client.put("/api/tools/not-a-real-tool", json={"path": "/x"})
    assert r.status_code == 404


def test_test_tool_route_forces_a_fresh_resolve(client, tmp_path):
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\necho 'git version 9.9.9'\n")
    fake.chmod(0o755)
    tools.set_override("git", str(fake))
    r = client.post("/api/tools/git/test")
    assert r.status_code == 200
    assert r.get_json()["version"] == "9.9.9"


# --------------------------------------------------------------------------- resolution order (D3)
def test_resolve_order_override_beats_sibling_beats_path(tmp_path, monkeypatch):
    fake_python_dir = tmp_path / "envbin"
    fake_python_dir.mkdir()
    sibling = fake_python_dir / "kaggle"
    sibling.write_text("#!/bin/sh\necho sibling\n")
    sibling.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(fake_python_dir / "python"))

    path_dir = tmp_path / "pathbin"
    path_dir.mkdir()
    path_copy = path_dir / "kaggle"
    path_copy.write_text("#!/bin/sh\necho path\n")
    path_copy.chmod(0o755)
    monkeypatch.setenv("PATH", str(path_dir))

    st = tools.status("kaggle", refresh=True)
    assert st.source == "sibling" and st.path == str(sibling)

    tools.set_override("kaggle", str(path_copy))
    st = tools.status("kaggle", refresh=True)
    assert st.source == "override" and st.path == str(path_copy)

    tools.set_override("kaggle", None)  # clears back to sibling/PATH resolution
    st = tools.status("kaggle", refresh=True)
    assert st.source == "sibling" and st.path == str(sibling)


def test_resolve_not_found_falls_back_to_the_bare_name(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nowhere" / "python"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    st = tools.status("nvidia-smi", refresh=True)
    assert st.source == "not-found" and st.exists is False
    # Still something a caller can exec — the same FileNotFoundError/127 a
    # bare hardcoded name always produced, never a raise from tools.path().
    assert tools.path("nvidia-smi") == "nvidia-smi"


# --------------------------------------------------------------------------- version gate
def test_kaggle_version_gate(tmp_path):
    script = tmp_path / "kaggle"
    script.write_text("#!/bin/sh\necho 'Kaggle CLI 2.2.0'\n")
    script.chmod(0o755)
    tools.set_override("kaggle", str(script))
    st = tools.status("kaggle", refresh=True)
    assert st.version == "2.2.0" and st.version_ok is False and st.ok is False
    assert "need >= 2.2.1" in st.error

    script.write_text("#!/bin/sh\necho 'Kaggle CLI 2.2.4'\n")
    st = tools.status("kaggle", refresh=True)
    assert st.version == "2.2.4" and st.version_ok is True and st.ok is True


def test_kaggle_real_installation_resolves_and_clears_the_gate():
    """Sanity check against the actually-installed CLI (2.2.4, in the xdash
    env) — a real, local, no-network `kaggle --version` call, same as the
    plan's own "Accept when" criterion for Settings -> Tools."""
    st = tools.status("kaggle", refresh=True)
    assert st.exists and st.executable
    assert st.version_ok is not False


def test_tools_with_no_version_regex_are_ok_on_existence_alone(tmp_path):
    script = tmp_path / "tmux"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    tools.set_override("tmux", str(script))
    st = tools.status("tmux", refresh=True)
    assert st.ok is True and st.version_ok is None


def test_validate_startup_reports_every_missing_required_tool_but_not_the_optional_one(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/nonexistent/python")
    monkeypatch.setenv("PATH", "")
    msgs = tools.validate_startup()
    assert any("'git'" in m for m in msgs)
    assert not any("nvidia-smi" in m for m in msgs)  # optional — never counted


# --------------------------------------------------------------------------- migration (D2)
def test_migrate_from_profiles_moves_keys_and_preserves_siblings(tmp_path, monkeypatch):
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()
    (repos_dir / "alpha.yaml").write_text(
        "display_name: Alpha\n"
        'kaggle_executable: "/opt/envs/alpha/bin/kaggle"  # points at the right env\n'
        "kaggle_push_concurrency: 3\n"
    )
    (repos_dir / "beta.yaml").write_text(
        "display_name: Beta\n"
        'kaggle_executable: "kaggle"\n'  # same as the bare default -> nothing to seed
        'colab_executable: "colab"\n'
    )
    monkeypatch.setattr(config_mod, "REPOS_DIR", repos_dir)

    result = tools.migrate_from_profiles()
    assert set(result["changed_profiles"]) == {"alpha", "beta"}
    assert result["seeded_overrides"] == {"kaggle": "/opt/envs/alpha/bin/kaggle"}

    alpha_text = (repos_dir / "alpha.yaml").read_text()
    assert "kaggle_executable" not in alpha_text
    assert "kaggle_push_concurrency: 3" in alpha_text  # an untouched sibling key survives
    assert "display_name: Alpha" in alpha_text

    beta_text = (repos_dir / "beta.yaml").read_text()
    assert "kaggle_executable" not in beta_text and "colab_executable" not in beta_text

    st = tools.status("kaggle", refresh=True)
    assert st.source == "override" and st.path == "/opt/envs/alpha/bin/kaggle"

    # Idempotent: nothing left to move, nothing new to seed.
    result2 = tools.migrate_from_profiles()
    assert result2 == {"changed_profiles": [], "seeded_overrides": {}}


def test_migrate_from_profiles_never_overwrites_an_existing_override(tmp_path, monkeypatch):
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()
    (repos_dir / "only.yaml").write_text('kaggle_executable: "/from/profile/kaggle"\n')
    monkeypatch.setattr(config_mod, "REPOS_DIR", repos_dir)
    tools.set_override("kaggle", "/already/set/kaggle")  # e.g. set by hand in Settings -> Tools first

    result = tools.migrate_from_profiles()
    assert result["seeded_overrides"] == {}  # the manual override wins, never clobbered
    assert "kaggle_executable" not in (repos_dir / "only.yaml").read_text()  # the key is still removed
    assert tools.status("kaggle", refresh=True).path == "/already/set/kaggle"


# --------------------------------------------------------------------------- local vs. remote split
# "Remote-host commands... must keep using bare names on the remote side —
# only the locally executed binary gets resolved." (task brief, and the
# module docstring above.)
def test_tmux_runner_resolves_locally_but_stays_bare_over_ssh(tmp_path):
    # _run() itself is always faked by the test harness (tests never touch a
    # real tmux — see conftest.py's world fixture), so this exercises the
    # extracted, pure _tmux_exe(host) the real _run() calls, exactly as
    # written there.
    from backend import hosts, tmux_runner

    fake_tmux = tmp_path / "tmux"
    fake_tmux.write_text("#!/bin/sh\nexit 0\n")
    fake_tmux.chmod(0o755)
    tools.set_override("tmux", str(fake_tmux))

    assert tmux_runner._tmux_exe(hosts.get_host("local")) == str(fake_tmux)

    hosts.upsert_host({"id": "box1", "kind": "ssh", "label": "box1", "ssh": {"host": "10.0.0.1"}})
    try:
        # bare — resolved on box1's own PATH, never this machine's override
        assert tmux_runner._tmux_exe(hosts.get_host("box1")) == "tmux"
    finally:
        hosts.remove_host("box1")


def test_probe_accelerator_resolves_nvidia_smi_locally_but_stays_bare_remotely(tmp_path):
    from backend import transport as transport_mod
    from backend.runners import machine as machine_mod

    fake_bin = tmp_path / "nvidia-smi"
    fake_bin.write_text("#!/bin/sh\necho 'Tesla T4, 15360'\n")
    fake_bin.chmod(0o755)
    tools.set_override("nvidia-smi", str(fake_bin))

    calls = []

    class _LocalSpy(transport_mod.LocalTransport):
        def run(self, argv, timeout=None):
            calls.append(list(argv))
            return super().run(argv, timeout=timeout)

    found = machine_mod.probe_accelerator(_LocalSpy())
    assert calls[-1][0] == str(fake_bin)
    assert found == {"name": "Tesla T4", "vram_gb": 15.0, "source": "nvidia-smi"}

    class _RemoteRecorder:
        def run(self, argv, timeout=None):
            calls.append(list(argv))
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="Tesla T4, 15360\n", stderr="")

    machine_mod.probe_accelerator(_RemoteRecorder())
    assert calls[-1][0] == "nvidia-smi"  # bare — this override must never leak onto a remote host


def test_sshtransport_resolves_ssh_and_rsync_locally(tmp_path, monkeypatch):
    """ssh/rsync are the one exception to "remote stays bare" — they ARE the
    locally-executed half of a remote operation (they're what makes the
    connection at all), so both resolve through tools.path() here."""
    from backend import transport as transport_mod

    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text("#!/bin/sh\nexit 0\n")
    fake_ssh.chmod(0o755)
    tools.set_override("ssh", str(fake_ssh))
    fake_rsync = tmp_path / "rsync"
    fake_rsync.write_text("#!/bin/sh\nexit 0\n")
    fake_rsync.chmod(0o755)
    tools.set_override("rsync", str(fake_rsync))

    host = transport_mod._AdHocSshHost({"host": "10.0.0.1", "user": "me"})
    t = transport_mod.SshTransport(host)
    assert t.argv(["true"])[0] == str(fake_ssh)
    assert t._rsh().split()[0] == str(fake_ssh)  # rsync's own -e (remote shell) argument
    # _rsync() itself is always faked by the test harness (tests never
    # ssh/rsync), so this checks the pure argv builder it calls instead.
    argv = t._rsync_argv("src/", "me@10.0.0.1:dst/", (), delete=False)
    assert argv[0] == str(fake_rsync)
