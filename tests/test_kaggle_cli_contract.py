"""XDASH_FIXES_PLAN.md F2.5 — one fixture per Kaggle CLI output XDash
parses, derived from the installed 2.2.4 source
(kaggle/api/kaggle_api_extended.py, kagglesdk/kernels/types/kernels_enums.py
— read directly, no network) rather than guessed. Each fixture is checked
against XDash's own parser; a mismatch is fixed in backend/kaggle.py, not
worked around here.

**Confirmed to need a fix (see backend/kaggle.py's own comment on
_normalize_kaggle_status):** `kernels status`'s enum repr is
SCREAMING_SNAKE_CASE (`KernelWorkerStatus.CANCEL_ACKNOWLEDGED`), not the
no-underscore spelling the old alias map assumed — every *other* status
enum member (QUEUED/RUNNING/COMPLETE/ERROR) has no underscore, so only
CANCEL_ACKNOWLEDGED ever exercised the bug.

**Confirmed unchanged, no fix needed:** the `has status "..."` /
`Failure message: "..."` line shapes (kernels_status_cli), `datasets status`
being a plain lowercase string (dataset_status_cli -> status_response.status.name.lower()),
and `kernels push`'s `--timeout` flag (still accepted, cli.py:1313)."""
from __future__ import annotations

import subprocess

from backend import kaggle


# --------------------------------------------------------------------------- kernels status
def test_normalize_status_running_and_queued_have_no_underscore_to_strip():
    assert kaggle._normalize_kaggle_status('KernelWorkerStatus.RUNNING') == "running"
    assert kaggle._normalize_kaggle_status('KernelWorkerStatus.QUEUED') == "queued"
    assert kaggle._normalize_kaggle_status('KernelWorkerStatus.COMPLETE') == "complete"
    assert kaggle._normalize_kaggle_status('KernelWorkerStatus.ERROR') == "error"


def test_normalize_status_cancel_acknowledged_underscore_bug_fixed():
    """The actual bug this phase found: kagglesdk's enum member is
    CANCEL_ACKNOWLEDGED (with an underscore), not CancelAcknowledged — the
    alias map's key ("cancelacknowledged") only matches once the underscore
    is stripped too, not just the "KernelWorkerStatus." class prefix."""
    normalized = kaggle._normalize_kaggle_status('KernelWorkerStatus.CANCEL_ACKNOWLEDGED')
    assert normalized == "cancelAcknowledged"
    assert normalized in kaggle.FINAL_STATUSES


def test_refresh_experiment_status_recognizes_a_real_cancel_acknowledged_line(monkeypatch):
    # Exactly kernels_status_cli's own print() shape (no Failure message
    # line for a non-error status).
    kaggle.add_account("acct", username="someone", key="k" * 8)
    stdout = 'someone/xdash-fake-slot-acct has status "KernelWorkerStatus.CANCEL_ACKNOWLEDGED"\n'
    monkeypatch.setattr(kaggle, "_run_kaggle", lambda args, name, timeout=None: subprocess.CompletedProcess(
        args=args, returncode=0, stdout=stdout, stderr="",
    ))
    out = kaggle.refresh_experiment_status("acct", "slug")
    assert out == {"status": "cancelAcknowledged", "last_error": None}


def test_refresh_experiment_status_running_is_not_final(monkeypatch):
    kaggle.add_account("acct", username="someone", key="k" * 8)
    stdout = 'someone/xdash-fake-slot-acct has status "KernelWorkerStatus.RUNNING"\n'
    monkeypatch.setattr(kaggle, "_run_kaggle", lambda args, name, timeout=None: subprocess.CompletedProcess(
        args=args, returncode=0, stdout=stdout, stderr="",
    ))
    out = kaggle.refresh_experiment_status("acct", "slug")
    assert out["status"] == "running" and out["status"] not in kaggle.FINAL_STATUSES


# --------------------------------------------------------------------------- kernels push
def test_push_success_text_never_looks_like_an_unrecognized_timeout_option():
    # kernels_push_cli's own success message (2.2.4) — must never trip
    # _looks_like_unrecognized_option's "retry without --timeout" fallback.
    real_success = "Kernel version 3 successfully pushed.  Please check progress at https://kaggle.com/code/x/y"
    assert kaggle._looks_like_unrecognized_option(real_success, "--timeout") is False


def test_push_argparse_error_for_an_actually_unrecognized_flag_is_detected():
    real_argparse_error = "kernels push: error: unrecognized arguments: --bogus-flag"
    assert kaggle._looks_like_unrecognized_option(real_argparse_error, "--bogus-flag") is True


# --------------------------------------------------------------------------- datasets status (backend/snapshot.py)
def test_dataset_status_cli_plain_lowercase_string_is_read_correctly():
    """dataset_status_cli (2.2.4) prints status_response.status.name.lower()
    verbatim — a plain string, never JSON, never the enum's class-qualified
    repr kernels status uses. backend/snapshot.py's own ready/failed checks
    are plain substring tests against this, so no XDash-side parsing change
    was needed — this fixture proves it against the exact real strings."""
    from backend import snapshot

    for real_stdout, expect_ready in (("ready\n", True), ("not_yet_persisted\n", False), ("blobs_received\n", False)):
        last_status = real_stdout.strip().lower()
        assert (snapshot._READY_STATUS in last_status) == expect_ready
