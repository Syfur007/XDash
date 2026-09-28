"""XDASH_PLAN.md §10 Phase 5's small backend additions: the Add-runtime
wizard's live-test-without-saving gate (per kind, plus the one dispatcher
in backend/runtimes.py), the SSH GPU probe, and the Kaggle CLI 2.x
groundwork (quota parsing; kernel-log-follow and Colab's OAuth plumbing are
covered in tests/test_procsession.py, since both need a real subprocess).
Nothing here makes a real ssh/rsync/kaggle/colab call — see each test's own
monkeypatch of the boundary function."""
from __future__ import annotations

import subprocess
import sys

import pytest

from backend import colab, hosts, kaggle, runtimes, transport
from backend.runners import machine as machine_mod


@pytest.fixture
def client():
    import server
    return server.app.test_client()


# --------------------------------------------------------------- transport.test_ssh_connection
def test_ssh_test_without_a_host_is_a_clean_failure():
    assert transport.test_ssh_connection({})["ok"] is False


def test_ssh_test_reports_ok_when_the_probe_succeeds(monkeypatch):
    monkeypatch.setattr(
        transport.SshTransport, "run",
        lambda self, argv, timeout=None: subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr=""),
    )
    result = transport.test_ssh_connection({"host": "10.0.0.7", "user": "syfur"})
    assert result == {"ok": True, "detail": "reachable"}


def test_ssh_test_reports_failure_when_unreachable(monkeypatch):
    monkeypatch.setattr(
        transport.SshTransport, "run",
        lambda self, argv, timeout=None: subprocess.CompletedProcess(args=argv, returncode=255, stdout="", stderr="refused"),
    )
    result = transport.test_ssh_connection({"host": "10.0.0.7"})
    assert result["ok"] is False and "ssh failed" in result["detail"]


def test_ssh_test_route_never_persists_anything(client, monkeypatch):
    monkeypatch.setattr(
        transport.SshTransport, "run",
        lambda self, argv, timeout=None: subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr=""),
    )
    r = client.post("/api/runtimes/test", json={"kind": "ssh", "fields": {"host": "box", "user": "u"}})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert not any(h.id == "test" for h in hosts.list_hosts())


# --------------------------------------------------------------- GPU probe
def test_parse_nvidia_smi_output():
    parsed = machine_mod.parse_nvidia_smi_output("NVIDIA A6000, 49140\n")
    assert parsed == {"name": "NVIDIA A6000", "vram_gb": 48.0, "source": "nvidia-smi"}


def test_parse_nvidia_smi_output_garbled_is_none():
    assert machine_mod.parse_nvidia_smi_output("") is None
    assert machine_mod.parse_nvidia_smi_output("no commas here\n") is None


def test_probe_accelerator_over_a_transport(monkeypatch):
    class _T:
        def run(self, argv, timeout=None):
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="Tesla T4, 15360\n", stderr="")
    found = machine_mod.probe_accelerator(_T())
    assert found == {"name": "Tesla T4", "vram_gb": 15.0, "source": "nvidia-smi"}


def test_probe_accelerator_transport_error_is_none():
    class _T:
        def run(self, argv, timeout=None):
            raise transport.TransportError("unreachable")
    assert machine_mod.probe_accelerator(_T()) is None


def test_set_accelerator_persists_and_probe_route_saves_it(client, monkeypatch):
    hosts.upsert_host({"id": "gpu-box", "kind": "ssh", "label": "GPU box", "ssh": {"host": "10.0.0.9"}})
    try:
        monkeypatch.setattr(
            machine_mod, "probe_accelerator",
            lambda t, timeout=15.0: {"name": "RTX A6000", "vram_gb": 48.0, "source": "nvidia-smi"},
        )
        r = client.post("/api/hosts/gpu-box/probe-gpu")
        assert r.status_code == 200
        body = r.get_json()
        assert body["found"] is True and body["accelerator"]["name"] == "RTX A6000"
        assert hosts.get_host("gpu-box").accelerator == {"name": "RTX A6000", "vram_gb": 48.0, "source": "nvidia-smi"}
    finally:
        hosts.remove_host("gpu-box")


def test_probe_route_unknown_host_is_404(client):
    r = client.post("/api/hosts/does-not-exist/probe-gpu")
    assert r.status_code == 404


# --------------------------------------------------------------- runtimes.test_runtime dispatch
def test_test_runtime_local_is_always_ok():
    assert runtimes.test_runtime("local", {})["ok"] is True


def test_test_runtime_unknown_kind():
    assert runtimes.test_runtime("carrier-pigeon", {})["ok"] is False


def test_test_runtime_dispatches_to_kaggle(monkeypatch):
    monkeypatch.setattr(kaggle, "test_credentials", lambda username, key, api_token: {"ok": True, "detail": "ok"})
    result = runtimes.test_runtime("kaggle", {"username": "me", "key": "k"})
    assert result == {"ok": True, "detail": "ok"}


