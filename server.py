"""Experiment Dashboard — Flask backend.

Run with:  python server.py

Flask is used deliberately instead of FastAPI: it has a much smaller, more
stable dependency chain (no pydantic version-matching issues), which matters
for older environments (this was written targeting Python 3.8).

This file, plus everything under backend/ and static/, is the entire
subsystem. It reads dashboard_config.yaml to find the host repo's
configs/logs/runs directories, so it can be copied into any repo that
follows the same layout and removed again without leaving a trace.
"""
from __future__ import annotations

import hmac
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_from_directory, send_file

from backend.config import settings
from backend import configs as cfg
from backend import terminals
from backend import reports
from backend import history
from backend import monitors
from backend import scheduler
from backend import tensorboard_manager as tb
from backend import tmux_runner as tmux
from backend import ledger
from backend import bridge
from backend import datasets_info
from backend import kaggle as kaggle_ops

APP_DIR = Path(__file__).resolve().parent

app = Flask(__name__, static_folder=str(APP_DIR / "static"), static_url_path="")

scheduler.ensure_worker_started()
kaggle_ops.ensure_kaggle_worker_started()


def err(message, code=400):
    return jsonify({"detail": message}), code


def _origin_matches_host(origin: str, host_header: str) -> bool:
    """True if an Origin header's host:port matches the request's own Host.

    Same-origin browser requests either omit Origin (simple GET/navigation)
    or send one matching the page's own host. A mismatch means some other
    site's page is making this request against the dashboard.
    """
    try:
        return urlparse(origin).netloc == host_header
    except Exception:
        return False


