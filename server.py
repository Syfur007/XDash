"""Experiment Dashboard — Flask backend.

Run with:  python server.py

Flask is used deliberately instead of FastAPI: it has a much smaller, more
stable dependency chain (no pydantic version-matching issues), which matters
for older environments (this was written targeting Python 3.8).

This file, plus everything under backend/ and static/, is the entire
subsystem. It reads repos/<profile>.yaml (MULTI_REPO_PLAN.md) to find a
host repo's configs/logs/runs directories — one deployment can drive
several sibling repos, switched at runtime via /api/repos/active.
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
from backend import hosts
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
from backend import colab as colab_ops
from backend import snapshot as snapshot_ops
from backend import notifications
from backend import run_notes
from backend import repos as repos_ops
from backend import runners as runner_registry
from backend.runners import registry as runner_slots
from backend.runners.base import ACTIVE_STATUSES
from backend import dataset_map
from backend import datasets as dataset_registry
from backend import profile_ops
from backend import runtimes as runtimes_mod
from backend import experiments
from backend import studies
from backend import paths
from backend import templates
from backend.store import StoreCorruptError

APP_DIR = Path(__file__).resolve().parent

app = Flask(__name__, static_folder=str(APP_DIR / "static"), static_url_path="")

scheduler.ensure_worker_started()
# No separate Kaggle poller any more: experiments.py's dispatcher polls its own
# Kaggle Attempts (_poll_kaggle_attempts), and the worker registry the old
# poller existed to watch is gone (XDASH_V2_PLAN.md §3.7).
experiments.ensure_dispatcher_started()


def err(message, code=400):
    return jsonify({"detail": message}), code


@app.errorhandler(StoreCorruptError)
def _store_corrupt(e):
    """A state file that won't parse (backend/store.py) fails every request
    that touches it, loudly and with the restore command — never as an empty
    list that a later save would write over the real data (X11)."""
    return err(str(e), 500)


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


@app.route("/api/paths", methods=["GET"])
def api_list_paths():
    try:
        return jsonify({"paths": paths.list_paths(request.args.get("scope", "repo"), request.args.get("kind", "file"))})
    except ValueError as e:
        return err(str(e), 400)


@app.route("/api/paths/exists", methods=["GET"])
def api_path_exists():
    """XDASH_PLAN.md §8.6's Settings path widget: a lightweight existence
    check for whatever's currently typed into a `*_dir`/`*_path`/`root`
    field — not a listing, just ✓/✗ for one value."""
    try:
        return jsonify(paths.path_exists(request.args.get("scope", "repo"), request.args.get("path", "")))
    except ValueError as e:
        return err(str(e), 400)


@app.route("/api/templates", methods=["GET"])
def api_list_templates():
    return jsonify({"templates": templates.list_templates()})


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


def _run_experiment_name(run: dict) -> str:
    return ((run.get("resolved_config") or {}).get("logging") or {}).get("experiment_name") or ""


@app.route("/api/runs/<run_id>/plots", methods=["GET"])
def api_run_plots(run_id):
    run = ledger.get_run(run_id)
    if run is None:
        return err(f"No manifest found for run '{run_id}'", 404)
    experiment_name = _run_experiment_name(run)
    return jsonify({"experiment_name": experiment_name, "plots": ledger.find_experiment_plots(experiment_name)})


@app.route("/api/runs/<run_id>/plots/<filename>", methods=["GET"])
def api_run_plot_file(run_id, filename):
    run = ledger.get_run(run_id)
    if run is None:
        return err(f"No manifest found for run '{run_id}'", 404)
    p = ledger.resolve_experiment_plot(_run_experiment_name(run), filename)
    if p is None:
        return err("Plot not found", 404)
    return send_file(p, mimetype="image/png")


@app.route("/api/runs/<run_id>/requeue-config", methods=["GET"])
def api_run_requeue_config(run_id):
    run = ledger.get_run(run_id)
    if run is None:
        return err(f"No manifest found for run '{run_id}'", 404)
    experiment_name = _run_experiment_name(run)
    config_path = cfg.find_config_by_experiment_name(experiment_name)
    if not config_path:
        return err(f"No config on disk matches experiment '{experiment_name}' anymore", 404)
    return jsonify({"config_path": config_path})


@app.route("/api/runs/notes", methods=["GET"])
def api_run_notes():
    return jsonify(run_notes.list_notes())


@app.route("/api/runs/<run_id>/note", methods=["PUT"])
def api_set_run_note(run_id):
    body = request.get_json(silent=True) or {}
    return jsonify(run_notes.set_note(run_id, body.get("tag", ""), body.get("note", "")))


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
    host_id = body.get("host_id")  # omitted/None -> local, unchanged from before hosts existed
    if not config_path:
        return err("Missing 'config_path'", 400)
    if mode not in ("train", "eval"):
        return err("mode must be 'train' or 'eval'", 400)
    if not tmux.tmux_available(host_id=host_id):
        return err(
            "'tmux' was not found on PATH. Install it (e.g. `sudo apt install tmux`) "
            "to run experiments from the dashboard.",
            400,
        )
    try:
        return jsonify(terminals.launch(config_path, mode, extra_args, host_id=host_id))
    except FileNotFoundError:
        return err(f"Config not found: {config_path}", 404)
    except (ValueError, hosts.HostError) as e:
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
        created = scheduler.add_item(config_path, mode, extra_args, host_id=body.get("host_id"))
    except FileNotFoundError:
        return err(f"Config not found: {config_path}", 404)
    except (ValueError, hosts.HostError) as e:
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


@app.route("/api/scheduler/paused", methods=["POST"])
def api_scheduler_set_paused():
    body = request.get_json(silent=True) or {}
    return jsonify({"paused": scheduler.set_paused(body.get("value", False))})


@app.route("/api/scheduler/notify_on_finish", methods=["POST"])
def api_scheduler_set_notify():
    body = request.get_json(silent=True) or {}
    return jsonify({"notify_on_finish": scheduler.set_notify_on_finish(body.get("value", False))})


@app.route("/api/scheduler/templates", methods=["GET"])
def api_scheduler_list_templates():
    return jsonify({"templates": scheduler.list_templates()})


@app.route("/api/scheduler/templates", methods=["POST"])
def api_scheduler_add_template():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(scheduler.add_template(
            body.get("name", ""), body.get("config_path", ""), body.get("mode", "train"), body.get("extra_args", ""),
        ))
    except FileNotFoundError:
        return err(f"Config not found: {body.get('config_path')}", 404)
    except ValueError as e:
        return err(str(e), 400)


@app.route("/api/scheduler/templates/<template_id>", methods=["DELETE"])
def api_scheduler_remove_template(template_id):
    if not scheduler.remove_template(template_id):
        return err("Template not found", 404)
    return jsonify({"removed": True})


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
            scope=body.get("scope") or kaggle_ops.SCOPE_REPO,
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


@app.route("/api/kaggle/accounts/<name>/weekly_budget", methods=["POST"])
def api_kaggle_set_weekly_budget(name):
    body = request.get_json(silent=True) or {}
    hours = body.get("hours")
    try:
        return jsonify(kaggle_ops.set_weekly_budget(name, float(hours) if hours is not None else None))
    except (kaggle_ops.KaggleOpsError, TypeError, ValueError) as e:
        return err(str(e), 400)


# Resume-snapshot buffer (Multi_runner_XDash.md Phase 5b/6) — read-only diagnostics for
# an account's own snapshot dataset, so a `snapshot-failed` block is diagnosable from the
# Compute tab without a shell. A real `kaggle datasets status` call, so the frontend
# triggers this on demand (a button), never auto-polls it.
@app.route("/api/kaggle/accounts/<name>/snapshot", methods=["GET"])
def api_kaggle_snapshot_status(name):
    try:
        return jsonify(snapshot_ops.status(name))
    except snapshot_ops.SnapshotError as e:
        return err(str(e), 400)


# The Compute runtime detail's Kaggle Settings tab (XDASH_PLAN.md §8.4) needs the
# real kernel slug/deep link for the one-time GITHUB_TOKEN secret checklist —
# kernel_slug_for_account()/kernel_url() already existed (dispatch and the
# kaggle-secret-missing block both use them) but nothing served them for an
# account the dashboard hasn't tried to dispatch to yet.
@app.route("/api/kaggle/accounts/<name>/kernel", methods=["GET"])
def api_kaggle_kernel_info(name):
    slug = kaggle_ops.kernel_slug_for_account(name)
    return jsonify({
        "kernel_slug": slug,
        "kernel_url": kaggle_ops.kernel_url(name, slug, edit=False),
        "kernel_edit_url": kaggle_ops.kernel_url(name, slug, edit=True),
    })


# CLI 2.x groundwork (XDASH_PLAN.md §10 Phase 5) — real `kaggle quota`, once
# settings.kaggle_executable points at a CLI >= 2.2.1 in a Python 3.11+ env.
# Degrades to {"available": False} against the installed 1.7.4.5, so the
# existing self-tracked estimate_usage()/usage_history() stay the only
# numbers shown until that env exists — see XDASH_PROGRESS.md's Phase 5
# section for the exact env-creation command.
@app.route("/api/kaggle/accounts/<name>/quota", methods=["GET"])
def api_kaggle_measured_quota(name):
    return jsonify(kaggle_ops.get_measured_quota(name))


# Live `kernels logs -f` (CLI >= 2.0.2) for the Compute runtime detail's Now
# tab — a real Kaggle API call once started; see backend/kaggle.py's own
# comment on exactly which line does that.
@app.route("/api/kaggle/accounts/<name>/logs/follow", methods=["POST"])
def api_kaggle_start_log_follow(name):
    body = request.get_json(silent=True) or {}
    slug = body.get("kernel_slug") or kaggle_ops.kernel_slug_for_account(name)
    return jsonify(kaggle_ops.start_kernel_log_follow(name, slug))


@app.route("/api/kaggle/accounts/<name>/logs/follow", methods=["GET"])
def api_kaggle_read_log_follow(name):
    slug = request.args.get("kernel_slug") or kaggle_ops.kernel_slug_for_account(name)
    return jsonify(kaggle_ops.read_kernel_log_follow(name, slug))


@app.route("/api/kaggle/accounts/<name>/logs/follow", methods=["DELETE"])
def api_kaggle_stop_log_follow(name):
    slug = request.args.get("kernel_slug") or kaggle_ops.kernel_slug_for_account(name)
    return jsonify({"stopped": kaggle_ops.stop_kernel_log_follow(name, slug)})


# --------------------------------------------------------------------------- colab
# Account registry only (Multi_runner_XDash.md Phase 4/6) — VM provisioning/teardown is
# the dispatcher's own job (backend/runners/colab.py's ColabRunner), not something a route
# here triggers directly; credential capture ("Connect account") stays a documented
# out-of-band step (backend/colab.py's own module docstring) — no interactive OAuth flow
# exists to wire a route to yet.
@app.route("/api/colab/accounts", methods=["GET"])
def api_colab_list_accounts():
    return jsonify({"accounts": colab_ops.list_accounts()})


@app.route("/api/colab/accounts", methods=["POST"])
def api_colab_add_account():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(colab_ops.add_account(
            body.get("name", ""), body.get("label", ""), body.get("gpu", ""),
            session_limit_hours=body.get("session_limit_hours"),
            auth=body.get("auth") or "oauth2", ssh_key=body.get("ssh_key") or "",
        ))
    except colab_ops.ColabOpsError as e:
        return err(str(e), 400)


@app.route("/api/colab/accounts/<name>", methods=["DELETE"])
def api_colab_remove_account(name):
    if not colab_ops.remove_account(name):
        return err("Account not found", 404)
    return jsonify({"removed": True})


@app.route("/api/colab/accounts/<name>/session_limit", methods=["POST"])
def api_colab_set_session_limit(name):
    body = request.get_json(silent=True) or {}
    hours = body.get("hours")
    try:
        return jsonify(colab_ops.set_session_limit(name, float(hours) if hours is not None else None))
    except (colab_ops.ColabOpsError, TypeError, ValueError) as e:
        return err(str(e), 400)


# Live VM state (Multi_runner_XDash.md Phase 6) — a real `colab sessions`/
# `colab stop` CLI call each, so both are on-demand buttons, never auto-polled.
# No manual "provision" route: a VM is only ever created by the dispatcher's
# own can_accept()/dispatch() pipeline for a real experiment (ColabRunner has
# no direct_launch either, for the same reason) — provisioning real, billable
# compute with nothing to run on it has no route to trigger it deliberately.
@app.route("/api/colab/accounts/<name>/session", methods=["GET"])
def api_colab_session_status(name):
    # list_sessions() never raises — a CLI/credentials failure degrades to
    # [] (see its own docstring), same "read-only status must degrade, not
    # 500" discipline as tmux_runner's own returncode-127 convention.
    sessions = colab_ops.list_sessions(name)
    return jsonify({"live": bool(sessions), "sessions": sessions})


@app.route("/api/colab/accounts/<name>/stop", methods=["POST"])
def api_colab_stop_session(name):
    stopped = colab_ops.stop_session(name)
    return jsonify({"stopped": stopped})


# Compute-unit balance and burn rate (`colab usage`, XDASH_PLAN.md X5) — a real
# network call, so on demand only, like the two routes above.
@app.route("/api/colab/accounts/<name>/usage", methods=["GET"])
def api_colab_usage(name):
    try:
        return jsonify(colab_ops.usage(name))
    except colab_ops.ColabOpsError as e:
        return err(str(e), 400)


# Connect-account OAuth flow (XDASH_PLAN.md §8.4/§10 Phase 5) — starts the
# CLI's copy-paste login as a subprocess with this account's own HOME, shows
# the sign-in URL, and accepts the pasted code back. See backend/colab.py's
# own comment on exactly which line makes the real Google network call —
# it is NOT this route, it's begin_connect()'s call into procsession.start().
@app.route("/api/colab/accounts/<name>/connect", methods=["POST"])
def api_colab_begin_connect(name):
    try:
        return jsonify(colab_ops.begin_connect(name))
    except colab_ops.ColabOpsError as e:
        return err(str(e), 400)


@app.route("/api/colab/accounts/<name>/connect", methods=["GET"])
def api_colab_connect_status(name):
    return jsonify(colab_ops.connect_status(name))


@app.route("/api/colab/accounts/<name>/connect/code", methods=["POST"])
def api_colab_submit_connect_code(name):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(colab_ops.submit_connect_code(name, body.get("code", "")))
    except colab_ops.ColabOpsError as e:
        return err(str(e), 400)


@app.route("/api/colab/accounts/<name>/connect", methods=["DELETE"])
def api_colab_cancel_connect(name):
    return jsonify({"cancelled": colab_ops.cancel_connect(name)})


# --------------------------------------------------------------------------- hosts (Multi_runner_XDash.md Phase 1)
# Machines XDash can open a tmux session on — the local device (always
# present, synthesized if data/hosts.json has no entry for it) plus any
# registered SSH boxes. See backend/hosts.py.
@app.route("/api/hosts", methods=["GET"])
def api_list_hosts():
    return jsonify({"hosts": [h.as_dict() for h in hosts.list_hosts()]})


@app.route("/api/hosts", methods=["POST"])
def api_upsert_host():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(hosts.upsert_host(body).as_dict())
    except hosts.HostError as e:
        return err(str(e), 400)


@app.route("/api/hosts/<host_id>", methods=["DELETE"])
def api_remove_host(host_id):
    try:
        if not hosts.remove_host(host_id):
            return err("Unknown host", 404)
    except hosts.HostError as e:
        return err(str(e), 400)
    return jsonify({"removed": True})


@app.route("/api/hosts/<host_id>/test", methods=["POST"])
def api_test_host(host_id):
    try:
        host = hosts.get_host(host_id)
    except hosts.HostError as e:
        return err(str(e), 404)
    from backend import transport as transport_mod
    t = transport_mod.for_host(host.id)
    reachable = t.available()
    return jsonify({
        "reachable": reachable,
        "tmux_available": tmux.tmux_available(host_id=host.id) if reachable else False,
    })


# --------------------------------------------------------------------------- runners (DASHBOARD_REDESIGN_PLAN.md Phase 0)
# Additive facade over terminals/scheduler/kaggle — every route above keeps working unchanged;
# these compose the same data into one runner-agnostic shape.
@app.route("/api/runners", methods=["GET"])
def api_list_runners():
    return jsonify({"runners": [r.as_dict() for r in runner_registry.list_runners()]})


# --------------------------------------------------------------------------- runtimes (XDASH_PLAN.md §3.6, closes X10)
# One shape for every runtime kind (local/ssh/colab/kaggle) — supersedes /api/runners (kind-
# specific as_dict(), no capacity semantics) and the old standalone /api/slots (only local +
# kaggle) for new UI. /api/runners stays: static/js/views/compute.js still calls it. The
# standalone /api/slots route was retired in Phase 6 — its last caller (spine.js's old flat
# Run Composer) was itself retired in Phase 3, and nothing else ever called it (grepped: zero
# hits in static/). `experiments.list_slots()` itself is unchanged and still backs
# GET /api/pulse's own `slots` field.
@app.route("/api/runtimes", methods=["GET"])
def api_list_runtimes():
    return jsonify({"runtimes": runtimes_mod.list_runtimes()})


# The Add-runtime wizard's live test gate (XDASH_PLAN.md §8.4/§10 Phase 5) — one
# endpoint for every kind, run against fields that are NOT saved yet. See
# backend/runtimes.py::test_runtime for the per-kind dispatch.
@app.route("/api/runtimes/test", methods=["POST"])
def api_test_runtime():
    body = request.get_json(silent=True) or {}
    kind = body.get("kind") or ""
    return jsonify(runtimes_mod.test_runtime(kind, body.get("fields") or {}))


# GPU probe (XDASH_PLAN.md §8.4 Compute Diagnostics) — runs nvidia-smi over the
# host's own transport (local or ssh) and persists the result onto the host
# record, same field runners/machine.py's accelerator() already reads.
@app.route("/api/hosts/<host_id>/probe-gpu", methods=["POST"])
def api_probe_host_gpu(host_id):
    from backend import transport as transport_mod
    from backend.runners import machine as machine_mod
    try:
        host = hosts.get_host(host_id)
    except hosts.HostError as e:
        return err(str(e), 404)
    t = transport_mod.for_host(host.id)
    found = machine_mod.probe_accelerator(t)
    if found is None:
        return jsonify({"found": False, "accelerator": host.accelerator})
    hosts.set_accelerator(host_id, found)
    return jsonify({"found": True, "accelerator": found})


@app.route("/api/experiments/active", methods=["GET"])
def api_experiments_active():
    units = []
    for r in runner_registry.list_runners():
        for u in (r.active_units() if hasattr(r, "active_units") else r.list_units()):
            if u.status in ACTIVE_STATUSES:
                units.append({
                    "unit_id": u.unit_id, "runner_id": u.runner_id, "label": u.label,
                    "status": u.status, "raw_status": u.raw_status,
                    "config_path": u.config_path, "mode": u.mode, "extra": u.extra,
                })
    return jsonify({"units": units})


# /api/runners/<id>/launch is retired (XDASH_PLAN.md Phase 0 item 8): a fourth launch path
# that bypassed the dispatcher, so nothing it started had an experiment, an attempt, a planned
# run dir or a collection. Every run goes through POST /api/experiments.


# --------------------------------------------------------------------------- experiments (XDASH_PLAN.md §3.3, §6, §7)
# Experiments are created as drafts; executing one is an action (queue, run now, a study's
# autopilot) — POST /api/experiments/actions, the one endpoint for a row, a selection or a
# whole study (§6.1). The per-id retry/cancel routes below stay for the current UI.
def _experiment_error(e):
    """ExperimentConflict (the state forbids it) -> 409; anything else -> 400."""
    return err(str(e), 409 if isinstance(e, experiments.ExperimentConflict) else 400)


@app.route("/api/experiments", methods=["GET"])
def api_list_experiments():
    return jsonify({"experiments": experiments.list_experiments(
        study=request.args.get("study"), status=request.args.get("status"),
        config=request.args.get("config"), slot=request.args.get("slot"),
        runtime=request.args.get("runtime"), q=request.args.get("q"),
    )})


@app.route("/api/experiments", methods=["POST"])
def api_create_experiments():
    """Creates **drafts** (X7); `then: "queue" | "run_now"` also executes them (the Run
    Composer sends `then: "queue"`). *configs* accepts either the fully-explicit
    `[{"path": ..., "seeds": [...]}]` shape or the Run Composer's flatter
    `{"configs": ["a.yaml", "b.yaml"], "seeds": [0, 1, 2]}` shorthand, applying the same seed
    list to every path. The response lists which ids were `created` and which `matched` an
    existing experiment (§3.3.1)."""
    body = request.get_json(silent=True) or {}
    configs = body.get("configs") or []
    if configs and isinstance(configs[0], str):
        seeds = body.get("seeds") or [None]
        configs = [{"path": c, "seeds": seeds} for c in configs]
    try:
        # extra_args: {"train": "...", "eval": "..."} or a legacy string (train-only) — only for
        # flags that aren't config; config overrides go in `overlay` (dotted keys, §4.3).
        result = experiments.create_experiments(
            configs=configs, extra_args=body.get("extra_args", ""),
            runtime=body.get("runtime"), pool=body.get("pool"), overlay=body.get("overlay"),
            study_id=body.get("study_id"), group=body.get("group"), studies=body.get("studies"),
            batch_name=body.get("batch_name"), priority=body.get("priority", 0),
            max_retries=body.get("max_retries", 1), force_on_retry=bool(body.get("force_on_retry", True)),
            max_legs=body.get("max_legs", 6), notes=body.get("notes", ""), then=body.get("then"),
            rerun=bool(body.get("rerun", True)),
        )
    except experiments.ExperimentError as e:
        return _experiment_error(e)
    return jsonify(result)


@app.route("/api/experiments/actions", methods=["POST"])
def api_experiment_actions():
    """`{action, ids | study_id | filter, params}` -> `{ok: [...], skipped: [{id, reason}]}`."""
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(experiments.apply_action(
            body.get("action"), ids=body.get("ids"), study_id=body.get("study_id"),
            filter=body.get("filter"), params=body.get("params"),
        ))
    except experiments.ExperimentError as e:
        return _experiment_error(e)


@app.route("/api/experiments/preflight", methods=["POST"])
def api_experiment_preflight():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(experiments.preflight(ids=body.get("ids"), specs=body.get("specs")))
    except experiments.ExperimentError as e:
        return _experiment_error(e)


@app.route("/api/experiments/<experiment_id>", methods=["GET"])
def api_get_experiment(experiment_id):
    try:
        return jsonify(experiments.get_experiment(experiment_id))
    except experiments.ExperimentError as e:
        return err(str(e), 404)


@app.route("/api/experiments/<experiment_id>", methods=["PATCH"])
def api_update_experiment(experiment_id):
    try:
        return jsonify(experiments.update_experiment(experiment_id, request.get_json(silent=True) or {}))
    except experiments.ExperimentError as e:
        if str(e).startswith("Unknown experiment"):
            return err(str(e), 404)
        return _experiment_error(e)


@app.route("/api/experiments/<experiment_id>/retry", methods=["POST"])
def api_retry_experiment(experiment_id):
    try:
        return jsonify(experiments.retry_experiment(experiment_id))
    except experiments.ExperimentError as e:
        return _experiment_error(e)


@app.route("/api/experiments/<experiment_id>/cancel", methods=["POST"])
def api_cancel_experiment(experiment_id):
    try:
        return jsonify(experiments.cancel_experiment(experiment_id))
    except experiments.ExperimentError as e:
        return err(str(e), 400)


@app.route("/api/experiments/<experiment_id>", methods=["DELETE"])
def api_delete_experiment(experiment_id):
    # ?remove_results=1 also deletes XDash's own outputs/kaggle/<id> download cache;
    # ?remove_ledger=1 also deletes this experiment's run(s) from the host repo's own ledger —
    # see experiments.delete_experiment()'s docstring for why each is opt-in.
    remove_results = request.args.get("remove_results", "").strip().lower() in ("1", "true", "yes")
    remove_ledger = request.args.get("remove_ledger", "").strip().lower() in ("1", "true", "yes")
    try:
        if not experiments.delete_experiment(experiment_id, remove_results=remove_results, remove_ledger=remove_ledger):
            return err("Experiment not found", 404)
    except experiments.ExperimentError as e:
        return err(str(e), 400)
    return jsonify({"removed": True, "removed_results": remove_results, "removed_ledger": remove_ledger})


# --------------------------------------------------------------------------- Experiment page (XDASH_PLAN.md §8.3, Phase 3)
# Three small read-only additions the Experiment page's Live/Metrics/Artifacts tabs need —
# nothing before Phase 3 served an attempt's console log or a run dir's files over HTTP.
@app.route("/api/experiments/<experiment_id>/log", methods=["GET"])
def api_experiment_log(experiment_id):
    try:
        return jsonify(experiments.get_attempt_log(
            experiment_id, attempt_id=request.args.get("attempt_id"), stage=request.args.get("stage"),
        ))
    except experiments.ExperimentError as e:
        return _experiment_error(e)


@app.route("/api/experiments/<experiment_id>/report", methods=["GET"])
def api_experiment_report(experiment_id):
    try:
        return jsonify(experiments.get_experiment_report(experiment_id))
    except experiments.ExperimentError as e:
        return _experiment_error(e)


@app.route("/api/experiments/<experiment_id>/curves", methods=["GET"])
def api_experiment_curves(experiment_id):
    # Phase 6 (§8.2/§8.3): TensorBoard scalars for the Metrics tab and for
    # Study Compare's overlaid-curves section. `tags=` narrows the response
    # (Compare asks for one metric's tag across many experiments at once).
    tags = [t.strip() for t in (request.args.get("tags") or "").split(",") if t.strip()] or None
    try:
        return jsonify(experiments.get_experiment_curves(
            experiment_id, attempt_id=request.args.get("attempt_id"), tags=tags,
        ))
    except experiments.ExperimentError as e:
        return _experiment_error(e)


@app.route("/api/experiments/<experiment_id>/artifacts", methods=["GET"])
def api_experiment_artifacts(experiment_id):
    try:
        return jsonify(experiments.list_attempt_artifacts(experiment_id, attempt_id=request.args.get("attempt_id")))
    except experiments.ExperimentError as e:
        return _experiment_error(e)


@app.route("/api/experiments/<experiment_id>/artifacts/<path:rel_path>", methods=["GET"])
def api_experiment_artifact_file(experiment_id, rel_path):
    try:
        path = experiments.resolve_attempt_artifact(experiment_id, rel_path, attempt_id=request.args.get("attempt_id"))
    except experiments.ExperimentError as e:
        return _experiment_error(e)
    return send_file(path)


# --------------------------------------------------------------------------- studies (XDASH_PLAN.md §3.2, §7)
# Replace batches (§3.8; /api/batches* is retired — nothing in the UI called it). Status is
# derived; delete removes memberships only.
def _study_error(e):
    return err(str(e), 404 if str(e).startswith("Unknown study") else 400)


@app.route("/api/studies", methods=["GET"])
def api_list_studies():
    include_archived = request.args.get("archived", "1").strip().lower() not in ("0", "false", "no")
    return jsonify({"studies": studies.list_studies(include_archived=include_archived)})


@app.route("/api/studies", methods=["POST"])
def api_create_study():
    try:
        return jsonify(studies.create_study(request.get_json(silent=True) or {}))
    except studies.StudyError as e:
        return _study_error(e)


@app.route("/api/studies/<study_id>", methods=["GET"])
def api_get_study(study_id):
    try:
        return jsonify(studies.get_study(study_id))
    except studies.StudyError as e:
        return _study_error(e)


@app.route("/api/studies/<study_id>", methods=["PATCH"])
def api_update_study(study_id):
    try:
        return jsonify(studies.update_study(study_id, request.get_json(silent=True) or {}))
    except studies.StudyError as e:
        return _study_error(e)


@app.route("/api/studies/<study_id>", methods=["DELETE"])
def api_delete_study(study_id):
    try:
        return jsonify(studies.delete_study(study_id))
    except studies.StudyError as e:
        return _study_error(e)


@app.route("/api/studies/<study_id>/autopilot", methods=["POST"])
def api_study_autopilot(study_id):
    """`{enabled, max_parallel, hold}` — hold: off *and* dequeue its queued members."""
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(studies.set_autopilot(
            study_id, enabled=body.get("enabled"), max_parallel=body.get("max_parallel"),
            hold=bool(body.get("hold", False)),
        ))
    except studies.StudyError as e:
        return _study_error(e)


@app.route("/api/studies/<study_id>/compare", methods=["GET"])
def api_study_compare(study_id):
    metrics = [m.strip() for m in (request.args.get("metrics") or "").split(",") if m.strip()] or None
    try:
        return jsonify(studies.compare(study_id, metrics=metrics, group_by=request.args.get("group_by") or "config"))
    except studies.StudyError as e:
        return _study_error(e)


@app.route("/api/experiments/compare", methods=["POST"])
def api_experiments_compare_adhoc():
    """Phase 6 (§8.2 U7): "Compare selected" from any bulk-bar selection —
    an arbitrary experiment-id list, not tied to one study. Same response
    shape as `GET /api/studies/<id>/compare`, minus `study_id`."""
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return err("ids must be a non-empty list", 400)
    metrics = body.get("metrics")
    try:
        return jsonify(studies.compare_ids(
            ids, metrics=metrics, group_by=body.get("group_by") or "config",
            baseline_config_path=body.get("baseline_config_path"),
        ))
    except studies.StudyError as e:
        return _study_error(e)


@app.route("/api/pulse", methods=["GET"])
def api_pulse():
    return jsonify(experiments.get_pulse())


def _dataset_error(e):
    return err(str(e), 400)


# XDASH_PLAN.md §5/§7 — the dataset registry (backend/datasets.py): per-runtime
# placement bindings (path/push/fetch/attach), replacing dataset_map.json's narrow
# name->Kaggle-slug map (which stays, underneath, as the Kaggle-slug precedence chain
# resolve_kaggle_dataset() already implements — see datasets.py's own docstring).
#
# Deviation from the plan's literal `GET /api/datasets`: that bare route already serves
# the Data Studio's fragment cards (backend/datasets_info.py, static/js/views/data.js) —
# a different, working feature this phase doesn't touch. The registry's own list is
# GET /api/datasets/registry instead; every route with a <name> segment doesn't collide
# with anything existing, so those match the plan exactly.
@app.route("/api/datasets/registry", methods=["GET"])
def api_list_dataset_registry():
    return jsonify({"datasets": dataset_registry.list_datasets(), "data_account": dataset_registry.data_account()})


@app.route("/api/datasets/registry/data_account", methods=["PUT"])
def api_set_dataset_data_account():
    body = request.get_json(silent=True) or {}
    dataset_registry.set_data_account(body.get("name"))
    return jsonify({"data_account": dataset_registry.data_account()})


@app.route("/api/datasets/<name>", methods=["PUT"])
def api_upsert_dataset(name):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(dataset_registry.upsert_dataset(name, sources=body.get("sources"), bindings=body.get("bindings")))
    except dataset_registry.DatasetError as e:
        return _dataset_error(e)


@app.route("/api/datasets/<name>/bindings/<runtime>", methods=["PUT"])
def api_set_dataset_binding(name, runtime):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(dataset_registry.set_binding(name, runtime, body))
    except dataset_registry.DatasetError as e:
        return _dataset_error(e)


@app.route("/api/datasets/<name>/check", methods=["POST"])
def api_check_dataset(name):
    """Runs a binding's check now, over the real runner's own Transport when
    it has one (local/ssh/colab); Kaggle has none, so it just reports the
    resolved binding (attach is declarative, nothing to dry-check)."""
    runtime_id = (request.args.get("runtime") or "").strip()
    if not runtime_id:
        return err("?runtime=<id> is required", 400)
    kind, _ = runner_slots.parse_slot_id(runtime_id)
    try:
        runner = runner_slots.get_runner(runtime_id)
    except KeyError:
        return err(f"Unknown runtime '{runtime_id}'", 404)
    transport = getattr(runner, "_transport", None)
    if transport is None:
        binding = dataset_registry.resolve_binding(name, runtime_id, kind)
        ok = bool(binding.get("mode"))
        detail = "mode: %s" % binding.get("mode") if ok else "no binding resolves"
        dataset_registry.record_check(name, runtime_id, ok, detail)
        return jsonify({"ok": ok, "mode": binding.get("mode"), "detail": detail})
    repo_root = getattr(getattr(runner, "host", None), "repo_root", None) or settings.repo_root
    return jsonify(dataset_registry.check_binding(name, runtime_id, kind, transport, repo_root))


@app.route("/api/datasets/<name>/configs", methods=["GET"])
def api_dataset_configs(name):
    """XDASH_PLAN.md §8.5's dataset detail page: "which configs ... use it"
    — best-effort, walks every config's own compose chain (backend/datasets.py's
    `configs_using_dataset()`), not a raw text grep."""
    return jsonify({"configs": dataset_registry.configs_using_dataset(name)})


@app.route("/api/datasets/kaggle-map", methods=["GET"])
def api_get_dataset_map():
    return jsonify({"entries": dataset_map.map_with_provenance()})


@app.route("/api/datasets/kaggle-map", methods=["PUT"])
def api_put_dataset_map():
    body = request.get_json(silent=True) or {}
    entries = body.get("entries")
    if not isinstance(entries, dict):
        return err('Body must be {"entries": {name: kaggle_dataset, ...}}', 400)
    saved = dataset_map.save_dataset_map(entries)
    return jsonify({"entries": dataset_map.map_with_provenance(), "saved": saved})


# --------------------------------------------------------------------------- notifications
# Shared by the Kaggle tab and the Scheduler tab — see backend/notifications.py.
@app.route("/api/notifications", methods=["GET"])
def api_get_notifications():
    return jsonify(notifications.get_notification_settings())


@app.route("/api/notifications/<channel>", methods=["PATCH"])
def api_update_notifications(channel):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(notifications.update_notification_settings(channel, body))
    except notifications.NotificationError as e:
        return err(str(e), 400)


@app.route("/api/notifications/<channel>/test", methods=["POST"])
def api_test_notification(channel):
    try:
        return jsonify(notifications.test_notification(channel))
    except notifications.NotificationError as e:
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
        return jsonify(monitors.add_monitor(
            body.get("name", ""), body.get("command", ""), body.get("watch_interval", 0), body.get("host_id"),
        ))
    except (ValueError, hosts.HostError) as e:
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


# --------------------------------------------------------------------------- repos (MULTI_REPO_PLAN.md Phases 1/2/4)
@app.route("/api/repos", methods=["GET"])
def api_list_repos():
    return jsonify({"repos": repos_ops.list_profiles(), "active": settings.profile_name})


@app.route("/api/repos/detect", methods=["POST"])
def api_detect_repo():
    """Read-only preview for the "+ New profile" wizard (XDASH_PLAN.md §8.6) —
    never writes a profile file, just reports what's findable at the given
    repo root so the wizard can show its guesses before Create."""
    body = request.get_json(silent=True) or {}
    return jsonify(repos_ops.detect_repo(body.get("repo_root", "")))


@app.route("/api/repos", methods=["POST"])
def api_create_repo():
    """The wizard's write path: a new `repos/<name>.yaml`, validated the same
    way a Settings PATCH is before it's written (backend/repos.py's
    `create_profile()`). Doesn't switch to it — POST /api/repos/active does
    that, same as switching to any other existing profile."""
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(repos_ops.create_profile(body.get("name", ""), body.get("display_name", ""), body.get("repo_root", "")))
    except repos_ops.RepoProfileError as e:
        return err(str(e), 400)


@app.route("/api/repos/active", methods=["POST"])
def api_set_active_repo():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(repos_ops.set_active_profile(body.get("profile", "")))
    except repos_ops.RepoProfileBusyError as e:
        return err(str(e), 409)
    except repos_ops.RepoProfileError as e:
        return err(str(e), 400)


@app.route("/api/repos/sessions", methods=["GET"])
def api_repo_sessions():
    """Every tmux + Kaggle session across every profile, each tagged with its
    owning repo — Terminals/Runs/Scheduler/Kaggle stay visible regardless of
    which profile is active (MULTI_REPO_PLAN.md §6 option B)."""
    return jsonify({"sessions": repos_ops.list_global_sessions()})


# --------------------------------------------------------------------------- system
@app.route("/api/system", methods=["GET"])
def api_system():
    return jsonify({
        "profile_name": settings.profile_name,
        "display_name": settings.display_name,
        "repo_root": str(settings.repo_root),
        "configs_dir": str(settings.configs_dir),
        "logs_dir": str(settings.logs_dir),
        "runs_dir": str(settings.runs_dir),
        "plots_dir": str(settings.plots_dir),
        "reports_dir": str(settings.reports_dir),
        "artifacts_dir": str(settings.artifacts_dir),
        "manifest_layout": settings.manifest_layout,
        "poll_interval_ms": settings.poll_interval_ms,
        "tensorboard_port": settings.tensorboard_port,
        "env_activate_cmd": settings.env_activate_cmd,
        "tmux_available": tmux.tmux_available(),
        # XDASH_PLAN.md §4.1's metrics section — served here so the frontend
        # can drop its own hardcoded lower_is_better list (static/app.js:6)
        # and read the profile's instead.
        "metrics": {"primary": settings.metrics_primary, "lower_is_better": settings.metrics_lower_is_better},
        "bridge": bridge.bridge_status(),
        # Settings → About (XDASH_PLAN.md §8.6): XDash's own per-profile state
        # dir, not the (potentially huge) host repo's outputs/ tree — the size
        # a Settings reader actually wants to know about is what XDash itself
        # has accumulated (logs, JSON stores), bounded by construction.
        "state_dir": str(settings.state_dir),
        "state_dir_size_bytes": _dir_size_bytes(settings.state_dir),
    })


def _dir_size_bytes(root: Path) -> int:
    total = 0
    try:
        for entry in root.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


# --------------------------------------------------------------------------- profile (XDASH_PLAN.md §7, §8.6)
# Settings edits repos/<profile>.yaml (XDASH_PLAN.md decision #2) — the file that connects
# XDash to the framework. Comment-preserving (ruamel.yaml round-trip), validated by a
# throwaway Settings load before anything real is touched, applied live via settings.reload().
def _profile_error(e):
    return err(str(e), 400)


@app.route("/api/profile", methods=["GET"])
def api_get_profile():
    try:
        return jsonify(profile_ops.get_profile())
    except profile_ops.ProfileError as e:
        return _profile_error(e)


@app.route("/api/profile", methods=["PATCH"])
def api_patch_profile():
    body = request.get_json(silent=True) or {}
    patch = body.get("patch") if isinstance(body.get("patch"), dict) else body
    try:
        return jsonify(profile_ops.patch_profile(patch))
    except profile_ops.ProfileError as e:
        return _profile_error(e)


@app.route("/api/profile/raw", methods=["GET"])
def api_get_profile_raw():
    try:
        return jsonify({"text": profile_ops.get_profile()["text"]})
    except profile_ops.ProfileError as e:
        return _profile_error(e)


@app.route("/api/profile/raw", methods=["PUT"])
def api_put_profile_raw():
    body = request.get_json(silent=True) or {}
    text = body.get("text")
    if not isinstance(text, str):
        return err('Body must be {"text": "<full profile yaml>"}', 400)
    try:
        return jsonify(profile_ops.put_raw(text))
    except profile_ops.ProfileError as e:
        return _profile_error(e)


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
            "  Set api_token in repos/<profile>.yaml (whichever profile loads first) before "
            "exposing this beyond localhost.\n",
            file=sys.stderr,
        )


if __name__ == "__main__":
    _warn_if_exposed()
    app.run(host=settings.server_host, port=settings.server_port, threaded=True)
