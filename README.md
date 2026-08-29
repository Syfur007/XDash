# Experiment Console

A small web dashboard for browsing YAML experiment configs, editing them,
launching training/eval runs as tmux sessions, watching them live,
restarting ones a reboot interrupted, tracking every run the orchestration
layer knows about, browsing registered datasets and previewing their
channel modes, viewing evaluation reports, and viewing TensorBoard — all
from one page.

It is designed to be **dropped into any repo as a single subdirectory** and
removed again without leaving a trace. It doesn't assume anything about your
model code; it only shells out to `train.py` / `eval.py` the same way you
would from a terminal. Features that need more than that (schema-aware
config validation, the model registry, dataset channel construction — see
"Framework integration" below) go through a separate, on-demand subprocess
call into the *host repo's* own Python environment, never a new dependency
in this folder's own `requirements.txt`.

```
your-repo/
├── configs/
│   ├── base.yaml
│   ├── dataset/            <- fragments the Data tab reads directly
│   │   └── clinicdb.yaml
│   └── mkunet/
│       └── mkunet_s_clinicdb_b16_lr001.yaml
├── logs/                   <- eval.py's report JSON files are found here
├── runs/
├── checkpoints/
├── artifacts/               <- optional: orchestration layer's manifests/ledger
│   ├── runs/<run_id>/manifest.json
│   └── ledger/{runs,compute,test_evals,stats}.csv
├── train.py
├── eval.py
└── dashboard/               <- this folder. Copy in, or delete, freely.
```

The `artifacts/` layout is entirely optional — the Runs tab and the Compute
summary just show nothing there if a repo hasn't adopted it. Everything else
works exactly the same either way.

## Setup

```bash
cd dashboard
pip install -r requirements.txt
python server.py
```

Open **http://localhost:6070**.

Requires **tmux** on your system (`sudo apt install tmux` / `brew install tmux`)
— every experiment is run inside a tmux session (see below). If you want live
TensorBoard embedding, make sure `tensorboard` is installed and on your `PATH`.

The backend is plain **Flask** (not FastAPI) specifically so it has a small,
stable dependency chain — this was built to also work in older environments
(e.g. Python 3.8 conda envs) without fighting pydantic/dependency version
mismatches.

## Configuration

Everything the dashboard needs to know about your repo lives in
[`dashboard_config.yaml`](./dashboard_config.yaml):

| key | meaning |
|---|---|
| `repo_root` | path to your repo root, relative to this folder (default: `..`) |
| `configs_dir` / `logs_dir` / `runs_dir` / `checkpoints_dir` / `plots_dir` | relative to `repo_root` |
| `reports_dir` | where eval.py writes report JSON files (defaults to `logs_dir` if unset) |
| `artifacts_dir` | where the orchestration layer writes manifests/ledger, if the host repo has that layout (default: `artifacts`) — powers the Runs and Compute-summary views |
| `python_executable` | interpreter used to launch `train.py` / `eval.py` |
| `bridge_python_executable` | interpreter used for on-demand calls into the host repo's own code (schema export, model registry, channel construction) — see "Framework integration" below. Blank = reuse `python_executable` |
| `train_script` / `eval_script` | script filenames |
| `eval_default_args` | flags always appended on eval runs (e.g. `["--ensemble"]`) |
| `env_activate_cmd` | command typed into the tmux pane before launching, e.g. `"conda activate thesis"` |
| `tmux_session_prefix` / `tmux_pane_width` / `tmux_pane_height` | tmux session settings |
| `tensorboard_port` | port TensorBoard is launched on |

Moved this folder somewhere else, or your repo has a different layout? Edit
this one file — nothing else needs to change. **Make sure `env_activate_cmd`
matches how you'd normally activate your environment by hand** (conda, venv,
etc.) — this is the #1 thing to check if a launched run fails immediately.

## How it works

### Configs
Recursively scans `configs/`. Files directly inside are grouped as "general";
files in a sub-directory are grouped by that sub-directory's name (e.g.
`configs/mkunet/...` → category `mkunet`). Selecting a config opens it in an
in-browser YAML editor (CodeMirror) with live validation before you can save.
"Launch in terminal" starts a training or eval run for that config,
optionally with extra CLI overrides (e.g. `--epochs 10 --lr 0.0005`), and
jumps you to the Terminals tab.

