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

import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .store import JsonStore, StoreCorruptError

DASHBOARD_DIR = Path(__file__).resolve().parent.parent
# Both overridable from the environment (XDASH_PLAN.md Phase 0 item 9): the
# test harness points them at a temp dir and a fake profile, so no test ever
# reads or writes the real data/ or a real host repo. Unset (every normal
# run), they are exactly the paths they always were.
REPOS_DIR = Path(os.environ.get("XDASH_REPOS_DIR") or (DASHBOARD_DIR / "repos")).resolve()
DATA_DIR = Path(os.environ.get("XDASH_DATA_DIR") or (DASHBOARD_DIR / "data")).resolve()
# Sibling to, but outside, any per-profile data/<profile>/ subtree (see
# Settings.state_dir below) — records which profile a restart should come
# back up on, instead of defaulting to whichever profile sorts first.
ACTIVE_REPO_FILE = DATA_DIR / "_active_repo.json"
active_repo_store = JsonStore(ACTIVE_REPO_FILE, dict)


def background_disabled() -> bool:
    """XDASH_DISABLE_BACKGROUND=1 keeps the scheduler/dispatcher threads from
    starting (the test harness sets it). Every tick function stays callable
    directly; only the self-starting loops are skipped."""
    return os.environ.get("XDASH_DISABLE_BACKGROUND", "").strip() not in ("", "0", "false")

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
SYSTEM_KAGGLE_ACCOUNTS_FILE = DATA_DIR / "kaggle_accounts.json"
SYSTEM_KAGGLE_CREDS_DIR = DATA_DIR / "kaggle_accounts"

# System-wide Colab registry (Multi_runner_XDash.md Phase 4) — same reasoning
# as the Kaggle one above (a Google account is a property of the person, not
# the repo), but system-scope *only*: unlike Kaggle, there is no pre-existing
# repo-scoped registry to stay backward compatible with, so there's no second
# scope to merge.
SYSTEM_COLAB_ACCOUNTS_FILE = DATA_DIR / "colab_accounts.json"
SYSTEM_COLAB_CREDS_DIR = DATA_DIR / "colab_accounts"


