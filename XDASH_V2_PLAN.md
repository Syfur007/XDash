# XDash v2 — object model correction, then a lifecycle UI

**Status:** supersedes `DISSERT_KAGGLE_TEMPLATE_RESUME_PLAN.md` in full, and
`EXPERIMENT_AUTOMATION_PLAN.md` §3 / §6 / §8.2. Those documents' §2 (Phase 0
defect findings) and §4.1 (greedy policy) remain valid and are carried
forward here by reference, not restated.

**Decisions locked in before writing (asked and answered):**

| Fork | Decision |
| --- | --- |
| Navigation | **Lifecycle IA** — five working surfaces + Settings, replacing today's ten tabs |
| Kaggle "worker" | **Deleted as a user-facing concept.** The account is the slot |
| Frontend stack | **Build step allowed** — Vite + a component framework, replacing the buildless vanilla-JS contract |
| Sequencing | **Correctness first, UI second.** Long-run resume deferred to Phase D |

---

## 1. Diagnosis: the plans were fine, the object model wasn't

Both prior plans reason carefully about *mechanism* and barely at all about
*nouns*. That is why the implementation is defensible line by line and
unusable as a whole. Three structural faults, in descending order of damage:

### 1.1 A Kaggle worker is an implementation artifact promoted to a UI object

`EXPERIMENT_AUTOMATION_PLAN.md` §1 already states the truth: *"Kaggle runs
one kernel per account at a time, however many workers that account
registers. Capacity is one slot per account; extra workers are
organizational, not parallelism."*

So a worker adds **zero capability**. What it adds is seven fields of Kaggle
trivia on the critical path to running anything — kernel slug, results dir,
budget hours, template override, fixed notebook path, dataset sources,
account binding (`static/index.html:536-560`). The code itself came to the
same conclusion and routed around the user: `ensure_template_worker()`
(`backend/kaggle.py:651`) silently fabricates one worker per account behind
the operator's back. But the UI kept presenting workers as things you
create and manage, so the dashboard now has a concept that the dashboard
itself does not believe in.

This single decision is load-bearing for three separate defects below
(§2.4's per-experiment kernel slug, §2.5's dataset attachment, and the
entire resume design), all of which become straightforward once the worker
is gone.

### 1.2 Four launch paths, three state models, nothing reconciling them

| Launch path | Entry point | Goes through the scheduler? |
| --- | --- | --- |
| Configs tab run-bar | `POST /api/runners/<id>/launch` → `LocalRunner.launch()` | **No** — calls `terminals.launch()` directly |
| Experiments → Queue | `POST /api/scheduler/items` | Yes |
| Runners → Push | `POST /api/kaggle/workers/<id>/push` | N/A |
| Assignments → Batch | `POST /api/batches/start` → `batch_runner` | Yes (local half) |

Three independent "what is planned / running" stores: scheduler items
(`data/<profile>/scheduler.json`), Kaggle worker state
(`kaggle_state.json`), assignment rows (`assignments.json`). Each has its
own status vocabulary. A single experiment's truth is spread across all
three and joined nowhere.

### 1.3 The UI is organized by subsystem, so one experiment spans five tabs

Today: Configs → (launch) → Experiments/Active → Experiments/Queue →
Runners/Kaggle → Experiments/Runs → Reports. Meanwhile **Runners** is a
junk drawer holding mclab capacity, Machine Stats, TensorBoard, Kaggle
accounts, Kaggle workers, and Notifications on one scrolling page
(`static/index.html:416-584`).

`DISSERT_KAGGLE_TEMPLATE_RESUME_PLAN.md` §7 proposed eight tabs to replace
ten — but it is a *list of page names with bullet lists of fields*, with no
interaction model, no primary action per page, and no statement of what
becomes fewer clicks. It renames the junk drawers. It does not remove them.

---

## 2. Verified implementation damage

Every item below was read in the source, not inferred from the plan text.

