"""backend/monitors.py + backend/host_tensorboard.py (XDASH_FIXES_PLAN.md F3
— issues #3/#4/#5): the host-agnostic tool catalog, per-host instances, the
per-host availability probe, the startup migration off the old per-profile
catalog, and per-host TensorBoard. No test here ever calls a real tmux/ssh —
tmux_runner's public functions (has_session/new_session/send_keys/
kill_session/capture_pane) are monkeypatched directly on `monitors.tmux`
(the module's own `from . import tmux_runner as tmux` binding), the exact
pattern tests/test_overlay.py's own
`monkeypatch.setattr(terminals.tmux, "new_session", ...)` already
establishes — conftest's `world` fixture fakes `tmux_runner._run` itself
(always returncode 127), which is one layer lower than what these tests
need to observe (which host_id a call carried)."""
from __future__ import annotations

import json

import pytest

from backend import hosts, monitors
from backend import host_tensorboard
from backend import transport as transport_mod
from backend.config import DATA_DIR


# --------------------------------------------------------------------------- catalog CRUD (host-agnostic)
def test_add_list_remove_are_host_agnostic():
    record = monitors.add_monitor("iostat", "iostat -x 1", 2)
    assert "host_id" not in record
    assert record["name"] == "iostat" and record["command"] == "iostat -x 1" and record["builtin"] is False

    catalog = monitors.list_monitors()
    ids = {m["id"] for m in catalog}
    assert record["id"] in ids
    assert all("host_id" not in m for m in catalog)

    with pytest.raises(ValueError):
        monitors.remove_monitor("builtin-nvidia-smi")  # built-ins can't be removed

    assert monitors.remove_monitor(record["id"]) is True
    assert monitors.remove_monitor(record["id"]) is False  # already gone


def test_add_monitor_requires_name_and_command():
    with pytest.raises(ValueError):
        monitors.add_monitor("", "nvidia-smi")
    with pytest.raises(ValueError):
        monitors.add_monitor("name only", "")


# --------------------------------------------------------------------------- session naming
def test_session_name_local_unchanged_remote_gets_host_suffix():
    local = monitors._session_name("builtin-nvidia-smi", "local")
    remote = monitors._session_name("builtin-nvidia-smi", "mclab")
    assert local.endswith("_mon_builtin-nvidia-smi")
    assert "mclab" not in local
    assert remote.endswith("_mon_mclab_builtin-nvidia-smi")
    assert monitors.is_monitor_session(local) and monitors.is_monitor_session(remote)


# --------------------------------------------------------------------------- the #5 fix: host comes from the caller, never the catalog
def test_start_stop_output_go_through_the_given_host_never_local(monkeypatch):
    """The actual regression test for issue #5: a built-in's own stored
    record carries no host at all any more, so starting it on 'mclab' must
    reach mclab specifically — never silently fall back to local, which is
    exactly what the old host_id-bound catalog entry used to do."""
    hosts.upsert_host({"id": "mclab", "kind": "ssh", "ssh": {"host": "mclab.example"}})

    calls = []

    def fake_has_session(session, host_id=None):
        calls.append(("has_session", session, host_id))
        return False

    def fake_new_session(session, host_id=None):
        calls.append(("new_session", session, host_id))

    def fake_send_keys(session, command, host_id=None):
        calls.append(("send_keys", session, command, host_id))

    monkeypatch.setattr(monitors.tmux, "has_session", fake_has_session)
    monkeypatch.setattr(monitors.tmux, "new_session", fake_new_session)
    monkeypatch.setattr(monitors.tmux, "send_keys", fake_send_keys)

    result = monitors.start_monitor("builtin-nvidia-smi", "mclab")
    assert result["alive"] is True
    assert "mclab" in result["session_name"]

    host_ids_used = {c[-1] for c in calls}
    assert host_ids_used == {"mclab"}, "start_monitor must never touch 'local' when a host was named"
    new_session_calls = [c for c in calls if c[0] == "new_session"]
    assert len(new_session_calls) == 1 and new_session_calls[0][2] == "mclab"

    # A second start (already "alive" per has_session) doesn't relaunch it.
    calls.clear()
    monkeypatch.setattr(monitors.tmux, "has_session", lambda session, host_id=None: True)
    monitors.start_monitor("builtin-nvidia-smi", "mclab")
    assert not any(c[0] == "new_session" for c in calls)

    # stop() and output() are equally host-explicit.
    kill_calls = []
    monkeypatch.setattr(monitors.tmux, "kill_session", lambda session, host_id=None: kill_calls.append((session, host_id)))
    monitors.stop_monitor("builtin-nvidia-smi", "mclab")
    assert kill_calls == [(monitors._session_name("builtin-nvidia-smi", "mclab"), "mclab")]

    monkeypatch.setattr(monitors.tmux, "capture_pane", lambda session, host_id=None: "GPU 0: T4\n")
    out = monitors.get_output("builtin-nvidia-smi", "mclab")
    assert out == {"alive": True, "output": "GPU 0: T4\n"}


