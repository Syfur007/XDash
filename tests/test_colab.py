"""X5 — the Colab CLI wrapper, checked against the real 0.7.2 CLI's
argument order and output formats — plus X3's hard guard: reap_idle()
never stops a VM holding an uncollected attempt."""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import colab, experiments, hosts
from backend.runners import colab as colab_runner

from conftest import SubprocessProxy

COLAB = "/opt/colab/bin/colab"

# Exactly what colab_cli.commands.session._format_session_line prints.
SESSIONS_OUT = (
    "[colab] Pruned 1 stale local session(s).\n"
    "[xdash-acct] m-s-abc123 | Hardware: T4 | Shape: Standard | Variant: GPU\n"
    "[?] m-s-orphan | Hardware: CPU | Shape: Standard | Variant: DEFAULT\n"
)
STATUS_OUT = (
    "[xdash-acct] m-s-abc123 | Hardware: T4 | Shape: High-RAM | Variant: GPU | Status: BUSY (train.py)\n"
    "  Last Execution: train.py | Cell: 1 at 2026-09-25 10:00\n"
)
USAGE_OUT = "Current balance: 87.42 compute units\nUsage rate: 1.96/hr\nActive assignments: 1\n"


@pytest.fixture
def account(monkeypatch, tmp_path):
    monkeypatch.setattr(colab, "colab_path", lambda: COLAB)
    key = tmp_path / "id_ed25519"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    Path(str(key) + ".pub").write_text("ssh-ed25519 AAAAC3Nza test\n")
    colab.add_account("acct", gpu="t4", ssh_key=str(key))
    return {"key": str(key)}


def _connect(name="acct"):
    tok = colab.token_path(name)
    tok.parent.mkdir(parents=True, exist_ok=True)
    tok.write_text("{}")


@pytest.fixture
def cli(monkeypatch):
    """Scripted `colab` subprocess: outputs keyed by subcommand."""
    calls = []
    outputs = {}

    def run(argv, env=None, capture_output=True, text=True, timeout=None, stdin=None):
        calls.append({"argv": list(argv), "env": dict(env or {}), "stdin": stdin})
        sub = next(a for a in argv[1:] if not a.startswith("-") and not a.startswith("/")
                   and a not in ("oauth2", "adc"))
        rc, out = outputs.get(sub, (0, ""))
        return subprocess.CompletedProcess(args=argv, returncode=rc, stdout=out, stderr="")

    monkeypatch.setattr(colab, "subprocess", SubprocessProxy(run))
    return {"calls": calls, "outputs": outputs}


def test_global_options_precede_the_command_and_home_is_per_account(account, cli):
    _connect()
    cli["outputs"]["sessions"] = (0, SESSIONS_OUT)
    rows = colab.list_sessions("acct")
    call = cli["calls"][0]
    argv = call["argv"]
    assert argv[0] == COLAB
    assert argv[1:3] == ["--config", str(colab.sessions_path("acct"))]
    assert argv.index("--config") < argv.index("sessions")
    assert call["env"]["HOME"] == str(colab.account_home("acct"))
    assert call["env"]["PYTHONUSERBASE"]  # a --user install still imports under the fake HOME
    assert call["stdin"] == subprocess.DEVNULL  # an expired login fails fast instead of prompting
    assert rows == [{"name": "xdash-acct", "endpoint": "m-s-abc123", "accelerator": "T4", "shape": "Standard",
                     "variant": "GPU", "status": None}]


def test_two_accounts_never_share_a_token(account, tmp_path):
    colab.add_account("other", ssh_key=account["key"])
    assert colab.token_path("acct") != colab.token_path("other")
    assert colab.account_home("acct") != colab.account_home("other")


def test_a_fresh_account_is_not_gated_on_sessions_json(account, cli):
    """The old wrapper refused every command until sessions.json existed —
    which only `colab new`, through that same wrapper, could create."""
    _connect()
    assert not colab.sessions_path("acct").exists()
    cli["outputs"]["sessions"] = (0, "[colab] No active sessions found on server.\n")
    cli["outputs"]["new"] = (0, "[colab] Creating session 'xdash-acct'...\n[colab] Session READY.\n")
    cli["outputs"]["status"] = (0, STATUS_OUT)
    out = colab.ensure_session("acct")
    new_call = next(c for c in cli["calls"] if "new" in c["argv"])
    assert new_call["argv"][-5:] == ["new", "--gpu", "T4", "-s", "xdash-acct"]
    assert out["accelerator"] == "T4" and out["identity_file"] == account["key"]


def test_not_connected_is_a_clear_error_not_a_hang(account, cli):
    with pytest.raises(colab.ColabNotConnectedError) as e:
        colab._run_colab(["sessions"], "acct")
    assert "HOME=" in str(e.value) and "sessions" in str(e.value)
    assert cli["calls"] == []


def test_gpu_is_validated(account):
    assert colab.validate_gpu("a100") == "A100"
    with pytest.raises(colab.ColabOpsError):
        colab.validate_gpu("V100")  # the CLI would silently rent an A100
    with pytest.raises(colab.ColabOpsError):
        colab.add_account("bad", gpu="K80")


def test_proxy_command_uses_the_absolute_cli_the_home_and_the_key(account):
    proxy = colab.proxy_command("acct", account["key"])
    assert COLAB in proxy and "HOME=%s" % colab.account_home("acct") in proxy
    assert "ssh --proxy-mode -s xdash-acct -i %s" % account["key"] in proxy
    assert proxy.index("--config") < proxy.index(" ssh ")


