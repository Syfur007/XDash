"""backend/housekeeping.py (XDASH_FIXES_PLAN.md F5, closes issue #1) —
every category rule against a temp tree, the never-touch list, symlink/`..`
escape refusal, dry_run's own zero-change guarantee, the delete_experiment/
delete_dataset cascades, scheduler items an in-flight attempt still needs,
and an unreachable host never blocking the sweep. Every test runs entirely
against the per-test fake world conftest.py already builds (a throwaway
data/ dir and host repo) — never the real data/ dir, never a real host, and
tmux is monkeypatched exactly the way tests/test_monitors.py already does
for a "session that's alive" (the global harness fake in conftest.py always
reports no tmux at all, so a test that needs one alive builds its own
fake here)."""
from __future__ import annotations

import os
import socket
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend import datasets as datasets_mod
from backend import experiments
from backend import hosts
from backend import housekeeping as hk
from backend import scheduler
from backend import terminals as terminals_mod
from backend import transport as transport_mod
from backend.config import settings

from conftest import HOST


# --------------------------------------------------------------------------- helpers
def _iso_ago(**kw) -> str:
    return (datetime.now() - timedelta(**kw)).isoformat(timespec="seconds")


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _age(path: Path, **kw) -> None:
    t = time.time() - timedelta(**kw).total_seconds()
    os.utime(path, (t, t))


def _machine_experiment(attempt_status="done", session_name="sess1", host_id="local"):
    """A minimal Experiment + Attempt with real scheduler train/eval items,
    crafted directly (same technique tests/test_completion.py's own
    _machine_attempt() helper uses) — no dispatch/FakeTransport needed since
    housekeeping only ever reads unit_ref/status, never runs anything."""
    sdata = scheduler._load()
    sdata["items"] += [
        {"id": "trainitem-" + session_name, "status": "completed", "mode": "train",
         "session_name": session_name, "host_id": host_id, "config_path": "experiment/demo.yaml", "extra_args": ""},
    ]
    scheduler._save(sdata)
    data = experiments._load()
    exp_id = "demo_exp-s%s" % session_name
    exp = {"experiment_id": exp_id, "config_path": "experiment/demo.yaml", "seed": 1, "pool": "*",
           "extra_args": {"train": "", "eval": ""}, "max_retries": 1, "max_legs": 6, "attempt_ids": [],
           "current_attempt_id": None, "created_at": "2026-01-01T00:00:00+00:00"}
    data["experiments"][exp_id] = exp
    attempt = experiments._append_attempt(data, exp)
    attempt.update({"status": attempt_status, "slot": "local", "started_at": "2026-01-01T00:00:00+00:00",
                     "unit_ref": {"train_item_id": "trainitem-" + session_name}})
    experiments._save(data)
    return exp_id, attempt["attempt_id"]


def _register_terminal_record(session_name="sess1", host_id="local"):
    records = terminals_mod._load()
    records.append({
        "session_name": session_name, "host_id": host_id, "config_path": "experiment/demo.yaml",
        "cli_config": None, "full_command": None, "mode": "train", "extra_args": "",
        "experiment_name": "demo_exp", "command": "x", "created_at": "2026-01-01T00:00:00",
        "restart_count": 0, "return_code": None,
    })
    terminals_mod._save(records)


class _FakeAliveTmux:
    """Enough of tmux_runner's public surface for one session to look 'alive
    and just finished' — monkeypatched *function by function* onto the real
    terminals_mod.tmux module object (never replacing the module itself, so
    marker_for()'s own tmux.DONE_MARKER read keeps working), exactly the
    convention tests/test_monitors.py already uses for monitors.tmux."""

    def __init__(self, monkeypatch, session_name, exit_code=0):
        self.session_name = session_name
        self.exit_code = exit_code
        self.alive = True
        self.killed = []
        self.captured = 0
        monkeypatch.setattr(terminals_mod.tmux, "has_session", self.has_session)
        monkeypatch.setattr(terminals_mod.tmux, "list_sessions", self.list_sessions)
        monkeypatch.setattr(terminals_mod.tmux, "capture_pane", self.capture_pane)
        monkeypatch.setattr(terminals_mod.tmux, "capture_pane_tail", self.capture_pane_tail)
        monkeypatch.setattr(terminals_mod.tmux, "kill_session", self.kill_session)

    def has_session(self, session, host_id=None):
        return session == self.session_name and self.alive

    def list_sessions(self, host_id=None):
        return [self.session_name] if self.alive else []

    def _pane_text(self, session):
        return "pane output\n%s:%d\n" % (terminals_mod.marker_for(session), self.exit_code)

    def capture_pane(self, session, host_id=None):
        if session != self.session_name or not self.alive:
            return None
        self.captured += 1
        return self._pane_text(session)

    def capture_pane_tail(self, session, lines=200, host_id=None):
        return self.capture_pane(session, host_id=host_id)

    def kill_session(self, session, host_id=None):
        if session == self.session_name:
            self.killed.append(session)
            self.alive = False