**Show resolved** (next to Save) calls into the host repo's own config
loader (via the bridge — see below) and shows the fully `compose:`-merged,
schema-validated config exactly as the training script would actually see
it — not just this one file's own lines. If the config doesn't validate,
you get the real field-level errors (e.g. `dataset.root — Field required`)
instead of finding out only when a training run crashes. Falls back to a
clear "not available" message if the host repo has no schema module.

### Create Config
A visual config builder — no YAML editing required. Optionally pick an
existing config as a **template**: its keys, sections, and current values
are loaded straight into the form (a nested dict becomes a collapsible
section; a boolean becomes a toggle switch; numbers, text, and lists get the
appropriate input). Edit values, add new fields or whole new sections with
the **+ Add field** control at any level (choose the type: text, number,
toggle, list, or a nested section), or remove anything with **✕**. A
**live preview** panel on the right shows the exact YAML that will be
written, updated as you type. When you're happy with it, pick a destination
folder (existing sub-directory or a new one) and filename and hit **Save
config** — it reuses the same save endpoint as the Configs editor, so the
new file shows up there immediately, ready to launch.

When the bridge can reach the host repo's schema, any field with a fixed set
of allowed values (`training.optimizer`, `dataset.channel_mode`, ...) renders
as a real dropdown instead of a free-text box a typo could slip through. The
`model:` section is deliberately left as free-form fields — each registered
architecture takes different constructor kwargs, so there's no one schema to
validate against — but it gets a **Profile params ▸** button instead, which
builds the model exactly as configured (via the real model registry) and
reports its actual trainable parameter count.

### Terminals
There is no queue — launching a config immediately opens a dedicated
**tmux session**, driven the same way you'd use it by hand: `cd` into the
repo, type `env_activate_cmd` if set, then type
`python train.py --config configs/... [extra args]`. The dashboard reads it
back with `tmux capture-pane`, so the Terminals tab is really just a live
window onto real tmux sessions:

- **Every active tmux session is listed**, not just ones the dashboard
  launched — sessions started outside the dashboard show up too, labeled
  "unmanaged", so this doubles as a general tmux overview.
- Clicking a session shows its **complete scrollable output**, plus a live
  metrics chart parsed from any `Epoch N | Train Loss: ... | Val Dice: ...`
  lines your training loop already prints — no extra instrumentation needed.
- **Stop** sends Ctrl-C to interrupt the current command without closing the
  session. **Kill session** (confirmation required) ends the tmux session
  entirely; a final snapshot of its output is saved first so deleting it
  doesn't lose the last thing it printed.
- Because these are real tmux sessions, `tmux attach -t <session_name>` from
  a terminal works too, alongside the dashboard.

### Scheduler
Queue up training/eval runs to launch automatically — the one part of the
dashboard with an actual background thread, since unattended overnight
scheduling needs something to notice a slot has freed up even if nobody has
the dashboard open. Everything else about the dashboard stays true to its
"nothing happens unless you're looking at it" design; this is a deliberate,
narrow exception.

- **Add to schedule**: pick a config, a mode (**Train**, **Eval**, or
  **Train + Eval**), and optional extra args. "Train + Eval" creates two
  linked entries — eval only launches after its train half *completes
  successfully*; if training fails or is cancelled, the eval half is marked
  "skipped" instead of ever running against a broken checkpoint.
- **Concurrency**: the +/- stepper controls how many scheduled experiments
  run at once. Raising it immediately allows more scheduled items to start;
  lowering it never kills anything already running, it just stops new ones
  from starting until things fall back below the new limit.
- **Scheduled / Running / Past**: three sections show every item at every
  stage. Reorder anything still in "Scheduled" with the ▲/▼ buttons — this
  changes what launches next. Cancel a running item (stops it, keeps it
  under Past for the record) or remove a scheduled/past item outright. All
  of this works freely while other scheduled experiments are mid-run.
- A scheduled item becomes an ordinary tmux session the moment it launches
  (via the exact same code path as "Launch in terminal"), so it shows up on
  the Terminals page too — the scheduler is just the layer that decides
  *when* to press that button, not a separate execution mechanism.

