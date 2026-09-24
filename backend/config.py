"""Loads a repo profile from repos/<profile_name>.yaml and exposes resolved,
absolute paths.

Multi-repo (MULTI_REPO_PLAN.md): this dashboard can drive several sibling
repos (segpriors, dissert, ...) from one running server, switched at runtime
via Settings.reload() — see backend/repos.py for the switch endpoint. Every
module elsewhere reads `from .config import settings` and re-reads its
attributes at call time (never captures a value at import time), which is
what makes an in-place reload() safe: every module's reference stays valid,
no re-import needed anywhere.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import yaml

DASHBOARD_DIR = Path(__file__).resolve().parent.parent
REPOS_DIR = DASHBOARD_DIR / "repos"
# Sibling to, but outside, any per-profile data/<profile>/ subtree (see
# Settings.state_dir below) — records which profile a restart should come
# back up on, instead of defaulting to whichever profile sorts first.
ACTIVE_REPO_FILE = DASHBOARD_DIR / "data" / "_active_repo.json"

# System-wide Kaggle registry. A Kaggle account is a property of the person,
# not of the repo: the same account can host kernels for several repos, and —
# the reason this scope has to exist rather than just being convenient — its
# weekly GPU quota is one real number shared across all of them. Registering
# the same account separately under two profiles would store its credentials
# twice and give each profile a partial view of that one quota, so both would
# under-count it and any quota gate built on them would be wrong.
#
# Sibling to ACTIVE_REPO_FILE, deliberately outside every data/<profile>/
# subtree. Per-repo accounts still live at the Settings paths below; the two
# scopes are merged for reads (backend/kaggle.py).
SYSTEM_KAGGLE_ACCOUNTS_FILE = DASHBOARD_DIR / "data" / "kaggle_accounts.json"
SYSTEM_KAGGLE_CREDS_DIR = DASHBOARD_DIR / "data" / "kaggle_accounts"

# System-wide Colab registry (Multi_runner_XDash.md Phase 4) — same reasoning
# as the Kaggle one above (a Google account is a property of the person, not
# the repo), but system-scope *only*: unlike Kaggle, there is no pre-existing
# repo-scoped registry to stay backward compatible with, so there's no second
# scope to merge.
SYSTEM_COLAB_ACCOUNTS_FILE = DASHBOARD_DIR / "data" / "colab_accounts.json"
SYSTEM_COLAB_CREDS_DIR = DASHBOARD_DIR / "data" / "colab_accounts"


def list_profile_names() -> List[str]:
    if not REPOS_DIR.is_dir():
        return []
    return sorted(p.stem for p in REPOS_DIR.glob("*.yaml"))


def _default_profile_name() -> str:
    names = list_profile_names()
    if not names:
        raise RuntimeError(
            f"No repo profiles found in {REPOS_DIR} — expected at least one <name>.yaml file "
            "(e.g. repos/segpriors.yaml)."
        )
    if ACTIVE_REPO_FILE.is_file():
        try:
            saved = json.loads(ACTIVE_REPO_FILE.read_text()).get("profile")
            if saved in names:
                return saved
        except Exception:
            pass
    return names[0]


class Settings:
    def __init__(self, profile_name: Optional[str] = None):
        self._first_load = True
        self._load(profile_name or _default_profile_name())

    def reload(self, profile_name: str) -> None:
        """Re-runs the load below onto the *same* object (identity
        preserved) so every `from .config import settings` reference held by
        another module keeps pointing at up-to-date data — see module
        docstring. server_host/server_port/api_token are deliberately never
        touched here (MULTI_REPO_PLAN.md §4): they're bound once, from
        whichever profile loaded first at process start, since they're
        properties of the deployment (the open socket), not of whichever
        repo is currently active.
        """
        self._first_load = False
        self._load(profile_name)

    def _load(self, profile_name: str) -> None:
        path = REPOS_DIR / f"{profile_name}.yaml"
        if not path.is_file():
            raise FileNotFoundError(f"Unknown repo profile '{profile_name}' ({path} not found)")
        with open(path, "r") as f:
            raw = yaml.safe_load(f) or {}

        self.profile_name = profile_name
        self.display_name = raw.get("display_name", profile_name)

        # Resolved relative to repos/ itself (this file's directory), not
        # DASHBOARD_DIR — what lets one shared XDash checkout drive several
        # sibling repos without a clone per repo (MULTI_REPO_PLAN.md §3).
        self.repo_root = (REPOS_DIR / raw.get("repo_root", "..")).resolve()
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

        # A format string for passing a seed on the command line — e.g.
        # "--seed {seed}" or "--seeds {seed}". The two repos spell this
        # differently (dissert's train.py takes --seed/--seeds directly;
        # segpriors' bare train.py has neither, only its
        # scripts/run_iccit_sweep.py wrapper's --seeds), so nothing in the
        # dispatcher may hardcode either spelling — every seed-bearing
        # extra_args string is built by formatting this template
        # (EXPERIMENT_AUTOMATION_PLAN.md §2.1). Left blank when unset: a
        # profile that hasn't declared a seed flag simply can't be given one
        # by the batch dispatcher, rather than the dispatcher guessing.
        self.seed_arg = (raw.get("seed_arg") or "").strip()

        # Where the orchestration layer writes run manifests/ledger. Two
        # layouts are supported (MULTI_REPO_PLAN.md §2/§3):
        #   "legacy"      artifacts/runs/<run_id>/manifest.json, artifacts/ledger/*.csv
        #   "experiments" outputs/experiments/<id>/checkpoints/[fold{N}/]manifest.json,
        #                 outputs/ledger/*.csv (ledger_dir is a sibling of
        #                 experiments_dir, not nested under artifacts_dir at all)
        # ledger_dir defaults to artifacts_dir/ledger when unset so an
        # existing "legacy" profile (segpriors) needs zero edits.
        self.artifacts_dir = (self.repo_root / raw.get("artifacts_dir", "artifacts")).resolve()
        self.runs_artifacts_dir = self.artifacts_dir / "runs"
        self.ledger_dir = (
            (self.repo_root / raw["ledger_dir"]).resolve()
            if raw.get("ledger_dir")
            else self.artifacts_dir / "ledger"
        )
        self.experiments_dir = (self.repo_root / raw.get("experiments_dir", "outputs/experiments")).resolve()
        self.manifest_layout = raw.get("manifest_layout", "legacy")
        if self.manifest_layout not in ("legacy", "experiments"):
            raise ValueError(
                f"repos/{profile_name}.yaml: manifest_layout must be 'legacy' or 'experiments', "
                f"got {self.manifest_layout!r}"
            )

        # Interpreter used for "bridge" calls into the host repo's own code
        # (schema export, model-registry introspection — see backend/bridge.py).
        # Needs the host repo's actual dependencies (pydantic, torch, ...)
        # importable, unlike python_executable above which only needs to run
        # train.py/eval.py. Defaults to python_executable so the common case
        # (one env running everything) needs zero extra config.
        self.bridge_python_executable = (raw.get("bridge_python_executable") or "").strip() or self.python_executable

        self.env_activate_cmd = (raw.get("env_activate_cmd") or "").strip()
        # Repo-specific default (MULTI_REPO_PLAN.md §5): two repos can share
        # a lot of config-name vocabulary, so a shared prefix risks tmux
        # session collisions between profiles. Only used when a profile
        # doesn't explicitly set its own.
        self.tmux_session_prefix = raw.get("tmux_session_prefix") or f"xdash-{profile_name}"
        self.tmux_pane_width = int(raw.get("tmux_pane_width", 500))
        self.tmux_pane_height = int(raw.get("tmux_pane_height", 50))
        self.tmux_history_limit = int(raw.get("tmux_history_limit", 100000))

        # Deployment-level, not per-profile (MULTI_REPO_PLAN.md §4): bound
        # once from whichever profile loads first at process start, and
        # never touched again by reload(). A profile file can still declare
        # these for documentation/standalone use.
        if self._first_load:
            self.server_host = raw.get("server_host", "127.0.0.1")
            self.server_port = int(raw.get("server_port", 6070))
            self.api_token = (raw.get("api_token") or "").strip()

        self.tensorboard_port = int(raw.get("tensorboard_port", 6006))
        self.tensorboard_host = raw.get("tensorboard_host", "127.0.0.1")

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
        self.kaggle_setup_reserve_hours = float(raw.get("kaggle_setup_reserve_hours", 0.75))
        self.kaggle_teardown_reserve_hours = float(raw.get("kaggle_teardown_reserve_hours", 0.25))
        # Fallback weekly GPU-hour quota for any account that hasn't set its
        # own weekly_budget_hours (backend/kaggle.py's set_weekly_budget()).
        # A budget is a property of the Kaggle account/tier, not of this
        # repo — this is only ever a default for an account that hasn't been
        # told its real one yet. Kaggle's free tier is commonly ~30 GPU-hrs/
        # week; left generous-but-finite so a newly registered account isn't
        # silently gated to 0 before anyone has configured it.
        self.kaggle_default_weekly_budget_hours = float(raw.get("kaggle_default_weekly_budget_hours", 30.0))

        # Tier-3 fallback for backend/estimates.py's est_hours() — used only when a config has
        # never run before (no measured history) and either declares no training.epochs or its
        # composed model+dataset has no prior run to derive a per-epoch rate from either. A
        # coarse guess, deliberately generous rather than tight, since underestimating is what
        # causes a Kaggle push to get killed mid-run at its session limit.
        self.est_hours_default = float(raw.get("est_hours_default", 6.0))
        self.kaggle_poll_interval_seconds = int(raw.get("kaggle_poll_interval_seconds", 180))
        self.kaggle_webhook_url = (raw.get("kaggle_webhook_url") or "").strip()

        # Colab (Multi_runner_XDash.md Phase 4). Unlike Kaggle's weekly
        # rolling quota, Colab's real constraint is a per-*session* cap that
        # differs by account tier (free ~12h, Pro+ ~24h) — set per account
        # (backend/colab.py's session_limit_hours), this is only the
        # not-yet-configured fallback. setup/teardown reserve mirror
        # Kaggle's own split (provisioning + rsync overhead on one side,
        # `colab stop` + result pull on the other).
        self.colab_executable = raw.get("colab_executable", "colab")
        self.colab_default_session_limit_hours = float(raw.get("colab_default_session_limit_hours", 12.0))
        self.colab_setup_reserve_hours = float(raw.get("colab_setup_reserve_hours", 0.25))
        self.colab_teardown_reserve_hours = float(raw.get("colab_teardown_reserve_hours", 0.1))
        self.colab_default_gpu = raw.get("colab_default_gpu", "T4")
        # How long a VM with nothing queued for its account stays up before
        # `colab stop` reclaims it — keeps a VM warm across attempts of the
        # same batch instead of paying the provisioning+rsync cost on every
        # single attempt (Colab-constraints table: "cold-VM rsync cost").
        self.colab_idle_grace_minutes = float(raw.get("colab_idle_grace_minutes", 10.0))
        # Filename (not a repo-relative path) of the shared launch-template notebook a
        # template-backed worker renders config/mode/extra_args into before every push, when it
        # has no per-worker `template_path` override — see backend/kaggle.py's
        # LAUNCH_SPEC_MARKER / _render_launch_notebook(). XDash-owned (resolved below, once
        # state_dir exists, as kaggle_default_template_file under data/<profile>/) — NOT
        # repo-relative like a worker's notebook_path/template_path override. This used to
        # resolve against repo_root (the host repo's own notebooks/ dir), which meant XDash
        # needed write access into a repo it doesn't own to keep the template current, and let
        # the host repo's copy silently drift from whatever XDash last edited — confirmed
        # happening in practice (2026-09-22: the host dissert repo's notebooks/ copy was still
        # the pre-resume-removal version, months stale, while every fix since had only ever
        # touched XDash's own copy, which nothing was actually reading).
        self.kaggle_default_template = raw.get("kaggle_default_template", "kaggle_worker_template.ipynb")

        # §3.4's precedence rule 3 — a profile's own legacy dataset-name -> Kaggle-dataset-slug
        # map, used to seed data/<profile>/dataset_map.json (XDash-owned storage, rule 2) the
        # first time it's read. Keys are case-folded here so a profile spelling "ClinicDB:" and
        # a config later spelling "clinicdb" still match (XDASH_V2_PLAN.md D11 — the old lookup
        # case-folded only at the call site, never at load, so a mixed-case key never matched).
        self.kaggle_dataset_map = {
            str(name).strip().casefold(): str(source).strip()
            for name, source in (raw.get("kaggle_dataset_map") or {}).items()
            if str(name).strip() and str(source).strip()
        }

        # Runtime state lives inside XDash/data, namespaced per profile
        # (MULTI_REPO_PLAN.md §5) so switching profiles never mixes one
        # repo's terminals/scheduler/Kaggle registry with another's.
        # state_file is just a session_name -> {config, mode, ...} map; tmux
        # itself is the source of truth for everything else while a session
        # is alive. dashboard_log_dir holds a best-effort snapshot of a
        # session's final output, taken right before it's killed, so
        # deleting a terminal doesn't lose its last output.
        self.state_dir = DASHBOARD_DIR / "data" / profile_name
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "terminals_state.json"
        self.monitors_file = self.state_dir / "monitors.json"
        self.scheduler_file = self.state_dir / "scheduler.json"
        self.run_notes_file = self.state_dir / "run_notes.json"
        # XDash-owned dataset-name -> Kaggle-dataset-slug map (XDASH_V2_PLAN.md §3.4 rule 2),
        # lazily seeded from kaggle_dataset_map above the first time it's read/written — see
        # backend/dataset_map.py's resolve_kaggle_dataset(). Per-profile like every other state
        # file here, so mapping clinicdb for dissert never leaks into another profile.
        self.dataset_map_file = self.state_dir / "dataset_map.json"
        # The resolved, absolute path backend/kaggle.py actually opens for the default template —
        # see kaggle_default_template's own comment above for why this moved out of repo_root.
        self.kaggle_default_template_file = self.state_dir / self.kaggle_default_template
        # backend/experiments.py's own store (XDASH_V2_PLAN.md §3/Phase B) — the single
        # dispatcher's state since the legacy assignments.json/batches.json pair and their
        # batch_runner.py dispatcher were retired (§3.7).
        self.experiments_store_file = self.state_dir / "experiments.json"
        self.dashboard_log_dir = self.state_dir / "dashboard_logs"
        self.dashboard_log_dir.mkdir(parents=True, exist_ok=True)

        # kaggle_accounts_file is the account/worker registry; kaggle_creds_dir
        # holds each account's real credentials (kaggle_creds_dir/<account>/
        # kaggle.json and/or .../access_token — an account may have either or
        # both). Both are gitignored — see backend/kaggle.py.
        self.kaggle_accounts_file = self.state_dir / "kaggle_accounts.json"
        # Retired with the worker registry (XDASH_V2_PLAN.md §3.7) — kept only so an
        # existing data/<profile>/kaggle_state.json is still addressable if it needs
        # inspecting by hand. Nothing reads it; Attempts in experiments.json replaced it.
        self.kaggle_state_file = self.state_dir / "kaggle_state.json"
        self.kaggle_creds_dir = self.state_dir / "kaggle_accounts"
        self.kaggle_creds_dir.mkdir(parents=True, exist_ok=True)

        # Runtime-editable settings for the 5 notification channels (Telegram,
        # Discord, Slack, email, ntfy.sh) — see backend/notifications.py.
        # Shared by the Kaggle tab (worker completion) and the Scheduler tab
        # (notify_on_finish), so it's a plain top-level state file rather
        # than scoped to either feature's name. Deliberately its own
        # gitignored file, not a repo-profile key, since it's edited from the
        # dashboard UI at runtime, not at deploy time. Kept the same on-disk
        # filename it shipped with (kaggle_notifications.json) so any
        # already-configured channels on a running deployment aren't
        # orphaned by this rename.
        self.notifications_file = self.state_dir / "kaggle_notifications.json"

    def ensure_dirs(self):
        for d in (self.configs_dir, self.logs_dir, self.runs_dir, self.checkpoints_dir, self.plots_dir, self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