@pytest.fixture
def alive_tmux(monkeypatch):
    return _FakeAliveTmux(monkeypatch, "sess1")


# --------------------------------------------------------------------------- thumbnails
def test_thumbnails_orphan_dir_and_lru_cap():
    datasets_mod.update_dataset("demo", tags=["kept"])
    root = settings.state_dir / "thumbs"
    orphan_dir = root / "not-a-real-dataset"
    _write(orphan_dir / "a.jpg", "x" * 10)
    kept_dir = root / "demo"
    old_f = kept_dir / "old.jpg"
    new_f = kept_dir / "new.jpg"
    _write(old_f, "y" * 1000)
    _write(new_f, "z" * 1000)
    _age(old_f, days=2)

    hk.set_settings({"thumbnail_cap_mb": 0.001})  # ~1KB cap: only the newest file for "demo" survives
    result = hk.inventory()["thumbnails"]
    assert result["eligible_count"] >= 2  # the orphan dir + at least one over-cap file

    # dry_run changes nothing
    before = sorted(p.name for p in root.rglob("*") if p.is_file())
    hk.clean(["thumbnails"], dry_run=True)
    after = sorted(p.name for p in root.rglob("*") if p.is_file())
    assert before == after

    cleaned = hk.clean(["thumbnails"], dry_run=False)["thumbnails"]
    assert cleaned["removed_count"] >= 2
    assert not orphan_dir.exists()
    assert not old_f.exists()
    assert new_f.exists()  # the newest kept file survives the cap eviction


# --------------------------------------------------------------------------- attempt logs
def test_attempt_logs_orphans_only_never_expire_by_age():
    known_dir = settings.attempt_logs_root / "atmpt_known00"
    orphan_dir = settings.attempt_logs_root / "atmpt_orphan0"
    _write(known_dir / "train.log")
    _write(orphan_dir / "train.log")
    _age(known_dir / "train.log", days=999)  # ancient, but still known -> kept forever
    _age(orphan_dir / "train.log", days=0)   # brand new, but orphan -> removed anyway

    data = experiments._load()
    data["attempts"]["atmpt_known00"] = {"attempt_id": "atmpt_known00", "status": "done"}
    experiments._save(data)

    hk.clean(["attempt_logs"], dry_run=False)
    assert known_dir.is_dir()
    assert not orphan_dir.exists()


# --------------------------------------------------------------------------- terminal snapshots
def test_terminal_snapshots_orphan_only():
    _register_terminal_record("known-sess")
    known_snap = settings.dashboard_log_dir / "known-sess.log"
    orphan_snap = settings.dashboard_log_dir / "orphan-sess.log"
    _write(known_snap)
    _write(orphan_snap)

    hk.clean(["terminal_snapshots"], dry_run=False)
    assert known_snap.is_file()
    assert not orphan_snap.exists()


# --------------------------------------------------------------------------- overlays
def test_overlays_orphan_only():
    from backend import framework
    root = settings.repo_root / framework.OVERLAY_DIR
    _write(root / "demo_exp-s42.yaml")
    _write(root / "gone_exp-s1.yaml")
    data = experiments._load()
    data["experiments"]["demo_exp-s42"] = {"experiment_id": "demo_exp-s42"}
    experiments._save(data)

    hk.clean(["overlays"], dry_run=False)
    assert (root / "demo_exp-s42.yaml").is_file()
    assert not (root / "gone_exp-s1.yaml").exists()