### Reboot resilience
A tmux session only survives the *dashboard* restarting — it doesn't survive
the *machine* rebooting. To handle that: every launch is recorded (config,
mode, extra args, experiment name) in
`dashboard/data/terminals_state.json`, independent of whether the tmux
session is currently alive. If a recorded session is gone and **no
evaluation report exists yet for that experiment**, the Terminals tab shows
it as **Interrupted** with a **Restart** button — one click launches a fresh
tmux session with the exact same config/mode/args, and your own
checkpoint/resume logic in `train.py` (e.g. `checkpoint.resume: true`) takes
it from there. If a report *does* exist, it's shown as **Completed** instead
(no restart offered, since eval already ran and produced results).

### Runs
Runs recorded by the orchestration layer (`artifacts/runs/<run_id>/manifest.json`
+ `artifacts/ledger/runs.csv`), grouped by **config hash** — two runs sharing
a hash differ only by seed/fold, not by what's actually being tested. Each
group renders as a seed × fold grid, one colored cell per run
(amber=running, emerald=done, red=failed, slate=pending); click a cell for
that run's full detail: git commit **and a dirty-tree warning** if the run
started from an uncommitted tree, hardware, env hash, timing, realized
GPU-hours, and any non-determinism the training script recorded rather than
silently assumed away. **Copy resolved config (YAML)** copies that run's
exact resolved config to the clipboard. A **Compute-hours** strip above the
grid totals `artifacts/ledger/compute.csv` — a running GPU-hour tally
against your project budget, at a glance instead of spreadsheet math. Shows
nothing if the host repo has no `artifacts/` layout yet.

### Data
Every dataset registered under `configs/dataset/*.yaml`, as configured
(root, modality, channel mode, dedup/external flags). Pick a dataset, a
channel mode (m1–m5), and a real sample image path, and **Preview
channels ▸** renders every channel the host repo's channel-construction
module would actually build — RGB, XY position, YCbCr, R/θ — as a small
tile grid, via the bridge (below). Below that, the **test-set evaluation
audit trail** straight off `artifacts/ledger/test_evals.csv`: every
one-time token ever issued to touch the guarded test set, with when and
against which config — a real audit log, not a convention someone has to
remember to honor.

### Reports
Any `.json` file under `reports_dir` that contains a `"metrics"` key is
treated as an evaluation report — no fixed naming convention required (this
correctly includes a bare `report.json` with no experiment-name prefix, not
just `<name>_report.json`). The Reports tab groups them by sub-directory the
same way Configs does, and clicking one shows every metric as a stat card, a
radar chart for the 0–1-scale metrics (dice/mIoU/precision/recall/
specificity/F2/accuracy/1−ECE), plus model, efficiency, environment, and
full config details. Metrics from the canonical `metrics/` module get extra
context instead of appearing as unexplained numbers: HD95/ASD/NSD show how
many images were excluded from the average (empty-mask cases) right next to
the value; Dice shows its 5th/25th-percentile band underneath; and
`fpr_on_normals`/`specificity_lesion_free` render as an explained **N/A**
(hover for why) rather than a bare dash when a dataset has no lesion-free
images. **Select two or more** (checkboxes) and hit **Compare selected** for
a side-by-side metrics table, an overlaid radar chart, and a config-diff
table that only lists keys that actually differ. Each report gets one
consistent color used everywhere on the comparison (table header swatch +
radar line); the best value in each metric row is highlighted in a distinct
emerald tone that's never one of the report accent colors, so it's always
unambiguous.

### History
A read-only, recursive file browser with a source switcher — built into the
directory panel's own header — for **Logs** (`logs_dir`: training logs, eval
reports, anything text-ish) or **Images** (`plots_dir`: eval plots,
prediction overlays). Each source only ever shows files of its own type
(images never show up under Logs and vice versa; empty folders after
filtering are hidden too). Selecting a text file previews it inline (JSON is
pretty-printed); selecting an image renders it directly, with an "open full
size" link. This is separate from Reports: Reports understands *report
content* specifically (metrics, config, comparisons); History is a plain
directory browser for everything else in those folders.