def test_test_runtime_dispatches_to_colab(monkeypatch):
    monkeypatch.setattr(colab, "test_config", lambda gpu: {"ok": True, "detail": "found"})
    result = runtimes.test_runtime("colab", {"gpu": "T4"})
    assert result == {"ok": True, "detail": "found"}


def test_test_runtime_kaggle_route(client, monkeypatch):
    monkeypatch.setattr(kaggle, "test_credentials", lambda username, key, api_token: {"ok": False, "detail": "no"})
    r = client.post("/api/runtimes/test", json={"kind": "kaggle", "fields": {"username": "x"}})
    assert r.status_code == 200 and r.get_json()["ok"] is False


# --------------------------------------------------------------- kaggle.test_credentials
def _fake_kaggle_script(tmp_path, exit_code=0, stdout="ok\n"):
    script = tmp_path / "fake_kaggle.py"
    script.write_text(f"""\
import sys
print({stdout!r}, end="")
sys.exit({exit_code})
""")
    return script


def test_kaggle_test_credentials_needs_at_least_one_credential():
    with pytest.raises(kaggle.KaggleOpsError):
        kaggle.test_credentials()


def test_kaggle_test_credentials_rejects_a_bad_key():
    with pytest.raises(kaggle.KaggleOpsError):
        kaggle.test_credentials(username="u", key="KGAT-looks-like-a-token")


def test_kaggle_test_credentials_real_subprocess_path_ok(monkeypatch, tmp_path):
    # XDASH_FIXES_PLAN.md F2 — settings.kaggle_executable is gone; the "clean
    # override path for tests" is backend/tools.set_override, though
    # _run_kaggle_argv itself is monkeypatched below anyway, so this is only
    # here to prove the seam exists (a real caller of tools.path("kaggle")
    # would need it, this one doesn't).
    from backend import tools
    tools.set_override("kaggle", sys.executable)
    script = _fake_kaggle_script(tmp_path, exit_code=0, stdout="kernel1\n")
    monkeypatch.setattr(kaggle, "_run_kaggle_argv", lambda args, env, timeout=None: subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=timeout,
    ))
    result = kaggle.test_credentials(username="me", key="abc123")
    assert result["ok"] is True and "kernel1" in result["detail"]


def test_kaggle_test_credentials_real_subprocess_path_failure(monkeypatch, tmp_path):
    script = _fake_kaggle_script(tmp_path, exit_code=1, stdout="401 Unauthorized\n")
    monkeypatch.setattr(kaggle, "_run_kaggle_argv", lambda args, env, timeout=None: subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=timeout,
    ))
    result = kaggle.test_credentials(api_token="sometoken")
    assert result["ok"] is False and "401" in result["detail"]


def test_kaggle_test_credentials_never_writes_into_the_account_store(monkeypatch, tmp_path):
    monkeypatch.setattr(kaggle, "_run_kaggle_argv", lambda args, env, timeout=None: subprocess.CompletedProcess(
        args=args, returncode=0, stdout="ok", stderr="",
    ))
    before = kaggle.list_accounts()
    kaggle.test_credentials(username="scratch", key="scratchkey")
    assert kaggle.list_accounts() == before


# --------------------------------------------------------------- kaggle quota parsing (CLI 2.2.4 contract, F2.6)
# Fixtures below are exactly the shape kagglesdk's quota_view_cli builds
# (rows via print_json) — see kaggle/api/kaggle_api_extended.py in the
# installed 2.2.4 source: a JSON LIST, one row per accelerator, string
# values with an "h" suffix, key "refreshAt" (camelCase, not the RPC's own
# "refresh_at" — print_json's label defaults to the field name it was asked
# for). The dict-shaped payload an earlier version of this parser guessed at
# (time_used/total_time_allowed/quota_refresh_time) has never been real.
_QUOTA_ROWS_GPU_ONLY = [
    {"resource": "GPU", "used": "3.20h", "remaining": "26.80h", "total": "30.00h", "refreshAt": "2026-09-29T00:00:00Z"},
]
_QUOTA_ROWS_GPU_AND_TPU = _QUOTA_ROWS_GPU_ONLY + [
    {"resource": "TPU", "used": "0.00h", "remaining": "20.00h", "total": "20.00h", "refreshAt": "2026-09-29T00:00:00Z"},
]


def test_parse_quota_json_gpu_row():
    parsed = kaggle.parse_quota_json(_QUOTA_ROWS_GPU_ONLY)
    assert parsed == {
        "used": 3.2, "limit": 30.0, "unit": "h/week",
        "resets_at": "2026-09-29T00:00:00Z", "source": "measured",
    }


def test_parse_quota_json_keeps_tpu_row_for_display():
    parsed = kaggle.parse_quota_json(_QUOTA_ROWS_GPU_AND_TPU)
    assert parsed["tpu"] == {"used": 0.0, "limit": 20.0, "resets_at": "2026-09-29T00:00:00Z"}


def test_parse_quota_json_unparseable_is_none():
    assert kaggle.parse_quota_json({"unrelated": True}) is None  # the old (never-real) dict shape
    assert kaggle.parse_quota_json([{"resource": "TPU", "used": "1h", "total": "2h"}]) is None  # no GPU row
    assert kaggle.parse_quota_json(None) is None
    assert kaggle.parse_quota_json("not a list") is None