| # | Defect | Location | Consequence |
| --- | --- | --- | --- |
| D1 | **Resume never resumes.** `--resume` is passed nowhere in the codebase. Template cell 14 runs an identical train command in both modes. | `grep -rn -- "--resume" backend/ static/` → 0 hits; `data/dissert/kaggle_worker_template.ipynb` cell 14 | The entire resume feature is validation theater |
| D2 | **Resume checkpoints ship as kernel source.** `push()` does `shutil.copytree(staged_resume_dir, tmpdir/"resume_source")` into the `kernels push` bundle. | `backend/kaggle.py:1113-1117` | Contradicts `EXPERIMENT_AUTOMATION_PLAN.md` §8.2, which specified `datasets create`/`version`. Pushes model weights through a code-upload path |
| D3 | **`restart()` silently becomes a resume.** It calls `push()` with no `run_mode`, which falls back to the *stored* mode. | `backend/kaggle.py:1184` vs `:1044` | Restarting a worker that last resumed re-resumes it |
| D4 | **One kernel slug + one results dir per account, shared by every experiment.** `ensure_template_worker()` derives `xdash-<profile>-<account>` once. | `backend/kaggle.py:670-681` | Every push overwrites the same kernel; every download extracts into the same directory; version history of all experiments is interleaved. §8.2 required the *opposite* (per-experiment slugs) |
| D5 | **A crash mid-dispatch strands a row *and* reports the batch done.** `dispatching` appears in neither `_settle_finished_batches`' liveness check nor `_reconcile_on_startup`. | `backend/batch_runner.py:540`, `:608` | Silent data loss disguised as success |
| D6 | **Dataset-slug resolution discards the value it just found.** The `compose` fragment loop assigns `value` and breaks; the `return` ignores `value` and falls through to the map lookup. | `backend/batch_runner.py:269-284` | A config declaring `kaggle_dataset` in a fragment is treated as unmapped |
| D7 | **Kaggle pool silently dead for unmapped configs.** `_kaggle_candidate_accounts()` returns `[]` when no slug maps, producing the blocked reason `quota-exhausted-or-no-idle-worker`. | `backend/batch_runner.py:308-309` | "You never configured a dataset mapping" is reported to the user as "you are out of quota" |
| D8 | **Side effects inside a sort comparator.** `feasibility(r)` is evaluated twice per row in the sort key; each call runs `list_accounts()` (filesystem walk per account) and can *create Kaggle workers*. | `backend/batch_runner.py:424` | O(n) account scans and registry mutations from a sort |
| D9 | **Local slot accounting over-counts.** `_local_free_slots()` counts every `pending` item, but a `both`-mode row enqueues two and the scheduler refuses to launch an eval item whose train dependency is unfinished. | `backend/batch_runner.py:226-233` vs `backend/scheduler.py:322-325` | Dependency-blocked items consume phantom slots; late-binding from §4 is defeated |
| D10 | **A failed Kaggle run produces no artifacts and no diagnosis.** Cell 14's `assert` aborts the notebook, so cells 15-16 (the packaging step) never run; `download()` then reports "No output files found — has the kernel finished?" | template cells 14, 16; `backend/kaggle.py:1389` | Every Kaggle failure is undiagnosable from the dashboard |
| D11 | **`kaggle_dataset_map` keys are not case-folded at load** but the lookup lower-cases. | `backend/config.py:210-214` vs `backend/batch_runner.py:276` | A profile writing `ClinicDB:` silently never matches |
| D12 | **`version_index`/`chain_id` computed twice, differently.** `push()` derives `version`/`chain` locally for the rendered notebook, then `state_patch` recomputes both from `_load_state()` and ignores them. | `backend/kaggle.py:1089` vs `:1149` | The notebook's lineage and the stored lineage disagree |

**D1-D4 together mean the resume feature has never worked and cannot work
as built.** It is not a partially-complete feature; it is a set of
assertions that pass, followed by a fresh training run.

---

## 3. The new object model

Five nouns. Every view, endpoint and state file is a projection of these.

### 3.1 Experiment

`(config_path, seed)` — the thing a researcher actually means. Identity:

```
experiment_id = f"{experiment_name}-s{seed}"      # e.g. mkunet_t_clinicdb-s0
```

Chosen because **dissert already uses exactly this string** as its output
directory name (`outputs/experiments/<experiment_name>-s<seed>/`, per
`OUTPUT_LAYOUT.md`). Adopting it means the dashboard's primary key and the
host repo's on-disk layout are the same key, so joining a plan to its
artifacts needs no mapping table.

Persistent. Replaces: assignment rows, and the planning half of batches.

### 3.2 Attempt

One execution of one Experiment on one Slot. An Experiment has 1..N
Attempts (a retry is an Attempt; a resume leg, in Phase D, is an Attempt).

```jsonc
{
  "attempt_id": "…", "experiment_id": "mkunet_t_clinicdb-s0",
  "slot": "kaggle:tanvir",            // or "local"
  "status": "running",                // CANONICAL_STATUSES, backend/runners/base.py:16
  "raw_status": "running",            // the pool's own word, never discarded
  "stages": [ {"name": "train", "status": "running"},
              {"name": "eval",  "status": "pending"} ],
  "unit_ref": {"session_name": "xdash-dissert-…"},   // or {"kernel": "user/slug", "version": 3}
  "started_at": "…", "ended_at": null,
  "blocked": null,                    // see §3.5
  "run_id": null                      // filled from the ledger on completion
}
```

