# XDash — the finalized plan

**Date:** 2026-09-25 · **Status:** proposed; nothing in this plan has been built yet.

**Supersedes:** `XDASH_V2_PLAN.md` §6 (IA and screens) and the Compute-tab layout in
`Multi_runner_XDash.md` Phase 6. **Keeps:** the settled decisions: lifecycle nav, no
Kaggle "workers", vanilla JS, correctness before UI, one Kaggle kernel per account. Also
keeps the whole runner backend (`MachineRunner` / `KaggleRunner` / `ColabRunner`,
`Transport`, hosts, registry, the leg loop, blocked codes, and notifications). That design
holds up. The problems are in what connects it to the framework, to the data, and to the
user.

---

## 0. The one-screen version

XDash exists to do seven things (§1). The runner layer does its part well. The four
layers around it do not yet:

1. **The framework connection is broken, silently.** The seed flag XDash sends takes
   dissert's single-run bypass, which writes no manifest and no ledger row. XDash also
   looks for outputs at a path dissert stopped using in commit `52477d1`. Train-only
   overrides crash eval. As a result, no XDash-launched run has ever been joined to a
   result, and resume can never trigger (§2.2, X1, X2, X12).
2. **Remote runs never come home.** SSH and Colab attempts finish without `collect()`. Colab
   then tears down the VM, and the outputs are lost with it (X3). Colab has no way to get
   its dataset (X4). A fresh Colab account can never provision (X5).
3. **Studies do not exist, and neither do planned experiments.** Creating an experiment
   means executing it. There is nowhere to organize a study first and run it later, and no
   way to choose between manual and automatic runtime assignment or execution (X7).
4. **The UI is organized around subsystems**, not the study → experiment → result loop.

The plan fixes correctness first (Phase 0), then the model (Phase 1), then the framework
and data connections (Phase 2), then the screens you proposed (Phases 3–6).

### Decisions made here that you may want to overrule

| # | Decision | Rejected alternative, and why |
|---|---|---|
| 1 | An Experiment can be in **several Studies** (many-to-many). "Reassign" = move. | Single home: a shared baseline would have to be run twice to appear in two studies. |
| 2 | **Settings edits `repos/<profile>.yaml`**, the file that connects XDash to the framework. The framework's own configs (`base.yaml` etc.) are edited in Experiments → Configs. | Binding Settings to `base.yaml` would make the "control center for the dashboard" edit one experiment default file. |
| 3 | **Overrides become a config overlay file**, not CLI flags. | CLI flags: eval.py rejects train-only flags, and a flag that changes the config changes `config_hash`, so eval cannot find train's run (X12). |
| 4 | **Data is placed at the config's declared `dataset.root` on every runtime** (symlink), and is never passed as `--dataset_dir`. | `dataset.root` is part of dissert's `config_hash`, so a per-runtime path would give one experiment a different identity on each runtime and break cross-runtime resume. |
| 5 | **Outputs are canonicalized**: every collected run is copied into the local `outputs/experiments/…` tree, whichever runtime produced it. | Leaving them in `outputs/kaggle/…` and `outputs/remote/…` staging dirs leaves three trees for every reader to search. |
| 6 | Keep **JSON stores**, but make every write atomic (X11). | SQLite: not needed at this scale (tens to hundreds of experiments). Revisit past ~5k attempts. |
| 7 | New screens are **buildless ES modules** (`<script type="module">`). | Vite/Svelte: no Node on this machine (locked decision). Classic scripts: they caused the load-order `ReferenceError` recorded in memory. |
| 8 | The Results tab is **removed**. Its content moves to the Experiment page and Study → Compare. | Your screen list has no Results tab, and every result belongs to an experiment or a study. |
| 9 | Add **`ruamel.yaml`** to `requirements.txt`. | PyYAML drops comments on write, and the profile YAML's comments are its documentation (and Settings' help text). |

---

## 1. Requirements → where each one lands

| Requirement | Object / mechanism | Screen |
|---|---|---|
| FR1 Connect a DL framework | Framework adapter (§4): sectioned profile YAML, command templates, bridge hooks | Settings |
| FR2 Connect GPU/TPU runtimes | Runtime = local / SSH host / Colab account / Kaggle account (built) + unified `/api/runtimes` | Compute |
| FR3 Connect data to framework + runtime | Dataset registry + per-runtime bindings, placed at `dataset.root` (§5) | Datasets |
| FR4 Create/organize/group/execute/monitor for a Study | Study, draft Experiments, runtime policy, execution modes (§3, §6) | Experiments, Lab |
| FR5 Organize outputs | Deterministic run dir per attempt + canonical collection (§3.5) | Experiment page |
| FR6 Automate runs on appropriate runtimes | Dispatcher (built) + study autopilot + `requires` matching (§6) | Experiments, Compute |
| FR7 Track studies and their experiments | Study record + derived status + membership (§3.2) | Lab, Experiments |
| U1 CRUD experiment configs | `/api/configs` create/duplicate/rename/delete + form/YAML editor | Experiments → Configs |
| U2 Assign/reassign to studies; study CRUD | `/api/studies` + membership actions | Experiments (left pane, bulk bar, drag) |
| U3 Handle individually and in groups | **One action endpoint, three scopes** (§6.1) | Row, selection bar, study header |
| U4 Manual/automatic runtime assignment | `runtime: {mode: auto\|pinned}` per experiment, bulk, and study default | Composer, table cell, bulk bar |
| U5 Manual/automatic execution | Run now / Queue / Study autopilot (§6.2) | Same three scopes |
| U6 View everything about an experiment | Experiment page: Overview, Live, Metrics, Artifacts, Config, History | Experiment page |
| U7 Compare within a study | Study Compare: seed-aggregated table, charts, curves, export | Experiments → Compare |

---

## 2. Verified state (read in the source on 2026-09-25)

### 2.1 Kept as-is

- **Runner layer.** `backend/runners/{base,machine,colab,kaggle,registry}.py`,
  `backend/transport.py` and `backend/hosts.py` form one dispatcher and one slot
  vocabulary. Adding a runtime kind means adding a host record, not new code.
- **Attempt/leg loop** (`experiments.py` `_try_open_next_leg`), with its guards, and the
  snapshot hub design for Kaggle resume.
- **Structured blocked codes**, the notification channels (`backend/notifications.py`),
  the bridge subprocess mechanism (`backend/bridge.py`), and the command palette.
- **The CSS token system** in `static/styles.css`.

### 2.2 Verified defects: Phase 0 fixes these before anything else

