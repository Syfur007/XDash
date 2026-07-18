# Experiment Console

A small, self-contained web dashboard for browsing YAML experiment configs,
editing them, launching training/eval runs as tmux sessions, watching them
live, restarting ones a reboot interrupted, viewing evaluation reports, and
viewing TensorBoard — all from one page.

It is designed to be **dropped into any repo as a single subdirectory** and
removed again without leaving a trace. It doesn't assume anything about your
model code; it only shells out to `train.py` / `eval.py` the same way you
would from a terminal.

```
your-repo/
├── configs/
│   ├── base_config.yaml
│   └── mkunet/
│       └── mkunet_s_clinicdb_b16_lr001.yaml
├── logs/                 <- eval.py's report JSON files are found here
├── runs/
├── checkpoints/
├── train.py
├── eval.py
└── exp_dashboard/        <- this folder. Copy in, or delete, freely.
```

## Setup

```bash
cd exp_dashboard
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
| `reports_dir` | where eval.py writes `*_report.json` files (defaults to `logs_dir` if unset) |
| `python_executable` | interpreter used to launch `train.py` / `eval.py` |
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

### Reboot resilience
A tmux session only survives the *dashboard* restarting — it doesn't survive
the *machine* rebooting. To handle that: every launch is recorded (config,
mode, extra args, experiment name) in
`exp_dashboard/data/terminals_state.json`, independent of whether the tmux
session is currently alive. If a recorded session is gone and **no
evaluation report exists yet for that experiment**, the Terminals tab shows
it as **Interrupted** with a **Restart** button — one click launches a fresh
tmux session with the exact same config/mode/args, and your own
checkpoint/resume logic in `train.py` (e.g. `checkpoint.resume: true`) takes
it from there. If a report *does* exist, it's shown as **Completed** instead
(no restart offered, since eval already ran and produced results).

### Reports
Any `.json` file under `reports_dir` that contains a `"metrics"` key is
treated as an evaluation report — no fixed naming convention required. The
Reports tab groups them by sub-directory the same way Configs does, and
clicking one shows every metric as a stat card, a radar chart for the
0–1-scale metrics (dice/mIoU/precision/recall/specificity/F2/accuracy), plus
model, efficiency, environment, and full config details. **Select two or
more** (checkboxes) and hit **Compare selected** for a side-by-side metrics
table, an overlaid radar chart, and a config-diff table that only lists keys
that actually differ. Each report gets one consistent color used everywhere
on the comparison (table header swatch + radar line); the best value in each
metric row is highlighted in a distinct emerald tone that's never one of the
report accent colors, so it's always unambiguous.

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

### TensorBoard
Starts a single shared `tensorboard --logdir runs/` process on demand — after
a confirmation prompt, since it spawns a background process on the server —
and opens it in a new browser tab rather than embedding it, which is more
reliable across browsers than an iframe. Since it points at the whole
`runs/` directory, every experiment's event file shows up automatically.

## Mobile
Below ~860px width the sidebar becomes a slide-in overlay (hamburger button
in the top bar), and every two-pane layout (Configs, Terminals, Reports,
History) stacks into a single scrollable column instead of a fixed side-by-
side split. Anywhere a name might be too long to read in full (config paths,
terminal/session names, report names, file-tree entries), hovering it shows
the full value as a tooltip.

## Notes & limitations

- No background worker or polling thread runs experiments — tmux is the
  source of truth, and the dashboard just reads it (via `capture-pane`) when
  the frontend asks. This means restarting the dashboard process loses
  nothing: there's no in-memory state to reconstruct, only the small
  metadata file recording which sessions were launched for which config.
- The dashboard does not sandbox or validate the commands it launches beyond
  checking the config path exists — treat it the same as a terminal you'd
  type `python train.py ...` into yourself.
- Log parsing is regex-based and generic: any `Key: 1.234` pair after an
  `Epoch N` marker is picked up automatically, so new metrics you add to your
  training loop's log line show up without touching this codebase. The same
  applies to reports — any key under `"metrics"` in the JSON shows up as a
  stat card automatically.