def test_ssh_key_must_be_ed25519(account, tmp_path):
    rsa = tmp_path / "id_rsa"
    rsa.write_text("x")
    Path(str(rsa) + ".pub").write_text("ssh-rsa AAAAB3 test\n")
    colab.add_account("rsa-acct", ssh_key=str(rsa))
    with pytest.raises(colab.ColabOpsError) as e:
        colab.ssh_key_for("rsa-acct")
    assert "ed25519" in str(e.value)
    colab.add_account("nokey", ssh_key=str(tmp_path / "missing"))
    with pytest.raises(colab.ColabOpsError):
        colab.ssh_key_for("nokey")


def test_status_and_usage_parse_the_real_text_output(account, cli):
    _connect()
    cli["outputs"]["status"] = (0, STATUS_OUT)
    s = colab.session_status("acct")
    assert s["accelerator"] == "T4" and s["shape"] == "High-RAM" and s["status"] == "BUSY (train.py)"
    cli["outputs"]["status"] = (0, "[colab] Session 'xdash-acct' not found.\n")  # exit 0, per the CLI
    assert colab.session_status("acct") is None
    cli["outputs"]["usage"] = (0, USAGE_OUT)
    assert colab.usage("acct") == {"balance": 87.42, "burn_per_h": 1.96, "assignments": 1, "unit": "CU", "source": "measured"}
    cli["outputs"]["usage"] = (1, "")
    with pytest.raises(colab.ColabOpsError):
        colab.usage("acct")


def test_a_cpu_only_grant_is_stopped_not_leaked(account, cli):
    _connect()
    cli["outputs"]["sessions"] = (0, "")
    cli["outputs"]["new"] = (0, "[colab] Session READY.\n")
    cli["outputs"]["status"] = (0, "[xdash-acct] m-s-1 | Hardware: CPU | Shape: Standard | Variant: DEFAULT | Status: IDLE\n")
    with pytest.raises(colab.NoAcceleratorError):
        colab.ensure_session("acct")
    assert any("stop" in c["argv"] for c in cli["calls"])


def test_colab_host_record_accepts_fresh_host_keys(account):
    rec = colab_runner._host_record("acct", "PROXY", account["key"])
    assert rec["ssh"]["user"] == "root" and rec["ssh"]["identity_file"] == account["key"]
    host = hosts.upsert_host(rec)
    from backend.transport import SshTransport
    opts = SshTransport(host)._opts()
    # First value wins in ssh: the per-record options must precede the base accept-new.
    assert opts.index("StrictHostKeyChecking=no") < opts.index("StrictHostKeyChecking=accept-new")


# ----------------------------------------------------------------- X3: the reap guard
@pytest.fixture
def reaper(account, monkeypatch):
    stops = []
    monkeypatch.setattr(colab, "stop_session", lambda name: stops.append(name) or True)
    hosts.upsert_host(colab_runner._host_record("acct", "PROXY", account["key"]))
    return colab_runner.ColabRunner("acct"), stops


def _colab_attempt(status, collect_state, ended_minutes_ago=60):
    store = experiments._load()
    exp = {"experiment_id": "demo_exp-s42", "config_path": "experiment/demo.yaml", "seed": 42, "pool": "*",
           "extra_args": {"train": "", "eval": ""}, "attempt_ids": [], "current_attempt_id": None}
    store["experiments"][exp["experiment_id"]] = exp
    a = experiments._append_attempt(store, exp)
    ended = (datetime.now(timezone.utc) - timedelta(minutes=ended_minutes_ago)).isoformat(timespec="seconds")
    a.update({"status": status, "slot": "colab:acct", "started_at": ended, "ended_at": ended if status != "running" else None,
              "unit_ref": {"train_item_id": "t", "eval_item_id": "e"}, "collect": {"state": collect_state}})
    experiments._save(store)
    return a


def test_reap_refuses_while_a_finished_attempt_is_uncollected(reaper):
    runner, stops = reaper
    _colab_attempt("done", "pending")
    runner.reap_idle()
    assert stops == []
    assert hosts.host_exists("colab-acct")


def test_reap_refuses_while_cancelled_outputs_are_uncollected(reaper):
    runner, stops = reaper
    _colab_attempt("cancelled", "pending")
    runner.reap_idle()
    assert stops == []


def test_reap_refuses_while_in_flight(reaper):
    runner, stops = reaper
    _colab_attempt("running", "pending", ended_minutes_ago=600)
    runner.reap_idle()
    assert stops == []


def test_reap_stops_an_idle_vm_once_everything_is_collected(reaper):
    """Also the tz fix: attempt timestamps are UTC-aware, and the old naive
    datetime.now() subtraction raised TypeError, so no VM was ever reaped."""
    runner, stops = reaper
    _colab_attempt("done", "done", ended_minutes_ago=60)
    runner.reap_idle()
    assert stops == ["acct"]
    assert not hosts.host_exists("colab-acct")


def test_reap_waits_out_the_grace_period(reaper):
    runner, stops = reaper
    _colab_attempt("done", "done", ended_minutes_ago=1)
    runner.reap_idle()
    assert stops == []


def test_can_accept_explains_a_not_connected_account(account, monkeypatch):
    runner = colab_runner.ColabRunner("acct")
    monkeypatch.setattr(colab, "colab_available", lambda: True)
    block = runner.can_accept({"config_path": "experiment/demo.yaml"}, 1.0)
    assert block["code"] == "colab-not-connected" and "HOME=" in block["detail"]
    _connect()
    assert runner.can_accept({"config_path": "experiment/demo.yaml"}, 1.0) is None
