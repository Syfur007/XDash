"""Loads dashboard_config.yaml and exposes resolved, absolute paths.

Keeping every path resolution in one place is what lets this whole
`exp_dashboard/` folder be dropped into, or removed from, a repo without
touching any other file.
"""
from __future__ import annotations

from pathlib import Path
import yaml

DASHBOARD_DIR = Path(__file__).resolve().parent.parent
_CONFIG_FILE = DASHBOARD_DIR / "dashboard_config.yaml"


class Settings:
    def __init__(self, path: Path = _CONFIG_FILE):
        with open(path, "r") as f:
            raw = yaml.safe_load(f) or {}

        self.repo_root = (DASHBOARD_DIR / raw.get("repo_root", "..")).resolve()
        self.configs_dir = self.repo_root / raw.get("configs_dir", "configs")
        self.logs_dir = self.repo_root / raw.get("logs_dir", "logs")
        self.runs_dir = self.repo_root / raw.get("runs_dir", "runs")
        self.checkpoints_dir = self.repo_root / raw.get("checkpoints_dir", "checkpoints")
        self.plots_dir = self.repo_root / raw.get("plots_dir", "logs")
        self.reports_dir = self.repo_root / raw.get("reports_dir", raw.get("logs_dir", "logs"))

        self.python_executable = raw.get("python_executable", "python")
        self.train_script = raw.get("train_script", "train.py")
        self.eval_script = raw.get("eval_script", "eval.py")
        self.eval_default_args = raw.get("eval_default_args", []) or []

        # Where the orchestration layer writes manifests/ledger
        # (artifacts/runs/<run_id>/manifest.json, artifacts/ledger/*.csv) — powers
        # the Runs/Ledger views (IMPLEMENTATION_PLAN.md Phase 1). Reading these is
        # plain stdlib json/csv; a host repo without this layout at all just means
        # the readers see empty directories, not an error.
        self.artifacts_dir = (self.repo_root / raw.get("artifacts_dir", "artifacts")).resolve()
        self.ledger_dir = self.artifacts_dir / "ledger"
        self.runs_artifacts_dir = self.artifacts_dir / "runs"

        # Interpreter used for "bridge" calls into the host repo's own code
        # (schema export, model-registry introspection — see backend/bridge.py).
        # Needs the host repo's actual dependencies (pydantic, torch, ...)
        # importable, unlike python_executable above which only needs to run
        # train.py/eval.py. Defaults to python_executable so the common case
        # (one env running everything) needs zero extra config.
        self.bridge_python_executable = (raw.get("bridge_python_executable") or "").strip() or self.python_executable

        self.env_activate_cmd = (raw.get("env_activate_cmd") or "").strip()
        self.tmux_session_prefix = raw.get("tmux_session_prefix", "xdash")
        self.tmux_pane_width = int(raw.get("tmux_pane_width", 500))
        self.tmux_pane_height = int(raw.get("tmux_pane_height", 50))
        self.tmux_history_limit = int(raw.get("tmux_history_limit", 100000))

        self.server_host = raw.get("server_host", "127.0.0.1")
        self.server_port = int(raw.get("server_port", 6070))

        self.tensorboard_port = int(raw.get("tensorboard_port", 6006))
        self.tensorboard_host = raw.get("tensorboard_host", "127.0.0.1")

        # Optional shared-secret required (via the X-Api-Token header) for
        # every state-changing request. Empty = no auth — fine when bound to
        # 127.0.0.1 for a single trusted user, but required reading before
        # binding server_host any wider (see the startup warning in server.py).
        self.api_token = (raw.get("api_token") or "").strip()

        self.poll_interval_ms = int(raw.get("poll_interval_ms", 2000))

        # Upper bounds for the scheduler — without these, max_concurrent is
        # only floored at 1 (no ceiling) and the item queue has no size
        # limit at all, so a caller (malicious or just a stuck retry loop)
        # can spawn unbounded tmux sessions / training processes.
        self.scheduler_max_concurrent_limit = int(raw.get("scheduler_max_concurrent_limit", 8))
        self.scheduler_max_queue_size = int(raw.get("scheduler_max_queue_size", 200))

        self.kaggle_executable = raw.get("kaggle_executable", "kaggle")
        self.kaggle_push_concurrency = max(1, int(raw.get("kaggle_push_concurrency", 3)))
        self.kaggle_default_budget_hours = float(raw.get("kaggle_default_budget_hours", 9.5))

        # Runtime state lives inside exp_dashboard/data so it never touches
        # the host repo. state_file is just a session_name -> {config, mode,
        # ...} map; tmux itself is the source of truth for everything else
        # while a session is alive. dashboard_log_dir holds a best-effort
        # snapshot of a session's final output, taken right before it's
        # killed, so deleting a terminal doesn't lose its last output.
        self.state_dir = DASHBOARD_DIR / "data"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "terminals_state.json"
        self.monitors_file = self.state_dir / "monitors.json"
        self.scheduler_file = self.state_dir / "scheduler.json"
        self.dashboard_log_dir = self.state_dir / "dashboard_logs"
        self.dashboard_log_dir.mkdir(parents=True, exist_ok=True)

        # kaggle_accounts_file is the account/worker registry; kaggle_creds_dir
        # holds each account's real credentials (kaggle_creds_dir/<account>/
        # kaggle.json and/or .../access_token — an account may have either or
        # both). Both are gitignored — see backend/kaggle.py.
        self.kaggle_accounts_file = self.state_dir / "kaggle_accounts.json"
        self.kaggle_state_file = self.state_dir / "kaggle_state.json"
        self.kaggle_creds_dir = self.state_dir / "kaggle_accounts"
        self.kaggle_creds_dir.mkdir(parents=True, exist_ok=True)

    def ensure_dirs(self):
        for d in (self.configs_dir, self.logs_dir, self.runs_dir, self.checkpoints_dir, self.plots_dir, self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