**`stages` is the fix for `EXPERIMENT_AUTOMATION_PLAN.md` §6's card
variation.** That plan had the UI branch on whether `unit_ref` carried
`train_item_id`/`eval_item_id` or `account`/`worker_id`. That is a
discriminator the frontend has to keep in sync with two backends. A
`stages` array is data: local emits two entries, Kaggle emits one (or two,
once the template reports its own stage boundaries — §4.A10). **One card
component, no branch, and Kaggle gains stage granularity for free the day
the notebook can report it.**

### 3.3 Slot

A unit of concurrency, and the *only* capacity concept.

| Slot id | Count | Metered by |
| --- | --- | --- |
| `local` | `scheduler.max_concurrent` | slots |
| `kaggle:<account>` | exactly **1** per account (platform limit) | slots **and** weekly GPU hours |

There is no worker. Kernel slug and results dir are **derived per
experiment**, not stored per worker:

```python
kernel_slug = f"xdash-{profile}-{slugify(experiment_id)}"     # truncate to 48 chars + 6-char hash
results_dir = f"outputs/kaggle/{experiment_id}"
```

This is what §8.2 of the old plan needed anyway ("Notebook slug becomes
per-experiment … so each experiment's legs are its own version history")
and it deletes D4 outright.

Account-level settings that survive: credentials, scope (system/repo),
`weekly_budget_hours`, `session_limit_hours`. Settings that move off the
worker and onto the *dataset map* (§3.4): `dataset_sources`.

**Escape hatch, explicitly separated:** pushing a hand-authored notebook
verbatim remains possible, but as a distinct action under Settings →
Templates ("Push a notebook to <account>"), not as a variant of the normal
path. It is not part of batch dispatch and never was — `batch_runner` already
filters notebook-backed workers out (`:295`).

### 3.4 Dataset mapping (closes `EXPERIMENT_AUTOMATION_PLAN.md` §2.5's open question)

§2.5 deferred the decision between "a new key on `configs/dataset/<name>.yaml`"
and "a standalone registry file". **Decide: both, with a precedence rule.**

1. `configs/dataset/<name>.yaml`'s own `kaggle_dataset: "user/slug"` wins if present.
2. Otherwise `data/<profile>/dataset_map.json` — XDash-owned, editable from the Data tab.
3. Otherwise `repos/<profile>.yaml`'s `kaggle_dataset_map` (today's mechanism) seeds #2 on first read.

XDash-owned storage must be the default because XDash must work against
repos it does not own and must never need write access to a host repo's
configs to become usable. The config-side key wins because a repo that
*does* adopt it should be self-describing.

Keys are compared case-folded on both sides (fixes D11).

### 3.5 Blocked, as structured data

Today `blocked_reason` is a string like
`"quota-exhausted-and-local-busy"` rendered into a table cell. That is why
a stalled batch is a mystery. Replace with:

```jsonc
{ "code": "no-dataset-mapping",
  "detail": "clinicdb has no Kaggle dataset slug",
  "since": "2026-09-22T11:04:00",
  "clears_at": null,                             // set for quota-exhausted
  "action": {"label": "Map it", "target": "data#map/clinicdb"} }
```

Codes and their actions:

| Code | Means | UI action |
| --- | --- | --- |
| `no-dataset-mapping` | §3.4 resolved nothing for this config | Deep-link to Data → map this dataset |
| `quota-exhausted` | every eligible account is over `weekly_budget_hours` | Show `clears_at` (UTC week boundary, already computed by `_utc_week_start()`) |
| `exceeds-session-cap` | `est_hours > session_limit_hours` on every account | Offer "run on mclab instead"; in Phase D, "split into N legs" |
| `pool-busy` | feasible, nothing free this tick | None — informational |
| `no-account` | pool is `kaggle_only` and zero accounts are registered | Deep-link to Compute → add account |

**D7 disappears by construction:** `no-dataset-mapping` and
`quota-exhausted` can no longer collapse into one string.

### 3.6 Batch and Run (kept, narrowed)

- **Batch** — a named set of Experiments created together plus its policy
  (`pool`, `max_retries`, `force_on_retry`). It is a *grouping and a
  policy*, not a state machine that rows belong to. Batch status is
  *derived* from its experiments, never stored — which removes the entire
  class of D5-style "batch says done, rows say otherwise" bugs.
- **Run** — the host repo's ledger record, unchanged. `Attempt.run_id` is
  the join. XDash never writes it.

### 3.7 What this retires

| Retired | Absorbed by |
| --- | --- |
| Kaggle worker (record, routes, UI, `kaggle_state.json` worker entries) | Slot + derived per-experiment kernel |
| Assignment row | Experiment |
| `batches.json` status field | Derived from Experiments |
| `unit_ref` shape-branching (§6 of the old plan) | `Attempt.stages` |
| `run_mode` / `resume_from_*` / `chain_id` on workers | Phase D's Attempt chain |
| The `runners/` facade's `launch()` | `POST /api/experiments` (see §5) |

