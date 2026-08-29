# Implementation Plan: Dashboard v2 — feature-rich, visual, framework-current

## Context

This dashboard (`dashboard/`) is a separately developed, separately versioned tool (its own
git repo, gitignored from the main `dissert` repo — confirmed via `git log` here showing 5
commits of its own history) that sits alongside the `dissert` research framework rather than
inside it. Its stated design contract (`README.md`, `server.py`'s module docstring) is:

- **Drop-in / drop-out**: copy the whole folder into any repo with a `configs/ logs/ runs/
  checkpoints/ train.py eval.py` layout, delete it again, leave no trace.
- **Deliberately minimal dependency chain**: `requirements.txt` is Flask + Werkzeug + PyYAML +
  tensorboard — nothing else. No pydantic, no torch, no pandas/pyarrow. This is explicit and
  load-bearing (`server.py:5-8`): it's what lets the dashboard run under an old Python 3.8 env
  without fighting version conflicts with whatever the host repo's ML stack needs.
- **tmux is the source of truth for execution**; the dashboard mostly reads, rarely holds
  state. The one exception (a background thread) is the Scheduler.
- **No build step**: vanilla HTML/CSS/JS, CDN-loaded libraries (CodeMirror, js-yaml, Chart.js),
  no bundler, no framework.

`dissert`'s own `IMPLEMENTATION_PLAN.md` independently confirmed this decoupling and
deliberately preserved it: *"Dashboard is fully decoupled ... none of this plan's new
dependencies or the config schema validator touch its separate Python-3.8 venv."* Six phases of
that plan have since shipped (`CHANGELOG.md`, 2026-08-27/28): a schema-validated, run-hashed,
manifest-and-ledger-tracked orchestration layer; a canonical `metrics/` package; a retrofitted
data layer with leakage guards and a guarded test loader; a channel-construction module; a
hardened model registry with capacity control; and a new Mamba/VSS model family. **None of this
is visible anywhere in the dashboard today** — it still tracks jobs purely via tmux + regex log
parsing, reads reports purely as "any JSON with a `metrics` key," and has no concept of a
run ID, config hash, manifest, ledger, leakage guard, test token, or model registry.

This plan closes that gap and, per the user's ask, makes the dashboard **feature-rich, visually
rich, and full of things you can actually do** — not just a config editor with a terminal
attached. It explicitly **preserves the drop-in/no-build-step/minimal-dependency contract**;
every feature that needs the host repo's heavier Python stack (pydantic schema, model registry,
Parquet, channel construction) goes through a new **bridge** layer (Phase 1) rather than adding
those dependencies to the dashboard's own `requirements.txt`.

## Design principles (apply throughout, not a separate phase)

1. **Preserve the existing visual identity, extend it.** `styles.css`'s "lab-instrumentation"
   dark theme (ink panels, hairline borders, amber/teal telemetry accents, JetBrains Mono for
   live numbers) is a real, deliberate design language, not a placeholder. New views reuse its
   token system (`--bg/--surface/--border/--amber/--teal/--emerald/--violet/--blue/--red`); new
   chart palettes derive from those same tokens rather than introducing a second palette.
2. **No build step, ever.** New frontend code is added as additional `<script type="module">`
   files under `static/js/` (native ES modules — no bundler needed, works today in every
   evergreen browser) rather than one continuously-growing `app.js`. New third-party JS is
   CDN-`<script>`-tag only, same as CodeMirror/js-yaml/Chart.js today.
