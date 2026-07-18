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

from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, send_file

from backend.config import settings
from backend import configs as cfg
from backend import terminals
from backend import reports
from backend import history
from backend import monitors
from backend import tensorboard_manager as tb
from backend import tmux_runner as tmux

APP_DIR = Path(__file__).resolve().parent

app = Flask(__name__, static_folder=str(APP_DIR / "static"), static_url_path="")


def err(message, code=400):
    return jsonify({"detail": message}), code


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
    body = request.get_json(force=True, silent=True) or {}
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
    body = request.get_json(force=True, silent=True) or {}
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
    if not terminals.stop(session_name):
        return err("Terminal is not running", 400)
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
    terminals.kill(session_name)
    return jsonify({"killed": True})


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
    body = request.get_json(force=True, silent=True) or {}
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
    body = request.get_json(force=True, silent=True) or {}
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
        "poll_interval_ms": settings.poll_interval_ms,
        "tensorboard_port": settings.tensorboard_port,
        "env_activate_cmd": settings.env_activate_cmd,
        "tmux_available": tmux.tmux_available(),
    })


# --------------------------------------------------------------------------- static frontend
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    app.run(host=settings.server_host, port=settings.server_port, threaded=True)
