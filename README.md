# XDash

A web dashboard for running deep-learning experiments across whatever compute
you have — this machine, an SSH box, Kaggle, Colab — organized around
**Studies** (a question you're asking) and **Experiments** (config × seed ×
overrides), not around which runtime happens to execute them.

> **Status**: all six build phases in `XDASH_PLAN.md` are implemented on an
> uncommitted working tree. See **`XDASH_STATUS.md`** first — it lists what's
> been verified only in tests vs. what still needs a real run on real
> hardware, and every action you (the user) need to take before that's
> possible (credentials, environment upgrades, one `.gitignore` line, etc.).

It is designed to be **dropped into any repo as a single subdirectory**. It
doesn't assume anything about your model code beyond a `train.py`/`eval.py`
pair and a `configs/` tree of YAML files; features that need more than that
(schema-aware config validation, dataset identity, run-directory layout) go
through a small **framework adapter** (a `repos/<profile>.yaml` profile file
+ on-demand bridge subprocess calls into the host repo's own Python
environment — see "Framework integration" below), never a new dependency in
this folder's own `requirements.txt`.

## Setup

```bash
conda create -n xdash python=3.11   # or reuse an existing 3.8+ env
conda activate xdash
pip install -r requirements.txt
python server.py
```

Open **http://localhost:6070**.