# --------------------------------------------------------------------------- remote staging
def test_remote_staging_orphan_attempt_and_empty_host_dir():
    root = settings.repo_root / "outputs" / "remote"
    _write(root / "hostA" / "atmpt_known00" / "manifest.json")
    _write(root / "hostA" / "atmpt_orphan0" / "manifest.json")
    (root / "hostB").mkdir(parents=True)  # already-empty host dir, no attempts at all

    data = experiments._load()
    data["attempts"]["atmpt_known00"] = {"attempt_id": "atmpt_known00", "status": "done"}
    experiments._save(data)

    hk.clean(["remote_staging"], dry_run=False)
    assert (root / "hostA" / "atmpt_known00").is_dir()
    assert not (root / "hostA" / "atmpt_orphan0").exists()
    assert not (root / "hostB").exists()


# --------------------------------------------------------------------------- crash temp dirs
def test_crash_temp_dirs_prefix_age_and_ownership(monkeypatch, tmp_path):
    monkeypatch.setattr(hk.tempfile, "gettempdir", lambda: str(tmp_path))
    old_hit = tmp_path / "kaggle_push_abc123"
    old_hit.mkdir()
    _write(old_hit / "f", "x")
    _age(old_hit, days=2)
    fresh_hit = tmp_path / "xdash_seed_def456"
    fresh_hit.mkdir()
    _write(fresh_hit / "f", "x")  # matches a prefix but too new — not eligible
    not_ours = tmp_path / "some_other_tool_tmp"
    not_ours.mkdir()
    _write(not_ours / "f", "x")
    _age(not_ours, days=2)  # old, but doesn't match any of our own prefixes

    result = hk.inventory()["crash_temp_dirs"]
    assert result["total_count"] == 2  # only the two prefix-matching entries are even counted
    assert result["eligible_count"] == 1

    # Ownership gate: pretend we're a different user than whoever owns these
    # (ourselves, in this test) — the entries must disappear from eligible.
    monkeypatch.setattr(hk.os, "getuid", lambda: os.getuid() + 12345)
    assert hk.inventory()["crash_temp_dirs"]["eligible_count"] == 0
    monkeypatch.undo()
    monkeypatch.setattr(hk.tempfile, "gettempdir", lambda: str(tmp_path))

    hk.clean(["crash_temp_dirs"], dry_run=False)
    assert not old_hit.exists()
    assert fresh_hit.exists()
    assert not_ours.exists()


# --------------------------------------------------------------------------- ssh control sockets
def test_ssh_control_socket_stale_vs_alive(monkeypatch, tmp_path):
    monkeypatch.setattr(transport_mod, "CONTROL_DIR", tmp_path)
    stale = tmp_path / "stale.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(stale))
    s.close()  # bound, never listened on — connecting to it now refuses, exactly "stale"

    alive_path = tmp_path / "alive.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(alive_path))
    listener.listen(1)
    try:
        result = hk.inventory()["ssh_control_sockets"]
        assert result["total_count"] == 2
        assert result["eligible_count"] == 1
        hk.clean(["ssh_control_sockets"], dry_run=False)
        assert not stale.exists()
        assert alive_path.exists()  # a live listener is never touched
    finally:
        listener.close()


# --------------------------------------------------------------------------- finished tmux sessions
def test_finished_tmux_session_attempt_owned_is_reaped_immediately(alive_tmux):
    _register_terminal_record("sess1")
    _machine_experiment(attempt_status="done", session_name="sess1")

    scan = hk._scan_finished_tmux_sessions(False)
    assert scan["eligible"] == [{"session_name": "sess1", "host_id": "local", "bytes": 0, "reason": "attempt-owned"}]

    hk.clean(["finished_tmux_sessions"], dry_run=False)
    assert alive_tmux.killed == ["sess1"]
    assert alive_tmux.captured >= 1  # persisted before it was killed
    # The record itself survives — only "terminal records" (a separate,
    # later category) decides whether to forget it.
    assert terminals_mod.get_terminal("sess1") is not None


def test_finished_tmux_session_ad_hoc_waits_out_its_grace_period(monkeypatch):
    fake = _FakeAliveTmux(monkeypatch, "sess2")
    _register_terminal_record("sess2")  # no scheduler item at all -> ad hoc

    hk.clean(["finished_tmux_sessions"], dry_run=False)
    assert fake.killed == []  # too soon — the grace clock only just started

    seen = hk._load_seen()
    seen["sess2"] = _iso_ago(hours=25)  # backdate past the default 24h grace
    hk._save_seen(seen)

    hk.clean(["finished_tmux_sessions"], dry_run=False)
    assert fake.killed == ["sess2"]


