# Dashboard Redesign Plan: experiment-oriented IA + unified runner model

## 1. Problem statement

The dashboard (`dashboard/`) has 11 top-level nav items — Configs, Create Config, Runs,
Terminals, Scheduler, Kaggle, Reports, History, Data, Machine Stats, TensorBoard — organized
around **implementation mechanism** (tmux vs Kaggle CLI vs file readers) rather than around the
**experiment lifecycle** (define → launch → monitor → collect → compare). Two concrete symptoms:

- **One experiment, five tabs.** Following a single training run from config to result today
  means: pick/edit it in Configs, launch it (Configs' own run-bar, or Scheduler), watch it in
  Terminals, and — once finished — read it in Runs and/or Reports. Five different views, four
  different data models, for one continuous piece of work.
- **Kaggle is a parallel universe, not a second runner.** `backend/kaggle.py` /
  `static/js/views/kaggle.js` implement an entire second vocabulary (Accounts → Workers → push /
  status / download) alongside the mclab-only vocabulary (Terminals → sessions → stop / kill /
  restart) used everywhere else. They share almost no UI language even though they're doing the
  same conceptual job: run this experiment somewhere, watch it, get the results back. The two
  even duplicate infrastructure — Scheduler's `notify_on_finish` and the Kaggle tab's
  notification panel both drive the exact same `backend/notifications.py`, from two different
  toggles in two different places.
- **The gap is already being hand-patched outside the dashboard.** `experiment_status.csv` (repo
  root, currently untracked) has a `worker` column with values like `w3`, `mclab`, and even
  `"w1, mclab"` — someone is already manually tracking which runner (which Kaggle worker, or the
  local machine) ran which config, in a spreadsheet, because the dashboard has no view that
  answers "which runner is/should be running this." That's the clearest evidence the current IA
  is missing a first-class concept, not just missing polish.

This plan restructures the tab set around the experiment lifecycle and introduces one **Runner**
abstraction (mclab = one `local` runner, each Kaggle account = one `kaggle` runner) so every
lifecycle view (launch, monitor, results) is written once against that abstraction, branching
only where the two backends genuinely differ — never hiding a difference, never faking parity
that doesn't exist.

**Hard constraint carried over from this project's own `IMPLEMENTATION_PLAN.md`, unchanged:** no
build step, Flask + vanilla JS, CDN-loaded third-party libs only, dashboard's own
`requirements.txt` stays minimal. This plan is an information-architecture and backend-facade
change, not a stack change. Frontend view scripts are loaded as plain classic `<script>` tags
sharing one global scope (`app.js`, `js/components/*.js`, `js/views/*.js` today) — that's the
real, current pattern (not the ES-module split `IMPLEMENTATION_PLAN.md` Phase 7 once proposed but
never carried out), and this plan keeps following it rather than introducing a second convention.

**Non-negotiable:** every capability listed in `README.md` today must still exist after this
plan, reachable in no more steps than today (usually fewer). Section 5 is an explicit old→new
mapping table for exactly this reason — treat it as the acceptance checklist.

---

## 2. The Runner abstraction

### 2.1 What "runner" means here

A **runner** is one thing capable of executing a config and producing a run. Today there are two
kinds, with real, load-bearing differences:

| | **mclab (`local`)** | **Kaggle account (`kaggle`)** |
|---|---|---|
| Where it executes | The machine the dashboard itself runs on (confirmed: dashboard is deployed and driven on mclab, `syf@100.102.242.23`) — `tmux_runner.py` shells out locally, no network hop | Kaggle's own cloud infra, reached over the network via the `kaggle` CLI |
| Unit of launch | One config + one mode (`train`/`eval`) + optional CLI overrides — chosen per launch | **Redesigned by this update (see §2.2): one config + one mode + optional overrides, chosen per launch — same shape as mclab**, rendered into a shared **template notebook** at push time rather than a bespoke per-worker notebook authored outside the dashboard. (Superseded design note, kept for context: the notebook previously *was* the unit — `notebooks/iccit-kaggle-worker3.ipynb` clones the repo fresh and iterates a whole hand-assigned block of configs×seeds internally. The user confirmed multi-config-per-notebook batching is not actually needed, which is what makes the §2.2 redesign possible.) |
| Concurrency | N tmux sessions at once, governed by `scheduler.py`'s `max_concurrent` (arbitrary, GPU-limited in practice) | ~1 kernel running per account at a time (Kaggle's own platform limit — `kaggle.py`'s `_concurrent_push_warning` already documents this) — **unchanged by the §2.2 redesign**, still a real Kaggle platform ceiling, not a dashboard choice |
| Live monitoring | Full scrollback via `tmux capture-pane`, updated on every poll; regex-parsed metrics chart | Status string only (`queued/preparing/running/complete/error/...`), polled every `kaggle_poll_interval_seconds`; **no mid-run log access** — this is a real Kaggle API limitation, not a gap in this dashboard, and the §2.2 redesign does not change it |
| Stop/interrupt | `Ctrl-C` (stop) or `kill-session` (kill), both instant | **Confirmed absent — live-verified 2026-09**, not just doc-researched: the installed `kaggle` CLI (pip package `kaggle` 1.7.4.5, from `Kaggle/kaggle-api`) exposes exactly `kernels {list, files, init, push, pull, output, status}` — no stop/cancel/interrupt, and (contrary to what this package's newer, not-yet-installed `Kaggle/kaggle-cli` rewrite's docs describe) no `delete` either. The honest UI state is "let it finish or budget-timeout, or cancel manually on kaggle.com" — implemented as `KaggleRunner.stop()`/`.kill()` both raising `RunnerCapabilityError` with that exact message, not a disabled-looking button |
| Restart | Same tmux mechanism, `restart_count` tracked | Re-`push` the same rendered notebook (same config/mode/args re-rendered into the template — mirrors mclab's restart exactly now, instead of "re-push whatever the notebook happened to contain") |
| Results | Already on local disk (`logs/`, `artifacts/`, `checkpoints/` — same filesystem the dashboard reads everywhere else) | Zipped kernel output → explicit `download()` → extract → `register_ledger()` merges into the *same* local `artifacts/ledger/runs.csv` mclab writes to directly |
| Capacity ceiling | Concurrency slots (soft, operator-set) | `budget_hours` per worker + a self-tracked weekly GPU-hour estimate per account (Kaggle exposes no real quota API — already documented as an estimate in `kaggle.py`'s `usage_history` docstring) |
| Credentials | None (same machine) | Per-account, two possible shapes (classic key / bearer token), stored under `data/kaggle_accounts/<name>/` |

**Live verification results (2026-09), implemented, not just researched:** the environment this
was built and tested against has `kaggle` 1.7.4.5 (`Kaggle/kaggle-api`) actually installed and a
real account configured. A real `kernels push` + `kernels status` round-trip during this feature's
own testing turned up one genuine, previously-latent bug, now fixed: this CLI version's `kernels
status` prints the Python enum's own repr (`has status "KernelWorkerStatus.RUNNING"`), not the
bare lowercase string (`"running"`) `IN_PROGRESS_STATUSES`/`FINISHED_STATUSES`/`FINAL_STATUSES`
have always compared against — meaning over-budget detection, the poller's final-status/
notification trigger, and `download_all()`'s "only download finished workers" filter were all
**silently never firing**, before this change, against this CLI version. Fixed by
`backend/kaggle.py`'s new `_normalize_kaggle_status()` (strips the enum-class prefix, lowercases,
aliases the one camelCase exception) called on every `refresh_status()` result. `--timeout` on
`kernels push` is confirmed present and working (already wired in §2.2); the newer
`Kaggle/kaggle-cli` rewrite's `--accelerator`/`delete` this plan's first draft cited from that
package's own docs are **not** in the actually-installed CLI — correctly never wired into any
code, only ever mentioned as a documented possibility.

The abstraction's job is **not** to paper over these differences. It's to give every lifecycle
view one shared shape to render (status, capacity, "can I do X here") so the *UI* stops
hand-writing two unrelated code paths, while every action that's genuinely runner-specific stays
runner-specific and capability-gated rather than faked.

### 2.2 Redesigned Kaggle unit of launch: template notebook + placeholder

*(Addition per user direction: keep multi-config batch notebooks off the table — one push =
one config, exactly like mclab.)*

Today, `push(worker_id)` (`backend/kaggle.py:516`) copies the worker's own notebook file
byte-for-byte into the upload tmpdir. Whatever configs/seeds that notebook runs is baked in by
whoever last hand-edited it — that's the coarse-grained "unit of launch" §2.1 described. The fix
is to stop copying the worker's own notebook and instead **render one shared template** per push:

**1. One template notebook, checked into the repo** (e.g.
`notebooks/kaggle_worker_template.ipynb`), replacing the bespoke per-worker notebooks
(`iccit-kaggle-worker3.ipynb`/`...worker4.ipynb`) as the thing that actually gets pushed. It keeps
today's boilerplate cells verbatim (query GPU, clone the repo at a pinned commit, build the
matching Miniconda env — cells already proven working in the existing worker notebooks), and
replaces the old "iterate my assigned block of configs" cell with **one placeholder cell**,
identified by a marker comment so substitution is robust to future edits reordering cells:

```python
# DASHBOARD:LAUNCH_SPEC — values below are substituted by backend/kaggle.py before push;
# left as-is, this cell will fail loudly (empty CONFIG_PATH) rather than silently no-op.
CONFIG_PATH = "__DASHBOARD_CONFIG_PATH__"
MODE = "__DASHBOARD_MODE__"            # "train" or "eval"
EXTRA_ARGS = "__DASHBOARD_EXTRA_ARGS__"
```
```python
import subprocess, shlex, sys
script = "train.py" if MODE == "train" else "eval.py"
cmd = [sys.executable, script, "--config", CONFIG_PATH] + (shlex.split(EXTRA_ARGS) if EXTRA_ARGS else [])
subprocess.run(cmd, check=True)
```

This second cell is deliberately the *exact same shape* as `tmux_runner.build_launch_command()`
(`python <script> --config <path> [extra args]`) — the two runners now agree on what "launching a
config" even means, down to the argument order.

**2. `backend/kaggle.py` gains a render step, stdlib only — no Papermill.** Papermill's
tagged-parameter-cell convention was considered (it's the standard tool for this exact problem)
and set aside specifically to honor `IMPLEMENTATION_PLAN.md`'s "minimal dependency footprint"
principle — this needs nothing beyond `json`/`re`, already dashboard dependencies. A new
`_render_launch_notebook(template_path, config_path, mode, extra_args) -> dict` loads the
template's JSON, finds the cell containing the `# DASHBOARD:LAUNCH_SPEC` marker, does a plain
string substitution of the three `__DASHBOARD_*__` tokens (each value put through the same
`shlex.quote`-style escaping `tmux_runner.py` already uses for shell safety, since it ends up
inside a Python string literal that must not break out of its quotes), and returns the modified
notebook dict. `push()` calls this instead of `shutil.copy(notebook_abs, ...)` when the worker is
template-backed.

**3. "Worker" is redefined from a fixed notebook binding to a reusable launch slot.** Today's
`{worker_id, notebook_path, kernel_slug, results_dir, budget_hours}` becomes
`{worker_id, kernel_slug, results_dir, budget_hours, template_path}` (defaulting `template_path`
to the shared template — still overridable per-worker for a genuinely different notebook if one
is ever needed, so nothing about today's "arbitrary notebook per worker" capability is removed,
just no longer the *only* mode). `push(worker_id)` becomes `push(worker_id, config_path, mode,
extra_args)` — the direct Kaggle-side counterpart of `terminals.launch(config_path, mode,
extra_args)`. `add_worker()`'s required fields drop `notebook_path` (now implied by the template
unless overridden).

**4. What this unlocks.** §3.2's Configs launch bar no longer needs the "hand-off assignment
helper, not a real launch" caveat — picking a Kaggle worker in the runner picker becomes a real
one-click launch, identical in shape to picking mclab, differing only in what happens next
(instant tmux session vs. a push that then needs polling) — exactly the parity the rest of this
plan's Runner abstraction (§2.3) was written to expose. `KaggleRunner.capabilities.
direct_launch` flips from `False` to `True`.

**5. Migration.** Existing bespoke worker notebooks (`iccit-kaggle-worker3.ipynb`,
`...worker4.ipynb`) keep working untouched — they're just workers with an explicit
non-default `template_path` pointing at themselves, which the render step leaves alone if it
finds no `# DASHBOARD:LAUNCH_SPEC` marker (falls back to today's verbatim-copy behavior,
degrading honestly rather than erroring on a notebook that predates this convention). New workers
default to the shared template. No forced rewrite of in-flight study notebooks.

### 2.3 Backend shape (new facade, existing modules untouched)

```
dashboard/backend/runners/
  base.py      # RunnerCapabilities, RunUnit, LaunchSpec, CapacitySnapshot dataclasses
               # + Runner ABC: list_units(), launch(spec), status(unit_id), stop(unit_id),
               #   kill(unit_id), restart(unit_id), pull_results(unit_id), capacity()
               # + list_runners() -> [LocalRunner, *KaggleRunner-per-account]
  local.py     # LocalRunner: thin adapter over terminals.py / tmux_runner.py / scheduler.py.
               # capabilities = {direct_launch, live_log, stop, kill, restart, queue}
  kaggle.py    # KaggleRunner: thin adapter over backend/kaggle.py, one instance per account.
               # capabilities = {direct_launch: True (per §2.2's template-notebook render),
               #                 live_log: False, stop: False (confirmed absent, live-verified), kill: False,
               #                 restart: True (=re-render + push), queue: True (=auto_chain),
               #                 budget_metered: True}
```

`local.py`/`kaggle.py` are **facades, not rewrites** — they call the existing, already-tested
functions in `terminals.py`, `tmux_runner.py`, `scheduler.py`, and `backend/kaggle.py` verbatim.
This is deliberate: every existing route (`/api/terminals/*`, `/api/scheduler/*`, `/api/kaggle/*`)
keeps working exactly as today, unchanged, for as long as any old frontend code still calls it.
The facade is what lets new unified views (`/api/experiments/*`, `/api/runners`) exist *alongside*
the old surface during migration instead of requiring a flag-day cutover.

A shared **status vocabulary** normalizes three currently-separate status enums:

- Terminals: `running / completed / failed / stopped / interrupted / unmanaged`
- Scheduler items: `pending / running / cancelling / completed / failed / cancelled / skipped`
- Kaggle workers: `queued / preparing / running / complete / error / cancelAcknowledged / unknown`

into one canonical set — `pending, running, stopping, done, failed, interrupted, cancelled,
skipped, unmanaged, unknown` — via a small per-runner mapping table. This is what lets a single
`badge.js`-style component render status consistently across every view instead of each view
inventing its own color logic (Kaggle's `over_budget` boolean, mclab's `restart_available`
boolean, etc. stay as *extra* flags layered on top of the canonical status, not replaced by it).

New endpoints (additive):
- `GET /api/runners` — every runner (mclab + each Kaggle account) with capability flags + a
  `CapacitySnapshot` (slots used/limit for mclab; budget hours used/limit + weekly estimate for
  each Kaggle account) — powers the new Runners tab and the Overview page's fleet strip.
- `GET /api/experiments/active` — every in-flight unit across every runner, canonical status,
  provenance (`runner_id`) — composes `terminals.list_terminals()` + a per-account
  in-progress-worker read, no new state.
- `GET/POST /api/experiments/queue` — supersedes nothing (Scheduler's routes stay); adds a
  `runner_id` field to the existing scheduler item shape so a queued item can target a Kaggle
  account, using that account's `auto_chain` as its de-facto concurrency-1 queue policy — read
  more in Phase 2.

---

## 3. Target information architecture

7 top-level nav items, down from 11 — 4 are straight consolidations of existing tabs into one
lifecycle view with internal tab-strips; 3 are unchanged; 1 (Overview) is genuinely new and
flagged as such.

```
Overview        <- NEW. Fleet snapshot, replaces nothing.
Configs          [ Browse | Create ]        <- merges "Create Config" nav item in as a sub-tab
Experiments      [ Active | Queue | Runs & Results ]  <- merges Runs + Terminals + Scheduler
                                                          + Kaggle's worker-tracking half
Runners          [ mclab | <account A> | <account B> | ... ]  <- merges Kaggle's account/worker
                                                                  management half + Machine Stats
                                                                  + TensorBoard + Notifications
Reports         <- unchanged
Data            <- unchanged
History         <- unchanged
```

### 3.1 Overview *(new)*

Landing page (replaces Configs as the default view). One fleet-wide glance:

- Runner strip: one compact card per runner (mclab + each Kaggle account) — capacity bar (slots
  or budget-hours), current unit count, a status dot. Click-through to Runners or Experiments.
- Recent activity feed — merges the scheduler `_tick`'s implicit history, the Kaggle poller's
  per-worker `history[]` log, and the notification log into one chronological stream. (Today
  these three histories exist but are only visible in three different places: a worker's own
  history array, the toast stack which forgets itself after a few seconds, and nowhere for the
  scheduler.)
- The existing topbar telemetry (pulse dot, RUNNING/RESTART/DONE/FAILED pills) stays in the
  topbar, visible from every tab as today — Overview doesn't duplicate it, just gives it a home
  page to sit above.

This is the one net-new surface in this plan; everything else below is reorganization of what
already exists. **Default recommendation: build it, but it's the easiest single phase to cut**
if you'd rather ship the consolidation first and decide later whether a landing page earns its
keep — see Phase 4.

### 3.2 Configs `[ Browse | Create ]`

Same panel, same CodeMirror editor, same resolved-config/schema validation, same
`Duplicate/diff` actions — unchanged. Two changes:

1. **Create Config** stops being its own nav item and becomes a second tab-strip entry at the
   top of this view (`Browse` / `Create`) — it's the same resource (a config file) from two
   entry points, not a different lifecycle stage. All of today's Create Config behavior (template
   loading, schema-aware field types, model picker, live YAML preview, save) is untouched.
2. **The launch bar gains a runner picker.** Today's single "Launch in terminal ▸" button
   assumes mclab. It becomes:
   - **Run now on:** `[mclab ▾]`, listing mclab plus every Kaggle worker that isn't currently
     in-flight (`kaggle: <account>/<worker>`) — thanks to §2.2's template-notebook redesign, both
     are now real, symmetric, one-click launches of *this exact config* with *this exact mode and
     extra args*: mclab opens a tmux session immediately; a Kaggle worker renders the template
     (§2.2 step 2) and pushes it immediately. Both jump to Experiments → Active on success. The
     only remaining difference the UI needs to surface is what happens *after* launch (mclab:
     live pane right away; Kaggle: status polling only, per §2.1's still-true monitoring row) —
     not whether launching itself works.
   - A second button, **"Add to queue ▸"**, opens the same add-to-queue affordance Scheduler has
     today (mode, extra args) but now also asks *which runner* — for mclab this is identical to
     today's Scheduler; for a Kaggle worker it queues the rendered push (mirrors today's
     `auto_chain`, now operating on real per-config renders instead of a static notebook).

### 3.3 Experiments `[ Active | Queue | Runs & Results ]`

The centerpiece — replaces Runs, Terminals, and Scheduler outright, and absorbs the tracking
half (not the credential/setup half) of the Kaggle tab.

**Active** *(replaces Terminals' live-session list + Kaggle's in-progress worker cards + today's
Scheduler "Running" section — all three are "what's executing right now," unified)*
- One card format for every in-flight unit, each tagged with a runner badge (`mclab` /
  `kaggle · <account>`).
- mclab cards: identical to today's Terminals — full scrollable pane, live metrics chart parsed
  from log lines, **Stop** (Ctrl-C) / **Kill** (with confirmation + final snapshot), bulk
  multi-select stop/kill (new — today's Terminals only supports one at a time), unchanged
  reboot-resilience (Interrupted + Restart for anything the dashboard recorded but tmux lost).
- Kaggle cards: status (`queued/preparing/running`), elapsed vs. `budget_hours` with the existing
  `over_budget` flag, last-checked time, **Refresh status** (today's per-worker `refresh_status`)
  — no fake log tail, no Stop button (confirmed absent from the CLI, live-verified — see §2.1).
- Unmanaged tmux sessions still show up read-only exactly as today (Terminals' existing
  behavior), clearly separated from dashboard-managed units.

**Queue** *(replaces Scheduler's Scheduled/Past sections + is the exposed control surface for
Kaggle's `auto_chain`)*
- mclab side: identical to today's Scheduler — concurrency stepper, reorder (▲/▼), templates,
  bulk-add-by-category, pause/resume, notify-on-finish toggle, cancel/remove, Train+Eval chaining
  with skip-on-failure. All unchanged, just living under this tab instead of its own nav item.
- Kaggle side: each account's worker list becomes a mini-queue view — `auto_chain` on/off is
  literally the same "keep launching the next queued thing automatically" concept the mclab side
  already has as `paused`/`max_concurrent`; presenting both with the same visual language
  (a queue list + a toggle) makes that equivalence legible instead of it being two unrelated
  settings in two unrelated tabs.
- **Past** section merges Scheduler's past-items history with each Kaggle worker's own
  `history[]` log (today only visible per-worker, buried in the Kaggle tab).

**Runs & Results** *(= today's Runs tab, verbatim, plus explicit Kaggle provenance)*
- Config-hash-grouped seed×fold grid, run-detail panel (manifest, git commit + dirty-tree badge,
  hardware, env hash, timing, `gpu_hours`, non-determinism flags, run notes/tags via
  `run_notes.py`, "Reproduce this run," "Copy resolved config"), Compute-hours strip, compare
  mode, CSV/JSON export — **entirely unchanged**, this sub-view *is* today's Runs tab.
- New: a run's provenance line shows `mclab @ <commit>` or `kaggle · <account>/<worker> ·
  downloaded <time>` (manifest already carries what's needed; this is a rendering addition, not
  a new data source).
- New: any run known to exist on Kaggle (via that worker's last `refresh_status`) but not yet
  downloaded shows a **Pull results ▸** action right on its (currently-empty) grid cell, instead
  of requiring a trip to the Kaggle tab's "Download all finished" button with no per-run
  visibility into what that will fetch.

### 3.4 Runners `[ mclab | <account> | ... ]`

New tab — the fleet-management layer, replacing the Kaggle tab's account/worker *configuration*
chrome and absorbing Machine Stats, TensorBoard, and the notification settings that are
currently split between the Kaggle tab and Scheduler.

- **mclab card**: repo root / python executable / `env_activate_cmd` (today buried in the sidebar
  footer's tiny text and the README — given a real settings-visible home here), tmux
  availability, current concurrency usage (slots used/limit — same number Scheduler already
  computes, just shown as this runner's capacity here too), and two embedded panels:
  - **Machine Stats**, verbatim — the monitor-command list (`nvidia-smi`/`htop`/`nvtop`/`free
    -h`/`df -h` + add-your-own), start/stop, auto-opening output — unchanged, just scoped under
    "this is mclab's own live telemetry" instead of a same-level sibling tab.
  - **TensorBoard**, verbatim — start/stop/open-in-new-tab against `runs/` — unchanged, folded in
    as a launch button + status chip on the mclab card instead of its own nav item (it's
    inherently mclab-local: it reads the local `runs/` directory the same way Machine Stats reads
    local processes).
- **One card per Kaggle account** (today's "Accounts" panel, essentially unchanged): credentials
  (add/rotate/remove legacy key or token, exactly as today's validation rules — reject a
  new-format token pasted into the classic-key field, etc.), rename, the account's worker list
  (add/remove worker: notebook path, kernel slug, results dir, budget hours — unchanged), usage
  sparkline + weekly-budget bar (`usage_history`/`estimate_usage`, unchanged), auto-chain toggle,
  registry export/import (unchanged, still credential-free by design).
- **Notifications** panel (shared, bottom of this tab): the 5-channel config
  (Telegram/Discord/Slack/email/ntfy) that today lives only inside the Kaggle tab, with its
  test-send buttons — unchanged backend (`notifications.py` already channel-agnostic), just given
  one home instead of being Kaggle-tab-only while Scheduler's `notify_on_finish` toggle points at
  the same settings from a different tab with no visible connection between the two.
- This is also the natural extension point if a third runner (another SSH-reachable lab box) is
  ever added — the reason for naming this tab "Runners" now instead of leaving it "Kaggle" is to
  make that extension point real rather than requiring a second bolt-on tab later.

### 3.5 Reports, Data, History — unchanged

All three stay exactly as they are today, including every listed README behavior (metric cards
with HD95/ASD/NSD exclusion counts, Dice percentile band, radar chart, compare mode with
config-diff and per-report color coding; dataset cards + channel-mode montage + test-eval audit
trail; the Logs/Images source-switching file browser). The only change is that **entry points
elsewhere now link into them** — a run card in Experiments → Runs & Results links straight to its
Reports entry instead of the user having to separately find it by filename in a parallel tree.

---

## 4. Nav count and rationale for what's *not* folded further

Reports, Data, and History could technically nest under Experiments/Configs too, but each is a
genuinely different *task shape*, not just a different data source, so folding them would blur
rather than clarify:
- **Reports** is a deep-dive/compare tool (its own multi-select + radar + diff workflow) — worth
  a first-class tab the same way it already is.
- **Data** is dataset/governance-facing (channel montage previews, the guarded test-set audit
  trail) — read by a different mental mode than "is my run healthy," even though it's
  config-adjacent.
- **History** is deliberately the *unstructured* escape hatch (a raw file browser, explicitly not
  experiment-aware per its own README description) — folding it into the highly-structured
  Experiments tab would misrepresent what it is.

If you'd rather consolidate further (e.g. Data as a sub-tab of Configs, History as a link from
Runners/Experiments instead of top-level), that's a straightforward variant of this same plan —
flagging it here as a legitimate alternative rather than deciding it unilaterally.

---

## 5. Capability preservation map (old → new, nothing dropped)

| Today | Where it lives after this plan |
|---|---|
| Configs: browse/edit/save/resolve/schema-validate | Configs → Browse (unchanged) |
| Create Config: template load, schema-aware fields, model picker, live preview, save | Configs → Create (unchanged) |
| "Launch in terminal ▸" | Configs → Browse's launch bar → **Run now on: mclab** (unchanged behavior for mclab; new: same button now also directly launches a picked Kaggle worker, per §2.2, or queues either) |
| Config diff & "Duplicate as new experiment" | Configs → Browse (unchanged) |
| Runs tab: config-hash groups, seed×fold grid, run detail, compute-hours strip, compare, CSV/JSON export | Experiments → Runs & Results (unchanged) |
| Run notes/tags (`run_notes.py`) | Experiments → Runs & Results run detail (unchanged) |
| Terminals: live pane, metrics chart, stop/kill, restart, reboot-resilience, unmanaged sessions | Experiments → Active (unchanged, mclab cards) |
| Terminals: single-session-at-a-time stop/kill | Experiments → Active gains **bulk** stop/kill on top (net addition, nothing removed) |
| Scheduler: add/mode/extra-args, concurrency stepper, reorder, templates, bulk-add-by-category, pause, notify-on-finish, Train+Eval chaining | Experiments → Queue (unchanged, mclab side) |
| Kaggle: accounts (add/credentials/rename/validate), workers (add/remove), push/status/download, push-all/refresh-all/download-all | Runners → account cards (config/credential parts) + Experiments → Active/Queue/Runs&Results (execution-tracking parts) |
| Kaggle: usage estimate + weekly sparkline, budget hours, over-budget flag | Runners → account card capacity bar |
| Kaggle: auto-chain | Experiments → Queue, Kaggle side (same setting, same effect, unified presentation) |
| Kaggle: registry export/import | Runners → account card (unchanged) |
| Kaggle: server-side notifications + browser notify toggle | Runners → Notifications panel (unchanged backend; Scheduler's own `notify_on_finish` now visibly points at the same settings) |
| Reports: stat cards w/ exclusion counts, percentile band, radar, compare, config-diff | Reports (unchanged) |
| Data: dataset cards, channel-mode montage, test-eval audit trail | Data (unchanged) |
| History: Logs/Images source switch, file tree, inline preview | History (unchanged) |
| Machine Stats: monitor list, add-your-own, auto-opening output | Runners → mclab card (unchanged) |
| TensorBoard: start/stop/open | Runners → mclab card (unchanged) |
| Command palette (Ctrl/⌘K) | Unchanged — extend its index to the new Overview/Runners tabs, same mechanism |
| Mobile responsive stacking (~860px breakpoint) | Carried forward to every new/merged view, same breakpoint |
| API token / Origin guard on every mutating route | Unchanged — new routes are normal Flask routes under the same `before_request` guard, same as `IMPLEMENTATION_PLAN.md` principle 5 already required |

No row in this table has an empty "after" column — that's the acceptance bar for this plan.

---

## 6. Frontend structure

Following the *actual* current convention (plain classic `<script>` tags sharing one global
scope — `app.js`, `js/components/badge.js`, `js/components/heatmap-grid.js`, `js/views/{runs,
data,kaggle}.js` — **not** the ES-module split `IMPLEMENTATION_PLAN.md` once proposed and never
executed):

```
static/js/
  core/
    runner-status.js     # canonical status enum + per-runner-kind mapping table + badge class
                          # (extends badge.js's existing STATUS_BADGE_CLASS map, doesn't replace it)
  views/
    overview.js           # new
    experiments-active.js # supersedes app.js's terminal-rendering functions + kaggle.js's
                           # in-progress-worker cards, reusing wireTerminalCard/renderTerminalDetail
                           # bodies rather than rewriting them
    experiments-queue.js  # supersedes app.js's scheduler-rendering functions + kaggle.js's
                           # auto-chain/worker-queue rendering
    experiments-runs.js   # = today's runs.js, renamed, logic untouched
    runners.js            # supersedes kaggle.js's account/worker *management* cards
                           # (credentials/rename/registry) + app.js's monitor + tensorboard
                           # functions, reusing their bodies
    configs.js             # split out of app.js's config-tree/editor/creator functions
                            # (mechanical extraction, no behavior change — same spirit as
                            # IMPLEMENTATION_PLAN.md Phase 7's proposed app.js split, scoped down
                            # to what this plan actually touches instead of a full rewrite)
```

`app.js` itself shrinks to boot/nav/shared-utility code (`api()`, `toast()`, `state`,
`showConfirm()`, telemetry polling) — the same role `IMPLEMENTATION_PLAN.md` Phase 7 already
described for a `core.js`, just named to match what's already there. This is a mechanical
extraction pass, not a rewrite: every function keeps its current body, only its file and (where a
tab merges) its DOM container IDs change.

`index.html`'s nav block goes from 11 `<div class="nav-item">` entries to 7, each `view-*`
`<section>` gaining internal tab-strips (a small new `.subtab` component, styled from the
existing `--border/--surface` tokens in `styles.css`, not a new palette) where a section merges
multiple former tabs.

---

## 7. Phased rollout

Each phase ships independently and leaves the dashboard fully usable — no phase requires the
next one to be safe to ship, matching this project's own "degrade honestly, ship incrementally"
convention from `IMPLEMENTATION_PLAN.md`.

**Phases 0-5 status: implemented and live-tested (2026-09), including the runner picker.**
Deviation from the plan as written, noted here rather than silently: Kaggle's worker cards
(push/refresh/download + credentials) were **not** split between Runners (management) and
Experiments (tracking) as §3.3/§3.4 originally described — kept as one cohesive block on Runners
(lower regression risk on a tool driving real experiments), with Experiments → Active instead
gaining a lightweight *read-only* "also running on Kaggle" summary card sourced from
`/api/experiments/active`, so Active still answers "what's executing right now across every
runner" without a second full control surface.

Configs' launch bar now has the `RUN ON` picker: `mclab` plus every template-backed Kaggle worker
(notebook-backed/legacy workers are deliberately excluded from this picker — they ignore
`config_path` entirely, so "launch *this* config there" isn't a meaningful action for them; they
keep their existing push button on Runners). Picking mclab still posts to `/api/terminals`
unchanged; picking a Kaggle worker posts to `POST /api/runners/kaggle:<account>/launch` with
`target=<worker_id>`, added in this pass — the same one-click symmetry §3.2 called for. Live
testing this route surfaced and fixed two real bugs before they could reach a user: a missing
`tmux.TmuxError`/`FileNotFoundError` catch (raw 500 instead of a clean 400/404, since the new
unified route hadn't mirrored `/api/terminals`'s existing pre-check+catch pattern), and a
double-repr'd error message on an unknown runner id (`KeyError.__str__` re-`repr()`s its own
message; fixed by reading `.args[0]` instead of `str(e)`).

**Phase 0 — Runner facade + Kaggle template-notebook launch (backend only, zero UI change)**
Build `backend/runners/{base,local,kaggle}.py` and the new read-only `/api/runners`,
`/api/experiments/active` endpoints, purely additive. Also lands §2.2's redesign: author
`notebooks/kaggle_worker_template.ipynb`, add `_render_launch_notebook()` to `backend/kaggle.py`,
change `push()`'s signature to accept `config_path`/`mode`/`extra_args` and render-before-copy,
update `add_worker()`'s field set (`template_path` replaces required `notebook_path`). Exit
criterion: existing UI is byte-for-byte unaffected; the new endpoints return correct data
cross-checked against `/api/terminals`, `/api/kaggle/accounts`, `/api/scheduler` for the same
live state; a manual `push()` call against the new template with a real config renders a notebook
whose launch cell, once run, produces the identical `python train.py --config ... ` invocation
`tmux_runner.build_launch_command()` would have produced for the same inputs on mclab.

**Phase 1 — Runners tab**
Ship the new nav item; migrate Kaggle account/worker *management* cards + Machine Stats +
TensorBoard + Notifications into it (all reusing existing render functions per §6); remove the
now-redundant Kaggle/Machine Stats/TensorBoard nav items. (The Kaggle-CLI stop/cancel fact-check
this phase originally gated on is already done — §2.1 — confirmed absent, live-verified against
the actually-installed CLI, not just its docs.)

**Phase 2 — Experiments tab**
Ship Active/Queue/Runs & Results as one nav item with the sub-tab strip; migrate Terminals +
Scheduler + Runs wholesale (reusing their render functions); migrate the Kaggle tab's
in-progress-worker tracking (not its account management, already moved in Phase 1) into Active
and Queue; remove the Terminals/Scheduler/Runs/Kaggle nav items — Kaggle as a standalone tab is
now fully retired (its two halves live in Runners and Experiments).

**Phase 3 — Configs runner picker + Create Config merge**
Add the `[Browse | Create]` sub-tab strip to Configs; remove the separate Create Config nav item.
Add the runner picker to the launch bar — both mclab and any Kaggle worker are now real,
symmetric one-click launches per §2.2/§3.2, no hand-off caveat needed.

**Phase 4 — Overview** *(optional — see §3.1 for the explicit call-out that this is the one
net-new surface; cut this phase without affecting anything else if a landing page turns out not
to earn its keep once Phases 0-3 are live)*

**Phase 5 — Status-vocabulary verification pass**
Confirm every view (Active, Queue, Runs & Results, Runners' capacity bars) renders through the
one canonical status enum from §2.3, not a leftover per-view special case; confirm the command
palette indexes the new tab structure; mobile responsive pass at the existing ~860px breakpoint
for every merged/new view, matching `README.md`'s already-documented behavior.

**Phase 6 — Google Colab as a third runner** *(research complete — see §8 below for findings and
proposed design; go/no-go and sequencing is an open decision, not committed work)*

**Phase 7 — (stretch, explicitly outside the literal ask, flagged for a separate decision)**
`experiment_status.csv`'s `worker` column (`w3`, `mclab`, `"w1, mclab"`) is real evidence of a
config × seed → runner **assignment-planning** need this redesign doesn't otherwise address — the
question of *which* runner *should* run a not-yet-launched config, across a whole block/study, is
different from "what is currently running" (Experiments → Active) or "what has run"
(Runs & Results). A lightweight assignment board (config × seed → runner, editable, exportable)
would close that gap and retire the hand-maintained CSV, but it's net-new scope beyond
"reorganize and unify what exists" — worth a separate go/no-go decision rather than bundling it
silently into this plan.

---

## 8. Google Colab as a third runner (research findings + proposed design)

*(Addition per user request: research whether Colab can join the same runner structure as the
now-templated Kaggle model.)*

### 8.1 Two different products share the "Colab" name — this matters

**Colab Enterprise** is a paid GCP/Vertex AI product (billed through a Google Cloud project,
provisioned via `gcloud colab executions create` / a `NotebookServiceClient` Python SDK,
runtimes billed as Compute Engine usage). It has a real, mature execution API
(`notebookExecutionJobs`, scheduled runs) — but it is **not** the free/Pro/Pro+ product at
`colab.research.google.com` this project already uses; adopting it means standing up a billed GCP
project, IAM service accounts, and Cloud Storage output buckets. Flagged for completeness, not
recommended as the integration path below.

**Consumer Google Colab** (free/Pro/Pro+, the actual product in use) is the relevant one, and as
of June 2026 Google shipped an **official, first-party CLI for exactly this** — `googlecolab/
google-colab-cli` on GitHub (Apache 2.0, announced on the Google Developers Blog alongside a
"Colab MCP Server" for AI-agent control), explicitly built for "developers and AI agents," not
just interactive notebook use.

### 8.2 Why this CLI is a *better* launch-granularity fit than Kaggle, not just a parallel option

`colab run <script.py> [SCRIPT_ARGS...] --gpu {T4,L4,G4,H100,A100} --timeout <secs>` takes a
**local `.py` file + argv directly** and executes it on a freshly-provisioned remote Colab
runtime — this is already the exact shape of `python train.py --config X [extra args]`, no
notebook, no template, no placeholder substitution needed at all. Where Kaggle's execution unit
is fundamentally a notebook (which is why §2.2 had to build a template+placeholder mechanism to
get it to config-granularity), Colab's new CLI is a script runner from the start — closer to
mclab's own model than Kaggle even after §2.2.

Concretely, a `ColabRunner` can be built as a thin variant of `LocalRunner` rather than a Kaggle-
style push/poll/download cycle: run `colab run --gpu <type> --timeout <secs> -- <script> --config
<path> [extra args]` **inside a dashboard-launched tmux session**, exactly like today's mclab
launches, and get the same live `tmux capture-pane` tail for free — because it's a real foreground
process, not a remote status poll. This is the one part of this addition worth flagging as
unverified rather than assumed: whether `colab run`'s own stdout streams incrementally (so the
tmux pane fills in live) or only prints in a batch at the end wasn't confirmed by the documentation
fetched during this research pass — first thing to check empirically in any Phase 6 spike.

### 8.3 Where Colab's constraints genuinely differ from both existing runners

- **A local keep-alive daemon is required, and it self-expires.** `colab new`/`colab run` spawns
  a *detached background process on the machine running the CLI* (i.e. on mclab) that pings
  `colab.research.google.com/tun/m/<endpoint>/keep-alive/` every 60 seconds to stop the remote
  runtime idling out; that daemon "automatically terminates after 24 hours to prevent permanent
  zombie processes." Two consequences worth designing around, not glossing over: (a) a training
  run longer than 24 hours will lose its remote runtime when the local daemon self-expires,
  regardless of training progress — Kaggle has no equivalent ceiling (the kernel executes fully
  server-side); (b) if mclab itself reboots, the daemon dies with it, and — unlike a Kaggle
  push, which keeps running on Kaggle's infra untouched by anything happening on mclab — the
  remote Colab runtime is very likely to time out shortly after, **losing training progress**,
  not just the dashboard's view of it. This is a materially worse reboot story than either
  existing runner and should be called out plainly to the user rather than silently inherited.
- **Free-tier automation sits in a ToS gray area the paid tier does not.** The official Colab FAQ
  (`research.google.com/colaboratory/faq.html`) lists "remote control such as SSH shells, remote
  desktops" and "bypassing the notebook UI to interact primarily via a web UI" as disallowed on
  *free* managed runtimes, and states these restrictions are lifted with a paid plan (Pro/Pro+)
  and a positive compute-unit balance. Google's own new CLI is squarely a "bypass the notebook UI"
  tool by that literal wording — plausibly the FAQ simply predates the CLI's June 2026 launch and
  the CLI is the now-sanctioned exception, but that wasn't independently confirmed in this
  research pass. **Recommendation: treat "Colab runner requires a paid Pro/Pro+ account" as a
  working assumption, and treat "confirm the FAQ has been reconciled with the official CLI" as a
  mandatory Phase 6 fact-check** before pointing any free-tier account at this.
  Sources: [Colab FAQ](https://research.google.com/colaboratory/faq.html), [Colab CLI announcement](https://developers.googleblog.com/introducing-the-google-colab-cli/), [Colab CLI repo](https://github.com/googlecolab/google-colab-cli).
- **Concurrency is not simply "unlimited."** The CLI's own session-tracking supports multiple
  concurrent `colab run`/`colab new` sessions, but that's the client tooling, not Google's backend
  policy — Colab has historically capped concurrent connected runtimes per account (tighter on
  free tier, looser on Pro/Pro+), and nothing in the CLI's design overrides that. Model this the
  same way `KaggleRunner` models Kaggle's ~1-concurrent ceiling: a capacity ceiling reported by
  `capacity()`, not an assumption of N-way parallelism.
- **Results land on the remote VM's disk, not mclab's**, same shape as Kaggle: an explicit pull
  step (`colab download`) is needed before a run's artifacts are visible to the rest of the
  dashboard, reusing `register_ledger()` verbatim (it already only cares about finding
  `artifacts/runs/*/manifest.json` under a results dir — Colab or Kaggle-sourced, same code).
- **No credential-store parallel needed.** Unlike Kaggle (per-account API keys), auth is
  `gcloud auth application-default login --scopes=...,https://www.googleapis.com/auth/colaboratory`
  or an OAuth2 device-code flow — one Google account login per runner instance, stored the same
  way the CLI itself stores it (`~/.config/colab-cli/sessions.json` on mclab), not a dashboard-
  owned secret file the way `data/kaggle_accounts/<name>/kaggle.json` is today. `Runners` tab
  cards would show connection status, not a credential-entry form.

### 8.4 Proposed `ColabRunner` shape, if greenlit

Given §8.2, this is deliberately **not** a second copy of §2.2's template-notebook mechanism —
Colab doesn't need one. `backend/runners/colab.py` wraps: a launch-command builder mirroring
`tmux_runner.build_launch_command()` but prefixing `colab run --gpu <configured type> --timeout
<budget_hours*3600> --`; the existing `tmux_runner.py`/`terminals.py` machinery for the actual
tmux session (live pane, stop via Ctrl-C — note: Ctrl-C stops the local `colab run` foreground
process, and per §8.3 the detached keep-alive daemon plus remote runtime need their own explicit
teardown, likely `colab stop -s <session>`, to actually release the remote resource rather than
leaving it billing/idling — a real gap to close, not assume away); and a `pull_results()` calling
`colab download`. Capability flags: `{direct_launch: True, live_log: <TBD Phase 6 spike>, stop:
True (local process) / needs verification (remote teardown), kill: True, restart: True, queue:
True (reuses Scheduler verbatim, runner_id="colab"), budget_metered: True}` — closer to
`LocalRunner`'s capability profile than `KaggleRunner`'s, which is itself a useful signal that
Colab, if adopted, slots in as "mclab with a remote GPU and a results-pull step" rather than a
third fully-distinct paradigm.

**One important consequence for §2-§7 above: if Colab is adopted, its default `--timeout 30`
(seconds) is a live footgun** — any launch-command builder for it must always pass an explicit,
generous `--timeout` derived from the run's expected duration (e.g. from `budget_hours`, mirroring
how Kaggle workers already carry one), never rely on the CLI's own default, or a real multi-hour
training run gets killed at 30 seconds with no warning.

### 8.5 Recommendation

Worth pursuing, with the Phase 6 fact-checks (stdout streaming behavior, ToS/paid-tier
requirement, remote-teardown command) resolved *before* committing to it in earnest — the
upside (config-granular launch parity with mclab, no notebook templating needed, wider
accelerator choice than Kaggle's fixed weekly quota) is real, but so is the 24-hour local-daemon
reboot fragility in §8.3, which is a genuinely new failure mode this project's other two runners
don't have. Treat as a small, time-boxed spike (verify the four open questions above against a
real account) before deciding whether it becomes a committed phase.

---

## 9. Open decisions (defaults chosen, flag if you want something else)

1. **Overview tab** — default: build it (Phase 4), but it's the one phase you can drop with zero
   knock-on effect if you'd rather stop at the consolidation (Phases 0-3+5).
2. **Kaggle template notebook authorship (§2.2)** — default: assume you (or whoever currently
   hand-writes worker notebooks like `iccit-kaggle-worker3.ipynb`) author
   `notebooks/kaggle_worker_template.ipynb` once, since its boilerplate cells (clone repo, build
   the pinned Miniconda env) are already proven in the existing worker notebooks and just need
   the multi-config iteration cell swapped for the single placeholder cell — flag if you'd rather
   this plan's implementation phase draft that notebook for your review instead.
3. **Colab (§8)** — default: **not** committed; run the Phase 6 spike (four fact-checks in §8.3/
   §8.5) before deciding go/no-go, given the reboot-fragility finding is a genuine new risk, not
   just an unknown.
4. **Phase 7 (assignment board / CSV retirement)** — default: **not** included in this plan's
   core scope; flagged only because real usage already needs it. Separate ask if wanted.
5. **Further nav consolidation (§4)** — default: keep Reports/Data/History top-level as argued;
   revisit only if the 7-item nav still feels crowded once built.