### Machine Stats
A small, permanent list of system/GPU monitoring commands — `nvidia-smi`,
`htop`, `nvtop`, `free -h`, `df -h` ship by default — each launched in its
own tmux session on demand, reusing the exact same capture-pane mechanism as
Terminals. One-shot commands (`nvidia-smi`, `df -h`) are automatically
wrapped in `watch -n <interval>` so they keep refreshing; commands that
already refresh themselves (`htop`, `nvtop`) are run as-is (set their watch
interval to `0`). Add your own via the form at the bottom of the page (name +
command + watch interval) — these persist in `data/monitors.json` and can be
removed; the built-in five can't be, though you can still stop them anytime.

Each card has its own output dropdown — it pops open automatically the
moment you start that service and closes automatically when it stops; you
can also click the card at any time to show or hide it manually in between,
independent of the auto behavior.

### TensorBoard
Starts a single shared `tensorboard --logdir runs/` process on demand — after
a confirmation prompt, since it spawns a background process on the server —
and opens it in a new browser tab rather than embedding it, which is more
reliable across browsers than an iframe. Since it points at the whole
`runs/` directory, every experiment's event file shows up automatically.

## Framework integration (the bridge)

A few features need more than reading files — schema-aware config
validation, the model registry, dataset channel construction. Rather than
adding those (potentially heavy: pydantic, torch, ...) to this folder's own
`requirements.txt`, the dashboard shells out to `bridge_python_executable`
(defaults to `python_executable`) running one small, single-purpose script
under `backend/bridge_scripts/` per feature. This keeps this whole folder's
own dependency footprint exactly what it's always been (Flask + PyYAML +
tensorboard), while still using the host repo's real code — never
re-implementing config validation or model construction on this side.

Every feature that goes through the bridge degrades cleanly if the host
repo doesn't have the module it needs (an older repo, or one that hasn't
adopted this layer yet): you get a clear "not available in this repo"
message, not a broken page. `GET /api/bridge/status` reports which of the
host repo's optional modules (`orchestration`, `models`, `metrics`,
`datasets`, `pandas`) currently import cleanly.

## Command palette
**Ctrl/⌘ K** opens a fuzzy jump-to-anything search — configs, runs,
reports, or any tab by name. Opening it is what triggers loading runs/
reports data if you haven't visited those tabs yet (consistent with the
rest of the dashboard's "nothing happens unless you're looking at it"
design — see Notes & limitations).

## Mobile
Below ~860px width the sidebar becomes a slide-in overlay (hamburger button
in the top bar), and every two-pane layout (Configs, Terminals, Runs,
Reports, History) stacks into a single scrollable column instead of a fixed
side-by-side split. Data and Scheduler are already single-column panel
stacks at any width, so they need no special handling. Anywhere a name
might be too long to read in full (config paths, terminal/session names,
report names, file-tree entries), hovering it shows the full value as a
tooltip.

## Notes & limitations

- Almost nothing runs in the background — tmux is the source of truth for
  experiments, and the dashboard just reads it (via `capture-pane`) when the
  frontend asks. Restarting the dashboard process loses nothing: there's no
  in-memory state to reconstruct, only small metadata files recording which
  sessions were launched for which config. The **Scheduler is the one
  exception** — it runs a lightweight thread that ticks every few seconds so
  queued experiments keep progressing even with no browser tab open;
  everything else follows the read-on-demand pattern.
- The dashboard does not sandbox or validate the commands it launches beyond
  checking the config path exists — treat it the same as a terminal you'd
  type `python train.py ...` into yourself.
- Log parsing is regex-based and generic: any `Key: 1.234` pair after an
  `Epoch N` marker is picked up automatically, so new metrics you add to your
  training loop's log line show up without touching this codebase. The same
  applies to reports — any key under `"metrics"` in the JSON shows up as a
  stat card automatically.
- Bridge calls are short-lived subprocesses (`bridge_python_executable
  <script> <args>`, never through a shell), each doing exactly one thing —
  export a schema, resolve a config, profile a model, build a channel
  preview — and none of it holds state between calls. The same
  origin/token guard that covers every other state-changing route
  (`server.py`'s `before_request` hook) covers the bridge-backed ones too;
  nothing about the bridge changes the dashboard's security model.
