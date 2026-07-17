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

        self.python_executable = raw.get("python_executable", "python")
        self.train_script = raw.get("train_script", "train.py")
        self.eval_script = raw.get("eval_script", "eval.py")
        self.eval_default_args = raw.get("eval_default_args", []) or []

        self.env_activate_cmd = (raw.get("env_activate_cmd") or "").strip()
        self.tmux_session_prefix = raw.get("tmux_session_prefix", "expdash")
        self.tmux_pane_width = int(raw.get("tmux_pane_width", 500))
        self.tmux_pane_height = int(raw.get("tmux_pane_height", 50))
        self.tmux_history_limit = int(raw.get("tmux_history_limit", 100000))

        self.server_host = raw.get("server_host", "0.0.0.0")
        self.server_port = int(raw.get("server_port", 8000))

        self.tensorboard_port = int(raw.get("tensorboard_port", 6006))
        self.tensorboard_host = raw.get("tensorboard_host", "0.0.0.0")

        self.poll_interval_ms = int(raw.get("poll_interval_ms", 2000))

        # Runtime state lives inside exp_dashboard/data so it never touches
        # the host repo. state_file is just a session_name -> {config, mode,
        # ...} map; tmux itself is the source of truth for everything else
        # while a session is alive. dashboard_log_dir holds a best-effort
        # snapshot of a session's final output, taken right before it's
        # killed, so deleting a terminal doesn't lose its last output.
        self.state_dir = DASHBOARD_DIR / "data"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "terminals_state.json"
        self.dashboard_log_dir = self.state_dir / "dashboard_logs"
        self.dashboard_log_dir.mkdir(parents=True, exist_ok=True)

    def ensure_dirs(self):
        for d in (self.configs_dir, self.logs_dir, self.runs_dir, self.checkpoints_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