3. **The dashboard's own Python dependency footprint stays minimal.** Reading `artifacts/runs/
   */manifest.json` and `artifacts/ledger/*.csv` needs nothing beyond stdlib `json`/`csv` — do
   that directly. Anything that needs the host repo's actual code (pydantic schema, model
   registry, Parquet, `datasets/channels.py`) goes through the Phase 1 bridge subprocess, never
   a new pinned import in `dashboard/requirements.txt`.
4. **Degrade honestly, don't crash.** A repo dropped in without the new framework layout (no
   `artifacts/`, no `orchestration/` package, older `train.py`) must still run the dashboard's
   existing features exactly as today. Every new view feature-detects its data source and shows
   a clear "not available in this repo" state instead of a stack trace.
5. **Every new mutating endpoint inherits the existing security guard.** `server.py`'s
   `_guard_mutating_requests` (API token + Origin check, from the C6-C8 fixes in
   `CODE_REVIEW.md`) already wraps every route via `before_request` — new routes get it for
   free as long as they're registered normally, but subprocess-invoking routes (the bridge) need
   their own extra scrutiny (see Phase 1's shell-safety note).

---

## Gap analysis: framework capability → dashboard today → planned

| Framework capability (CHANGELOG phase) | Dashboard today | Planned |
|---|---|---|
| Run ID / config hash / manifest (`orchestration/manifest.py`, `runid.py`) | Jobs identified only by a random tmux session name; no way to tell which runs share a config | **Phase 1/3**: Run Registry keyed by `run_id`, grouped by `config_hash` |
| Ledger (`artifacts/ledger/{runs,compute,test_evals,stats}.csv`) | Not read at all | **Phase 1/3/6**: Runs table view, Compute/GPU-hours view, Test-Evals audit trail |
| Config schema validation (`orchestration/schema.py`, pydantic) | Configs editor validates only "is this parseable YAML," not "is this a valid config" — a bad value only fails when `train.py` starts | **Phase 2**: inline schema validation + resolved-config preview before launch |
| `compose:` config layering (`configs/base.yaml` + `dataset/model/train` fragments) | Configs tree shows raw files; a fragment's *effective* merged values are invisible in the UI | **Phase 2**: "Resolved config" pane showing the fully composed + validated dict |
| Canonical metrics (`metrics/aggregate.py`): NSD, `dice_p5/p25`, `hd95_excluded_n`/`asd_excluded_n`, ECE, `fpr_on_normals`, `specificity_lesion_free` | Reports tab shows every key under `metrics` as a generic stat card — technically forward-compatible, but no percentile bands, no exclusion-count callout, no per-image distribution | **Phase 4**: metrics-aware report view with these fields rendered meaningfully, not just listed |
| Per-image Parquet (`metrics/aggregate.py:write_per_image_parquet`) | Not read (and not yet called by `eval.py` either — see Phase 4's dependency note) | **Phase 4**: per-image box/violin plots, once wired |
| Leakage guards / test-token ledger (`datasets/splits.py`, `Test_Evals` ledger — already has 4 real rows on disk) | Invisible | **Phase 6**: Test-Evals audit view (which runs touched the guarded test set, when, with what token) |
| `duplicate_cross_check()` finding (35/379 ColonDB images, 9.2%, cross-split duplicates — a real finding in `CHANGELOG.md` Phase 3) | No way to see this without reading the changelog | **Phase 5**: Data Studio surfaces dataset manifest + duplicate report per dataset |
| Channel modes m1–m5 (`datasets/channels.py`) | Invisible; no config UI hints these exist beyond a bare string field | **Phase 2 (schema-aware form) + Phase 5 (channel montage preview)** |
| Model registry + capacity control (`models/build.py`, `models/registry.py`'s budget guard) | Create Config is a blind key-value form with no model-aware validation | **Phase 2**: model picker driven by the actual registry, with param/FLOPs shown before launch |
| Mamba/VSS family + `scan_impl` manifest field | Invisible | **Phase 3**: run detail shows which scan implementation actually ran (`ss2d` fused vs `ss2d_ref` fallback) and whether non-determinism was recorded |
| `orchestration.runner.run_sweep()` (seed×fold expansion, idempotent skip) | Dashboard can only launch one train/eval at a time; no multi-seed/fold sweep UI | **Phase 6**: Sweep Launcher (needs a small framework-side CLI shim — flagged as a cross-repo dependency, not silently assumed) |
| Future S7–S16 artifacts (stats, attribution, uncertainty, robustness, profiling — Phases 9-14, not yet built) | N/A | **Phase 9**: scaffolded now against the spec's §20 artifact table so these views activate the moment the JSON exists, instead of being built from scratch later |

---

## Phase 0 — Housekeeping

**Goal:** clear the ground before adding anything.

- Confirm with the user whether `exp_dashboard/` (byte-identical backend, per `CODE_REVIEW.md`
  L1, plus a leftover nested `.git`) should be deleted now that `dashboard/` is the actively
  developed copy (later mtimes, its own 5-commit git history, this plan living inside it). **Do
  not delete unilaterally — this is a destructive action outside this plan's scope**; flag it
  and let the user confirm, per L1's own recommendation.
- Add `artifacts_dir` (default `"artifacts"`) to `dashboard_config.yaml`/`backend/config.py`,
  resolved the same way `configs_dir`/`logs_dir` already are — every phase below reads under it.
- Add `bridge_python_executable` to `dashboard_config.yaml`, defaulting to the existing
  `python_executable` value — the interpreter the bridge (Phase 1) subprocess-invokes. Kept as a
  separate key because a real deployment might launch training with one env but want bridge
  calls (schema export, registry introspection) against a different/lighter env; defaulting to
  the same value means zero config changes for the common case.
- Confirm `dashboard/requirements.txt` stays untouched by this entire plan (design principle 3)
  — add a short comment at its top pointing at this file's Phase 1, so a future contributor
  doesn't casually `pip install pandas` into it.

**Exit criterion:** dashboard still starts and behaves identically to today with the new config
keys present but unused.

---

## Phase 1 — The bridge: reading the host repo's real state without adopting its dependencies

**Goal:** every later phase that needs pydantic schema, the model registry, Parquet, or
`datasets/channels.py` goes through one well-guarded mechanism, built once.

- **New `backend/ledger.py`** (stdlib only — `csv`, `json`, `pathlib`): readers for
  `artifacts/ledger/runs.csv`, `compute.csv`, `test_evals.csv`, `stats.csv` (whichever exist —
  each is independently optional) and `artifacts/runs/<run_id>/manifest.json`. Exposes
  `list_runs()`, `get_run(run_id)`, `runs_grouped_by_config_hash()`, `list_compute_rows()`,
  `list_test_evals()`. Pure file reads, no subprocess — this tier needs nothing new in
  `requirements.txt`.
- **New `backend/bridge.py`** — the one place that shells out to `bridge_python_executable`
  with `cwd=repo_root` and `PYTHONPATH=repo_root` set explicitly in the subprocess `env` (a
  bare `cwd=` does **not** put `repo_root` on `sys.path` for an absolute script path — this is
  the concrete gotcha to get right). Runs small, single-purpose scripts under
  `backend/bridge_scripts/*.py` (shipped inside `dashboard/`, so the host repo needs zero
  dashboard-aware code — consistent with the drop-in contract) that `import` the host repo's
  actual packages and print one JSON object to stdout. `bridge.py` wraps this with:
  - A short timeout (e.g. 15s) and a clear `BridgeUnavailable` exception distinguishing "the
    script raised" (host repo genuinely lacks this module — expected in an older/partial repo)
    from "the interpreter/timeout failed" (misconfiguration).
  - An in-process TTL+mtime cache (schema/registry output only changes when the host repo's
    code changes) so e.g. opening the Create Config form doesn't re-run a subprocess per
    keystroke.
  - **Shell-safety note**: the bridge never accepts free-form user text as a script argument
    that reaches a shell — arguments are passed via `subprocess.run([...], shell=False)` exactly
    like `tmux_runner.py` already does for the launch command, and any config path argument goes
    through the exact same `configs._resolve()` path-traversal check used everywhere else before
    being handed to the bridge.
- **`backend/bridge_scripts/export_schema.py`**: `from orchestration.schema import Config;
  print(json.dumps(Config.model_json_schema()))`. Feeds Phase 2's form generator.
- **`backend/bridge_scripts/resolve_config.py`**: `from utils.config import load_config;
  load_config(sys.argv[1])`, catching and JSON-encoding a `pydantic.ValidationError` as a
  structured `{"valid": false, "errors": [...]}` instead of letting it crash the subprocess.
  Feeds Phase 2's resolved-config preview and pre-launch validation.
- **`backend/bridge_scripts/list_models.py`**: introspects `models/registry.py`'s registered
  entries (family name, size variants, and — where cheap to compute without a GPU — param count
  via `models.build`) into a flat JSON list. Feeds Phase 2's model picker.
- **`backend/bridge_scripts/channel_preview.py`**: given a config path and a sample image,
  calls `datasets.channels.build_channels()` and returns each channel as a small base64 PNG.
  Feeds Phase 5's channel montage.
- **`backend/bridge_scripts/read_parquet.py`**: `import pandas as pd; pd.read_parquet(path).
  to_dict(orient="records")`. This is the one script that needs the *host repo's* pandas/pyarrow
  (already pinned there per `CHANGELOG.md` Phase 2 — `pyarrow==17.0.0`), not the dashboard's —
  exactly the point of routing it through the bridge instead of adding pyarrow to
  `dashboard/requirements.txt`. Feeds Phase 4's per-image plots.
- `GET /api/bridge/status` — runs a trivial bridge script (`import orchestration, models,
  metrics, datasets; print("ok")`) and reports which of the five modules above import cleanly,
  so the frontend can show per-feature "unavailable in this repo" banners instead of a broken
  button. Cached with a short TTL, manually refreshable.

**Exit criterion:** `GET /api/bridge/status` against the real `dissert` repo reports all modules
available; pointed at a scratch repo containing only `train.py`/`eval.py` (no `orchestration/`
package), it reports them cleanly unavailable with no server error.

---

## Phase 2 — Config system v2: schema-aware, registry-aware

**Goal:** a config is validated and its *effective* (composed) form is visible before it's ever
launched — closing the exact class of bug `CHANGELOG.md` Phase 1 found by hand (the
`mkunet_s_colondb` config silently training on ClinicDB for want of a required-field check).

- **Configs editor**: add a "Resolved" toggle next to the existing raw-YAML CodeMirror pane —
  calls `resolve_config.py` via the bridge, shows the fully `compose:`-merged dict as
  read-only pretty-printed YAML, with schema-validation errors (if any) rendered as inline
  annotations pinned to the offending key, not a raw pydantic traceback.
- **Save button gains a pre-flight check**: `write_config` already validates parseable YAML; add
  an optional bridge-backed schema check (bypassable if the bridge is unavailable — degrade
  honestly, principle 4) with a clear "saved, but this config does not validate against the
  current schema" warning rather than blocking the save outright (someone may be intentionally
  editing a fragment, not a launchable config).
- **Create Config rebuilt on top of the schema**: today's builder infers field type from
  whatever value a template config happens to have (`fieldType()` in `app.js`), so
  e.g. `channel_mode` renders as a free-text box a user could mistype as `"m6"`. Once
  `export_schema.py` is available, every `Literal[...]` field (there are several already —
  `optimizer`, `scheduler`, `modality`, `channel_mode`, `grad_clip_mode`, ...) renders as a
  dropdown of its real allowed values instead of free text; required-vs-optional and each
  field's default come from the schema instead of being invisible.
- **Model picker**: a new sub-panel in Create Config, populated from `list_models.py` — pick a
  registered family (`mk_unet`, `gmk_unet`, `mamba_unet`, ...) and a size variant, see its
  param count and (once cached) whether it's within the project's budget ceiling, before
  generating the `model:` block — replacing manual copy-paste of a `channels:`/`depths:` list
  from an existing config.
- **Config diff & clone**: "Duplicate as new experiment" action in the Configs tree — opens the
  Create Config flow pre-seeded from the selected file (already partially possible via "Load as
  template," but now offered directly from the Configs tree's context menu) plus a **diff view**
  between any two selected configs (reuses the existing `flatten_dict`/config-diff logic already
  built for Reports comparison in `backend/reports.py`, generalized into a shared
  `backend/diffing.py` helper both Configs and Reports import).

**Exit criterion:** opening any real experiment config under `configs/experiment/` shows its
resolved form matching what `utils.config.load_config()` actually returns (spot-check 3 configs
against a manual `python -c` call); the model picker lists all 6 real families from the current
registry.

---

## Phase 3 — Runs & Experiments Hub

**Goal:** replace "a list of tmux session names" as the mental model with "a registry of runs,"
without losing anything the current Terminals tab does well (live log tail, stop/kill/restart,
reboot resilience).

- **New "Runs" nav item**, positioned where Terminals is today; Terminals becomes a secondary
  "Live Sessions" sub-view reachable from a run's detail panel (a run's tmux session is one
  facet of it, not the primary identity) — the existing tmux-session machinery
  (`terminals.py`/`tmux_runner.py`) is untouched, just re-presented.
- **Run cards grouped by `config_hash`**: each group shows the experiment name, the resolved
  model/dataset, and a seed×fold matrix (rows = fold, columns = seed, or a flat list for
  non-CV configs) with each cell colored by status (running/done/failed/pending) — sourced from
  `backend/ledger.py`'s `runs_grouped_by_config_hash()` when `artifacts/` exists, falling back
  to today's flat tmux-session list when it doesn't (principle 4).
- **Run detail panel** (opened from a card or matrix cell) shows, from the manifest: `run_id`,
  config hash, git commit **+ dirty-tree badge** (a prominent warning chip if `dirty: true` —
  the spec's own reporting layer is required to exclude these; the dashboard should make that
  visible at the point of launch, not just at reporting time), hardware (GPU name, driver,
  CUDA, host RAM), env hash, start/end time, realized `gpu_hours`, and — when present —
  `nondeterministic_ops` and any `record()`-ed extras (Mamba's `scan_impl` is the first real
  example: shows "ss2d_ref (pure-PyTorch fallback)" vs "ss2d (fused kernel)" as a small badge).
  Below that, the existing live log tail / metrics chart / stop-kill-restart controls, unchanged.
- **"Reproduce this run"**: a button on a completed run's detail panel that pre-fills the
  Configs launch bar with the exact config path + seed, so re-running the identical combination
  (e.g. after a fix) is one click instead of hand-editing YAML — reads straight off the
  manifest's `resolved_config`/`seed`, no guessing.
- **Bulk actions on the Terminals/Live-Sessions list**: multi-select checkboxes + "Stop
  selected" / "Kill selected" (each still going through the existing per-session
  `_is_managed()` guard, so an unmanaged session still can't be bulk-killed) — today's UI only
  supports one session at a time.
- **Telemetry bar upgrade**: the topbar's single "RUNNING / NEEDS RESTART / DONE / FAILED"
  pill row (today driven only by tmux status) also folds in ledger-known runs that aren't
  currently backed by a live tmux session (e.g. a sweep-launched run from a future headless
  `orchestration.runner` invocation), so the counts stay accurate once Phase 6 exists.

**Exit criterion:** launching a real `train.py` run through the dashboard, letting it write a
manifest, and reloading the Runs tab shows the correct grouped card with live status; killing
the dashboard server and restarting it loses no run history (manifests/ledger are the source of
truth, same reboot-resilience guarantee the current Terminals tab already provides for tmux).

---

## Phase 4 — Results & Reports v2

**Goal:** make the canonical metrics module's actual guarantees visible, not just its numbers.

- **Framework-side dependency, flagged not assumed**: `metrics/aggregate.py`'s
  `write_per_image_parquet()` exists but **`eval.py` does not currently call it** (verified —
  no call site found). The per-image visualizations below need a Parquet file to exist. This
  plan does not modify `eval.py` (out of the dashboard's scope), but the box/violin-plot and
  per-image-table features are correspondingly gated behind "does a `*_per_image.parquet` exist
  next to this report" and show an honest "not available for this report" state otherwise —
  flag to the user as a small, separate ask for the main framework track if these visuals matter
  before that gap is closed.
- **Metric cards get context, not just a number**: `hd95`/`asd`/`nsd` cards show their
  `*_excluded_n` count directly beside the value ("HD95: 12.4px · 3 of 62 images excluded —
  empty mask"), sourced straight from `EMPTY_MASK_CONVENTION`-documented fields already in the
  report JSON — today these sit as unexplained extra stat cards indistinguishable from any other
  metric, which is exactly the kind of silent-misread the canonical module was built to prevent.
- **Percentile band**: `dice_p5`/`dice_p25` rendered as a small range indicator under the mean
  Dice card (worst-5%/worst-25% context in one glance), not just another card in the grid.
- **Per-image distribution** (when the Parquet sidecar exists, via `read_parquet.py`): a
  box-plot-per-metric strip (hand-rolled SVG — Chart.js has no first-class box plot and this
  keeps the CDN dependency list unchanged) across the report's own per-image scores, plus a
  sortable/filterable per-image table (worst-N images by Dice, useful for picking qualitative
  failure examples) with an inline thumbnail if a matching overlay exists under `plots_dir`
  (reuses History's existing image-serving route).
- **Lesion-free-subset transparency**: `fpr_on_normals`/`specificity_lesion_free` render as
  "N/A — no lesion-free images in this dataset" (their real, documented `None` state for
  ClinicDB/ColonDB) instead of looking like a missing/broken field.
- **K-Fold rollup view**: for an experiment with multiple `best_fold*.pth`-style reports, a new
  aggregate card showing mean ± std across folds per metric (currently: no cross-fold view
  exists at all — a user manually opens N reports one at a time).
- **Radar chart upgrade**: keep the existing 0-1-scale radar (it works well), add ECE as an
  additional axis (inverted, since lower is better) now that calibration is a first-class
  canonical metric.
- **Compare view gains an "only differing config keys" filter** (the underlying
  `flatten_dict`-based diff already computes this — today it's shown as a flat table of every
  key; add a toggle to collapse to just the differences, sharing `backend/diffing.py` from
  Phase 2).

**Exit criterion:** the real `gmkunet_t_clinicdb` report (`logs/gmkunet_t_clinicdb/report.json`,
already on disk, already carrying `nsd`/`dice_p5`/`hd95_excluded_n`/`ece` per Phase 2's
changelog entry) renders every new field correctly in the UI, cross-checked against the raw JSON.

---

## Phase 5 — Data Studio (new tab)

**Goal:** surface the leakage-guard/dedup machinery the spec treats as a gating requirement
(S1: "All assertions pass") — currently a `pytest` result, invisible from the UI.

- **New "Data" nav item.** Per-dataset cards (ClinicDB, ColonDB, BUSI, ISIC18 — read from
  `configs/dataset/*.yaml`) showing: split sizes (train/val/test counts), modality,
  `dataset.dedup`/`dataset.external` flags as-configured, and — where a manifest CSV or
  duplicate-exclusion list exists on disk (path convention to confirm with the framework side;
  `datasets/preprocess.py`'s `build_manifest`/`dedup` take a caller-supplied `out_path` rather
  than a fixed location today) — a duplicate-count callout. This is where a finding like the
  real "35/379 ColonDB images (9.2%) are cross-split duplicates" (`CHANGELOG.md` Phase 3) would
  become visible to anyone opening the dashboard, instead of living only in a changelog entry.
- **Channel-mode montage** (bridge-backed via `channel_preview.py`): pick a dataset + a
  `channel_mode` (m1-m5) + a sample image, see each constructed channel rendered as a small
  grayscale/false-color tile in a grid — directly answers the spec's S2 gate artifact ("Channel
  montage figure per dataset") and makes the XY/Rθ/YCbCr channels tangible instead of an
  abstract config string.
- **Test-Evals audit trail**: a simple table straight off `artifacts/ledger/test_evals.csv`
  (already has 4 real rows in this repo right now) — run ID, token (truncated), issued time,
  config hash. This is the dashboard-visible enforcement of "the test set is guarded, and every
  touch is logged," which today is enforced in code but has zero UI presence.

**Exit criterion:** the real `test_evals.csv`'s 4 rows render correctly; a channel montage for
`clinicdb` at `m5` renders all 10 expected channels against a real ClinicDB sample image.

---

## Phase 6 — Sweep launcher & Compute ledger

**Goal:** the "performable actions" a dissertation workflow actually needs beyond one-run-at-a-time.

- **Cross-repo dependency, flagged explicitly**: `orchestration.runner.run_sweep()` is a plain
  Python function with no CLI entrypoint (`orchestration/runner.py` has no `if __name__ ==
  "__main__":` block — confirmed). Launching a seed×fold sweep from a tmux-shelled command (the
  dashboard's only execution mechanism) needs a small CLI wrapper on the framework side, e.g.
  `python -m orchestration.run_cli --config <path> --seeds 1,2,3 --folds 0,1,2,3,4`. **This
  plan does not add that file** (it lives in the main repo's `orchestration/` package, outside
  `dashboard/`) — Phase 6 is written against its existence and should be sequenced together with
  a small request to the framework track to add it; until then, the Sweep Launcher UI ships in a
  visibly "requires `orchestration.run_cli` — not found in this repo" disabled state (principle 4).
- **Sweep Launcher UI**: pick a config, a seed list (chips, project-constant default of 3 per
  the spec's §18), and a fold list (or "no CV"); shows the resulting run matrix (same visual
  component as Phase 3's seed×fold grid) *before* launching, with idempotent-skip awareness —
  a cell already `status: done` in the ledger is shown pre-greyed with a note, matching what
  `run_sweep()` will actually do (skip it) rather than surprising the user.
  Launches as a single tmux session running the CLI wrapper; the session's output feeds the
  matrix's live status the same way a single run's does today.
- **Compute ledger view**: reads `artifacts/ledger/compute.csv` (wall seconds, GPU-hours, peak
  memory, device) — a running GPU-hour total against the spec's stated ~1,230 GPU-hour training
  budget (§18), so "how much of the budget have we spent" is a glance instead of manual
  spreadsheet math.

**Exit criterion:** with the CLI wrapper present (coordinate a stub for testing if the real one
isn't ready yet), launching a 2-seed sweep from the UI produces 2 real runs with correct
manifests, visible in both the Runs Hub (Phase 3) and this matrix; with the wrapper absent, the
UI shows the disabled state and nothing crashes.

---

## Phase 7 — Visual design system & component library

**Goal:** the "tons of visual content" ask, executed as a reusable component layer rather than
one-off markup per view — every phase above already assumes these components exist.

- **New `static/js/components/` (ES modules, no build step)**: `data-table.js` (sortable,
  filterable, used by Runs/Reports/Data/Compute), `sparkline.js` (tiny inline SVG trend line —
  for GPU-hours-over-time, a run's loss curve thumbnail in a card), `stat-tile.js` (the existing
  Reports stat-card pattern, generalized and reused everywhere a single number+label+trend
  appears), `heatmap-grid.js` (CSS-grid + color-scale cells — the seed×fold status matrix in
  Phase 3/6 today, the same component Phase 9's Shapley heat table and mode×dataset heat map
  reuse later), `badge.js` (status pills: running/done/failed/dirty-tree/unmanaged — one visual
  vocabulary instead of ad hoc colored spans), `boxplot.js` (hand-rolled SVG, Phase 4).
- **`styles.css` gains a components layer** (`--chart-series-1..6` tokens derived from the
  existing accent colors, consistent card/table/badge classes) appended to, not replacing, the
  current token block.
- **Loading/empty/error states standardized**: today each view hand-writes its own
  `<div class="empty-state">Loading…</div>`; a shared `renderState(container, {loading|empty|
  error, message})` helper keeps this consistent as the number of views roughly doubles.
- **`app.js` split**: given the file is already 1,659 lines and this plan adds ~6 new views'
  worth of logic, split it into `static/js/views/*.js` (one per nav item, matching the existing
  function-per-view organization already visible in the file) importing shared `api()`/`toast()`/
  `state` helpers from a new `static/js/core.js` — a mechanical refactor with no behavior change,
  done early in this phase so every subsequent phase's frontend work lands in the right file
  from the start rather than being retrofitted later.

**Exit criterion:** every existing view renders pixel-identical to today after the `app.js`
split (regression check, not a redesign); the new component set is exercised by at least one
real view (the Phase 3 Runs Hub) before other phases depend on it.

---

## Phase 8 — Real-time & interactivity polish

**Goal:** reduce the "click refresh, wait, click again" loop the current 2-second-poll model
imposes, and make the dashboard feel like an instrument panel, not a form.

- **Server-Sent Events for live data** (`GET /api/stream/terminals`, `/api/stream/monitors`):
  Flask can stream SSE without adding a dependency (a generator + `text/event-stream`
  mimetype); the frontend swaps its `setInterval` polling loops for an `EventSource` where
  available, falling back to the existing poll on any error — no behavior regression, just fewer
  wasted round-trips and snappier updates. Threaded Flask (`app.run(..., threaded=True)`,
  already set) handles concurrent SSE + regular request handling fine at this scale (single-user
  tool, not a production multi-tenant server).
- **Command palette** (`Cmd/Ctrl-K`): fuzzy-jump to any config, run, or report by name — a
  single new `static/js/palette.js` module, no new dependency, built on the same data already
  loaded into `state`.
- **Notification center**: today's `toast()` calls disappear after a few seconds with no
  history; add a small bell icon + dropdown log of the last ~20 toasts (run completed, run
  failed, config saved, ...) so an action taken while the user was on a different tab isn't lost.
- **Persistent GPU/CPU mini-widget in the topbar**: today Machine Stats requires navigating to
  its own tab and manually starting `nvidia-smi`/`htop` in a tmux pane; add a lightweight
  polling widget (reuses the existing Machine Stats monitor mechanism, just always-on for one
  or two built-in metrics) showing a live GPU-utilization/VRAM sparkline in the topbar, visible
  from every view.

**Exit criterion:** SSE endpoints degrade to the existing polling behavior with `EventSource`
disabled/blocked (verified by testing with it force-disabled); command palette finds a real
config/run/report by partial name.

---

## Phase 9 — Analysis suite scaffolding (forward-looking)

**Goal:** `dissert`'s own plan is explicit that Phases 9-14 (statistics, profiling, attribution,
uncertainty, robustness, reporting) are not built yet. Building the dashboard's side of this
*now*, against the spec's own §20 artifact-inventory table, means these tabs activate the
instant the framework starts emitting the JSON — instead of a second dashboard project later.

- **New nav items, each gated behind "artifact not found yet" until the corresponding framework
  phase lands** (principle 4 applied at the largest scale in this plan):
  - **Stats** — reads `reports/json/stats/<family>.json` (§10/S7): p-values, Holm-corrected
    p-values, Cliff's delta, a critical-difference diagram (hand-rolled SVG: Nemenyi CD bars are
    simple enough not to need a new charting dependency).
  - **Profiling** — reads `reports/json/*` profiling output (§14/S16): efficiency table, a
    Dice-vs-params/FLOPs/latency Pareto-frontier scatter (Chart.js scatter plot — it already
    handles this natively, no new dependency).
  - **Attribution** — Shapley heat table and per-group Dice-drop bar chart (§11/S10), built on
    the `heatmap-grid.js` component from Phase 7.
  - **Uncertainty** — reliability diagram + risk-coverage curve (§12/S13).
  - **Robustness** — degradation-curve line charts per corruption family, severity 1-5 (§13/S14).
- Each view's data-loading function is written against the exact JSON shape the spec's §20 table
  and the corresponding module docstrings describe, so when Phase 9-14 of the *framework* plan
  land, the dashboard-side work is "point it at the real file," not "design and build the view."

**Exit criterion:** every new tab renders its "not yet available" state cleanly against the
current repo (none of these artifacts exist yet — confirmed, `reports/` doesn't exist on disk).
Full exit criterion (real data rendering correctly) is deferred until the framework side ships
the first real artifact of each kind — track as a re-verification item at that point, not now.

---

## Phase 10 — Security parity, mobile, docs, verification pass

**Goal:** close the loop — every new surface gets the same scrutiny `CODE_REVIEW.md` already
gave the original dashboard.

- Every new POST/DELETE route added in Phases 1-9 is confirmed to sit behind the existing
  `_guard_mutating_requests` (it does automatically, being a normal Flask route under the same
  `app` — this is a verification step, not new code) and every new path-accepting endpoint
  (bridge script arguments, dataset manifest paths) goes through the same resolve-then-check-
  parents pattern `configs.py`/`history.py`/`reports.py` already use, extended to
  `backend/ledger.py`/`backend/bridge.py`'s new path handling.
- Mobile responsiveness pass for every new view, matching the existing ~860px breakpoint
  behavior (slide-in sidebar, stacked two-pane layouts) already documented in `README.md`.
- Rewrite `dashboard/README.md`'s feature list to cover the new tabs/actions (currently accurate
  only for the pre-this-plan feature set), and add a short "Framework compatibility" section
  explaining the bridge/degrade-gracefully model so a future drop-in into an older repo isn't
  surprising.
- **Final end-to-end pass**: run the dashboard against the live `dissert` repo, exercise every
  new view against real on-disk data (real manifests, real ledger CSVs, real reports, real
  configs) — not synthetic fixtures — the same standard the framework's own `CHANGELOG.md`
  entries held themselves to throughout.

**Exit criterion:** a fresh `python server.py` against this repo, clicked through every nav item
with no console errors, no unguarded new route, and every "not yet available" state showing
exactly where expected (Phase 9 tabs) and nowhere else.

---

## Suggested sequencing

Phases 0-1 are prerequisites for everything else and should be done first and together (the
bridge is small but every later phase either uses it or explicitly doesn't). After that, Phase 3
(Runs Hub) delivers the single biggest visible/functional jump for the least new infrastructure
and is recommended next, followed by Phase 2 (they reinforce each other — a resolved-config
preview is much more useful once you can see it against a real run's manifest). Phase 7's
component library is written incrementally as Phases 3-6 need pieces of it, not as a standalone
phase done in isolation — the ordering above lists it after Phase 6 only because that's where
its component *set* is complete, not where work on it starts. Phase 9 is explicitly the lowest
priority — it scaffolds against artifacts that don't exist yet — and Phase 6's sweep launcher is
gated on a small cross-repo ask (the `orchestration.run_cli` wrapper), so either could be
deferred without blocking anything else in this plan.

## Explicit non-goals

- **No framework-side code changes** are part of this plan, even where a gap is identified
  (`eval.py` not calling `write_per_image_parquet`, `orchestration/runner.py` lacking a CLI
  entrypoint). Both are flagged inline above as small, separate asks for that track.
- **No migration off Flask/vanilla-JS/no-build-step.** The existing architecture is a deliberate
  choice (documented in `server.py`'s own module docstring) for a single-user local tool, not a
  legacy constraint to escape.
- **No new authentication system.** The existing `api_token`/Origin-check model
  (`CODE_REVIEW.md` C6-C8's fix) is sufficient for this tool's threat model; new routes extend
  it, this plan doesn't replace it.
- **Resolving `dashboard/` vs `exp_dashboard/` duplication** is flagged (Phase 0) but left as a
  user decision, not executed by this plan.
