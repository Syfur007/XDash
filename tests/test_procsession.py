"""backend/procsession.py — the generic line-buffered subprocess session
XDASH_PLAN.md §10 Phase 5 needs twice: Colab's copy-paste OAuth
(begin_connect/connect_status/submit_connect_code) and Kaggle's live
`kernels logs -f` (start_kernel_log_follow/read_kernel_log_follow).

Every "CLI" in these tests is a local Python fixture script, never the real
`colab`/`kaggle` binary — see backend/colab.py's and backend/kaggle.py's own
comments on exactly which line would make a real network call. Nothing here
crosses that line.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from backend import colab, kaggle, procsession


def _write_script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake_cli.py"
    path.write_text(body)
    return path


# ----------------------------------------------------------------- the primitive itself
def test_line_session_reads_output_and_accepts_stdin(tmp_path):
    script = _write_script(tmp_path, """\
import sys
print("hello", flush=True)
line = input()
print("got:" + line, flush=True)
""")
    session = procsession.start("t1", [sys.executable, str(script)])
    deadline = time.time() + 5
    while "hello" not in session.snapshot()["output"] and time.time() < deadline:
        time.sleep(0.05)
    assert "hello" in session.snapshot()["output"]
    assert session.send("world")
    deadline = time.time() + 5
    while "got:world" not in session.snapshot()["output"] and time.time() < deadline:
        time.sleep(0.05)
    snap = session.snapshot()
    assert "got:world" in snap["output"]
    assert procsession.get("t1") is session
    assert procsession.stop("t1")
    assert procsession.get("t1") is None


def test_a_second_start_under_the_same_id_replaces_the_first(tmp_path):
    script = _write_script(tmp_path, "import time\ntime.sleep(30)\n")
    first = procsession.start("t2", [sys.executable, str(script)])
    second = procsession.start("t2", [sys.executable, str(script)])
    deadline = time.time() + 5
    while first.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert first.poll() is not None  # terminated when replaced
    assert procsession.get("t2") is second
    procsession.stop("t2")


def test_a_bad_argv_records_an_error_not_an_exception():
    session = procsession.start("t3", ["/no/such/binary-xyz"])
    assert session.error is not None
    assert session.snapshot()["done"] is True
    procsession.stop("t3")


# ----------------------------------------------------------------- Colab Connect-account
@pytest.fixture
def colab_account(monkeypatch, tmp_path):
    monkeypatch.setattr(colab, "colab_path", lambda: sys.executable)
    colab.add_account("connacct", gpu="t4")
    return "connacct"


def _fake_oauth_script(tmp_path: Path, good_code: str = "GOODCODE") -> Path:
    return _write_script(tmp_path, f"""\
import sys
print("Go to https://accounts.google.com/o/oauth2/fake?state=abc and sign in", flush=True)
code = input()
if code.strip() == {good_code!r}:
    print("Login successful", flush=True)
    sys.exit(0)
print("Bad code", flush=True)
sys.exit(1)
""")


def test_begin_connect_surfaces_the_url_then_accepts_the_pasted_code(monkeypatch, tmp_path, colab_account):
    script = _fake_oauth_script(tmp_path)
    monkeypatch.setattr(colab, "colab_argv", lambda name, args: [sys.executable, str(script)])
    monkeypatch.setattr(colab, "has_credentials", lambda name, account=None: True)

    status = colab.begin_connect(colab_account)
    assert status["active"] is True

    deadline = time.time() + 5
    while not (colab.connect_status(colab_account) or {}).get("url") and time.time() < deadline:
        time.sleep(0.05)
    status = colab.connect_status(colab_account)
    assert status["url"] == "https://accounts.google.com/o/oauth2/fake?state=abc"

    # awaiting_code needs the child to have gone quiet for a moment — real
    # idle time, not a mocked clock, since procsession's own timestamps drive it.
    deadline = time.time() + 5
    while not colab.connect_status(colab_account)["awaiting_code"] and time.time() < deadline:
        time.sleep(0.1)
    assert colab.connect_status(colab_account)["awaiting_code"] is True

    result = colab.submit_connect_code(colab_account, "GOODCODE")
    deadline = time.time() + 5
    while not result["done"] and time.time() < deadline:
        time.sleep(0.05)
        result = colab.connect_status(colab_account)
    assert result["done"] is True and result["returncode"] == 0 and result["connected"] is True


def test_a_wrong_code_leaves_a_nonzero_exit(monkeypatch, tmp_path, colab_account):
    script = _fake_oauth_script(tmp_path)
    monkeypatch.setattr(colab, "colab_argv", lambda name, args: [sys.executable, str(script)])
    monkeypatch.setattr(colab, "has_credentials", lambda name, account=None: False)
    colab.begin_connect(colab_account)
    deadline = time.time() + 5
    while not colab.connect_status(colab_account).get("url") and time.time() < deadline:
        time.sleep(0.05)
    colab.submit_connect_code(colab_account, "WRONGCODE")
    deadline = time.time() + 5
    status = colab.connect_status(colab_account)
    while not status["done"] and time.time() < deadline:
        time.sleep(0.05)
        status = colab.connect_status(colab_account)
    assert status["returncode"] != 0 and status["connected"] is False


def test_submit_code_with_no_session_is_a_clear_error(colab_account):
    with pytest.raises(colab.ColabOpsError):
        colab.submit_connect_code(colab_account, "x")


def test_cancel_connect_stops_the_subprocess(monkeypatch, tmp_path, colab_account):
    script = _write_script(tmp_path, "import time\ntime.sleep(30)\n")
    monkeypatch.setattr(colab, "colab_argv", lambda name, args: [sys.executable, str(script)])
    colab.begin_connect(colab_account)
    assert colab.connect_status(colab_account)["active"] is True
    assert colab.cancel_connect(colab_account) is True
    assert colab.connect_status(colab_account) == {"active": False}


# ----------------------------------------------------------------- Kaggle live log follow
def test_kernel_log_follow_streams_lines_then_stops(monkeypatch, tmp_path):
    script = _write_script(tmp_path, """\
import time
for i in range(3):
    print("log line %d" % i, flush=True)
    time.sleep(0.05)
""")
    monkeypatch.setattr(kaggle, "_kernel_logs_argv", lambda slug: [sys.executable, str(script)])
    monkeypatch.setattr(kaggle, "_creds_dir", lambda name: tmp_path)
    (tmp_path / kaggle.TOKEN_FILENAME).write_text("faketoken")

    result = kaggle.start_kernel_log_follow("acct", str(script))
    assert result["active"] is True
    deadline = time.time() + 5
    while "log line 2" not in kaggle.read_kernel_log_follow("acct", str(script))["output"] and time.time() < deadline:
        time.sleep(0.05)
    out = kaggle.read_kernel_log_follow("acct", str(script))
    assert "log line 0" in out["output"] and "log line 2" in out["output"]
    assert kaggle.stop_kernel_log_follow("acct", str(script)) is True
    assert kaggle.read_kernel_log_follow("acct", str(script))["active"] is False


def test_reading_a_log_follow_that_was_never_started_is_inactive():
    out = kaggle.read_kernel_log_follow("nope", "nope")
    assert out == {"active": False, "lines": [], "done": True}