Requires **tmux** on your system (`sudo apt install tmux` / `brew install
tmux`) for local/SSH runs — every local or SSH experiment runs inside a tmux
session (see Compute → Sessions below). If you want live TensorBoard
embedding or training-curve charts in Study Compare, make sure `tensorboard`
is installed (it's in `requirements.txt` already).

The backend is plain **Flask**, kept to a small, stable dependency chain —
this also runs in older environments (e.g. a Python 3.8 conda env) without
fighting pydantic/dependency version mismatches.

### Running the tests

```bash
conda run -n xdash python -m pytest tests -q
```

A permanent pytest suite (`tests/`) with a Flask test client, fake runners
and fake transports — nothing in it touches a real Kaggle account, SSH host,
Colab VM, or your `data/` directory. See `XDASH_STATUS.md` for the current
pass count and the two known, pre-existing, environment-only failures.

## Configuration

Everything XDash needs to know about your repo lives in a **profile**:
`repos/<profile>.yaml` (see `repos/dissert.yaml` for a worked example). A
profile is sectioned —

| section | meaning |
|---|---|
| `framework` | `repo_root`, `configs_dir`, which config globs are launchable vs. fragments, the dataset-identity keys |
| `commands` | the `train`/`eval` command templates every runtime (local, SSH, Colab, Kaggle) renders identically, plus the interpreter and env-activate line |
| `outputs` | where the host repo writes manifests/ledger, and the `run_id=` regex XDash cross-checks against |
| `hooks` | `module:function` bridge hooks into the host repo (config resolution, run-dir location, run status) |
| `metrics` | the primary metric and which metric keys are lower-is-better (drives Study Compare's highlighting) |
| `runtimes` | Kaggle/Colab/scheduler defaults |

Settings → your profile's own page renders this file as a form generated
from its own YAML shape (any key you add by hand shows up with no code
change — bool→toggle, number→numeric input, list→chip editor, `*_dir`/`root`
→a path picker), plus a **Raw YAML** view. Edits are validated (a throwaway
config load) before anything is written, and comments/order are preserved.

A legacy flat-key profile (`repo_root`, `train_script`, ... instead of the
sectioned form above) still works unmigrated — every reader falls back to
the flat spelling. `backend/migrate_profile.py` does a one-shot, reversible
rewrite into the sectioned form if you want it (`git diff`/`git checkout` to
inspect or revert).

## The screens

The sidebar is **Lab · Experiments · Compute · Datasets · Settings** — the
study → experiment → result loop, not a tab per subsystem. Every URL is
addressable (`#/lab`, `#/experiments?study=<id>&tab=compare`,
`#/x/<experiment_id>/live`, `#/compute/<runtime_id>`, `#/datasets/<name>`,
`#/settings/<section>`) — paste a link, refresh, back/forward all work.
**Every experiment name shown anywhere is a link to its own page.**

### Lab
The control room: what's running, what's stuck and why, and what just
finished — with no other tab open. **⚠ Needs attention** (blocked or
recently-failed experiments, each with a one-click fix) comes first, then
**Studies** (progress bar, best metric so far, ETA), **Runtimes** (every
registered runtime — local/SSH/Colab/Kaggle — as a tile or list, your
choice, remembered per section), **Running now**, and **Recent**. All of it
comes from one `GET /api/pulse` poll.

### Experiments
Four subtabs:

- **Experiments** — a left pane of Studies (a study groups experiments
  around one question, a primary metric, and an optional baseline config for
  deltas) plus a table of every experiment, grouped by config+overrides so
  seeds aggregate visually. Row actions and a selection-bar bulk action both
  go through the same one action endpoint (queue / run now / cancel / retry
  / delete / change runtime / add-to-study / duplicate), so group handling
  never drifts from individual handling. **+ New ▾** opens the Composer:
  pick configs × seeds, add config overrides (dotted key → value — this
  replaces raw CLI flags, and is validated against the host repo's own
  config loader before you can save), an inline preflight matrix shows the
  resulting experiment ids and which runtime each would land on, then **Save
  as drafts / Queue / Run now**.
- **Configs** — recursively scans your `configs/` tree (Browse), or build one
  visually with no YAML editing (Create — a template picker, a form with
  Add-field/Add-section and a live YAML preview). **Show resolved** calls
  into the host repo's own config loader and shows the fully merged,
  schema-validated config exactly as `train.py` would see it, with real
  field-level errors if it doesn't validate. "Create experiments from this
  config →" opens the Composer pre-filled.
- **Compare** — mean ± std per config across seeds, with **n** and a delta
  vs. the study's baseline; the primary metric column comes first, and the
  best value per metric is highlighted (direction-aware — a study's profile
  says which metrics are lower-is-better). Below the table: a bar±std chart,
  a per-seed strip plot, a radar chart, training curves overlaid straight
  from each run's TensorBoard event files, and a seed×fold status heatmap.
  **Export** to CSV, Markdown, or LaTeX. Any selection from the Experiments
  table's bulk bar can be sent here too — **"Compare selected"** opens
  Compare pre-filled with that exact id list, across studies or with none at
  all (Unfiled), not just one study's members.
- **Sessions** — real tmux sessions on this machine (see "Terminals" below),
  plus a glance at what's also running on Kaggle or under another profile.

### Experiment page (`#/x/<id>`)
Not a nav item — reached by clicking any experiment name. Seven tabs:
**Overview** (status, progress, key metrics, the attempt/leg timeline, code
commit, data binding used), **Live** (console with a stage selector — tmux
capture for local/SSH/Colab, the Kaggle log once collected), **Metrics**
(the eval report's metric cards, plus training curves read from TensorBoard
event files), **Artifacts** (the run directory's file tree — checkpoints,
plots, reports), **Config** (the resolved config as run, plus any
overrides), **History** (every attempt/leg, its runtime, duration, and
outcome), **Notes**.

### Compute
A board of every runtime (local, every SSH host, every Colab/Kaggle account)
as a tile or list — state, accelerator, capacity, quota. **+ Add runtime**
walks you through picking a kind, filling its form, and a live connectivity
test that must pass before it saves. Click a runtime for its detail page:
**Now** (what's running here), **Queue** (what's pinned/likely-next here),
**History** (a success-rate sparkline + past attempts), **Settings**
(host/account credentials and limits), **Diagnostics** (test connection, GPU
probe, Kaggle quota check, Colab session check). Below the board, the
original local-machine tooling still lives here:

- **Queue** — the local scheduler: pick a config, mode (Train / Eval / Train
  + Eval), and extra args; a concurrency stepper controls how many run at
  once; Scheduled / Running / Past sections, reorderable while queued.
- **Machines** — registered SSH hosts.
- **Kaggle** / **Colab** — account management (credentials, weekly budget,
  session limits, the one-time GITHUB_TOKEN secret checklist for Kaggle).
- **Monitors** — a small permanent list of system/GPU monitoring commands
  (`nvidia-smi`, `htop`, `nvtop`, `free -h`, `df -h`), each in its own tmux
  session; add your own by name + command + watch interval.
- **TensorBoard** — starts one shared `tensorboard --logdir` process on
  demand and opens it in a new tab.

### Datasets
Every registered dataset × every runtime, as a matrix: ✓ path · ⇡ push · ↓
fetch · ⊕ attach · ✗ missing — resolved exact-runtime → kind-default, the
same order a real dispatch resolves it in. Click a cell to edit that binding
and **Check** it right now; click a dataset's name for its detail page
(fragment info, Kaggle source, which configs/experiments use it, and a link
into the channel-mode montage preview + the guarded test-set evaluation
audit trail, both unchanged from before).

### Settings
Your active profile as a dynamic form (see "Configuration" above), a raw
YAML view, system/bridge health, the repo-profile switcher and "+ New
profile" wizard, notebook templates, and alert-channel notifications (fired
server-side when an attempt finishes, fails, or blocks — these work even
with no browser tab open).

## Command palette
**Ctrl/⌘ K** opens a fuzzy jump-to-anything search — studies, experiments,
configs, runtimes, datasets, runs, reports, or any tab by name — plus
action-verb entries on top of that same search: **"Queue drafts — \<study
name>"** queues every draft in a matching study in one keystroke, plus
per-study "Compare" and "Toggle autopilot" entries, and "New study"/"New
experiments…". Opening the palette is what triggers loading data for
anything you haven't visited yet (consistent with the rest of the app's
"nothing happens unless you're looking at it" design).