Note on `backend/runners/`: its *read* half (`list_units()`, `capacity()`,
`CANONICAL_STATUSES`, `RunnerCapabilities`) is good and is kept — it is
where the Slot abstraction lands. Its *write* half (`launch()`) is deleted,
because `LocalRunner.launch()` bypasses `scheduler.py`'s queue entirely
(`EXPERIMENT_AUTOMATION_PLAN.md` §0.4 already flagged this) and is the
fourth launch path from §1.2.

---

## 4. Phase A — correctness (no UI work; ship independently)

Ordered so each item is separately verifiable. Nothing here depends on the
new UI, and every item fixes a defect that is real today.

**A1. Remove the resume path entirely.**
Delete `mark_resume_origin()`, `set_run_mode()`, `resume_worker()`,
`validate_resume_state()`, the `run_mode`/`resume_from_*`/`chain_id`/
`version_index` parameters on `push()` and `_render_launch_notebook()`, the
four `/api/kaggle/workers/<id>/resume*` routes, `batch_runner`'s resume
branch (`:474-489`), `assignments.py`'s resume fields, the batch builder's
resume toggle (`static/index.html:616-626`), and the template's `RUN_MODE`
cells (9's resume asserts, 12 entirely).

*Why removal and not a "not implemented" guard:* the current code passes
validation and then silently runs a fresh training job (D1). A guard that
raised would be honest but would leave a half-schema that Phase D must
either adopt or fight. Phase D's contract (§6) differs from it materially —
dataset-versioned legs, not staged directories — so carrying it forward
costs more than re-adding it.

*Keep:* nothing from the resume implementation. Phase D re-derives lineage
from `Attempt`, which did not exist when this was written.

**A2. Fix batch settlement and reconciliation (D5).**
Add `dispatching` to the liveness set in `_settle_finished_batches()` and
to `_reconcile_on_startup()`. On startup, a `dispatching` row with an empty
`unit_ref` returns to `pending` (the dispatch never landed); with a
populated one it resolves against the unit like any in-flight row.

**A3. Fix dataset-slug resolution (D6, D11).**
Return the `value` found in the `compose` fragment loop. Case-fold
`kaggle_dataset_map` keys at load in `backend/config.py`.

**A4. Split the blocked vocabulary (D7).**
Implement §3.5's codes in `batch_runner`. Minimum viable version for Phase
A: distinct `code` strings; the `action` deep-links land with Phase C.

**A5. Make scoring pure (D8).**
Compute `feasibility()` once per row into a dict before sorting. Move
`ensure_template_worker()` out of the scoring path — it is deleted
altogether by Phase B, but it must not run from a comparator before then.

**A6. Fix local slot accounting (D9).**
`_local_free_slots()` counts a `pending` item only when it has no
`depends_on`, or its dependency is `completed` — mirroring exactly what
`scheduler._tick():322-325` will actually launch.

**A7. Make Kaggle failures diagnosable (D10).**
Restructure the template's run cell: wrap train/eval in `try/finally`, always
run the packaging cell, and always write
`/kaggle/working/dissert/outputs/xdash_status.json` with
`{stage, returncode, started_at, ended_at, setup_seconds}`. `download()`
reads it to classify the attempt instead of inferring from zip presence.
This also produces the **measured** `setup_reserve_hours` that
`EXPERIMENT_AUTOMATION_PLAN.md` §8.1 asked for and nothing supplies.

**A8. Force `restart()` fresh (D3).**
Trivial once A1 lands (`run_mode` no longer exists). Listed separately so
it is verified, not assumed.

**A9. Reconcile lineage bookkeeping (D12).**
Deleted by A1. Listed so the defect is closed explicitly rather than
silently disappearing.

**A10. (Optional, enables stage granularity)** have the template emit a
stage marker to stdout at the train→eval boundary and record it in
`xdash_status.json`, so a Kaggle `Attempt` can report two `stages` like a
local one. Not required for Phase B; it is the reason `stages` is an array
rather than a boolean.

**Phase A acceptance:** a batch of 4 rows on dissert, one config
deliberately unmapped, one deliberately over the session cap — dispatches
the feasible rows, blocks the other two with *distinct* codes, survives
`kill -9` mid-dispatch with no stranded row and no false "done", and a
deliberately-failing Kaggle push comes back with a readable status file.

---

## 5. Phase B — the API the new UI needs

One resource, one verb. These replace `/api/assignments*`,
`/api/batches/*`, `/api/kaggle/*/workers*`, `/api/kaggle/workers/*`, and
`/api/runners/<id>/launch`.

```
GET    /api/experiments              ?batch=&status=&config=&slot=
POST   /api/experiments              create N — THE single launch verb
GET    /api/experiments/<id>         detail: attempts, stages, log tail, artifacts, run join
POST   /api/experiments/<id>/retry   new Attempt
POST   /api/experiments/<id>/cancel
DELETE /api/experiments/<id>         only while pending/blocked

GET    /api/slots                    capacity + quota: used / reserved / limit / clears_at
GET    /api/pulse                    everything the Lab view polls, in ONE request
GET    /api/datasets/kaggle-map      §3.4 resolution, with provenance per entry
PUT    /api/datasets/kaggle-map
```

`POST /api/experiments` body — note it is the Run Composer's form, 1:1:

```jsonc
{ "configs": ["experiment/mkunet/mkunet_t_clinicdb.yaml"],
  "seeds": [0, 1, 2],
  "extra_args": "--epochs 200",
  "pool": "either",
  "batch_name": "ablation-clinicdb-sep",   // optional; groups them
  "max_retries": 1, "force_on_retry": true }
```

**`/api/pulse` is a performance requirement, not a convenience.** The Lab
view needs counts, active attempts, slot capacity, blocked list and recent
events; today's frontend would poll five endpoints every 2 s, and
`list_accounts()` alone walks every account's results directory per call
(`kaggle.py:382`). One endpoint, one TTL cache behind it.

**Optional, recommended:** `GET /api/stream` (SSE). Flask already runs
`threaded=True` (`server.py:1060`); an SSE endpoint pushing attempt
transitions removes the poll entirely for the live views and is the single
biggest "feels fast" lever. Falls back to `/api/pulse` polling when the
connection drops.

Kept unchanged: `/api/configs`, `/api/config*`, `/api/runs*`, `/api/ledger/*`,
`/api/reports*`, `/api/datasets`, `/api/history/*`, `/api/monitors*`,
`/api/tensorboard/*`, `/api/repos*`, `/api/notifications*`, `/api/paths`,
`/api/templates`, `/api/kaggle/accounts*` (minus the worker sub-routes).

---

## 6. Phase C — the UI

### 6.1 Stack

**Vite + Svelte 5 + TypeScript.** Recommended over Preact for this codebase
specifically: the app is table- and polling-heavy, Svelte stores model
"one polled snapshot fans out to twelve components" with less ceremony than
hooks, single-file components map cleanly onto the existing
markup+scoped-CSS structure, and the migration path from imperative DOM
code is shorter for someone whose current codebase is vanilla JS. If you'd
rather stay in React idiom, Preact + `@preact/signals` is a drop-in
substitute for everything below; nothing in §6.2-§6.5 depends on the choice.

Deployment, and the contract this breaks:

- `npm run build` → `static/dist/`. Flask serves `static/dist/index.html`.
- **Commit `static/dist/`.** `python server.py` then still runs on a machine
  with no Node — only *developing* the frontend needs it. This preserves
  most of the drop-in contract from `IMPLEMENTATION_PLAN.md`; what it
  genuinely costs is that the folder is no longer editable-in-place.
  That is the tradeoff you accepted, stated plainly so it is not
  rediscovered later.
- Dev: `vite` on :5173 proxying `/api` → :6070.
- CDN libs become npm deps: CodeMirror 5 → **CodeMirror 6**, Chart.js 4,
  js-yaml. Losing the CDN also removes three render-blocking external
  requests.
- **`static/styles.css` is imported verbatim as the global stylesheet.**
  Its token system (`--bg/--surface/--amber/--teal/…`, the tint ladder, the
  type scale) is a real design language with documented reasoning at
  `styles.css`'s `:root` block (`:16-100`). Do not re-derive it. Component styles go in `<style>`
  blocks referencing those tokens.

### 6.2 Information architecture

```
┌──────────────┐
│ ◉ Lab        │  what is happening RIGHT NOW + what is stuck and why
│ ⬡ Experiments│  the spine: browse → compose → launch → track
│ ▤ Results    │  runs, reports, compare, plots, TensorBoard
│ ⚙ Compute    │  slots, quota, accounts, alerts, machine stats
│ ⛁ Data       │  datasets, Kaggle slug mapping, channel preview, test-eval audit
├──────────────┤
│ ⋯ Settings   │  repo profiles, paths, templates, health
└──────────────┘
```

Old → new, as an acceptance checklist (nothing in `README.md` may become
harder to reach):

| Today | Lands in | Clicks then → now |
| --- | --- | --- |
| Overview | **Lab** | 1 → 1 |
| Configs / Browse | Experiments (left pane) | 1 → 1 |
| Configs / Create | Experiments → `+ Run` → "New config" | 2 → 2 |
| Configs run-bar | Experiments → `+ Run` | 2 → 2 |
| Experiments / Active | **Lab** | 2 → 1 |
| Experiments / Queue | **Lab** (queued section) + Experiments filter | 2 → 1 |
| Experiments / Runs & Results | **Results** | 2 → 1 |
| Runners / mclab | Compute | 1 → 1 |
| Runners / Kaggle accounts | Compute | scroll → 1 |
| Runners / Kaggle **workers** | **deleted** (§3.3) | — |
| Runners / Machine Stats | Compute | scroll → 1 |
| Runners / TensorBoard | Results (toolbar button) | scroll → 1 |
| Runners / Notifications | Compute | scroll → 1 |
| Assignments board | Experiments table | 1 → 1 |
| Assignments batch builder | **Run Composer** | 3 → 1 |
| Templates | Settings → Templates | 1 → 2 |
| Reports | Results → Reports | 1 → 2 |
| History | Results → Files (drawer) | 1 → 2 |
| Data | Data | 1 → 1 |

Three surfaces get demoted (Templates, Reports, History). All three are
low-frequency; each is still two clicks. Everything on the daily path gets
shorter.

### 6.3 Lab — the control room

Answers exactly three questions, in this order, above the fold:
**What is running? What is stuck, and why? What just finished?**

```
┌────────────────────────────────────────────────────────────────────────┐
│ CAPACITY                                                                │
│ mclab   ▮▮▯▯  2/4 slots      tanvir ▮ 1/1 · 6.2/30h                    │
│ emon    ▯ 0/1 · 0/30h         syfur ▯ 0/1 · 29.4/30h  ⚠ resets Mon 00:00│
├────────────────────────────────────────────────────────────────────────┤
│ RUNNING  3                                                              │
│ ┌────────────────────────────┐ ┌────────────────────────────┐          │
│ │ mkunet_t_clinicdb  s0      │ │ mamba_t_clinicdb  s1       │          │
│ │ kaggle · tanvir            │ │ mclab                      │          │
│ │ [train ███████░░] [eval ░] │ │ [train ██████████][eval ██]│          │
│ │ ep 142/200 · dice .871 ▁▃▅▇│ │ ep 200/200 · dice .863 ▁▄▆▇│          │
│ │ 4h12m elapsed · ~1h48m left│ │ 6h02m elapsed              │          │
│ └────────────────────────────┘ └────────────────────────────┘          │
├────────────────────────────────────────────────────────────────────────┤
│ ⚠ BLOCKED  2                                                            │
│ mamba_s_busi s0    no Kaggle dataset mapping for `busi`      [ Map it ] │
│ mkunet_l_busi s0   est 14.2h > 9.5h session cap        [ Run on mclab ] │
├────────────────────────────────────────────────────────────────────────┤
│ QUEUED 11   ·   DONE TODAY 6   ·   FAILED 1                             │
├────────────────────────────────────────────────────────────────────────┤
│ RECENT                                                                  │
│ ✓ mkunet_t_clinicdb s2  dice .869  (+.004 vs best)   12m ago   [Compare]│
│ ✗ mamba_t_clinicdb  s0  train exit 1                 41m ago   [Log]    │
└────────────────────────────────────────────────────────────────────────┘
```

The **BLOCKED** band is the single most important new element in this
redesign. It is the surface that converts §3.5's structured codes into
something a person can act on, and it is the direct answer to "the current
structure is horrible at the user level": today, a batch that stalls on an
unmapped dataset is reported as a quota problem, in a table cell, on a tab
you would have no reason to open.

Replaces: Overview, Experiments→Active, Experiments→Queue summary, Runners'
capacity panels.

### 6.4 Experiments — the spine

```
┌─────────────┬──────────────────────────────────────────┬────────────────┐
│ CONFIGS     │  EXPERIMENTS            [+ Run]  [⚙ cols] │  INSPECTOR     │
│ ⌕ filter    │  ▾ ablation-clinicdb-sep    6 · 4✓ 1● 1⚠  │  mkunet_t_     │
│ ▾ experiment│   ● mkunet_t_clinicdb s0 kaggle  .871 4h12│  clinicdb s0   │
│  ▾ mkunet   │   ✓ mkunet_t_clinicdb s1 kaggle  .868 5h01│ ┌────────────┐ │
│    mkunet_t │   ✓ mkunet_t_clinicdb s2 mclab   .869 5h44│ │Config │Plan│ │
│    mkunet_s │   ⚠ mamba_s_busi      s0   —      —    —  │ │Live   │Res │ │
│  ▾ mamba    │   ✓ mamba_t_clinicdb  s0 mclab   .863 6h02│ │History     │ │
│    mamba_t  │  ▾ (unbatched)              4              │ ├────────────┤ │
│             │   ○ mkunet_l_busi     s0   —      —    —  │ │ 2026-09-22 │ │
│ [+ new]     │                                            │ │ ep 142/200 │ │
│             │  ▣ 2 selected  [Run] [Cancel] [Delete]    │ │ dice 0.871 │ │
└─────────────┴──────────────────────────────────────────┴────────────────┘
```

- **Left**: the config tree from today's Configs tab, unchanged in data.
  Selecting a config filters the table and loads it into the Inspector's
  Config pane (CodeMirror 6, editable, same save/validate flow).
- **Center**: one row per Experiment. Group by batch (default) or config.
  Multi-select drives bulk Run / Cancel / Delete.
- **Right**: Inspector, tabbed — Config (editor), Plan (pool, seeds, args,
  est with its tier), Live (log tail + metric chart), Result (metrics,
  plots, report), History (every Attempt, with the raw pool status kept as
  secondary text per `EXPERIMENT_AUTOMATION_PLAN.md` §6).

Replaces: Configs (both subtabs), Assignments (board + form + batch
builder), Experiments→Queue (item list), Experiments→Active (session
detail/log).

### 6.5 Run Composer — the one launch verb

The single highest-leverage screen. It collapses the four launch paths from
§1.2 into one dialog, and it front-loads feasibility so a run never stalls
for a reason the user could have been told before pressing the button.

```
┌─ New run ──────────────────────────────────────────────────────────┐
│ WHAT    ▣ mkunet_t_clinicdb   ▣ mamba_t_clinicdb    [+ add]        │
│                                            2 configs               │
│ SEEDS   [ 0, 1, 2 ]                      → 6 experiments           │
│ ARGS    [ --epochs 200                 ]  (applied to all)         │
│ WHERE   ◉ Anywhere   ○ mclab only   ○ Kaggle only                  │
│         ↳ 2 mclab slots free · 2 Kaggle accounts free (57.4h left) │
│ RETRY   [1] attempt(s)   ▣ add --force on retry                    │
│ NAME    [ ablation-clinicdb-sep        ]  (optional — groups them) │
├────────────────────────────────────────────────────────────────────┤
│ ⓘ Est. 31.2h total · 4 parallel · finishes ~Wed 14:00              │
│   estimate tier 2 (epochs × measured rate) — not measured hours    │
│ ⚠ mamba_t_clinicdb: no Kaggle dataset mapping → mclab only  [Map]  │
├────────────────────────────────────────────────────────────────────┤
│                                   [ Cancel ]  [ Launch 6 runs ]    │
└────────────────────────────────────────────────────────────────────┘
```

Two details that are not decoration:

- **The estimate declares its tier.** `backend/estimates.py` already tracks
  which of the three tiers produced a number, and
  `EXPERIMENT_AUTOMATION_PLAN.md` §9 states outright that the greedy is only
  as good as `est_hours`. A tier-3 guess must not render as confidently as
  measured hours.
- **Preflight warnings are the UI-layer fix for D6/D7.** The unmapped
  dataset is surfaced *at composition time*, with the fix one click away —
  rather than at dispatch time, as a blocked row, mislabelled as a quota
  problem.

### 6.6 Results, Compute, Data, Settings

**Results** — run group tree + seed×fold heatmap + compare, all of which
already exist (`static/js/views/runs.js`, `components/heatmap-grid.js`) and
port mostly as-is. Reports become a tab here. History becomes a "Files"
drawer. TensorBoard becomes a toolbar button, not a tab.

**Compute** — Slots & quota (the real capacity model from §3.3, with
weekly usage sparklines from `usage_history()`), Kaggle accounts
(credentials, scope, budget, validate — all existing routes), local
concurrency + pause, notification channels (today split across two places
driving the same `backend/notifications.py`), machine-stat monitors.
**No worker management.**

**Data** — dataset fragments (existing), the **Kaggle slug mapping editor**
(new, §3.4, with provenance shown per entry: config-declared / XDash map /
profile default), channel preview (existing), test-eval audit (existing).

**Settings** — repo profiles + switcher, resolved paths, notebook
templates, bridge/tmux/kaggle health.

### 6.7 Cross-cutting

- **One status vocabulary.** `CANONICAL_STATUSES` from
  `backend/runners/base.py:16` everywhere; the pool's native string always
  kept as secondary text/tooltip.
- **Command palette survives.** `static/js/palette.js` is good; port it and
  extend it to actions ("run mkunet_t_clinicdb seeds 0-2") not just
  navigation.
- **Deep links.** Every surface addressable (`#/experiments/mkunet_t_clinicdb-s0/live`)
  so §3.5's blocked actions and notifications can link into the exact view.
- **Keyboard.** `/` filter, `j/k` row nav, `Enter` inspector, `r` run
  selected, `⌘K` palette.
- **Mobile.** The current CSS already has a hamburger/backdrop pattern;
  Lab and Experiments must stay usable at ~400px, since "is it done yet" is
  a phone question.

### 6.8 Migration: strangler, not big-bang

1. Mount the Vite build at `/v2`; old app stays at `/`.
2. Port **Lab** first — it is the highest-value view and needs only
   `/api/pulse` + `/api/slots`.
3. Port **Experiments** + **Run Composer**. At this point `/v2` is
   self-sufficient for the daily loop; flip the default.
4. Port Results, Compute, Data, Settings.
5. Delete `static/app.js`, `static/js/`, `static/index.html`, and the
   retired routes from §3.7 in one commit.

You are shippable at every step, and step 3 is where the redesign actually
pays off.

---

## 7. Phase D — long-run resume, done properly (deferred, spec'd)

Not built in this plan; specified so Phase B does not foreclose it. Phase A
deletes the current implementation (§4.A1) precisely so this can be built
once, correctly.

Prerequisites, in order:

1. **Per-experiment kernel slug** — lands free with §3.3.
2. **`EXPERIMENT_AUTOMATION_PLAN.md` §8.1's budget split** —
   `session_limit_hours` / `setup_reserve_hours` / `teardown_reserve_hours`,
   with `setup_reserve_hours` **measured** from §4.A7's `xdash_status.json`,
   not guessed. `train_budget_hours` is passed as `--max-hours`.
3. **`dissert/XDASH_RESUME_CONTRACT.md`'s four asks** — `--max-hours`, an
   `interrupted` manifest status, fold-split reuse, `describe_run()`.
   Without these, classification is the degraded heuristic §8.2 already
   described, which cannot distinguish a budget stop from a crash.
4. **Checkpoint transport is a Kaggle dataset, never a kernel push** (D2) —
   `datasets create` / `datasets version -t -d`, one live dataset per
   in-flight experiment, deleted on `done`.

The loop guards from §8.2 (`max_legs`, stop on `epochs_completed == 0`, stop
on no progress between legs, reserve each leg's hours) carry over unchanged
— they were the soundest part of that section.

**The load-bearing principle, restated because it is what the current
implementation lost:** the design must never depend on what Kaggle does to
a session it kills. Training self-limits, saves, and exits 0.

---

## 8. Sequencing and acceptance

| Phase | Content | Independently shippable? | Acceptance |
| --- | --- | --- | --- |
| **A** | §4 correctness, D1-D12 | Yes | §4's four-row mixed-feasibility batch, survives `kill -9`, distinct blocked codes, readable Kaggle failure |
| **B** | §3 object model + §5 API | Yes (old UI keeps working against a shim) | Experiments/Attempts/Slots served; worker routes gone; `/api/pulse` under 200 ms warm |
| **C1** | Vite scaffold + Lab at `/v2` | Yes | Lab answers the three questions of §6.3 with no other tab open |
| **C2** | Experiments + Run Composer; flip default | Yes | A 2-config × 3-seed run, from opening the dashboard to launched, in **one dialog** |
| **C3** | Results, Compute, Data, Settings; delete old frontend | Yes | §6.2's old→new table passes in full |
| **D** | Resume via dataset-versioned legs | Yes | One over-budget experiment through two legs, same `run_id`, one ledger row |

## 9. Non-goals and risks

- **No new parallelism inside a Kaggle account.** One kernel per account is
  a platform limit. Deleting the worker concept does not reduce throughput
  because workers never provided any (§1.1).
- **Quota remains self-tracked**, from local manifests plus §2.3's in-flight
  reservation. It is a soft throttle and misses kernels run outside XDash.
- **No simultaneous cross-repo dispatch** — `EXPERIMENT_AUTOMATION_PLAN.md`
  §5's profile-switch guard still applies, unchanged.
- **The greedy is only as good as `est_hours`.** Unchanged from §9 of the
  old plan; the mitigation is now visible in the UI (§6.5 shows the tier)
  rather than only documented.
- **Committed build output is a real cost.** `static/dist/` in git means
  merge noise and a build step in the release ritual. The alternative —
  requiring Node to *run* XDash — was judged worse. Revisit if the dist
  diff becomes painful.
- **Deleting the worker record is a one-way migration.** Three accounts
  currently hold auto-created workers (`auto-dissert-tanvir`,
  `auto-dissert-emon`, `auto-dissert-syfur`), none of which carry state
  worth preserving beyond `dataset_sources` — which §3.4 relocates to the
  dataset map. Migrate those three values, then drop the records.