def test_finished_tmux_session_dry_run_kills_nothing(alive_tmux):
    _register_terminal_record("sess1")
    _machine_experiment(attempt_status="done", session_name="sess1")
    hk.clean(["finished_tmux_sessions"], dry_run=True)
    assert alive_tmux.killed == []
    assert alive_tmux.alive is True


def test_finished_tmux_sessions_tolerates_an_unreachable_host(monkeypatch):
    hosts.upsert_host({"id": "downbox", "kind": "ssh", "label": "Down box", "ssh": {"host": "nowhere.example"}})

    def boom(host_id=None):
        raise transport_mod.TransportError("unreachable")

    monkeypatch.setattr(terminals_mod, "list_terminals", lambda host_id=None: boom(host_id) if host_id == "downbox" else [])
    # Must not raise, must not block the rest of the sweep.
    result = hk._scan_finished_tmux_sessions(False)
    assert result["total_count"] == 0


# --------------------------------------------------------------------------- terminal records
def test_terminal_records_pruned_after_retention_once_not_alive(alive_tmux):
    _register_terminal_record("sess1")
    _machine_experiment(attempt_status="done", session_name="sess1")
    hk.set_settings({"retention_days": 0})

    hk.clean(["finished_tmux_sessions"], dry_run=False)  # reaps it -> no longer alive, seen_at stamped
    assert alive_tmux.alive is False

    hk.clean(["terminal_records"], dry_run=False)
    assert terminals_mod.get_terminal("sess1") is None


def test_terminal_records_lost_uses_its_own_lost_at():
    _register_terminal_record("lostsess")
    records = terminals_mod._load()
    for r in records:
        if r["session_name"] == "lostsess":
            r["status"] = "lost"
            r["lost_at"] = _iso_ago(days=30)
    terminals_mod._save(records)
    # The global harness fake (conftest.py) already makes every tmux session
    # report "not alive" — exactly what a genuinely lost record needs here.

    hk.set_settings({"retention_days": 14})
    hk.clean(["terminal_records"], dry_run=False)
    assert terminals_mod.get_terminal("lostsess") is None


# --------------------------------------------------------------------------- scheduler history
def test_scheduler_history_protects_items_of_a_non_terminal_attempt():
    sdata = scheduler._load()
    sdata["items"] += [
        {"id": "old-done", "status": "completed", "mode": "train", "session_name": None,
         "config_path": "experiment/demo.yaml", "extra_args": "", "ended_at": _iso_ago(days=30)},
        {"id": "old-protected", "status": "completed", "mode": "train", "session_name": None,
         "config_path": "experiment/demo.yaml", "extra_args": "", "ended_at": _iso_ago(days=30)},
    ]
    scheduler._save(sdata)
    data = experiments._load()
    data["attempts"]["atmpt_inflight"] = {
        "attempt_id": "atmpt_inflight", "status": "running",  # not terminal -> protects its item
        "unit_ref": {"train_item_id": "old-protected"},
    }
    experiments._save(data)

    hk.set_settings({"retention_days": 14})
    result = hk.clean(["scheduler_history"], dry_run=False)["scheduler_history"]
    assert result["removed_count"] == 1
    remaining_ids = {i["id"] for i in scheduler.list_items()["items"]}
    assert "old-protected" in remaining_ids
    assert "old-done" not in remaining_ids


def test_scheduler_history_dry_run_removes_nothing():
    sdata = scheduler._load()
    sdata["items"].append({"id": "old-done2", "status": "completed", "mode": "train", "session_name": None,
                            "config_path": "experiment/demo.yaml", "extra_args": "", "ended_at": _iso_ago(days=30)})
    scheduler._save(sdata)
    hk.set_settings({"retention_days": 14})
    hk.clean(["scheduler_history"], dry_run=True)
    assert any(i["id"] == "old-done2" for i in scheduler.list_items()["items"])


# --------------------------------------------------------------------------- migration backups / orphan accounts / kaggle downloads (manual)
def test_migration_backups_scan_only_matches_known_legacy_names():
    _write(settings.state_dir / "kaggle_state.json", "{}")
    _write(settings.state_dir / "assignments.json", "[]")
    _write(config_dir_pre_v3_bak())
    _write(settings.state_dir / "not_a_legacy_file.json", "{}")

    result = hk.inventory()["migration_backups"]
    assert result["eligible_count"] == 3  # kaggle_state.json + assignments.json + the *.pre-v3.bak
    hk.clean(["migration_backups"], dry_run=False)
    assert not (settings.state_dir / "kaggle_state.json").exists()
    assert not (settings.state_dir / "assignments.json").exists()
    assert (settings.state_dir / "not_a_legacy_file.json").exists()  # never touched — not on the exact list