## Keyboard shortcuts
- **`/`** focuses the active screen's own filter/search input.
- **`j`** / **`k`** move a highlight down/up in whichever table is relevant
  right now (the Experiments table, or the Compare table when that subtab is
  open).
- **`r`** runs the highlighted Experiments-table row's own primary action —
  whichever button that row already shows first for its status (Queue for a
  draft, Cancel while in flight, Retry once terminal).
- **`Ctrl`/`⌘ K`** opens the command palette.

All four are one shared handler (`static/js/lib/shortcuts.js`) that never
fires while you're typing in a field or a modal is open.

## Framework integration (the bridge)
A few features need more than reading files — schema-aware config
validation, run-directory location, the model registry, dataset channel
construction. Rather than adding those (potentially heavy: pydantic, torch,
...) to this folder's own `requirements.txt`, XDash shells out to a small,
single-purpose script per feature under `backend/bridge_scripts/`, run by
whichever Python the profile's `bridge_python_executable` names (defaults to
the launch interpreter). This keeps this folder's own dependency footprint
exactly what it's always been (Flask + PyYAML + tensorboard + ruamel.yaml),
while still using the host repo's real code.

Every bridge-backed feature degrades cleanly if the host repo doesn't have
the module it needs: a clear "not available in this repo" message, not a
broken page. Settings → About shows which of the host repo's optional
modules currently import cleanly.

## Mobile
Below ~860px width the sidebar becomes a slide-in overlay (hamburger button
in the top bar); every two-pane layout (Configs, Sessions, Compare's
supporting tables, History) stacks into a single scrollable column; wide
tables (Compare, the Experiments table, History, Artifacts) keep their
columns and scroll horizontally inside their own panel rather than
squeezing into something unreadable; buttons and tabs get a larger minimum
tap target; and the Experiment page's tab strip and header actions scroll
horizontally instead of wrapping into a tall stack. Anywhere a name might be
too long to read in full, hovering it shows the full value as a tooltip.

## Notes & limitations
- Almost nothing runs in the background — tmux/the run's own runner is the
  source of truth for an experiment, and XDash just polls it. The dispatcher
  loop (queued experiments, study autopilot, Kaggle/Colab polling) is the one
  deliberate background exception, same as the local Scheduler always was.
- XDash does not sandbox the commands it launches beyond validating the
  config path and overrides — treat it the same as a terminal you'd type
  `python train.py ...` into yourself.
- Live data is **polling with cursors**, not a websocket/SSE connection —
  simple, and there's no connection state to ever recover from. The visible
  screen polls every few seconds and pauses while the tab is hidden.
- See **`XDASH_PLAN.md`** for the full design (why each decision was made)
  and **`XDASH_PROGRESS.md`** for exactly what was built in each phase, every
  deviation from the plan, and every deferred live-acceptance check.
  **`XDASH_STATUS.md`** is the one-page version of all of that.