def test_start_unknown_host_raises_host_error():
    with pytest.raises(hosts.HostError):
        monitors.start_monitor("builtin-nvidia-smi", "does-not-exist")


def test_start_unknown_monitor_raises_value_error():
    with pytest.raises(ValueError):
        monitors.start_monitor("does-not-exist")


def test_remove_monitor_stops_it_on_every_host_it_might_be_running_on(monkeypatch):
    hosts.upsert_host({"id": "boxA", "kind": "ssh", "ssh": {"host": "a.example"}})
    hosts.upsert_host({"id": "boxB", "kind": "ssh", "ssh": {"host": "b.example"}})
    record = monitors.add_monitor("iostat", "iostat -x 1")

    killed = []
    monkeypatch.setattr(monitors.tmux, "has_session", lambda session, host_id=None: True)
    monkeypatch.setattr(monitors.tmux, "kill_session", lambda session, host_id=None: killed.append(host_id))
    monitors.remove_monitor(record["id"])
    assert set(killed) == {"local", "boxA", "boxB"}


# --------------------------------------------------------------------------- migration (F3.1)
def test_migrate_from_profiles_drops_host_id_dedupes_and_is_idempotent(settings):
    profile = settings.profile_name  # "fake" in the test world — the only profile REPOS_DIR knows about
    legacy_path = DATA_DIR / profile / "monitors.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(json.dumps([
        {"id": "builtin-nvidia-smi", "name": "GPU", "command": "nvidia-smi", "watch_interval": 2,
         "builtin": True, "host_id": "local"},  # already in DEFAULT_MONITORS by command -> deduped away
        {"id": "old-iostat", "name": "iostat", "command": "iostat -x 1", "watch_interval": 1,
         "builtin": False, "host_id": "local"},  # new -> merged, host_id dropped
    ]))

    monitors.migrate_from_profiles()

    catalog = monitors.list_monitors()
    assert all("host_id" not in m for m in catalog)
    by_command = {m["command"]: m for m in catalog}
    assert "iostat -x 1" in by_command
    assert by_command["iostat -x 1"]["name"] == "iostat"
    # deduped: still exactly one "nvidia-smi" entry (the built-in), not two
    assert sum(1 for m in catalog if m["command"] == "nvidia-smi") == 1

    count_after_first_run = len(catalog)
    monitors.migrate_from_profiles()  # a second run (e.g. the next server start) is a no-op
    assert len(monitors.list_monitors()) == count_after_first_run


def test_migrate_from_profiles_tolerates_a_missing_or_corrupt_legacy_file(settings):
    monitors.migrate_from_profiles()  # no legacy file at all -> nothing to do, no crash
    baseline = len(monitors.list_monitors())

    legacy_path = DATA_DIR / settings.profile_name / "monitors.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text("{not json")
    monitors.migrate_from_profiles()  # corrupt -> skipped, not raised
    assert len(monitors.list_monitors()) == baseline


# --------------------------------------------------------------------------- per-host availability (F3.3)
class _FakeProbeTransport:
    def __init__(self, stdout):
        self.stdout = stdout
        self.calls = 0

    def run(self, argv, timeout=None):
        import subprocess
        self.calls += 1
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=self.stdout, stderr="")


def test_check_availability_parses_yes_no_lines_and_caches(monkeypatch):
    fake = _FakeProbeTransport("nvidia-smi:yes\nnvtop:no\n")
    monkeypatch.setattr(transport_mod, "for_host", lambda host_id: fake)

    result = monitors.check_availability("mclab", ["nvidia-smi", "nvtop"])
    assert result == {"nvidia-smi": True, "nvtop": False}
    assert fake.calls == 1

    # Within the TTL, a second call is served from cache — no second probe.
    monitors.check_availability("mclab", ["nvidia-smi", "nvtop"])
    assert fake.calls == 1

    # force=True bypasses the cache.
    monitors.check_availability("mclab", ["nvidia-smi"], force=True)
    assert fake.calls == 2


def test_check_availability_degrades_to_false_when_the_host_is_unreachable(monkeypatch):
    def refuse(host_id):
        raise transport_mod.TransportError("unreachable")

    monkeypatch.setattr(transport_mod, "for_host", refuse)
    result = monitors.check_availability("mclab", ["nvidia-smi"])
    assert result == {"nvidia-smi": False}