def config_dir_pre_v3_bak() -> Path:
    from backend import config as config_mod
    return config_mod.DATA_DIR / "hosts.json.pre-v3.bak"


def test_orphan_account_dirs_manual_category():
    from backend import kaggle as kaggle_backend
    kaggle_backend.add_account("keepme", username="u", key="k")
    (settings.kaggle_creds_dir / "keepme").mkdir(parents=True, exist_ok=True)
    (settings.kaggle_creds_dir / "ghost").mkdir(parents=True, exist_ok=True)

    result = hk.inventory()["orphan_account_dirs"]
    assert result["auto"] is False
    hk.clean(["orphan_account_dirs"], dry_run=False)
    assert (settings.kaggle_creds_dir / "keepme").is_dir()
    assert not (settings.kaggle_creds_dir / "ghost").exists()


def test_kaggle_downloads_manual_category_orphan_only():
    root = settings.repo_root / "outputs" / "kaggle"
    _write(root / "demo_exp-s42" / "output.zip")
    _write(root / "gone_exp-s1" / "output.zip")
    data = experiments._load()
    data["experiments"]["demo_exp-s42"] = {"experiment_id": "demo_exp-s42"}
    experiments._save(data)

    hk.clean(["kaggle_downloads"], dry_run=False)
    assert (root / "demo_exp-s42").is_dir()
    assert not (root / "gone_exp-s1").exists()


# --------------------------------------------------------------------------- never-touch list
def test_never_touches_run_outputs_ledger_repos_or_live_stores(monkeypatch, tmp_path):
    # crash_temp_dirs otherwise targets the real system tempdir even under
    # CATEGORY_ORDER's dry_run — never acceptable in a test (hard constraint:
    # no sweep, not even a read-only one, against the real /tmp).
    monkeypatch.setattr(hk.tempfile, "gettempdir", lambda: str(tmp_path / "faketmp"))
    keep_output = settings.repo_root / "outputs" / "experiments" / "demo_exp" / "abcdef-s42" / "checkpoints" / "manifest.json"
    _write(keep_output, "{}")
    keep_ledger = settings.ledger_dir / "runs.csv"
    _write(keep_ledger, "run_id\n")
    from backend import config as config_mod
    keep_profile = config_mod.REPOS_DIR / "fake.yaml"
    assert keep_profile.is_file()
    keep_terminals_store = settings.state_file
    _write(keep_terminals_store, "[]")
    keep_scheduler_store = settings.scheduler_file
    _write(keep_scheduler_store, "{}")
    keep_creds = settings.kaggle_creds_dir / "registered"
    from backend import kaggle as kaggle_backend
    kaggle_backend.add_account("registered", username="u", key="k")
    keep_creds.mkdir(parents=True, exist_ok=True)
    _write(keep_creds / "kaggle.json", "{}")

    before = keep_profile.read_text()
    hk.clean(hk.CATEGORY_ORDER, dry_run=False)

    assert keep_output.is_file()
    assert keep_ledger.is_file()
    assert keep_profile.is_file() and keep_profile.read_text() == before
    assert keep_terminals_store.is_file()
    assert keep_scheduler_store.is_file()
    assert keep_creds.is_dir() and (keep_creds / "kaggle.json").is_file()


# --------------------------------------------------------------------------- symlink / escape refusal
def test_removal_refuses_a_path_outside_its_root(tmp_path):
    root = settings.attempt_logs_root
    root.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim"
    victim.mkdir()
    _write(victim / "secret.txt", "do not delete me")

    act = hk._act_remove_path(root)
    with pytest.raises(hk.HousekeepingError):
        act({"path": str(victim), "bytes": 0, "reason": "orphan"})
    assert (victim / "secret.txt").is_file()

    with pytest.raises(hk.HousekeepingError):
        act({"path": str(root.parent), "bytes": 0, "reason": "orphan"})  # a ".."-style escape
    assert root.is_dir()