| # | Defect | Where | Consequence |
|---|---|---|---|
| X1 | `seed_arg: "--seed {seed}"` takes dissert's single-run bypass. It skips `run_sweep`, so no manifest and no ledger row are written. | `repos/dissert.yaml`; dissert `train.py:511` | `_find_run_id_for()` never matches, so `run_id` is always null. XDash-launched runs never appear in the ledger, and `--max-hours` interruption cannot be seen. |
| X2 | The run-dir join is stale. XDash looks for `outputs/experiments/<name>-s<seed>`, but dissert writes `outputs/experiments/<name>/<hash7>-s<seed>/` (since `52477d1`). | `results_ingest.py:253`, `runners/machine.py:258-260` | `classify_run()` always returns None, so **resume never chains**. Remote `collect()` pulls an empty path. |
| X3 | Machine attempts are never collected. `MachineRunner.poll()` always returns `finished: False`, and the push path (`on_scheduler_item_finished`) resolves without calling `collect()` or `classify_run()`. | `experiments.py:794-809`, `runners/machine.py:222` | SSH/Colab results never reach local disk or the ledger. On Colab, `reap_idle()` then runs `colab stop`, and **the outputs are deleted with the VM**. |
| X4 | No runtime gets its dataset. Rsync excludes `data/`, nothing fetches it, and `ColabRunner.can_accept()` only checks that a Kaggle mapping exists. | `transport.py:41`, `runners/colab.py:153` | Every Colab run fails at data load. An SSH host must be pre-staged by hand, and nothing reports it when it isn't. |
| X5 | The Colab wrapper misuses the CLI. Global `--config`/`-c` are placed after the subcommand (reference §3 says before). The OAuth token at `~/.config/colab-cli/token.json` is shared by every account. `_run_colab` refuses to run until `sessions.json` exists, but only `colab new` (which goes through `_run_colab`) creates it. | `backend/colab.py:186-193`, `:301` | A fresh account can never provision, and multiple accounts are not isolated. |
| X6 | Kaggle runs the GitHub default-branch HEAD (template cell 4 `git clone`). SSH/Colab run the rsynced working tree. Neither records the commit. | `data/dissert/kaggle_worker_template.ipynb` | The same experiment can run different code depending on the runtime, with no record of which. |
| X7 | Experiments cannot exist without executing. `create_experiments()` always appends a pending attempt and calls `_dispatch_tick()`. | `experiments.py:214` | No drafts, no "plan a study, run later", and no manual execution. |
| X8 | Identity ignores arguments. `experiment_id = <name>-s<seed>`, and re-posting an existing id with different `extra_args` silently reuses it **with its old args**. | `experiments.py:93`, `create_experiments()` | A re-run with `--epochs 50` actually runs the old configuration. |
| X9 | The Kaggle run log is discarded: `download_experiment()` extracts only `*.zip`, and `<slug>.log` is deleted with the tmpdir. | `kaggle.py` (per `KAGGLE_API.md` §1) | Failed Kaggle runs cannot be diagnosed from the dashboard. |
| X10 | `list_slots()`/`/api/pulse` report only local + Kaggle. | `experiments.py:1066` | Lab cannot see SSH or Colab capacity. |
| X11 | Every JSON store writes non-atomically (`write_text`). `_load()` returns an empty store on a parse error, and the next `_save()` overwrites the file. | `experiments.py:75-89`; same pattern in `colab.py`, `hosts.py`, `scheduler.py`, `terminals.py`, `kaggle.py`, `dataset_map.py`, `monitors.py`, `notifications.py`, `repos.py`, `run_notes.py`, `snapshot.py`, `results_ingest.py` | A crash mid-write **wipes every experiment**. |
| X12 | Shared `extra_args` go to both train and eval, and `eval.py` uses strict `parse_args()`. | `runners/machine.py:215`, template cell 16; dissert `eval.py:186` | Any train-only flag (`--epochs`, `--lr`, the Composer's own placeholder) makes eval exit 2. Even a tolerated flag changes `config_hash`, so eval looks in the wrong run dir. |
| — | `/api/runners/<id>/launch` still exists. It is a fourth launch path that bypasses the dispatcher. | `server.py:696` | Retire it. |

**Nothing has run end-to-end.** `data/dissert/experiments.json` holds one experiment with
two attempts, both failed. Every runtime kind is "structurally verified" only, so the
Phase 0 acceptance is live, on real runtimes.

---

## 3. The object model

```
 Study ◆──*── Membership ──*──◆ Experiment ──1──▶ Config (YAML file)
 (question,        (group label)   (config + seed      │ composes
  metric,                           + overlay)         ▼
  defaults,                           │ 1..*        Dataset ──*── Binding ──1── Runtime
  autopilot)                          ▼                           (path|push|fetch|attach)
                                   Attempt/leg ──*──1──▶ Runtime (local | ssh | colab | kaggle)
                                      │ 1..*
                                      ▼
                                     Run (framework manifest + ledger row; XDash only reads it)
```

### 3.1 Config: a YAML file in the framework's `configs_dir`

Configs stay files. XDash never copies them into its own store. The profile declares
which globs are **launchable** (`experiment/**/*.yaml`) and which are **fragments**
(dataset/model/training). The tree shows them in two groups, and only launchable configs
get a Run affordance. CRUD covers create (blank, from a template, or duplicate), edit
(form or raw YAML, validated through the `resolve_config` hook), rename, and delete.
Deleting a config that experiments reference asks for confirmation and lists them. Those
experiments keep their history, and their Experiment pages show "config file missing".

### 3.2 Study

```jsonc
{
  "study_id": "st_7f3a2c",                  // immutable
  "name": "MK-UNet size ablation on ClinicDB",
  "question": "Does model size matter below 1M params?",
  "description": "markdown",
  "tags": ["ablation", "clinicdb"],
  "primary_metric": {"key": "dice", "direction": "max"},
  "baseline": {"config_path": "experiment/mkunet/mkunet_clinicdb.yaml"},  // deltas in Compare
  "defaults": {                              // pre-fills the Composer; never overrides an experiment
    "seeds": [7, 42, 1337], "overlay": {}, "runtime": {"mode": "auto", "allow": ["*"]}
  },
  "autopilot": {"enabled": false, "max_parallel": 2},
  "priority": 0,
  "archived": false,
  "created_at": "…", "updated_at": "…"
}
```

Status is **derived, never stored** (this rule carries over from the batch model):
`planning` (all drafts), `running`, `attention` (any blocked or failed), `complete`,
`archived`. **Batch is retired into Study.** `batch_name` migrates to a membership, and
batch `paused` migrates to "autopilot off plus hold queued".

### 3.3 Experiment

```jsonc
{
  "experiment_id": "mkunet_t_clinicdb-s42",   // immutable; §3.3.1
  "name": "mkunet_t_clinicdb-s42",             // display, editable
  "config_path": "experiment/mkunet/mkunet_t_clinicdb.yaml",
  "seed": 42,
  "overlay": {"training.epochs": 200},         // dotted keys → overlay YAML (§4.3); replaces extra_args
  "extra_args": {"train": "", "eval": ""},     // escape hatch for non-config flags only
  "studies": [{"study_id": "st_7f3a2c", "group": "tiny"}],
  "runtime": {"mode": "auto", "allow": ["*"], "requires": {"min_vram_gb": null}},
                                               // or {"mode": "pinned", "slot": "ssh:mclab-gpu2"}
  "priority": 0, "max_retries": 1, "max_legs": 6,
  "notes": "",
  "created_at": "…", "attempt_ids": [], "current_attempt_id": null
}
```

**3.3.1 Identity.** `<experiment_name>-s<seed>` when the overlay is empty, which keeps
every existing id valid. With a non-empty overlay it is
`<experiment_name>-s<seed>-<sha1(canonical overlay)[:6]>`. Two different overlays can
never share a record, which closes X8. Creating an id that already exists is still
idempotent, and the API response says it matched an existing experiment instead of
creating one.

**3.3.2 Lifecycle.** Status is derived: no attempt → **draft**. Otherwise it is the current
attempt's status: `queued` (renamed from `pending`), `blocked`, `dispatching`, `running`,
`done`, `failed`, `cancelled`.

| State | Editable |
|---|---|
| draft | everything |
| queued / blocked | runtime policy, priority, studies, notes (dequeue to edit overlay/seed) |
| dispatching / running | studies, notes |
| terminal | runtime policy, priority, studies, notes. Overlay and seed are frozen because they are identity; use **Duplicate & edit** instead. |

### 3.4 Attempt: new fields only

```jsonc
"code":  {"commit": "a1b2c3d", "dirty": false, "pushed": true},   // X6
"data":  {"dataset": "clinicdb", "mode": "fetch", "source": "kaggle:syfur007/clinicdb-images"},
"run":   {"config_hash": "f76c81e…", "run_dir": "outputs/experiments/mkunet_t_clinicdb/f76c81e-s42",
          "run_ids": ["R-f76c81e-s42-f-"]},                      // planned at dispatch (§4.4)
"config_sha": "…",                                               // drift detection
"accelerator": "A6000",
"log_ref": "data/dissert/logs/<attempt_id>/"                     // persisted console logs
```

### 3.5 Run and output organization (FR5)

XDash never writes a run. It knows each attempt's **planned run dir** before launch
(§4.4), so every reader can look in exactly one place:

- **local:** the framework writes straight into the canonical tree.
- **ssh / colab / kaggle:** after completion, `collect()` pulls into staging, then
  **canonicalizes**: it copies `run_dir` into the local `experiments_dir` and registers the
  ledger row with `manifest_path` rewritten to the canonical path. Staging is deleted.
  If the destination exists with a different `run_meta.created_at`, the new copy goes to
  `<run_dir>.<attempt_id>` and the attempt gets an `output-conflict` flag. That case is
  rare, because the path is hash-scoped.
- **Console logs** (tmux pane, Kaggle `<slug>.log`) are persisted per attempt under
  `data/<profile>/logs/<attempt_id>/`, so deleting a tmux session never loses output.

### 3.6 Runtime: one shape for every kind (closes X10)

```jsonc
{ "id": "ssh:mclab-gpu2", "kind": "ssh", "label": "MCLab A6000",
  "state": "online",                      // online|offline|provisioning|idle|busy|unconfigured
  "accelerator": {"name": "RTX A6000", "vram_gb": 48},   // probed; Colab: as granted
  "capacity": {"used": 1, "limit": 2},
  "quota": null,                          // Kaggle: {used, limit, unit:"h/week", resets_at, source:"measured"|"self-tracked"}
                                          // Colab:  {balance, burn_per_h, unit:"CU", source:"measured"} via `colab usage`
  "capabilities": {"live_log": true, "stop": true, "volatile": false, "live_checkpoints": true},
  "running": ["mkunet_t_clinicdb-s42"], "queued_for": ["…"], "health": {"last_ok": "…", "error": null} }
```

### 3.7 Dataset and bindings: see §5.

### 3.8 Storage

- `data/<profile>/experiments.json` gains `studies` and drops `batches` (migrated).
- `data/<profile>/datasets.json` replaces `dataset_map.json` (migrated).
- A new **`backend/store.py`** holds one `JsonStore` used by all 14 stores: atomic write
  (tmp file + `fsync` + `os.replace`), a `.bak` of the last good copy, and on a parse
  error it **raises instead of returning empty** (closes X11).
- The migration is small. Today there is one experiment, zero batches, and one dataset
  mapping. It runs once on load and writes a `schema_version`.

---

## 4. The framework adapter (FR1)

XDash needs eight things from a framework. Each has a profile key. A missing optional hook
degrades one feature and never breaks the dashboard.

| Capability | Profile key | Used for | If missing |
|---|---|---|---|
| Config discovery | `framework.configs_dir`, `launchable`, `fragments` | Config tree, CRUD | required |
| Launch commands | `commands.train`, `commands.eval` (templates) | Every runtime and the Kaggle notebook | required |
| Config resolution | `hooks.resolve_config` | Validation, the resolved view, dataset root, overlay check | Raw YAML only; no validation |
| Config schema | `hooks.config_schema` | Form editor, overlay key autocomplete | Generic type-inferred form |
| Run location | `hooks.locate_run` | Planned run dir and run ids at dispatch (§4.4) | Parse `outputs.run_id_pattern` from the log after the run |
| Run status | `hooks.describe_run` | Done vs interrupted (resume), epochs, checkpoint files | No resume; trust exit code |
| Metrics | `metrics.primary`, `metrics.lower_is_better`, `metrics.report_glob` | Compare, Lab "best so far" | Compare uses whatever keys the reports contain |
| Dataset identity | `framework.dataset_name_key`, `dataset_root_key` | §5 placement | Datasets tab lists fragments only |

### 4.1 Profile YAML, restructured into sections

Flat keys are still read, so both profiles keep working until they are migrated.
`config.py` gains one `_get(raw, "commands.train", legacy="train_script")` helper, and a
one-shot script rewrites both profiles in place, preserving comments via ruamel.

```yaml
display_name: dissert

framework:
  repo_root: ../../dissert
  configs_dir: configs
  launchable: ["experiment/**/*.yaml"]
  fragments: {dataset: "dataset/*.yaml", model: "model/**/*.yaml", training: "training/*.yaml"}
  experiment_name_key: logging.experiment_name
  dataset_name_key: dataset.name
  dataset_root_key: dataset.root

commands:                      # placeholders: {python} {config} {seed} {budget} {resume}
  python: python
  env_activate: conda activate thesis
  train: "{python} train.py --config {config} --seeds {seed} --repeats 1 {budget} {resume} {extra}"
  eval:  "{python} eval.py --config {config} --seeds {seed} --repeats 1 {extra}"
  budget: "--max-hours {hours}"
  resume: "--resume"

outputs:
  layout: experiments
  experiments_dir: outputs/experiments
  ledger_dir: outputs/ledger
  run_id_pattern: 'run_id=(\S+)'

hooks:                          # module:function in the host repo, run by bridge_scripts/call.py
  resolve_config: utils.config:load_config
  config_schema: orchestration.schema:json_schema     # verify name in dissert
  locate_run: xdash:dissert.locate_run                # XDash-shipped adapter fn (§4.4)
  describe_run: orchestration.status:describe_run

metrics:
  primary: dice
  lower_is_better: [hd95, asd, nsd, ece, mean_ms, median_ms, p95_ms]   # moves out of app.js:6

runtimes: {kaggle: {…}, colab: {…}, scheduler: {…}}
server: {host: 127.0.0.1, port: 6070, api_token: "", poll_interval_ms: 2000}
tmux: {…}
tensorboard: {…}
```

`--seeds {seed} --repeats 1` fixes X1. It goes through `run_sweep` for exactly one seed with
no repeat axis, which produces a manifest, a ledger row, and the legacy
`<hash7>-s<seed>/` dir.

### 4.2 One generic bridge entry point

`bridge_scripts/call.py <module:function> <json-args>` replaces per-feature scripts for
framework hooks. `xdash:` prefixed hooks resolve to XDash-shipped adapter functions in
`bridge_scripts/adapters/<framework>.py`. Those import host modules but live in XDash,
which keeps the rule that the host repo needs zero XDash-aware code.

### 4.3 Overrides are an overlay config (fixes X12)

When `overlay` is non-empty, dispatch writes `<runtime repo_root>/.xdash/overlays/<experiment_id>.yaml`:

```yaml
compose: ["../../configs/experiment/mkunet/mkunet_t_clinicdb.yaml"]   # repo-relative → portable
training:
  epochs: 200
```

Both train and eval get `--config .xdash/overlays/<id>.yaml`. They resolve an identical
config, so they compute an identical `config_hash`, and eval finds train's run. The file
is written with the local file API, over the transport for SSH/Colab, or embedded as
`OVERLAY_YAML` in the Kaggle LAUNCH_SPEC and written by the template. The one ask of the
host repo is a single `.gitignore` line for `.xdash/`. The Composer validates an overlay
through `resolve_config` before saving, so a typo'd key fails at composition time, not at
hour three.

### 4.4 Knowing the run dir before launch (fixes X2)

dissert's run dir is deterministic:
`experiments_dir/<experiment_name>/<config_hash[:7]>-s<seed>/`, with run id
`R-<hash7>-s<seed>-f<fold|->` (`orchestration/runid.py`). The dissert adapter's
`locate_run(config, seed)` runs `load_config` → `config_hash` → `experiment_paths`, and
XDash stores the result on the attempt at dispatch. Three things read it:

- **Classification and resume:** `classify_run()` reads `attempt.run.run_dir`. It never
  reconstructs the path.
- **Remote collect:** `collect()` pulls exactly `run_dir` plus the ledger.
- **Live views:** the Experiment page reads TensorBoard events, plots and eval reports from
  `run_dir` while the run is still going (local, and remote after heartbeat pulls).

The `run_id=` lines dissert prints are parsed from the log as a cross-check. A mismatch
flags `run-mismatch` instead of guessing.

### 4.5 The Kaggle template consumes rendered commands

LAUNCH_SPEC carries `TRAIN_CMD`/`EVAL_CMD` (already rendered from `commands.*` with
`{python}` → the venv interpreter), plus `OVERLAY_YAML` and `CODE_COMMIT`. The template
stops rebuilding commands itself, so all four runtime kinds run the exact same command
line.

**Code provenance (fixes X6).** The template runs `git checkout <CODE_COMMIT>` after the
clone. At dispatch, XDash records `{commit, dirty, pushed}` on every attempt. On Kaggle a
dirty tree or an unpushed commit blocks with **`code-not-pushed`**, whose action is
"push, then retry". A profile flag `allow_unpushed: true` downgrades this to a warning. A
Kaggle error containing `No user secrets exist` maps to **`kaggle-secret-missing`**, with
a deep link to that account's kernel page for the one-time manual secret attach.

---

## 5. Datasets per runtime (FR3; fixes X4)

### 5.1 Principle: put the data where the config already looks

Every runtime makes `<repo_root>/<dataset.root>` resolve to real data, usually through a
symlink. Nothing passes `--dataset_dir`. `dataset.root` is part of dissert's
`config_hash`, and `orchestration/runid.py` does not strip it, so changing it per runtime
would split one experiment into several identities and break resume across runtimes. The
Kaggle template already works this way (cell 11), and this generalizes it.

### 5.2 Registry: `data/<profile>/datasets.json`

```jsonc
{"clinicdb": {
   "name": "ClinicDB", "fragment": "dataset/clinicdb.yaml",
   "root": "data/polyp/ClinicDB",                 // read from the fragment, never typed
   "sources": {"kaggle": {"slug": "syfur007/clinicdb-images", "subpath": "ClinicDB"}},
   "bindings": {
     "local":           {"mode": "path"},                          // default: repo_root/root
     "ssh:mclab-gpu2":  {"mode": "path", "path": "/data/shared/ClinicDB"},
     "ssh:*":           {"mode": "push"},                          // rsync local copy once, incremental after
     "colab:*":         {"mode": "fetch", "source": "kaggle"},     // kagglehub on the VM
     "kaggle:*":        {"mode": "attach", "source": "kaggle"}     // dataset_sources + symlink (built)
   },
   "checks": {"ssh:mclab-gpu2": {"ok": true, "at": "…", "detail": "612 files"}}
}}
```

**Resolution order:** exact runtime id → `kind:*` → the kind default (local: `path`;
ssh: `path`; colab: `fetch` from Kaggle if a source exists; kaggle: `attach`).

| Mode | What dispatch does before launch |
|---|---|
| `path` | `test -d <path>` over the transport, then `ln -sfn <path> <repo_root>/<root>` when the path differs from the default |
| `push` | `transport.push(local <root>, host cache)` + symlink. Rsync makes repeats cheap on persistent hosts. |
| `fetch` | Download on the runtime from the source into a host cache, then symlink. Kaggle credentials come from one account marked as the **data account**, as env for that step only. |
| `attach` | Kaggle only: `dataset_sources` + the template's existing symlink cell |

`can_accept()` on every runner calls the resolver. An unresolvable binding blocks with
**`no-dataset-binding`** (the old `no-dataset-mapping` stays as an alias), with an action
that deep-links to that dataset × runtime cell. Precedence carries over: a config's own
`dataset.kaggle_dataset` still wins for the Kaggle source.

**Later in Phase 4, optional:** "Publish local copy to Kaggle (private)" runs
`kaggle datasets create`, so a dataset that exists only locally becomes available to
Kaggle and Colab in one click.

---

## 6. Dispatch and execution semantics

### 6.1 One action endpoint, three scopes (U3)

`POST /api/experiments/actions {action, ids | study_id | filter, params}` takes these
actions: `run_now`, `queue`, `dequeue`, `cancel`, `retry`, `delete`, `set_runtime`,
`set_priority`, `add_to_study`, `move_to_study`, `remove_from_study`, `duplicate`. The
row button, the selection bar and the study header all call it. The only difference is
scope, so group handling cannot drift from individual handling. Every response is
per-id: `{ok: [...], skipped: [{id, reason}]}`.

### 6.2 Execution modes (U5)

- **Queue (automatic).** Creates an attempt in `queued`. The dispatcher places and starts
  it whenever an allowed runtime can accept it. This is today's behavior.
- **Run now (manual).** Runs preflight for this experiment against its pinned runtime, or
  the best auto candidate. If something can accept it now, XDash claims and dispatches it
  synchronously, ahead of the queue. If not, it returns the reason per runtime and the
  dialog offers "Queue instead". It never queues silently.
- **Study autopilot (automatic, continuous).** While it is on, the study's drafts are
  promoted to `queued` in priority order, and never more than `max_parallel` members are
  in flight. Drafts added to the study later are picked up too. Turning it off stops
  promotion but leaves already-queued experiments alone ("Hold" also dequeues them).

### 6.3 Runtime assignment (U4)

- **Auto:** `allow` is a list of kinds or slot ids (today's `pool`, already normalized on
  read) plus optional `requires.min_vram_gb`, matched against `runtime.accelerator.vram_gb`.
  The dispatcher picks the best runtime using the existing `dispatch_priority`.
- **Pinned:** exactly one slot. If that runtime cannot accept, the experiment blocks with
  that runtime's reason. It never falls back.
- **Reassign:** allowed while draft, queued or blocked. For running experiments the UI
  offers "Cancel and requeue on…", with an explicit confirmation that states what is lost.
  Resumable progress is kept, because the next leg resumes from the canonical checkpoint.

### 6.4 Ordering

The queue order is `(-manual, -study.priority, -experiment.priority, feasibility_kind_count, -est_hours, created_at)`,
which is the existing greedy with priority in front.

### 6.5 Preflight: one call powers every "can this run?" answer

`POST /api/experiments/preflight {ids | specs}` returns an experiment × runtime matrix.
Each cell shows `ok` or the block code and detail, the estimate (with its tier), and the
data mode that would be used. The Composer, the Run-now dialog and the Experiment page
all render this one response.

### 6.6 One completion path for every kind (fixes X3)

`on_scheduler_item_finished` stops resolving directly. It calls the same
`_poll_and_resolve` path as Kaggle: `collect()` → canonicalize (§3.5) → `classify_run()`
→ resolve or open the next leg. For Colab, `reap_idle()` must not stop a VM that has an
uncollected attempt. That becomes a hard guard, verified by test.

---

## 7. API surface (final)

| Area | Routes |
|---|---|
| Configs | `GET /api/configs` (tree: launchable, fragments) · `GET/PUT /api/configs/<path>` · `POST /api/configs` (create: blank, template or duplicate) · `POST /api/configs/<path>/rename` · `DELETE /api/configs/<path>` · `GET /api/configs/<path>/resolved` · `GET /api/configs/schema` |
| Studies | `GET/POST /api/studies` · `GET/PATCH/DELETE /api/studies/<id>` (delete: memberships only; experiments untouched) · `GET /api/studies/<id>/compare?metrics=&group_by=config` · `POST /api/studies/<id>/autopilot` |
| Experiments | `GET /api/experiments?study=&status=&runtime=&config=&q=` · `POST /api/experiments` (creates **drafts**; `then: "queue"\|"run_now"` optional) · `GET/PATCH /api/experiments/<id>` · `POST /api/experiments/actions` · `POST /api/experiments/preflight` |
| Experiment detail | `GET …/<id>/log?attempt=&stage=&since=` (cursor-paged; `live: bool`) · `GET …/<id>/metrics` (series from log parse and TB events) · `GET …/<id>/artifacts` (run_dir tree) · `GET …/<id>/artifacts/<path>` · `GET …/<id>/report` |
| Runtimes | `GET /api/runtimes` (§3.6, replaces `/api/runners` + `/api/slots`) · `GET /api/runtimes/<id>` (+ history) · existing host/Kaggle/Colab account CRUD kept · `POST /api/colab/accounts/<n>/connect` + `…/connect/code` (OAuth copy-paste flow) |
| Datasets | `GET /api/datasets` · `PUT /api/datasets/<name>` · `PUT /api/datasets/<name>/bindings/<runtime>` · `POST /api/datasets/<name>/check?runtime=` · existing channel-preview/audit kept |
| Settings | `GET /api/profile` (YAML text + parsed tree + per-key comments + hints + mtime) · `PATCH /api/profile` (dotted-path patch, comment-preserving, validated by a dry-run load, then `settings.reload()`) · `PUT /api/profile/raw` · `GET /api/system` (tools, versions, bridge/hook health) · notifications kept |
| Lab | `GET /api/pulse` (adds studies summary and all runtimes) |
| **Retired** | `/api/runners/<id>/launch` · `/api/batches*` · `/api/slots` · `/api/runs*`, `/api/reports*`, `/api/history/*` (folded into experiment/study routes once the UI has moved) · `/api/terminals` POST (raw ad-hoc launches; every run is an experiment) |

Live data uses **polling with cursors**, not SSE. The visible screen polls every 2–5 s,
and polling pauses when the tab is hidden. That is enough for one user, and there is no
connection state to recover from.

---

## 8. Screens

**Shell.** The sidebar holds Lab · Experiments · Compute · Datasets · Settings. The
Experiment page is a route, not a nav item. A hash router makes every view addressable:
`#/lab`, `#/experiments?study=st_7f3a2c&tab=compare`, `#/x/<experiment_id>/live`,
`#/compute/<runtime_id>`, `#/datasets/<name>`, `#/settings/<section>`. The topbar holds the
profile switcher, a running-count pill, and ⌘K search, which indexes studies,
experiments, configs, runtimes and datasets. **Rule: every experiment name rendered
anywhere is a link to its page**, through one `experimentLink()` helper.

### 8.1 Lab: overview in tiles and lists

Each section has a **▦/☰ toggle** (tiles or list), remembered per section in localStorage.

```
┌ Lab ──────────────────────────────────────────────────────────────────────┐
│ ⚠ NEEDS ATTENTION 2                                                        │
│  mamba_s_busi-s7    blocked · no data binding on colab:*       [Bind data] │
│  gmkunet_l-s42      failed · CUDA OOM (attempt 2/2)          [Open] [Retry]│
├ STUDIES ──────────────────────────────────────────────────────── ▦ ☰ ──────┤
│ ┌ Size ablation ──────────┐ ┌ Dataset transfer ───────┐ ┌───────────────┐  │
│ │ ███████░░░ 11/15        │ │ ██░░░░░░░░ 2/12         │ │  + New study  │  │
│ │ ●2 running ⚠1 ○1 draft  │ │ ●1 running · autopilot  │ │               │  │
│ │ best dice .871 mk_t s42 │ │ best dice .702          │ │               │  │
│ │ ETA ~Wed 14:00          │ │ ETA ~Fri                │ │               │  │
│ └─────────────────────────┘ └─────────────────────────┘ └───────────────┘  │
├ RUNTIMES ─────────────────────────────────────────────────────── ▦ ☰ ──────┤
│ This machine   mclab-gpu2    kaggle:tanvir        colab:emon               │
│ RTX 3060 12G   A6000 48G     T4×2 · 6.2/30h wk    idle · 81 CU             │
│ ▮▮▯▯ 2/4       ▮▯ 1/2        ▮ 1/1                ▯ 0/1                    │
├ RUNNING NOW 4 ─────────────────────────────────────────────────── ▦ ☰ ─────┤
│ mkunet_t_clinicdb-s42 · Size ablation · ssh:mclab-gpu2 · leg 1             │
│ train ███████░░ ep 142/200 · val_dice .871 ▁▃▅▇ · 4h12m · ~1h48m left      │
├ RECENT ───────────────────────────────────────────────────────── ▦ ☰ ──────┤
│ ✓ mkunet_s_clinicdb-s7   dice .869 (+.004 vs baseline)   12m   Size abl.   │
│ ✗ mamba_t_clinicdb-s7    train exit 1                     41m   Transfer   │
└───────────────────────────────────────────────────────────────────────────┘
```

Needs-attention comes first, because the most valuable thing Lab can show is a stall and
its fix. Everything is backed by one `/api/pulse`.

### 8.2 Experiments: configs, studies, and every experiment

```
┌ Experiments ───────────────────────────────────────────────────────────────┐
│ STUDIES        │ [Experiments] [Configs] [Compare]              [+ New ▾]  │
│ ⌕ filter       │ Size ablation — "Does size matter below 1M params?"       │
│ ▸ All       27 │ dice ↑ · seeds 7,42,1337 · runtime auto(any) · autopilot ◯ │
│ ▸ Unfiled    3 │ 11/15 done · ●2 · ⚠1           [Queue drafts] [Edit] [⋯]   │
│ ● Size abl. 15 │ Draft 1 · Queued 1 · Running 2 · Blocked 1 · Done 11 · All │
│ ● Transfer  12 │ ┌─┬─────────────────────┬────┬───────────────┬────────┬──────┐│
│ ◌ Archived   2 │ │☐│ Experiment          │Seed│ Runtime       │ Status │ dice ││
│                │ │☐│ ▾ mkunet_t_clinicdb │    │               │ 2/3 ✓  │.870± ││
│ [+ New study]  │ │☐│   …-s7              │ 7  │ auto → mclab  │ ✓ done │ .869 ││
│                │ │☐│   …-s42             │ 42 │ 📌 ssh:mclab  │ ● ep142│ .871 ││
│                │ │☐│ ▾ mkunet_l_clinicdb │    │               │        │      ││
│                │ │☐│   …-s7              │ 7  │ auto (any)    │ ⚠ data │  —   ││
│                │ └─┴─────────────────────┴────┴───────────────┴────────┴──────┘│
│                │ ▣ 3 selected [Run now] [Queue] [Runtime ▾] [Study ▾]         │
│                │              [Compare] [Cancel] [Delete]                     │
└────────────────────────────────────────────────────────────────────────────┘
```

- **Left pane: studies.** CRUD on studies. Drag rows onto a study to add them (Shift-drag
  moves them). "All" and "Unfiled" are virtual studies.
- **Experiments tab.** Rows are grouped by config, and each group row shows seed-aggregated
  status and metric. Status chips filter the list. The Runtime cell is an inline
  dropdown: Auto (any), Auto within…, or Pin to…. It shows "auto → mclab" once placed.
  The row actions are Run ▸, Queue, and ⋯.
- **Configs tab.** The launchable config tree plus a fragments group; a CodeMirror editor
  with a **Form ↔ YAML** toggle (the form comes from `config_schema`, or is type-inferred);
  **Resolved** view; New / Duplicate / Rename / Delete; and "Create experiments from this
  config →", which opens the Composer pre-filled.
- **Compare tab** (U7). Rows are configs by default: mean ± std over seeds, n, and a delta
  vs the study baseline. Columns are metrics with the primary first, direction-aware best
  highlighting, and repeat/fold info in tooltips. Below the table: bar ± std, per-seed
  strip plot, radar (existing), overlaid training curves from TB events, and the existing
  seed × fold heatmap. Export as CSV, Markdown or LaTeX. Any selection from any study can
  be compared ad hoc from the bulk bar.
- **The Composer** ("+ New ▾ → Experiments") evolves the Run Composer: configs × seeds ×
  overlay (key/value rows with autocomplete). It previews the resulting experiment ids,
  including which already exist. The rest of the form is target study + group, runtime
  policy, then **Save as drafts / Queue / Run now**, with the preflight matrix inline.

### 8.3 Experiment page (`#/x/<id>`)

```
┌ ← mkunet_t_clinicdb-s42   ● running · leg 1/6 · ssh:mclab-gpu2 (A6000)        ┐
│ Size ablation · group tiny · config experiment/mkunet/mkunet_t_clinicdb.yaml   │
│ overlay training.epochs=200 · commit a1b2c3d ✓ pushed · data clinicdb (path)   │
│              [Cancel] [Reassign ▾] [Duplicate & edit] [TensorBoard] [⋯]        │
├ Overview │ Live │ Metrics │ Artifacts │ Config │ History │ Notes ──────────────┤
│ train ███████░░ ep 142/200   val_dice .871 ▁▃▅▇   4h12m elapsed · ~1h48m left  │
│ ┌ Live console (follow ⏵) ────────────────────────── stage: train ▾ ────────┐  │
│ │ 2026-09-25 14:02:11 | INFO | epoch 142 | loss .082 | val_dice .8710        │  │
│ │ …                                                                          │  │
│ └───────────────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────────┘
```

| Tab | Content | Source |
|---|---|---|
| Overview | Status, progress, key metrics vs study best/baseline, attempt/leg timeline (runtime, duration, outcome), code commit, data binding used | experiment + attempts + report |
| Live | Console with follow, stage selector, and parsed progress. Local/SSH/Colab: tmux capture (live). Kaggle: status + elapsed; live log once CLI 2.x is in (Phase 5), otherwise the full log after completion (X9 fix). | `/log?since=` |
| Metrics | Training curves (TB events, log-parse fallback while running) and the eval report metric table | `/metrics`, `/report` |
| Artifacts | `run_dir` tree: plots gallery (overlays, curves), reports (rendered `report.md`), checkpoints with sizes and download, framework logs | `/artifacts` |
| Config | The resolved config as run, the overlay, and a **drift diff** vs the current file when `config_sha` changed | attempt + `/resolved` |
| History | Every attempt and leg: runtime, raw status, block codes, durations, run ids | attempts |
| Notes | Free text (the existing `run_notes` moves here) | experiment |

### 8.4 Compute: runtimes

The board at the top shows every runtime as a tile or list row with state, accelerator,
capacity, quota bar and current job, plus **+ Add runtime**. Clicking one opens
`#/compute/<id>`:

- **Now:** running attempts, with a link to each Experiment page.
- **Queue:** experiments pinned here, then the auto-eligible ones in dispatch order
  ("likely next").
- **History:** attempts that ran here, with outcome, hours, and a success-rate sparkline.
- **Settings:**
  - *SSH:* host, user, key, `repo_root` per profile, env activate, max concurrent.
  - *Kaggle:* credentials, weekly budget, and a checklist including the one-time
    GITHUB_TOKEN secret with a link to the kernel.
  - *Colab:* tier / session limit, GPU preference, and "data account" for fetch.
- **Diagnostics:** Test connection, GPU probe (fills `accelerator`), machine-stats monitors
  (existing), raw tmux sessions on that host (the old Sessions view, as a debug tool),
  and TensorBoard.

**The Add runtime wizard** starts with a kind picker, then shows that kind's form, then
runs a live test, which must pass before the runtime is saved as `online`.
*Colab → Connect account* drives the CLI's copy-paste OAuth: XDash starts the login
subprocess with a per-account `HOME`, shows the sign-in URL, and you paste the code back.

### 8.5 Datasets

```
┌ Datasets ───────────────────────────────────────────────────────────────────┐
│            │ This machine  │ ssh:mclab-gpu2 │ colab:*       │ kaggle:*       │
│ ClinicDB   │ ✓ path        │ ✓ path         │ ↓ fetch kaggle│ ⊕ attach       │
│ ColonDB    │ ✓ path        │ ⇡ push         │ ✗ no source   │ ✗ no slug      │
│ BUSI       │ ✗ missing     │ ✗ —            │ ✗ —           │ ✗ —            │
│ ISIC18     │ ✗ missing     │ ✗ —            │ ✗ —           │ ✗ —            │
└─────────────────────────────────────────────────────────────────────────────┘
```

Clicking a cell edits that binding (mode, path, source) and **Check** runs it now.
Clicking a row opens the dataset page:

- the fragment (name, root, split) and its sources (Kaggle slug, with status and file count)
- "Publish local copy to Kaggle"
- which configs and studies use it
- channel preview and test-eval audit (existing)

### 8.6 Settings: dynamic YAML with read-only system info

```
┌ Settings ─────────────────────────────────────────────────────────────────┐
│ PROFILE [dissert ▾] [+ New profile]   repos/dissert.yaml · saved 2m ago   │
│ ┌──────────────┐ ┌ commands ───────────────────────────────── [Form|YAML]┐ │
│ │ framework    │ │ train  [{python} train.py --config {config} --seeds…]  │ │
│ │ commands   • │ │   # Format for the train command; placeholders …       │ │
│ │ outputs      │ │ env_activate [conda activate thesis        ]           │ │
│ │ hooks        │ │ + add key                                              │ │
│ │ metrics      │ └──────────────────────────────── [Revert] [Save] ───────┘ │
│ │ runtimes     │ ┌ System (read-only) ───────────────────────────────────┐  │
│ │ server  ⟳    │ │ bridge ✓ thesis py3.10 · hooks 4/4 importable         │  │
│ ├──────────────┤ │ tmux 3.4 ✓ rsync ✓ ssh ✓ · kaggle 1.7.4.5 ⚠ old · colab ✗│ │
│ │ Alerts       │ │ repo_root ✓ · configs 23 · outputs 1.2 GB · XDash a1b2c3 │ │
│ │ About        │ └────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────────┘
```

**The form is dynamic in two ways:**

1. **Shape.** The form is generated from the YAML itself. Mappings become sections,
   scalars become widgets by value type (bool → toggle, number → numeric, `*_dir`/`*_path`/
   `root` → path picker with an ✓ exists check, lists → chip editor), and the comments
   above a key become its help text.
   `backend/profile_hints.py` adds type, enum, and `restart_required` for known keys. It is
   optional, so a key XDash has never seen still renders and saves.
2. **Change on disk.** The page polls `mtime`. An outside edit reloads the form, or warns
   if you have unsaved changes.

**Saving** is a dotted-path patch applied with ruamel, so comments and order are kept. It is
validated by constructing a throwaway `Settings` from the patched text, then written
atomically, then applied with `settings.reload()`. Keys that need a restart
(`server.*`) are marked ⟳. **Raw YAML** is always one click away.

**Other sections:**

- **Alerts:** the notification channels (existing).
- **About:** versions, paths, and data-dir sizes.
- **+ New profile:** a wizard that takes the repo root and detects `configs/`, `train.py`
  and the output layout.

---

## 9. Frontend architecture

- **Buildless ES modules.** New screens go in `static/js/screens/*.js` and shared code in
  `static/js/lib/*.js` (`api`, `router`, `poller`, `store`), with components in
  `static/js/components/*.js` (badge, data-table with selection, tabs, tile/list toggle,
  sparkline, log viewer, YAML form, preflight matrix). `index.html` ends up loading a
  single `main.js`. While migrating, legacy globals are bridged with `window.x =` shims,
  so old and new code coexist.
- **Render pattern (kept).** `render(state)` is pure, with no DOM reads, and uses one
  delegated `data-action` listener per screen.
- **One poller.** Only the visible screen polls, polling pauses on `visibilitychange`, and
  it backs off on errors.
- **The `app.js` monolith (2,766 lines) is taken apart by screen** as each one is
  rebuilt. It is deleted in Phase 6.
- **Verification.** There is no browser automation here: no Node, no Playwright, and
  Firefox is only a snap stub. UI acceptance is therefore **you clicking through**, at the
  end of each UI phase, against a written checklist. Backend acceptance is automated
  (Phase 0 adds the harness).

---

## 10. Phases

Each phase can ship on its own and ends with acceptance checks. Anything that reaches
outside this machine (a Kaggle push, an rsync to a host, a Colab VM that burns compute
units) runs **only after you say go**.

### Phase 0: make what exists correct (backend only)
1. `backend/store.py` (atomic, fail-loud) replaces all 14 stores' load/save. **X11**
2. `seed_arg` → `--seeds {seed} --repeats 1` in `repos/dissert.yaml`. **X1**
3. The dissert adapter `locate_run` stores `run.run_dir`/`run_ids` on the attempt at
   dispatch, and `classify_run`/`collect` read it. **X2**
4. The machine completion path goes through `collect` → canonicalize → classify
   (§6.6), and Colab `reap_idle` refuses to stop a VM with an uncollected attempt. **X3**
5. Split `extra_args` into train and eval; a stopgap until Phase 1's overlay. **X12**
6. Colab wrapper rewrite against `colab-cli-reference.md`:
   - global options go before the command
   - per-account `HOME` for `token.json` (verify XDG support)
   - `sessions.json` is session state, not a credential gate
   - validate `--gpu` against `T4|L4|G4|H100|A100` (an unknown value silently becomes A100)
   - absolute `colab` path in the ProxyCommand; require an ed25519 key
   - parse the real `status`/`sessions` text output
   - `colab usage` for compute-unit balance
   **X5**
7. Kaggle: keep `<slug>.log`, record the commit, pin it in the template, add the
   `code-not-pushed` and `kaggle-secret-missing` codes. **X6, X9**
8. Retire `/api/runners/<id>/launch`.
9. **A permanent test harness:** `tests/` with pytest in the `xdash` env, the Flask test
   client, fake runners and fake transports. This turns the ad-hoc isolated tests from
   earlier phases into a suite that stays.
10. Add SUPERSEDED banners to the older plans' affected sections.

**Acceptance:** the suite is green. With your go-ahead, one real experiment on each
runtime you have (local, then SSH / Colab / Kaggle as available) reaches `done` with a
`run_id`, a ledger row, and its run dir in the canonical tree. One over-budget run
self-limits, classifies as `interrupted`, and **chains a second leg**. `kill -9` of the
store mid-write loses nothing.

### Phase 1: the model (Study, drafts, policies)
Study CRUD and membership; experiment drafts and identity v2 with overlay (the overlay
file replaces the Phase 0 stopgap for X12); `runtime` policy (auto / pinned / requires);
priority; `/api/experiments/actions`; preflight; autopilot; the migration
(`batches` → studies, `pool` → `runtime.allow`, `pending` → `queued`).
**Acceptance (API tests):**
- Create a 3-config × 3-seed study as drafts: nothing dispatches.
- Queue one experiment, Run-now one pinned experiment, and turn autopilot on with
  `max_parallel=2`: at most 2 are ever in flight.
- Move an experiment between studies, and share a baseline between two studies.
- Re-posting with a different overlay creates a new id.

### Phase 2: the framework and data connections (backend)
Sectioned profile YAML with legacy reads and a migration script; command templates used
by the machine runners and the Kaggle template; `call.py` hooks; the metrics section;
the dataset registry, resolver, the four placement modes, checks, and
`no-dataset-binding` in every `can_accept()`; `/api/runtimes` unified (**X10**);
`/api/profile` GET/PATCH with ruamel.
**Acceptance:**
- A Colab (or SSH-without-data) run fetches or pushes its dataset and trains.
- The same experiment produces the **same `config_hash`** on local and on a remote
  runtime.
- A PATCH to `commands.train` keeps every comment in the file.

### Phase 3: UI shell + Experiments + Experiment page (the daily loop)
Router, modules, the shell, the Experiments screen (Studies pane, Experiments tab,
Configs tab with CRUD and the form editor, the Composer), and the Experiment page (every
tab). The old Experiments subtabs and the Results tab are retired here.
**Acceptance (your click-through):** from an empty study to a queued 2-config × 3-seed
run in **one dialog**; open a running experiment from Lab and watch its console follow
live; reassign a blocked experiment's runtime inline.

### Phase 4: Datasets and Settings screens
The dataset matrix, binding editor, checks, and publish-to-Kaggle. Settings: the dynamic
YAML form, raw view, on-disk change detection, system info, alerts, and the new-profile
wizard.
**Acceptance:** add a key by hand in the YAML file and it appears in Settings without a
code change; fix a ✗ binding from the matrix and the blocked experiment dispatches on
the next tick.

### Phase 5: Lab and Compute
Lab with tile/list sections and needs-attention first. Compute board, runtime detail,
Add-runtime wizard, and the Colab Connect-account flow. **Kaggle CLI 2.x** in a separate
Python 3.11+ env pointed to by `kaggle_executable` (per account `KAGGLE_API_TOKEN`). That
brings real `kaggle quota`, live `kernels logs -f`, and per-experiment `--accelerator`.
**Acceptance:** Lab answers "what's running, what's stuck and why, what finished" with no
other tab open; Kaggle quota shows `source: measured`; a Kaggle run shows a live log.

### Phase 6: Compare, reporting, cleanup
Study Compare (table, charts, curves, export); palette actions ("queue size-ablation
drafts"); keyboard shortcuts (`/`, `j`/`k`, `r`, `⌘K`); a mobile pass on Lab and the
Experiment page. Delete `app.js` and the retired routes, and update `README.md`.
**Acceptance:** Compare for a 5-config × 3-seed study renders mean ± std with deltas vs
baseline and exports to LaTeX; nothing in the old README is harder to reach.

**Why this order.** Phase 0 makes the results that every later screen displays real.
Phase 1's model is what the screens are about. The data placement in Phase 2 depends on
the command templates and on knowing the run dir. The daily loop (Phase 3) is where
nearly all the value is, so it comes before the rarer surfaces.

---

## 11. Non-goals and risks

- **Not in scope:** cross-experiment dependencies/DAGs, hyperparameter-search
  orchestration (dissert's `search.py` stays a framework feature), multi-user auth,
  simultaneous dispatch across profiles, and cost accounting beyond quota.
- **Colab is volatile.** Nothing survives a long XDash outage. Its runtime card says so,
  and Run-now on Colab warns for estimates over half the session cap.
- **The Kaggle secret stays manual**: once per account, through the web UI. XDash can only
  detect it missing and link to the fix.
- **`est_hours` is machine-blind.** Recording `accelerator` per attempt (§3.4) is the
  groundwork. A per-accelerator rate is future work.
- **`dataset.root` in `config_hash`** is handled by placement (§5.1). A cleaner fix in
  dissert itself, stripping location fields from the hash, is optional and outside XDash.
- **Canonicalizing remote outputs** writes into the host repo's `outputs/`. This is the
  same trust level XDash already has there (local runs write there), but it is a new
  writer, so every copy is conflict-checked (§3.5).
