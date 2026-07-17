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

Open **http://localhost:8000**.

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
| `configs_dir` / `logs_dir` / `runs_dir` / `checkpoints_dir` | relative to `repo_root` |
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
Any `.json` file under `logs/` that contains a `"metrics"` key is treated as
an evaluation report — no fixed naming convention required. The Reports tab
groups them by sub-directory the same way Configs does, and clicking one
shows every metric as a stat card, a radar chart for the 0–1-scale metrics
(dice/mIoU/precision/recall/specificity/F2/accuracy), plus model, efficiency,
environment, and full config details. **Select two or more** (checkboxes) and
hit **Compare selected** for a side-by-side metrics table (best value per row
highlighted), an overlaid radar chart, and a config-diff table that only
lists the keys that actually differ between the runs.

### TensorBoard
Starts a single shared `tensorboard --logdir runs/` process on demand and
embeds it in an iframe. Since it points at the whole `runs/` directory, every
experiment's event file shows up automatically.

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