def test_removal_refuses_a_symlink_escaping_its_root(tmp_path):
    root = settings.attempt_logs_root
    root.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim_dir"
    victim.mkdir()
    _write(victim / "secret.txt", "do not delete me")
    link = root / "atmpt_evil0000"
    link.symlink_to(victim, target_is_directory=True)

    data = experiments._load()  # no known attempts -> the linked name looks orphaned
    experiments._save(data)
    result = hk.clean(["attempt_logs"], dry_run=False)["attempt_logs"]
    assert result["errors"]  # refused, reported, never raised out of clean()
    assert victim.is_dir() and (victim / "secret.txt").is_file()


def test_removal_refuses_the_category_root_itself():
    root = settings.attempt_logs_root
    root.mkdir(parents=True, exist_ok=True)
    act = hk._act_remove_path(root)
    with pytest.raises(hk.HousekeepingError):
        act({"path": str(root), "bytes": 0, "reason": "orphan"})
    assert root.is_dir()


# --------------------------------------------------------------------------- dry_run makes zero changes
def test_dry_run_across_every_category_changes_nothing_on_disk(monkeypatch, tmp_path):
    # Same reasoning as the never-touch test above: crash_temp_dirs must
    # never be pointed at the real system tempdir from inside a test.
    monkeypatch.setattr(hk.tempfile, "gettempdir", lambda: str(tmp_path / "faketmp"))
    # Seed at least one item per filesystem-backed category so dry_run has
    # something to (not) act on.
    datasets_mod.update_dataset("demo", tags=[])
    _write(settings.state_dir / "thumbs" / "orphan-ds" / "a.jpg", "x" * 10)
    _write(settings.attempt_logs_root / "atmpt_orphan1" / "train.log")
    _register_terminal_record("orphan-snap-sess")
    _write(settings.dashboard_log_dir / "orphan-snap-sess-but-different.log")
    from backend import framework
    _write(settings.repo_root / framework.OVERLAY_DIR / "gone-s1.yaml")
    _write(settings.repo_root / "outputs" / "remote" / "hostX" / "atmpt_orphanR" / "f.txt")
    _write(settings.repo_root / "outputs" / "kaggle" / "gone_exp2" / "f.txt")
    _write(settings.state_dir / "kaggle_state.json", "{}")
    crash_dir = tmp_path / "faketmp" / "xdash_seed_zzz"
    _write(crash_dir / "f")
    _age(crash_dir, days=2)

    def _snapshot(root: Path):
        if not root.is_dir():
            return frozenset()
        return frozenset((str(p.relative_to(root)), p.is_file() and p.stat().st_size) for p in root.rglob("*"))

    roots = [
        settings.state_dir / "thumbs", settings.attempt_logs_root, settings.dashboard_log_dir,
        settings.repo_root / "outputs", settings.state_dir, tmp_path / "faketmp",
    ]
    # Reading terminal status can itself self-heal a record (F0.6's
    # _reconcile_lost — a pre-existing, unrelated behavior: the ordinary
    # Terminals page's own GET does the same thing on first read). Settle
    # that once, *before* the snapshot, so the snapshot only ever measures
    # housekeeping's own actions, never an incidental first-read side effect.
    terminals_mod.list_terminals()
    before = [_snapshot(r) for r in roots]
    before_terminals = terminals_mod.list_terminals()
    before_scheduler = scheduler.list_items()

    hk.clean(hk.CATEGORY_ORDER, dry_run=True)

    after = [_snapshot(r) for r in roots]
    assert before == after
    assert terminals_mod.list_terminals() == before_terminals
    assert scheduler.list_items() == before_scheduler


# --------------------------------------------------------------------------- cascades
def test_delete_experiment_cascades_logs_scheduler_item_and_tmux_session(alive_tmux):
    _register_terminal_record("sess1")
    exp_id, attempt_id = _machine_experiment(attempt_status="done", session_name="sess1")
    log_dir = settings.attempt_log_dir(attempt_id)
    _write(log_dir / "train.log", "some output")

    assert experiments.delete_experiment(exp_id) is True

    assert not log_dir.exists()
    assert terminals_mod.get_terminal("sess1") is None
    assert alive_tmux.killed == ["sess1"]
    remaining_ids = {i["id"] for i in scheduler.list_items()["items"]}
    assert "trainitem-sess1" not in remaining_ids


def test_delete_dataset_cascades_its_thumbnails():
    datasets_mod.update_dataset("demo", tags=["x"])
    thumb_dir = settings.state_dir / "thumbs" / "demo"
    _write(thumb_dir / "a.jpg", "x")
    assert thumb_dir.is_dir()

    datasets_mod.delete_dataset("demo")

    assert not thumb_dir.exists()