def test_list_tools_for_host_reports_alive_and_available(monkeypatch):
    hosts.upsert_host({"id": "mclab", "kind": "ssh", "ssh": {"host": "mclab.example"}})
    monkeypatch.setattr(monitors, "check_availability", lambda host_id, binaries, force=False: {"nvidia-smi": True})
    monkeypatch.setattr(monitors.tmux, "has_session", lambda session, host_id=None: "nvidia-smi" in session)

    tools = monitors.list_tools_for_host("mclab")
    by_id = {t["id"]: t for t in tools}
    nv = by_id["builtin-nvidia-smi"]
    assert nv["alive"] is True and nv["available"] is True and nv["available_detail"] is None
    other = by_id["builtin-htop"]
    assert other["alive"] is False
    assert other["available"] is False
    assert "not installed on" in other["available_detail"]


# --------------------------------------------------------------------------- per-host TensorBoard (F3.4)
def test_forward_and_cancel_argv_use_the_same_ssh_options_as_run(monkeypatch):
    host = hosts.upsert_host({"id": "mclab", "kind": "ssh", "ssh": {"host": "mclab.example", "port": 2222}})
    t = transport_mod.SshTransport(host)
    fwd = t.forward_argv(15000, 6006)
    cancel = t.cancel_forward_argv(15000, 6006)
    assert fwd[-5:-2] == ["-O", "forward", "-L"]
    assert "127.0.0.1:15000:127.0.0.1:6006" in fwd
    assert cancel[-5:-2] == ["-O", "cancel", "-L"]
    assert fwd[-1] == cancel[-1] == t._target()
    # Same base ssh options (ControlPath etc.) as an ordinary run()/argv() call.
    assert set(t._opts()) <= set(fwd)


def test_host_tensorboard_local_delegates_to_tensorboard_manager(monkeypatch):
    calls = []
    monkeypatch.setattr(host_tensorboard.tensorboard_manager, "start", lambda: calls.append("start") or {"running": True})
    monkeypatch.setattr(host_tensorboard.tensorboard_manager, "stop", lambda: calls.append("stop") or {"running": False})
    monkeypatch.setattr(host_tensorboard.tensorboard_manager, "status", lambda: calls.append("status") or {"running": False})

    assert host_tensorboard.start("local") == {"running": True}
    assert host_tensorboard.stop("local") == {"running": False}
    assert host_tensorboard.status("local") == {"running": False}
    assert calls == ["start", "stop", "status"]


def test_host_tensorboard_remote_starts_tmux_then_opens_a_forward(monkeypatch, settings, tmp_path):
    remote_root = tmp_path / "remote-repo"
    host = hosts.upsert_host({
        "id": "mclab", "kind": "ssh", "ssh": {"host": "mclab.example"},
        "repos": {settings.profile_name: {"repo_root": str(remote_root)}},
        "env_activate_cmd": "conda activate dissert-py310",
    })

    tmux_calls = []
    session_alive = {"v": False}  # a stateful fake: new_session/kill_session flip it, has_session reads it — a
    # real tmux session persists across the has_session call that follows new_session inside start()/status();
    # an unconditional has_session=False fake would make start() think its own just-created session never came up.
    monkeypatch.setattr(host_tensorboard.tmux, "has_session", lambda session, host_id=None: session_alive["v"])
    monkeypatch.setattr(host_tensorboard.tmux, "new_session", lambda session, host_id=None: (tmux_calls.append(("new", session, host_id)), session_alive.__setitem__("v", True)))
    monkeypatch.setattr(host_tensorboard.tmux, "send_keys", lambda session, cmd, host_id=None: tmux_calls.append(("keys", cmd, host_id)))

    forward_calls = []
    monkeypatch.setattr(transport_mod.SshTransport, "open_forward", lambda self, lp, rp: forward_calls.append((lp, rp)))

    result = host_tensorboard.start("mclab")
    assert result["running"] is True
    assert result["port"] is not None
    assert str(remote_root) in result["logdir"]

    typed = [c[1] for c in tmux_calls if c[0] == "keys"]
    assert any(k.startswith("cd %s" % remote_root) for k in typed)
    assert any(k == "conda activate dissert-py310" for k in typed)
    assert any("tensorboard --logdir" in k and "--host 127.0.0.1" in k for k in typed)
    assert len(forward_calls) == 1

    # A second start while already running doesn't open a second forward.
    host_tensorboard.start("mclab")
    assert len(forward_calls) == 1

    cancel_calls = []
    monkeypatch.setattr(transport_mod.SshTransport, "close_forward", lambda self, lp, rp: cancel_calls.append((lp, rp)))
    kill_calls = []
    monkeypatch.setattr(host_tensorboard.tmux, "kill_session", lambda session, host_id=None: (kill_calls.append(host_id), session_alive.__setitem__("v", False)))
    stopped = host_tensorboard.stop("mclab")
    assert stopped["running"] is False
    assert cancel_calls == forward_calls
    assert kill_calls == ["mclab"]



