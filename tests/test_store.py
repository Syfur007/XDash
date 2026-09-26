"""X11 — every JSON store writes atomically, keeps a .bak, and fails loudly
on a file it can't parse (backend/store.py)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from backend import store
from backend.store import JsonStore, StoreCorruptError, backup_path

from conftest import DATA, XDASH_ROOT


def test_roundtrip_and_backup(tmp_path):
    s = JsonStore(tmp_path / "x.json", dict)
    assert s.load() == {}
    s.save({"v": 1})
    assert s.load() == {"v": 1}
    assert not backup_path(s.path).exists()  # nothing good to back up before the first save
    s.save({"v": 2})
    assert s.load() == {"v": 2}
    assert json.loads(backup_path(s.path).read_text()) == {"v": 1}
    # No temp files left behind.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.json", "x.json.bak"]


def test_parse_error_raises_instead_of_returning_empty(tmp_path):
    s = JsonStore(tmp_path / "x.json", dict)
    s.save({"precious": True})
    s.save({"precious": True, "more": 1})
    s.path.write_text('{"precious": tr')  # a torn write, the pre-X11 way
    with pytest.raises(StoreCorruptError) as e:
        s.load()
    assert "x.json.bak" in str(e.value) and "cp " in str(e.value)


def test_corrupt_file_is_never_copied_over_the_backup(tmp_path):
    s = JsonStore(tmp_path / "x.json", dict)
    s.save({"v": 1})
    s.save({"v": 2})
    s.path.write_text("garbage")
    s.save({"v": 3})  # a blind overwrite (e.g. save_dataset_map) must keep the last *good* copy
    assert json.loads(backup_path(s.path).read_text()) == {"v": 1}
    assert s.load() == {"v": 3}


def test_missing_file_with_backup_is_not_a_fresh_install(tmp_path):
    s = JsonStore(tmp_path / "x.json", dict)
    s.save({"v": 1})
    s.save({"v": 2})
    s.path.unlink()
    with pytest.raises(StoreCorruptError):
        s.load()


def test_crash_between_temp_write_and_replace_loses_nothing(tmp_path, monkeypatch):
    s = JsonStore(tmp_path / "x.json", dict)
    s.save({"experiments": {"a": 1}})
    real_replace = os.replace

    def crash_on_main(src, dst):
        if str(dst) == str(s.path):
            raise OSError("simulated crash: power cut before the rename")
        return real_replace(src, dst)

    monkeypatch.setattr(store.os, "replace", crash_on_main)
    with pytest.raises(OSError):
        s.save({"experiments": {"a": 1, "b": 2}})
    monkeypatch.setattr(store.os, "replace", real_replace)
    assert s.load() == {"experiments": {"a": 1}}  # the old, complete copy
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


WRITER = textwrap.dedent("""
    import sys
    sys.path.insert(0, %(root)r)
    from backend.store import JsonStore
    s = JsonStore(%(path)r, dict)
    i = 0
    while True:
        i += 1
        # ~1 MB per write so a kill lands mid-write often.
        s.save({"n": i, "pad": "x" * 1000000, "check": i * 7})
""")


def test_kill_9_mid_write_loses_nothing(tmp_path):
    """The plan's Phase 0 acceptance, in-suite: SIGKILL a process that is
    saving as fast as it can, repeatedly; the store always loads, and always
    holds one complete version."""
    path = tmp_path / "hammer.json"
    JsonStore(path, dict).save({"n": 0, "pad": "", "check": 0})
    script = WRITER % {"root": str(XDASH_ROOT), "path": str(path)}
    env = dict(os.environ)
    for round_ in range(8):
        proc = subprocess.Popen([sys.executable, "-c", script], env=env)
        time.sleep(0.25 + 0.05 * round_)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
        data = JsonStore(path, dict).load()  # must never raise
        assert data["check"] == data["n"] * 7
        assert len(data["pad"]) in (0, 1000000)


def test_migration_runs_in_memory_and_keeps_a_permanent_pre_migration_copy(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"items": ["old"]}))
    s = JsonStore(path, dict, schema_version=2, migrate=lambda d, v: dict(d, items=d["items"] + ["migrated"]))
    data = s.load()
    assert data == {"items": ["old", "migrated"], "schema_version": 2}
    assert json.loads(path.read_text()) == {"items": ["old"]}  # a read never writes
    s.save(data)
    keep = tmp_path / "s.json.pre-v2.bak"
    assert json.loads(keep.read_text()) == {"items": ["old"]}
    s.save({**data, "x": 1})
    assert json.loads(keep.read_text()) == {"items": ["old"]}  # never overwritten


def test_mode_is_applied_before_the_file_exists(tmp_path):
    s = JsonStore(tmp_path / "secret.json", dict, mode=0o600)
    s.save({"token": "t"})
    assert (s.path.stat().st_mode & 0o777) == 0o600


# ----------------------------------------------------------------- every store uses it
def _stores():
    """(label, path, load) for every XDash-owned JSON store (X11's list)."""
    from backend import (colab, dataset_map, experiments, hosts, kaggle, monitors, notifications, run_notes,
                         scheduler, terminals)
    from backend.config import settings
    return [
        ("experiments", lambda: settings.experiments_store_file, experiments._load),
        ("scheduler", lambda: settings.scheduler_file, scheduler._load),
        ("terminals", lambda: settings.state_file, terminals._load),
        ("monitors", lambda: settings.monitors_file, monitors._load),
        ("run_notes", lambda: settings.run_notes_file, run_notes._load),
        ("notifications", lambda: settings.notifications_file, notifications._load_notifications),
        ("dataset_map", lambda: settings.dataset_map_file, dataset_map.load_dataset_map),
        ("hosts", lambda: hosts.HOSTS_FILE, hosts._load_records),
        ("kaggle-system", lambda: kaggle._scope_paths(kaggle.SCOPE_SYSTEM)[0], lambda: kaggle._load_scope(kaggle.SCOPE_SYSTEM)),
        ("kaggle-repo", lambda: kaggle._scope_paths(kaggle.SCOPE_REPO)[0], lambda: kaggle._load_scope(kaggle.SCOPE_REPO)),
        ("colab", lambda: colab.SYSTEM_COLAB_ACCOUNTS_FILE, colab._load),
    ]


@pytest.mark.parametrize("index", range(11))
def test_every_store_fails_loud_on_corruption(index):
    label, path_fn, load = _stores()[index]
    path = Path(path_fn())
    assert DATA.resolve() in path.resolve().parents, "%s store escaped the test data dir" % label
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"truncated": ')
    with pytest.raises(StoreCorruptError):
        load()


def test_experiment_writes_are_atomic_with_backup():
    from backend import experiments
    from backend.config import settings
    data = experiments._load()
    data["experiments"]["x"] = {"experiment_id": "x"}
    experiments._save(data)
    experiments._save(data)
    assert backup_path(settings.experiments_store_file).is_file()


def test_active_profile_pointer_tolerates_corruption(monkeypatch):
    from backend import config
    config.ACTIVE_REPO_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.ACTIVE_REPO_FILE.write_text("{nope")
    assert config._default_profile_name() == "fake"