def test_get_measured_quota_no_quota_information(monkeypatch):
    monkeypatch.setattr(kaggle, "_run_kaggle", lambda args, name, timeout=None: subprocess.CompletedProcess(
        args=args, returncode=0, stdout="No quota information available\n", stderr="",
    ))
    result = kaggle.get_measured_quota("acct")
    assert result == {"available": False, "detail": "No quota information available"}


def test_get_measured_quota_401(monkeypatch):
    monkeypatch.setattr(kaggle, "_run_kaggle", lambda args, name, timeout=None: subprocess.CompletedProcess(
        args=args, returncode=1, stdout="", stderr="401 Client Error: Unauthorized",
    ))
    result = kaggle.get_measured_quota("acct")
    assert result["available"] is False and "401" in result["detail"]


def test_get_measured_quota_real_json(monkeypatch):
    import json as _json
    monkeypatch.setattr(kaggle, "_run_kaggle", lambda args, name, timeout=None: subprocess.CompletedProcess(
        args=args, returncode=0, stdout=_json.dumps(_QUOTA_ROWS_GPU_ONLY), stderr="",
    ))
    result = kaggle.get_measured_quota("acct")
    assert result == {
        "available": True, "used": 3.2, "limit": 30.0, "unit": "h/week",
        "resets_at": "2026-09-29T00:00:00Z", "source": "measured",
    }


def test_get_measured_quota_is_cached_per_account(monkeypatch):
    calls = []

    def fake_run(args, name, timeout=None):
        calls.append(name)
        import json as _json
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=_json.dumps(_QUOTA_ROWS_GPU_ONLY), stderr="")

    monkeypatch.setattr(kaggle, "_run_kaggle", fake_run)
    kaggle.get_measured_quota("acct")
    kaggle.get_measured_quota("acct")  # cache hit — no second subprocess
    assert calls == ["acct"]
    kaggle.get_measured_quota("acct", force=True)  # Diagnostics' manual Refresh bypasses it
    assert calls == ["acct", "acct"]


def test_kaggle_quota_route(client, monkeypatch):
    monkeypatch.setattr(kaggle, "get_measured_quota", lambda name, force=False: {"available": False, "detail": "old cli"})
    r = client.get("/api/kaggle/accounts/acct/quota")
    assert r.status_code == 200 and r.get_json() == {"available": False, "detail": "old cli"}


def test_kaggle_quota_route_refresh_param_forces(client, monkeypatch):
    seen = {}

    def fake(name, force=False):
        seen["force"] = force
        return {"available": False, "detail": "x"}

    monkeypatch.setattr(kaggle, "get_measured_quota", fake)
    client.get("/api/kaggle/accounts/acct/quota?refresh=1")
    assert seen["force"] is True


# --------------------------------------------------------------- colab.test_config
def test_colab_test_config_ok_when_cli_present(monkeypatch):
    monkeypatch.setattr(colab, "colab_available", lambda: True)
    assert colab.test_config("T4") == {"ok": True, "detail": "CLI found on PATH. Save, then use Connect account to finish sign-in."}


def test_colab_test_config_rejects_bad_gpu(monkeypatch):
    monkeypatch.setattr(colab, "colab_available", lambda: True)
    result = colab.test_config("not-a-gpu")
    assert result["ok"] is False


def test_colab_test_config_missing_cli(monkeypatch):
    monkeypatch.setattr(colab, "colab_available", lambda: False)
    result = colab.test_config("")
    assert result["ok"] is False and "PATH" in result["detail"]


def test_test_runtime_route_for_colab(client, monkeypatch):
    monkeypatch.setattr(colab, "test_config", lambda gpu: {"ok": True, "detail": "fine"})
    r = client.post("/api/runtimes/test", json={"kind": "colab", "fields": {"gpu": "T4"}})
    assert r.status_code == 200 and r.get_json()["ok"] is True


# --------------------------------------------------------------- Kaggle kernel deep link
def test_kaggle_kernel_info_route_when_username_unknown(client):
    r = client.get("/api/kaggle/accounts/nobody/kernel")
    assert r.status_code == 200
    body = r.get_json()
    assert body["kernel_slug"].startswith("xdash-")
    assert body["kernel_url"] is None  # no stored account -> no known kaggle_username


def test_kaggle_kernel_info_route_with_a_real_account(client, monkeypatch):
    monkeypatch.setattr(kaggle, "_load_accounts", lambda: {"accounts": [
        {"name": "acct", "kaggle_username": "someone", "workers": [], "scope": "system"}]})
    r = client.get("/api/kaggle/accounts/acct/kernel")
    body = r.get_json()
    assert body["kernel_url"] == f"https://www.kaggle.com/code/someone/{body['kernel_slug']}"
    assert body["kernel_edit_url"] == f"https://www.kaggle.com/code/someone/{body['kernel_slug']}/edit"