# --------------------------------------------------------------------------- HTTP routes (server.py)
@pytest.fixture
def client():
    import server
    return server.app.test_client()


def test_monitor_catalog_routes_carry_no_host(client):
    r = client.post("/api/monitors", json={"name": "iostat", "command": "iostat -x 1", "watch_interval": 1})
    assert r.status_code == 200
    body = r.get_json()
    assert "host_id" not in body
    monitor_id = body["id"]

    r = client.get("/api/monitors")
    assert r.status_code == 200
    assert any(m["id"] == monitor_id for m in r.get_json()["monitors"])

    r = client.delete("/api/monitors/builtin-htop")  # a built-in can't be removed
    assert r.status_code == 400

    r = client.delete("/api/monitors/%s" % monitor_id)
    assert r.status_code == 200 and r.get_json()["removed"] is True

    r = client.delete("/api/monitors/%s" % monitor_id)
    assert r.status_code == 404


def test_host_tools_routes_use_the_url_host_not_the_catalog(client, monkeypatch):
    """The HTTP-level version of the #5 regression test: the host is a URL
    segment, and /api/hosts/<host>/tools/<id>/start reaches exactly that
    host, never local — a plain GET for an unknown host 404s cleanly."""
    hosts.upsert_host({"id": "mclab", "kind": "ssh", "ssh": {"host": "mclab.example"}})
    monkeypatch.setattr(monitors, "check_availability", lambda host_id, binaries, force=False: {b: True for b in binaries})

    calls = []
    monkeypatch.setattr(monitors.tmux, "has_session", lambda session, host_id=None: False)
    monkeypatch.setattr(monitors.tmux, "new_session", lambda session, host_id=None: calls.append(("new", host_id)))
    monkeypatch.setattr(monitors.tmux, "send_keys", lambda session, cmd, host_id=None: calls.append(("keys", host_id)))
    monkeypatch.setattr(monitors.tmux, "kill_session", lambda session, host_id=None: calls.append(("kill", host_id)))
    monkeypatch.setattr(monitors.tmux, "capture_pane", lambda session, host_id=None: "output\n")

    r = client.get("/api/hosts/mclab/tools")
    assert r.status_code == 200
    tools = {t["id"]: t for t in r.get_json()["tools"]}
    assert tools["builtin-nvidia-smi"]["available"] is True

    r = client.post("/api/hosts/mclab/tools/builtin-nvidia-smi/start")
    assert r.status_code == 200 and r.get_json()["alive"] is True
    assert set(c[1] for c in calls if c[0] in ("new", "keys")) == {"mclab"}

    monkeypatch.setattr(monitors.tmux, "has_session", lambda session, host_id=None: True)  # the session "started" above
    r = client.get("/api/hosts/mclab/tools/builtin-nvidia-smi/output")
    assert r.status_code == 200 and r.get_json()["output"] == "output\n"

    r = client.post("/api/hosts/mclab/tools/builtin-nvidia-smi/stop")
    assert r.status_code == 200
    assert ("kill", "mclab") in calls

    r = client.get("/api/hosts/does-not-exist/tools")
    assert r.status_code == 404
    r = client.post("/api/hosts/does-not-exist/tools/builtin-nvidia-smi/start")
    assert r.status_code == 404
    r = client.post("/api/hosts/mclab/tools/no-such-tool/start")
    assert r.status_code == 404


def test_host_tensorboard_routes_local_and_unknown_host(client, monkeypatch):
    monkeypatch.setattr(host_tensorboard.tensorboard_manager, "status", lambda: {"running": False, "port": 6006, "logdir": "runs"})
    r = client.get("/api/hosts/local/tensorboard/status")
    assert r.status_code == 200 and r.get_json()["running"] is False

    r = client.get("/api/hosts/does-not-exist/tensorboard/status")
    assert r.status_code == 404


def test_host_tensorboard_refuses_a_host_with_no_ssh_transport(monkeypatch):
    """A defensive guard, not reachable through server.py today (every
    backend/hosts.py host is either local or ssh) — kept because
    for_host_record() is the one seam a future host kind would go through."""
    host = hosts.upsert_host({"id": "weird", "kind": "ssh", "ssh": {"host": "weird.example"}})
    monkeypatch.setattr(transport_mod, "for_host_record", lambda h: transport_mod.LocalTransport(h.id))
    monkeypatch.setattr(host_tensorboard.tmux, "has_session", lambda session, host_id=None: False)
    monkeypatch.setattr(host_tensorboard.tmux, "new_session", lambda session, host_id=None: None)
    monkeypatch.setattr(host_tensorboard.tmux, "send_keys", lambda session, cmd, host_id=None: None)
    with pytest.raises(transport_mod.TransportError):
        host_tensorboard.start("weird")