def _get(raw: Dict[str, Any], dotted: str, legacy: Optional[str] = None, default: Any = None) -> Any:
    """One value read two ways (XDASH_PLAN.md §4.1): *dotted* into the
    sectioned profile shape (e.g. "commands.train" -> raw["commands"]["train"]),
    falling back to *legacy* — a flat top-level key from before the profile was
    sectioned (e.g. "train_script") — then *default*. Both spellings keep
    working forever: nothing here ever stops reading the flat key, so an
    unmigrated profile.yaml is exactly as valid as a migrated one. An empty
    string is treated as "unset" (YAML has no way to write "explicitly blank
    but present" that would need to be told apart from "absent" here)."""
    node: Any = raw
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            node = None
            break
        node = node[part]
    if node not in (None, ""):
        return node
    if legacy is not None:
        value = raw.get(legacy)
        if value not in (None, ""):
            return value
    return default


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
    try:
        saved = (active_repo_store.load() or {}).get("profile")
    except (StoreCorruptError, AttributeError):
        # Deliberately tolerant, unlike every other store: this file is only
        # a pointer to which profile to start on, and losing it costs a
        # profile switch, not data. Refusing to start the server over it
        # would be the worse failure.
        saved = None
    if saved in names:
        return saved
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
        # `framework.repo_root`/`framework.configs_dir` (XDASH_PLAN.md §4.1) are the
        # sectioned spellings; the flat `repo_root`/`configs_dir` keep working as
        # their legacy fallback (`_get()`, above list_profile_names()).
        self.repo_root = (REPOS_DIR / _get(raw, "framework.repo_root", legacy="repo_root", default="..")).resolve()
        self.configs_dir = self.repo_root / _get(raw, "framework.configs_dir", legacy="configs_dir", default="configs")
        self.logs_dir = self.repo_root / raw.get("logs_dir", "logs")
        self.runs_dir = self.repo_root / raw.get("runs_dir", "runs")
        self.checkpoints_dir = self.repo_root / raw.get("checkpoints_dir", "checkpoints")
        self.plots_dir = self.repo_root / raw.get("plots_dir", "logs")
        self.reports_dir = self.repo_root / raw.get("reports_dir", raw.get("logs_dir", "logs"))

        # §4.1 `framework:` section — dataset identity keys used by the resolver
        # (§5) to find a dataset's declared root/name in a resolved config or
        # fragment. Legacy flat spellings kept for a profile that hasn't migrated.
        self.dataset_name_key = _get(raw, "framework.dataset_name_key", legacy="dataset_name_key", default="dataset.name")
        self.dataset_root_key = _get(raw, "framework.dataset_root_key", legacy="dataset_root_key", default="dataset.root")
        # Where a dataset-identity fragment lives, as a configs_dir-relative glob
        # (backend/datasets.py's registry resolves a dataset's `root` by scanning
        # these, never by trusting a typed-in value — XDASH_PLAN.md §5.2).
        fragments_raw = _get(raw, "framework.fragments", default=None)
        self.dataset_fragment_glob = (
            (fragments_raw or {}).get("dataset") if isinstance(fragments_raw, dict) else None
        ) or "dataset/*.yaml"

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
        #
        # Superseded, for the actual launch command line, by `commands.train`/
        # `commands.eval` below (XDASH_PLAN.md §4.1/§4.5) — those are the single
        # source every runtime kind renders its command from. `seed_arg` (and
        # train_script/eval_script above) now only feed the *default* commands
        # template a profile gets when it hasn't declared `commands.*` itself
        # (see _default_command_template below), so nothing behaves differently
        # until a profile actually adds a `commands:` section.
        self.seed_arg = (raw.get("seed_arg") or "").strip()

        def _default_command_template(stage: str) -> str:
            script = self.train_script if stage == "train" else self.eval_script
            seed = ("%s " % self.seed_arg) if self.seed_arg else ""
            tail = "{budget} {resume} {extra}" if stage == "train" else "{extra}"
            return "{python} %s --config {config} %s%s" % (script, seed, tail)

        commands_raw = raw.get("commands")
        commands_raw = commands_raw if isinstance(commands_raw, dict) else {}
        # commands.python/env_activate become the single source for
        # python_executable/env_activate_cmd below — every other reader
        # (backend/hosts.py's per-host fallback, terminals.py) is unaffected,
        # since it only ever reads settings.python_executable/env_activate_cmd,
        # never raw.get("python_executable") directly.
        self.commands: Dict[str, str] = {
            "python": _get(raw, "commands.python", legacy="python_executable", default="python"),
            "env_activate": _get(raw, "commands.env_activate", legacy="env_activate_cmd", default=""),
            "train": (commands_raw.get("train") or "").strip() or _default_command_template("train"),
            "eval": (commands_raw.get("eval") or "").strip() or _default_command_template("eval"),
            "budget": (commands_raw.get("budget") or "").strip() or "--max-hours {hours}",
            "resume": (commands_raw.get("resume") or "").strip() or "--resume",
        }
        self.python_executable = self.commands["python"]
        self.env_activate_cmd = self.commands["env_activate"]

        # Framework hooks (XDASH_PLAN.md §4) — `module:function` in the host
        # repo, or `xdash:<framework>.<function>` for an XDash-shipped adapter
        # under backend/bridge_scripts/adapters/, run by bridge_scripts/call.py.
        # Read so far: `locate_run` (the planned run dir, §4.4) and, since
        # Phase 1, `resolve_config` (validates an overlay when an experiment
        # is composed, §4.3); the rest of the sectioned profile shape lands in
        # Phase 2. A missing hook degrades one feature and never breaks
        # dispatch.
        hooks = raw.get("hooks")
        hooks = hooks if isinstance(hooks, dict) else {}
        self.hooks: Dict[str, str] = {str(k): str(v).strip() for k, v in hooks.items() if str(v or "").strip()}
        # XDASH_PLAN.md §4.3 — the key the framework's config loader reads
        # includes from (dissert: `compose`, resolved relative to the file
        # that names it). An experiment's overlay is written as a config file
        # whose only include is the experiment's own config; blank means this
        # framework can't compose one, so experiments here can't carry an
        # overlay (creating one with a non-empty overlay is refused).
        self.overlay_compose_key = _get(raw, "framework.overlay_compose_key", legacy="overlay_compose_key", default="")
        # The line dissert prints per finished run (`run_id=R-... status=...`),
        # parsed from a persisted console log as a cross-check against the
        # run ids planned at dispatch (§4.4's `run-mismatch`).
        self.run_id_pattern = _get(raw, "outputs.run_id_pattern", legacy="run_id_pattern", default=r"run_id=(\S+)")

        # §4.1 `metrics:` section — `primary` is the default metric Compare/Lab
        # sort and highlight by; `lower_is_better` moves the hardcoded list out
        # of static/app.js (its own module-scope constant used to be the only
        # copy) so a repo whose metrics run the other way (e.g. a loss, not a
        # score) doesn't need an XDash code change, only a profile edit —
        # served to the frontend via GET /api/system. `report_glob` is what a
        # study/experiment metrics reader looks for under a run dir; unset
        # means "whatever backend/reports.py already finds by content".
        metrics_raw = raw.get("metrics")
        metrics_raw = metrics_raw if isinstance(metrics_raw, dict) else {}
        lower_is_better = metrics_raw.get("lower_is_better")
        self.metrics_primary = (metrics_raw.get("primary") or "").strip()
        self.metrics_lower_is_better: List[str] = (
            [str(m).strip() for m in lower_is_better if str(m).strip()]
            if isinstance(lower_is_better, list)
            # Same fallback list static/app.js:6 used to hardcode, kept as the
            # default so an unmigrated profile's Compare/report views render
            # exactly as before.
            else ["hd95", "asd", "mean_ms", "median_ms", "std_ms", "p95_ms", "eval_duration_s", "ece"]
        )
        self.metrics_report_glob = (metrics_raw.get("report_glob") or "").strip()

        # XDASH_PLAN.md §4.5 — a Kaggle dispatch whose working tree is dirty,
        # or whose HEAD isn't on any remote branch, blocks with
        # `code-not-pushed` (Kaggle clones from GitHub, so it would silently
        # run different code). True downgrades that block to a warning
        # recorded on the attempt.
        self.allow_unpushed = bool(raw.get("allow_unpushed", False))

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
        # The key `colab ssh --proxy-mode -i` sends to the VM and the ssh
        # client then authenticates with. Must be ed25519 (colab-cli-reference
        # §5.2: RSA is rejected). Resolved against the *real* home here, since
        # every colab subprocess runs under a per-account HOME (X5) where
        # `~/.ssh` would point somewhere empty.
        self.colab_ssh_key = str(Path(raw.get("colab_ssh_key") or "~/.ssh/id_ed25519").expanduser())
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
        # The interpreter TRAIN_CMD/EVAL_CMD are rendered for (XDASH_PLAN.md §4.5) —
        # the venv the template's own setup cell (`## 4. Reproduce the training
        # environment`) creates at this fixed path. Not auto-discovered: the
        # template creates it, XDash only needs to agree on where. A profile
        # whose template sets up a different interpreter path overrides this.
        self.kaggle_venv_python = _get(
            raw, "runtimes.kaggle.venv_python", legacy="kaggle_venv_python",
            default="/kaggle/working/py38_env/bin/python",
        )

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
        self.state_dir = DATA_DIR / profile_name
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # Per-attempt console logs (XDASH_PLAN.md §3.5): tmux panes and the
        # Kaggle `<slug>.log`, kept under logs/<attempt_id>/ so deleting a tmux
        # session or a Kaggle staging dir never loses a run's output.
        self.attempt_logs_root = self.state_dir / "logs"
        self.state_file = self.state_dir / "terminals_state.json"
        self.monitors_file = self.state_dir / "monitors.json"
        self.scheduler_file = self.state_dir / "scheduler.json"
        self.run_notes_file = self.state_dir / "run_notes.json"
        # XDash-owned dataset-name -> Kaggle-dataset-slug map (XDASH_V2_PLAN.md §3.4 rule 2),
        # lazily seeded from kaggle_dataset_map above the first time it's read/written — see
        # backend/dataset_map.py's resolve_kaggle_dataset(). Per-profile like every other state
        # file here, so mapping clinicdb for dissert never leaks into another profile.
        self.dataset_map_file = self.state_dir / "dataset_map.json"
        # The dataset registry (XDASH_PLAN.md §5.2) — replaces dataset_map.json's
        # narrow name->Kaggle-slug map with per-runtime placement bindings
        # (path/push/fetch/attach). dataset_map.json is still read (never
        # written) as the migration source the first time this is empty.
        self.datasets_file = self.state_dir / "datasets.json"
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

    def attempt_log_dir(self, attempt_id: str) -> Path:
        """data/<profile>/logs/<attempt_id>/ — created on first use. The id
        is XDash-generated (atmpt_<hex>), but it's still checked so a crafted
        one can never escape the logs root."""
        if not attempt_id or "/" in attempt_id or attempt_id in (".", ".."):
            raise ValueError("Invalid attempt id %r" % attempt_id)
        path = self.attempt_logs_root / attempt_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def display_path(self, path: Path) -> str:
        """*path* relative to XDash's own directory when it lives inside it
        (`data/dissert/logs/atmpt_x`), absolute otherwise — the form stored on
        records, so they don't embed one machine's absolute checkout path."""
        try:
            return Path(path).resolve().relative_to(DASHBOARD_DIR).as_posix()
        except ValueError:
            return str(path)


settings = Settings()