@app.before_request
def _guard_mutating_requests():
    """Defense against unauthenticated / cross-origin control of the dashboard.

    Several endpoints here (Terminals, Monitors, Scheduler) can run arbitrary
    shell commands on this machine, and there is no session/login system —
    so every state-changing request gets two checks:

    1. If api_token is configured, it must be supplied via X-Api-Token.
    2. A cross-origin Origin header is rejected outright, so a malicious page
       loaded in the same browser as the dashboard can't silently drive it
       (the browser's CORS policy already blocks most of this since no
       Access-Control-Allow-Origin is ever sent, but this covers requests
       that don't require a CORS preflight).
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None

    if settings.api_token:
        supplied = request.headers.get("X-Api-Token", "")
        if not hmac.compare_digest(supplied, settings.api_token):
            return err("Missing or invalid X-Api-Token header", 401)

    origin = request.headers.get("Origin")
    if origin is not None and not _origin_matches_host(origin, request.headers.get("Host", "")):
        return err("Cross-origin request blocked", 403)

    return None


# --------------------------------------------------------------------------- configs
@app.route("/api/configs", methods=["GET"])
def api_list_configs():
    return jsonify({"groups": cfg.list_configs()})


@app.route("/api/config", methods=["GET"])
def api_get_config():
    path = request.args.get("path", "")
    try:
        return jsonify(cfg.read_config(path))
    except FileNotFoundError:
        return err(f"Config not found: {path}", 404)
    except ValueError as e:
        return err(str(e), 400)


@app.route("/api/config", methods=["POST"])
def api_save_config():
    body = request.get_json(silent=True) or {}
    path = body.get("path")
    raw = body.get("raw", "")
    if not path:
        return err("Missing 'path'", 400)
    try:
        return jsonify(cfg.write_config(path, raw))
    except ValueError as e:
        return err(str(e), 400)
    except Exception as e:
        return err(f"Invalid YAML: {e}", 400)


# --------------------------------------------------------------------------- runs / ledger
# Read-only views onto the orchestration layer's on-disk state (see
# backend/ledger.py). Every route here degrades to an empty result — never
# an error — when the host repo hasn't adopted the artifacts/ layout yet.
@app.route("/api/runs", methods=["GET"])
def api_list_runs():
    return jsonify({"groups": ledger.runs_grouped_by_config_hash()})


@app.route("/api/runs/<run_id>", methods=["GET"])
def api_get_run(run_id):
    run = ledger.get_run(run_id)
    if run is None:
        return err(f"No manifest found for run '{run_id}'", 404)
    return jsonify(run)


@app.route("/api/ledger/<table>", methods=["GET"])
def api_ledger_table(table):
    try:
        return jsonify({"rows": ledger.list_ledger_rows(table)})
    except ValueError as e:
        return err(str(e), 400)


# --------------------------------------------------------------------------- bridge
# Read-only (and profile-only) views into the host repo's own code/env,
# via backend/bridge.py's subprocess mechanism. A BridgeError means "the
# host repo doesn't have this" (422, expected/showable); a
# BridgeUnavailable means "the bridge mechanism itself is broken" (503,
# a configuration problem worth surfacing distinctly).
@app.route("/api/bridge/status", methods=["GET"])
def api_bridge_status():
    return jsonify(bridge.bridge_status())


@app.route("/api/config/schema", methods=["GET"])
def api_config_schema():
    try:
        return jsonify(bridge.run_bridge_script("export_schema.py"))
    except bridge.BridgeError as e:
        return err(str(e), 422)
    except bridge.BridgeUnavailable as e:
        return err(str(e), 503)


@app.route("/api/config/resolved", methods=["GET"])
def api_config_resolved():
    path = request.args.get("path", "")
    if not path:
        return err("Missing 'path'", 400)
    try:
        repo_rel = cfg.repo_relative_path(path)
    except ValueError as e:
        return err(str(e), 400)
    try:
        # use_cache=False: a config a user is actively editing must always
        # be re-resolved, never served a stale cached validation result.
        return jsonify(bridge.run_bridge_script("resolve_config.py", [repo_rel], use_cache=False))
    except bridge.BridgeError as e:
        return err(str(e), 422)
    except bridge.BridgeUnavailable as e:
        return err(str(e), 503)


@app.route("/api/models/registry", methods=["GET"])
def api_models_registry():
    try:
        return jsonify(bridge.run_bridge_script("list_models.py"))
    except bridge.BridgeError as e:
        return err(str(e), 422)
    except bridge.BridgeUnavailable as e:
        return err(str(e), 503)


@app.route("/api/models/profile", methods=["POST"])
def api_models_profile():
    body = request.get_json(silent=True) or {}
    kwargs = body.get("kwargs")
    if not isinstance(kwargs, dict) or "name" not in kwargs:
        return err("Body must be {'kwargs': {'name': ..., ...}}", 400)
    try:
        return jsonify(bridge.run_bridge_script("profile_model.py", [json.dumps(kwargs)], use_cache=False))
    except bridge.BridgeError as e:
        return err(str(e), 422)
    except bridge.BridgeUnavailable as e:
        return err(str(e), 503)


# --------------------------------------------------------------------------- datasets (Data Studio)
@app.route("/api/datasets", methods=["GET"])
def api_list_datasets():
    return jsonify({"datasets": datasets_info.list_dataset_fragments()})


@app.route("/api/datasets/channel-preview", methods=["POST"])
def api_dataset_channel_preview():
    body = request.get_json(silent=True) or {}
    image_path = body.get("image_path")
    mode = body.get("mode", "m1")
    modality = body.get("modality", "colour")
    if not image_path:
        return err("Missing 'image_path'", 400)
    try:
        resolved = (settings.repo_root / image_path).resolve()
    except (OSError, ValueError) as e:
        return err(f"Invalid image_path: {e}", 400)
    if settings.repo_root.resolve() not in resolved.parents and resolved != settings.repo_root.resolve():
        return err("image_path escapes the repo root", 400)
    try:
        result = bridge.run_bridge_script(
            "channel_preview.py",
            [json.dumps({"image_path": str(resolved), "mode": mode, "modality": modality})],
            timeout=30,
            use_cache=False,
        )
        return jsonify(result)
    except bridge.BridgeError as e:
        return err(str(e), 422)
    except bridge.BridgeUnavailable as e:
        return err(str(e), 503)


# --------------------------------------------------------------------------- terminals
@app.route("/api/terminals", methods=["GET"])
def api_list_terminals():
    return jsonify({"terminals": terminals.list_terminals()})


@app.route("/api/terminals/<session_name>", methods=["GET"])
def api_get_terminal(session_name):
    term = terminals.get_terminal(session_name, include_log=True)
    if not term:
        return err("Terminal not found", 404)
    return jsonify(term)


@app.route("/api/terminals", methods=["POST"])
def api_launch_terminal():
    body = request.get_json(silent=True) or {}
    config_path = body.get("config_path")
    mode = body.get("mode", "train")
    extra_args = body.get("extra_args", "")
    if not config_path:
        return err("Missing 'config_path'", 400)
    if mode not in ("train", "eval"):
        return err("mode must be 'train' or 'eval'", 400)
    if not tmux.tmux_available():
        return err(
            "'tmux' was not found on PATH. Install it (e.g. `sudo apt install tmux`) "
            "to run experiments from the dashboard.",
            400,
        )
    try:
        return jsonify(terminals.launch(config_path, mode, extra_args))
    except FileNotFoundError:
        return err(f"Config not found: {config_path}", 404)
    except ValueError as e:
        return err(str(e), 400)
    except tmux.TmuxError as e:
        return err(str(e), 400)


@app.route("/api/terminals/<session_name>/stop", methods=["POST"])
def api_stop_terminal(session_name):
    try:
        if not terminals.stop(session_name):
            return err("Terminal is not running", 400)
    except ValueError as e:
        return err(str(e), 403)
    return jsonify({"stopped": True})


@app.route("/api/terminals/<session_name>/restart", methods=["POST"])
def api_restart_terminal(session_name):
    try:
        return jsonify(terminals.restart(session_name))
    except FileNotFoundError:
        return err("The config for this experiment no longer exists", 404)
    except ValueError as e:
        return err(str(e), 400)
    except tmux.TmuxError as e:
        return err(str(e), 400)


@app.route("/api/terminals/<session_name>", methods=["DELETE"])
def api_kill_terminal(session_name):
    try:
        terminals.kill(session_name)
    except ValueError as e:
        return err(str(e), 403)
    return jsonify({"killed": True})


# --------------------------------------------------------------------------- scheduler
@app.route("/api/scheduler", methods=["GET"])
def api_scheduler_list():
    return jsonify(scheduler.list_items())


@app.route("/api/scheduler/items", methods=["POST"])
def api_scheduler_add():
    body = request.get_json(silent=True) or {}
    config_path = body.get("config_path")
    mode = body.get("mode", "train")
    extra_args = body.get("extra_args", "")
    if not config_path:
        return err("Missing 'config_path'", 400)
    try:
        created = scheduler.add_item(config_path, mode, extra_args)
    except FileNotFoundError:
        return err(f"Config not found: {config_path}", 404)
    except ValueError as e:
        return err(str(e), 400)
    return jsonify({"items": created})


@app.route("/api/scheduler/items/<item_id>", methods=["DELETE"])
def api_scheduler_remove(item_id):
    if not scheduler.remove_item(item_id):
        return err("Scheduler item not found", 404)
    return jsonify({"removed": True})


@app.route("/api/scheduler/items/<item_id>/cancel", methods=["POST"])
def api_scheduler_cancel(item_id):
    try:
        return jsonify(scheduler.cancel_item(item_id))
    except ValueError as e:
        return err(str(e), 404)


@app.route("/api/scheduler/reorder", methods=["POST"])
def api_scheduler_reorder():
    body = request.get_json(silent=True) or {}
    scheduler.reorder_pending(body.get("order", []))
    return jsonify(scheduler.list_items())


@app.route("/api/scheduler/max_concurrent", methods=["POST"])
def api_scheduler_max_concurrent():
    body = request.get_json(silent=True) or {}
    try:
        value = scheduler.set_max_concurrent(body.get("value", 1))
    except (TypeError, ValueError):
        return err("value must be a whole number", 400)
    return jsonify({"max_concurrent": value})


# --------------------------------------------------------------------------- kaggle
@app.route("/api/kaggle/accounts", methods=["GET"])
def api_kaggle_list_accounts():
    return jsonify({"accounts": kaggle_ops.list_accounts()})


@app.route("/api/kaggle/accounts", methods=["POST"])
def api_kaggle_add_account():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(kaggle_ops.add_account(
            body.get("name", ""), body.get("username", ""), body.get("key", ""), body.get("api_token", ""),
        ))
    except kaggle_ops.KaggleOpsError as e:
        return err(str(e), 400)


@app.route("/api/kaggle/accounts/<name>", methods=["DELETE"])
def api_kaggle_remove_account(name):
    if not kaggle_ops.remove_account(name):
        return err("Account not found", 404)
    return jsonify({"removed": True})


@app.route("/api/kaggle/accounts/<name>/credentials", methods=["PATCH"])
def api_kaggle_update_credentials(name):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(kaggle_ops.update_credentials(
            name, body.get("username", ""), body.get("key", ""), body.get("api_token", ""),
        ))
    except kaggle_ops.KaggleOpsError as e:
        return err(str(e), 400)


@app.route("/api/kaggle/accounts/<name>/credentials/<kind>", methods=["DELETE"])
def api_kaggle_remove_credential(name, kind):
    try:
        return jsonify(kaggle_ops.remove_credential(name, kind))
    except kaggle_ops.KaggleOpsError as e:
        return err(str(e), 400)


@app.route("/api/kaggle/accounts/<name>/rename", methods=["POST"])
def api_kaggle_rename_account(name):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(kaggle_ops.rename_account(name, body.get("name", "")))
    except kaggle_ops.KaggleOpsError as e:
        return err(str(e), 400)


@app.route("/api/kaggle/accounts/<name>/validate", methods=["POST"])
def api_kaggle_validate_account(name):
    try:
        return jsonify(kaggle_ops.validate_account(name))
    except kaggle_ops.KaggleOpsError as e:
        return err(str(e), 400)


@app.route("/api/kaggle/accounts/<name>/workers", methods=["POST"])
def api_kaggle_add_worker(name):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(kaggle_ops.add_worker(
            name,
            body.get("worker_id", ""),
            body.get("notebook_path", ""),
            body.get("kernel_slug", ""),
            body.get("results_dir", ""),
            body.get("budget_hours"),
        ))
    except kaggle_ops.KaggleOpsError as e:
        return err(str(e), 400)


@app.route("/api/kaggle/accounts/<name>/workers/<worker_id>", methods=["DELETE"])
def api_kaggle_remove_worker(name, worker_id):
    if not kaggle_ops.remove_worker(name, worker_id):
        return err("Worker not found", 404)
    return jsonify({"removed": True})


@app.route("/api/kaggle/workers/<worker_id>/push", methods=["POST"])
def api_kaggle_push(worker_id):
    try:
        return jsonify(kaggle_ops.push(worker_id))
    except kaggle_ops.KaggleOpsError as e:
        return err(str(e), 400)


@app.route("/api/kaggle/workers/<worker_id>/status", methods=["POST"])
def api_kaggle_refresh_status(worker_id):
    try:
        return jsonify(kaggle_ops.refresh_status(worker_id))
    except kaggle_ops.KaggleOpsError as e:
        return err(str(e), 400)


@app.route("/api/kaggle/workers/<worker_id>/download", methods=["POST"])
def api_kaggle_download(worker_id):
    try:
        return jsonify(kaggle_ops.download(worker_id))
    except kaggle_ops.KaggleOpsError as e:
        return err(str(e), 400)


@app.route("/api/kaggle/push_all", methods=["POST"])
def api_kaggle_push_all():
    return jsonify({"results": kaggle_ops.push_all()})


@app.route("/api/kaggle/refresh_all", methods=["POST"])
def api_kaggle_refresh_all():
    return jsonify({"results": kaggle_ops.refresh_all()})


@app.route("/api/kaggle/download_all", methods=["POST"])
def api_kaggle_download_all():
    return jsonify({"results": kaggle_ops.download_all()})


@app.route("/api/kaggle/accounts/<name>/auto_chain", methods=["POST"])
def api_kaggle_set_auto_chain(name):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(kaggle_ops.set_auto_chain(name, bool(body.get("enabled"))))
    except kaggle_ops.KaggleOpsError as e:
        return err(str(e), 400)


@app.route("/api/kaggle/registry/export", methods=["GET"])
def api_kaggle_export_registry():
    return jsonify(kaggle_ops.export_registry())


@app.route("/api/kaggle/registry/import", methods=["POST"])
def api_kaggle_import_registry():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(kaggle_ops.import_registry(body))
    except kaggle_ops.KaggleOpsError as e:
        return err(str(e), 400)


# --------------------------------------------------------------------------- reports
@app.route("/api/reports", methods=["GET"])
def api_list_reports():
    return jsonify({"groups": reports.list_reports()})


@app.route("/api/reports/<path:rel_path>", methods=["GET"])
def api_get_report(rel_path):
    try:
        return jsonify(reports.get_report(rel_path))
    except FileNotFoundError:
        return err(f"Report not found: {rel_path}", 404)
    except ValueError as e:
        return err(str(e), 400)


@app.route("/api/reports/compare", methods=["POST"])
def api_compare_reports():
    body = request.get_json(silent=True) or {}
    paths = body.get("paths", [])
    if not isinstance(paths, list) or len(paths) < 2:
        return err("Provide at least 2 report paths to compare", 400)
    try:
        return jsonify(reports.compare_reports(paths))
    except FileNotFoundError as e:
        return err(f"Report not found: {e}", 404)
    except ValueError as e:
        return err(str(e), 400)


# --------------------------------------------------------------------------- history
@app.route("/api/history/tree", methods=["GET"])
def api_history_tree():
    source = request.args.get("source", "logs")
    try:
        return jsonify({"tree": history.get_tree(source)})
    except ValueError as e:
        return err(str(e), 400)


@app.route("/api/history/file/<source>/<path:rel_path>", methods=["GET"])
def api_history_file(source, rel_path):
    try:
        return jsonify(history.read_file(source, rel_path))
    except FileNotFoundError:
        return err(f"File not found: {rel_path}", 404)
    except ValueError as e:
        return err(str(e), 400)


@app.route("/api/history/raw/<source>/<path:rel_path>", methods=["GET"])
def api_history_raw(source, rel_path):
    try:
        p = history.resolve_raw_path(source, rel_path)
    except FileNotFoundError:
        return err(f"File not found: {rel_path}", 404)
    except ValueError as e:
        return err(str(e), 400)
    return send_file(p, mimetype=history.guess_mimetype(p))


# --------------------------------------------------------------------------- monitors (machine stats)
@app.route("/api/monitors", methods=["GET"])
def api_list_monitors():
    return jsonify({"monitors": monitors.list_monitors()})


@app.route("/api/monitors", methods=["POST"])
def api_add_monitor():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(monitors.add_monitor(body.get("name", ""), body.get("command", ""), body.get("watch_interval", 0)))
    except ValueError as e:
        return err(str(e), 400)


@app.route("/api/monitors/<monitor_id>", methods=["DELETE"])
def api_remove_monitor(monitor_id):
    try:
        ok = monitors.remove_monitor(monitor_id)
    except ValueError as e:
        return err(str(e), 400)
    if not ok:
        return err("Monitor not found", 404)
    return jsonify({"removed": True})


@app.route("/api/monitors/<monitor_id>/start", methods=["POST"])
def api_start_monitor(monitor_id):
    try:
        return jsonify(monitors.start_monitor(monitor_id))
    except tmux.TmuxError as e:
        return err(str(e), 400)
    except ValueError as e:
        return err(str(e), 404)


@app.route("/api/monitors/<monitor_id>/stop", methods=["POST"])
def api_stop_monitor(monitor_id):
    try:
        return jsonify(monitors.stop_monitor(monitor_id))
    except ValueError as e:
        return err(str(e), 404)


@app.route("/api/monitors/<monitor_id>/output", methods=["GET"])
def api_monitor_output(monitor_id):
    try:
        return jsonify(monitors.get_output(monitor_id))
    except ValueError as e:
        return err(str(e), 404)


# --------------------------------------------------------------------------- tensorboard
@app.route("/api/tensorboard/status", methods=["GET"])
def api_tb_status():
    return jsonify(tb.status())


@app.route("/api/tensorboard/start", methods=["POST"])
def api_tb_start():
    try:
        return jsonify(tb.start())
    except tb.TensorboardLaunchError as e:
        return err(str(e), 400)


@app.route("/api/tensorboard/stop", methods=["POST"])
def api_tb_stop():
    return jsonify(tb.stop())


# --------------------------------------------------------------------------- system
@app.route("/api/system", methods=["GET"])
def api_system():
    return jsonify({
        "repo_root": str(settings.repo_root),
        "configs_dir": str(settings.configs_dir),
        "logs_dir": str(settings.logs_dir),
        "runs_dir": str(settings.runs_dir),
        "plots_dir": str(settings.plots_dir),
        "reports_dir": str(settings.reports_dir),
        "artifacts_dir": str(settings.artifacts_dir),
        "poll_interval_ms": settings.poll_interval_ms,
        "tensorboard_port": settings.tensorboard_port,
        "env_activate_cmd": settings.env_activate_cmd,
        "tmux_available": tmux.tmux_available(),
    })


# --------------------------------------------------------------------------- static frontend
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def _warn_if_exposed():
    if settings.server_host not in ("127.0.0.1", "localhost", "::1") and not settings.api_token:
        print(
            f"\n WARNING: server_host is '{settings.server_host}' (not loopback-only) and "
            "api_token is unset.\n"
            "  Every API below is reachable from the network with no authentication at all, "
            "and several of them\n"
            "  (Terminals, Monitors, Scheduler) can run arbitrary shell commands on this "
            "machine.\n"
            "  Set api_token in dashboard_config.yaml before exposing this beyond localhost.\n",
            file=sys.stderr,
        )


if __name__ == "__main__":
    _warn_if_exposed()
    app.run(host=settings.server_host, port=settings.server_port, threaded=True)
