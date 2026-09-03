// static/js/views/runs.js
//
// Runs Hub: groups runs recorded under artifacts/ by config_hash (two runs
// sharing a hash differ only by seed/fold, not by what's being tested —
// see orchestration/runid.py's own docstring on that distinction), with a
// seed x fold status grid per group and a manifest-backed detail panel.
//
// Reads /api/runs and /api/runs/<id> (backend/ledger.py) — plain file
// reads on the server side, so this view works even against a host repo
// with no orchestration package at all; it just shows zero groups instead
// of erroring (see IMPLEMENTATION_PLAN.md's "degrade honestly" principle).
//
// Deliberately a plain classic <script>, like app.js/badge.js/
// heatmap-grid.js — uses api()/toast()/escapeHtml()/state/
// renderStatusBadge()/renderSeedFoldGrid()/wireSeedFoldGrid() as
// page-global bindings rather than ES-module imports (see
// IMPLEMENTATION_PLAN.md Phase 7 for the eventual full split).

state.runGroups = [];
state.selectedRunId = null;
state.runFilter = "";
state.runCompareMode = false;
state.runCompareSelection = new Set();
state.runNotes = {}; // run_id -> {tag, note, updated_at}, from GET /api/runs/notes

async function loadRuns() {
  const listBody = document.getElementById("run-group-list-body");
  listBody.innerHTML = `<div class="empty-state">Loading…</div>`;
  // Notes first (small, fast) so renderRunGroups (triggered by loadRunGroups)
  // already has them for the has-note cell marker on its first paint.
  await loadRunNotes();
  await Promise.all([loadRunGroups(), loadComputeSummary()]);
}

async function loadRunGroups() {
  const listBody = document.getElementById("run-group-list-body");
  try {
    const data = await api("/api/runs");
    state.runGroups = data.groups || [];
    renderRunGroups();
  } catch (e) {
    listBody.innerHTML = `<div class="empty-state">Failed to load runs: ${escapeHtml(e.message)}</div>`;
  }
}

async function loadRunNotes() {
  try {
    state.runNotes = await api("/api/runs/notes");
  } catch (e) {
    state.runNotes = state.runNotes || {};
  }
}

// The Compute ledger (artifacts/ledger/compute.csv) is a project-wide
// GPU-hour/wall-time tally, not per-run detail (that's already on each
// run's own manifest — see renderRunDetail's "GPU-hours" row) — surfaced
// here as a running total against the spec's stated training budget
// (Technical_Framework_Spec.md §18: ~1,230 GPU-hours), so "how much have
// we spent" is a glance instead of manual spreadsheet math.
async function loadComputeSummary() {
  const el = document.getElementById("compute-summary-strip");
  try {
    const data = await api("/api/ledger/compute");
    const rows = data.rows || [];
    if (!rows.length) {
      el.innerHTML = "";
      return;
    }
    const sum = (key) => rows.reduce((total, r) => total + (parseFloat(r[key]) || 0), 0);
    const totalGpuHours = sum("gpu_hours");
    const totalWallHours = sum("wall_seconds") / 3600;
    el.innerHTML =
      `<div class="compute-summary-chip"><b>${totalGpuHours.toFixed(2)}</b>GPU-hours logged</div>` +
      `<div class="compute-summary-chip"><b>${totalWallHours.toFixed(2)}</b>wall-hours</div>` +
      `<div class="compute-summary-chip"><b>${rows.length}</b>compute record${rows.length === 1 ? "" : "s"}</div>`;
  } catch (e) {
    el.innerHTML = "";
  }
}

function groupLabel(group) {
  const first = group.runs[0] || {};
  const logging = (first.resolved_config && first.resolved_config.logging) || {};
  return logging.experiment_name || group.config_hash.slice(0, 12);
}

function matchesRunFilter(group, label) {
  const q = state.runFilter.trim().toLowerCase();
  if (!q) return true;
  if (label.toLowerCase().includes(q)) return true;
  if (group.config_hash.toLowerCase().includes(q)) return true;
  return group.runs.some((r) => (r.status || "").toLowerCase().includes(q) || (r.git && r.git.commit || "").toLowerCase().includes(q));
}

// Best run in a group by ledger.best_metric, direction-aware via the same
// HIGHER_IS_BETTER/LOWER_IS_BETTER sets the Reports tab's own compare view
// uses (app.js) — undefined for a metric this app doesn't recognize, same
// "don't guess a direction" rule compareReports() already follows.
// The ledger's monitor_metric records the literal training-time checkpoint
// key (e.g. "val_dice", the name actually used to pick the best epoch),
// while HIGHER_IS_BETTER/LOWER_IS_BETTER (app.js) were built for eval
// report.json's metric keys, which drop that val_/train_ prefix — without
// stripping it here, a real project's own monitor_metric would never match
// either set and best-run highlighting would silently never fire.
function _normalizedMetricKey(key) {
  return (key || "").replace(/^(val_|train_)/, "");
}

function computeBestRun(runs) {
  const monitorRow = runs.find((r) => r.ledger && r.ledger.monitor_metric);
  const monitorKey = monitorRow && monitorRow.ledger.monitor_metric;
  const normKey = _normalizedMetricKey(monitorKey);
  if (!monitorKey || !(HIGHER_IS_BETTER.has(normKey) || LOWER_IS_BETTER.has(normKey))) return null;
  const lowerBetter = LOWER_IS_BETTER.has(normKey);
  let best = null;
  for (const r of runs) {
    const v = r.ledger && parseFloat(r.ledger.best_metric);
    if (v === undefined || isNaN(v)) continue;
    if (!best || (lowerBetter ? v < best.v : v > best.v)) best = { runId: r.run_id, v };
  }
  return best ? best.runId : null;
}

function groupSummaryStats(runs) {
  const values = runs.map((r) => r.ledger && parseFloat(r.ledger.best_metric)).filter((v) => v !== undefined && !isNaN(v));
  if (values.length < 2) return null;
  const mean = values.reduce((a, b) => a + b, 0) / values.length;
  const variance = values.reduce((a, b) => a + (b - mean) ** 2, 0) / values.length;
  const monitorRow = runs.find((r) => r.ledger && r.ledger.monitor_metric);
  const label = (monitorRow && monitorRow.ledger.monitor_metric) || "best_metric";
  return `${escapeHtml(label)} ${mean.toFixed(3)} ± ${Math.sqrt(variance).toFixed(3)} (n=${values.length})`;
}

function renderRunGroups() {
  const listBody = document.getElementById("run-group-list-body");
  const countEl = document.getElementById("run-group-count");
  const totalRuns = state.runGroups.reduce((n, g) => n + g.runs.length, 0);
  countEl.textContent = state.runGroups.length
    ? `${state.runGroups.length} config${state.runGroups.length === 1 ? "" : "s"} · ${totalRuns} run${totalRuns === 1 ? "" : "s"}`
    : "";

  if (!state.runGroups.length) {
    listBody.innerHTML = `<div class="empty-state">No runs recorded yet under artifacts/runs/. A run started through the orchestration layer (or anything that writes a manifest.json there) will show up here, grouped by config hash.</div>`;
    return;
  }

  const visibleGroups = state.runGroups
    .map((group, idx) => ({ group, idx, label: groupLabel(group) }))
    .filter(({ group, label }) => matchesRunFilter(group, label));

  if (!visibleGroups.length) {
    listBody.innerHTML = `<div class="empty-state">No config groups match "${escapeHtml(state.runFilter)}".</div>`;
    return;
  }

  let html = "";
  visibleGroups.forEach(({ group, idx, label }) => {
    const notedRunIds = new Set(group.runs.filter((r) => state.runNotes[r.run_id]).map((r) => r.run_id));
    const bestRunId = computeBestRun(group.runs);
    const stats = groupSummaryStats(group.runs);
    html += `<div class="category">
      <div class="category-label" title="config_hash ${escapeHtml(group.config_hash)}">
        <span>${escapeHtml(label)} <span style="color:var(--text-faint);">(${escapeHtml(group.config_hash.slice(0, 7))})</span></span>
        <span class="run-count">${group.runs.length} run${group.runs.length === 1 ? "" : "s"}</span>
      </div>
      ${stats ? `<div class="entity-card-sub" style="padding:0 10px 4px;">${stats}</div>` : ""}
      <div class="run-group-grid" data-group-idx="${idx}">${renderSeedFoldGrid(group.runs, { bestRunId, notedRunIds })}</div>
    </div>`;
  });
  listBody.innerHTML = html;
  listBody.querySelectorAll(".run-group-grid").forEach((el) => wireSeedFoldGrid(el, handleRunCellClick));
}

// ----------------------------------------------------------- compare mode
function handleRunCellClick(runId, el) {
  if (state.runCompareMode) toggleRunCompareSelection(runId, el);
  else selectRun(runId);
}

function toggleRunCompareMode() {
  state.runCompareMode = !state.runCompareMode;
  if (!state.runCompareMode) state.runCompareSelection.clear();
  document.getElementById("btn-run-compare-mode").textContent = state.runCompareMode ? "Exit compare mode" : "Compare runs";
  document.getElementById("btn-run-compare-mode").classList.toggle("btn-primary", state.runCompareMode);
  document.querySelectorAll(".seedfold-cell.selected").forEach((el) => el.classList.remove("selected"));
  updateRunCompareButton();
}

function toggleRunCompareSelection(runId, el) {
  if (state.runCompareSelection.has(runId)) {
    state.runCompareSelection.delete(runId);
    if (el) el.classList.remove("selected");
  } else {
    state.runCompareSelection.add(runId);
    if (el) el.classList.add("selected");
  }
  updateRunCompareButton();
}

function updateRunCompareButton() {
  const btn = document.getElementById("btn-compare-runs");
  const n = state.runCompareSelection.size;
  document.getElementById("run-compare-count").textContent = n;
  btn.classList.toggle("hidden", n < 2);
}

async function compareSelectedRuns() {
  const ids = Array.from(state.runCompareSelection);
  const bodyEl = document.getElementById("run-detail-body");
  document.getElementById("run-detail-title").textContent = `Comparing ${ids.length} runs`;
  document.getElementById("run-detail-subtitle").textContent = "";
  document.getElementById("run-detail-actions").innerHTML = "";
  bodyEl.innerHTML = `<div class="empty-state">Loading…</div>`;
  try {
    const runs = await Promise.all(ids.map((id) => api(`/api/runs/${encodeURIComponent(id)}`)));
    renderRunComparison(runs);
  } catch (e) {
    bodyEl.innerHTML = `<div class="empty-state">Couldn't load one or more runs: ${escapeHtml(e.message)}</div>`;
  }
}

function renderRunComparison(runs) {
  const headerCells = runs.map((r) => `<th>${escapeHtml(r.run_id)}<br><span style="font-weight:400; color:var(--text-faint);">seed ${r.seed}${r.fold !== null && r.fold !== undefined ? ", fold " + r.fold : ""}</span></th>`).join("");

  const summaryRows = [
    ["Status", runs.map((r) => r.status || "–")],
    ["GPU-hours", runs.map((r) => (r.gpu_hours === null || r.gpu_hours === undefined ? "–" : Number(r.gpu_hours).toFixed(3)))],
    ["Duration", runs.map((r) => (r.start_time && r.end_time ? fmtDuration(r.start_time, r.end_time) : "–"))],
    ["best_metric", runs.map((r) => (r.ledger && r.ledger.best_metric) || "–")],
    ["monitor_metric", runs.map((r) => (r.ledger && r.ledger.monitor_metric) || "–")],
  ];

  const flatConfigs = runs.map((r) => flattenObj(r.resolved_config || {}));
  const allKeys = [];
  const seenK = new Set();
  for (const fc of flatConfigs) for (const k in fc) if (!seenK.has(k)) { seenK.add(k); allKeys.push(k); }
  const diffRows = allKeys.map((k) => {
    const values = flatConfigs.map((fc) => fc[k]);
    const distinct = new Set(values.map((v) => JSON.stringify(v)));
    return { key: k, values, differs: distinct.size > 1 };
  }).filter((row) => row.differs);

  document.getElementById("run-detail-body").innerHTML = `
    <div class="report-section">
      <h3>Summary</h3>
      <table class="compare-table">
        <thead><tr><th>Field</th>${headerCells}</tr></thead>
        <tbody>${summaryRows.map((row) => `<tr><td>${escapeHtml(row[0])}</td>${row[1].map((v) => `<td>${escapeHtml(String(v))}</td>`).join("")}</tr>`).join("")}</tbody>
      </table>
    </div>
    <div class="report-section">
      <h3>Config differences (${diffRows.length} of ${allKeys.length} keys differ)</h3>
      ${diffRows.length ? `<table class="compare-table">
        <thead><tr><th>Key</th>${headerCells}</tr></thead>
        <tbody>${diffRows.map((row) => `<tr><td>${escapeHtml(row.key)}</td>${row.values.map((v) =>
          `<td class="differs">${escapeHtml(Array.isArray(v) ? v.join(", ") : (v ?? "–"))}</td>`
        ).join("")}</tr>`).join("")}</tbody>
      </table>` : `<div class="empty-state" style="height:auto;padding:20px 0;">These configs are identical.</div>`}
    </div>
  `;
}

async function selectRun(runId) {
  state.selectedRunId = runId;
  document.getElementById("run-detail-title").textContent = runId;
  document.getElementById("run-detail-subtitle").textContent = "Loading…";
  document.getElementById("run-detail-actions").innerHTML = "";
  const bodyEl = document.getElementById("run-detail-body");
  bodyEl.innerHTML = `<div class="empty-state">Loading…</div>`;

  try {
    const run = await api(`/api/runs/${encodeURIComponent(runId)}`);
    renderRunDetail(run);
    loadRunPlots(runId);
  } catch (e) {
    bodyEl.innerHTML = `<div class="empty-state">Failed to load run: ${escapeHtml(e.message)}</div>`;
  }
}

function kvRows(rows) {
  return `<table class="kv-table">${rows
    .map(([k, v]) => `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(v === null || v === undefined ? "–" : String(v))}</td></tr>`)
    .join("")}</table>`;
}

const RUN_MANIFEST_KNOWN_KEYS = new Set([
  "run_id", "config_hash", "resolved_config", "seed", "fold", "git", "env_hash",
  "hardware", "status", "start_time", "end_time", "gpu_hours",
  "nondeterministic_ops", "error", "ledger",
]);

// torch's non-deterministic-op warning fires once per call to the op, not
// once per process (TORCH_WARN vs TORCH_WARN_ONCE) — an op used every
// training step can leave a run's nondeterministic_ops array with thousands
// of copies of the same string. training/determinism.py now dedupes on
// capture for new runs, but manifests already written before that fix (or
// by any other tool) can still carry the bloat, so collapse identical
// entries here too rather than flooding the panel with one red line each.
function renderNondeterministicOps(ops) {
  const counts = new Map();
  ops.forEach((op) => counts.set(op, (counts.get(op) || 0) + 1));
  return Array.from(counts.entries())
    .map(([op, count]) => escapeHtml(op) + (count > 1 ? ` <span style="color:var(--text-faint);">(×${count})</span>` : ""))
    .join("<br>");
}

function renderRunDetail(run) {
  const git = run.git || {};
  const hw = run.hardware || {};

  document.getElementById("run-detail-title").textContent = run.run_id;
  let subtitle = renderStatusBadge(run.status);
  if (git.dirty) {
    subtitle += ` <span class="badge red" title="This run started from an uncommitted git tree — the spec's reporting layer excludes dirty-tree runs from any reported table.">dirty tree</span>`;
  }
  document.getElementById("run-detail-subtitle").innerHTML = subtitle;

  const actionsEl = document.getElementById("run-detail-actions");
  actionsEl.innerHTML = `<button class="btn btn-sm btn-ghost" id="btn-copy-resolved-config">Copy resolved config (YAML)</button>
    <button class="btn btn-sm btn-ghost" id="btn-requeue-run" title="Send this run's config back to the Scheduler as a new train item">Re-run</button>`;
  document.getElementById("btn-copy-resolved-config").addEventListener("click", () => copyResolvedConfig(run));
  document.getElementById("btn-requeue-run").addEventListener("click", () => requeueRun(run));

  // A scannable hero row for the handful of fields that matter most at a
  // glance — the full kv-table below still carries everything, this is
  // purely a "don't make me read a table for the first 4 facts" shortcut.
  const heroStats = [
    ["Status", run.status || "–"],
    ["Seed", run.seed !== null && run.seed !== undefined ? run.seed : "–"],
    ["Fold", run.fold === null || run.fold === undefined ? "no CV" : run.fold],
    ["GPU-hours", run.gpu_hours === null || run.gpu_hours === undefined ? "–" : Number(run.gpu_hours).toFixed(2)],
    ["Duration", run.start_time && run.end_time ? fmtDuration(run.start_time, run.end_time) : "–"],
  ];
  let html = `<div class="entity-stat-row">${heroStats.map(([label, value]) =>
    `<div class="entity-stat"><div class="entity-stat-label">${escapeHtml(label)}</div><div class="entity-stat-value">${escapeHtml(String(value))}</div></div>`
  ).join("")}</div>`;

  html += `<div id="run-plots-gallery"></div>`;

  const note = state.runNotes[run.run_id] || { tag: "", note: "" };
  html += `<h4 class="run-detail-section-title">Notes</h4>
    <div class="scheduler-add-form" style="padding:10px 0;">
      <div class="field">
        <label>Tag</label>
        <input class="text-input" id="run-note-tag" value="${escapeHtml(note.tag || "")}" placeholder="e.g. baseline" />
      </div>
      <div class="field grow">
        <label>Note</label>
        <input class="text-input grow" id="run-note-text" value="${escapeHtml(note.note || "")}" placeholder="free text" />
      </div>
      <button class="btn btn-sm btn-primary" id="btn-save-run-note">Save</button>
    </div>`;

  const rows = [
    ["Config hash", run.config_hash],
    ["Seed", run.seed],
    ["Fold", run.fold === null || run.fold === undefined ? "— (no CV)" : run.fold],
    ["Status", run.status],
    ["Git commit", git.commit ? git.commit.slice(0, 12) : "–"],
    ["Git dirty", git.dirty === null || git.dirty === undefined ? "unknown" : git.dirty ? "yes" : "no"],
    ["Env hash", run.env_hash ? run.env_hash.slice(0, 12) : "–"],
    ["GPU", hw.gpu_name || (hw.cuda_available ? "CUDA (name unavailable)" : "CPU")],
    ["CUDA", hw.cuda_version || "–"],
    ["Host RAM", hw.host_ram_gb ? `${hw.host_ram_gb} GB` : "–"],
    ["Start time", run.start_time],
    ["End time", run.end_time],
    ["GPU-hours", run.gpu_hours === null || run.gpu_hours === undefined ? "–" : Number(run.gpu_hours).toFixed(3)],
  ];

  html += kvRows(rows);

  if (run.error) {
    html += `<div class="alert-box red">
      <b>Error</b>${escapeHtml(run.error)}
    </div>`;
  }

  if (run.nondeterministic_ops && run.nondeterministic_ops.length) {
    html += `<div class="alert-box red">
      <b>Non-deterministic ops recorded</b> this run is not guaranteed bit-reproducible:<br>
      ${renderNondeterministicOps(run.nondeterministic_ops)}
    </div>`;
  }

  if (run.ledger) {
    const ledgerRows = Object.entries(run.ledger).filter(([k]) => !["run_id", "config_hash", "git_commit", "git_dirty", "manifest_path"].includes(k));
    if (ledgerRows.length) {
      html += `<h4 class="run-detail-section-title">Runs-ledger record</h4>${kvRows(ledgerRows.map(([k, v]) => [k, v]))}`;
    }
  }

  const extraKeys = Object.keys(run).filter((k) => !RUN_MANIFEST_KNOWN_KEYS.has(k));
  if (extraKeys.length) {
    html += `<h4 class="run-detail-section-title">Recorded extras</h4>${kvRows(extraKeys.map((k) => [k, JSON.stringify(run[k])]))}`;
  }

  document.getElementById("run-detail-body").innerHTML = html;
  document.getElementById("btn-save-run-note").addEventListener("click", () => saveRunNote(run.run_id));
}

// ----------------------------------------------------------- training-curve plots
// logs_dir/<experiment_name>/plots/*.png — the training pipeline's own
// already-rendered matplotlib output (see backend/ledger.py's
// find_experiment_plots docstring for why this beats re-deriving a chart
// from the raw log client-side).
async function loadRunPlots(runId) {
  const el = document.getElementById("run-plots-gallery");
  if (!el) return; // user already navigated to a different run/view
  try {
    const data = await api(`/api/runs/${encodeURIComponent(runId)}/plots`);
    renderRunPlotsGallery(runId, data.plots || []);
  } catch (e) {
    if (state.selectedRunId === runId && el.isConnected) el.innerHTML = "";
  }
}

function renderRunPlotsGallery(runId, plots) {
  const el = document.getElementById("run-plots-gallery");
  if (!el || state.selectedRunId !== runId) return; // a later selectRun() already replaced this pane
  if (!plots.length) { el.innerHTML = ""; return; }
  el.innerHTML = `<h4 class="run-detail-section-title">Training curves</h4>
    <div class="plots-gallery">
      ${plots.map((f) => {
        const src = `/api/runs/${encodeURIComponent(runId)}/plots/${encodeURIComponent(f)}`;
        const caption = f.replace(/\.png$/i, "").replace(/^epoch_/, "");
        return `<a href="${src}" target="_blank" rel="noopener" class="plots-gallery-item" title="${escapeHtml(f)}">
          <img src="${src}" loading="lazy" alt="${escapeHtml(f)}" />
          <span>${escapeHtml(caption)}</span>
        </a>`;
      }).join("")}
    </div>`;
}

async function saveRunNote(runId) {
  const tag = document.getElementById("run-note-tag").value.trim();
  const note = document.getElementById("run-note-text").value.trim();
  try {
    const result = await api(`/api/runs/${encodeURIComponent(runId)}/note`, { method: "PUT", body: JSON.stringify({ tag, note }) });
    if (tag || note) state.runNotes[runId] = result;
    else delete state.runNotes[runId];
    toast("Note saved", "ok");
    renderRunGroups(); // refresh the has-note marker in the grid
  } catch (e) {
    toast("Couldn't save note: " + e.message, "err");
  }
}

// ----------------------------------------------------------- re-run / requeue
async function requeueRun(run) {
  try {
    const data = await api(`/api/runs/${encodeURIComponent(run.run_id)}/requeue-config`);
    quickAddSchedulerItem(data.config_path, "train", "", `Re-queued '${data.config_path}' from run ${run.run_id}`);
  } catch (e) {
    toast("Couldn't re-run: " + e.message, "err");
  }
}

// ----------------------------------------------------------- export
function _runsForExport() {
  return state.runGroups
    .map((g) => ({ group: g, label: groupLabel(g) }))
    .filter(({ group, label }) => matchesRunFilter(group, label))
    .flatMap(({ group, label }) => group.runs.map((r) => ({ ...r, _group_label: label })));
}

const RUN_EXPORT_FIELDS = ["run_id", "config_hash", "_group_label", "seed", "fold", "status", "gpu_hours", "start_time", "end_time"];

function exportRunsCsv() {
  const runs = _runsForExport();
  if (!runs.length) { toast("No runs to export (check the filter)", "err"); return; }
  const header = [...RUN_EXPORT_FIELDS.map((f) => (f === "_group_label" ? "experiment_name" : f)), "best_metric", "monitor_metric"];
  const escapeCsv = (v) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [header.join(",")];
  for (const r of runs) {
    const row = RUN_EXPORT_FIELDS.map((f) => r[f]);
    row.push((r.ledger && r.ledger.best_metric) || "", (r.ledger && r.ledger.monitor_metric) || "");
    lines.push(row.map(escapeCsv).join(","));
  }
  downloadBlob(lines.join("\n"), "runs_export.csv", "text/csv");
}

function exportRunsJson() {
  const runs = _runsForExport();
  if (!runs.length) { toast("No runs to export (check the filter)", "err"); return; }
  downloadBlob(JSON.stringify(runs, null, 2), "runs_export.json", "application/json");
}

function downloadBlob(content, filename, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  toast(`Exported ${filename}`, "ok");
}

function copyResolvedConfig(run) {
  if (!run.resolved_config) {
    toast("No resolved config recorded on this run", "err");
    return;
  }
  let yamlText;
  try {
    yamlText = jsyaml.dump(run.resolved_config);
  } catch (e) {
    toast("Couldn't serialize resolved config: " + e.message, "err");
    return;
  }
  navigator.clipboard.writeText(yamlText).then(
    () => toast("Resolved config copied as YAML"),
    () => toast("Clipboard write failed — check browser permissions", "err")
  );
}

function initRunsButtons() {
  document.getElementById("btn-refresh-runs").addEventListener("click", loadRuns);
  document.getElementById("run-group-filter").addEventListener("input", (e) => { state.runFilter = e.target.value; renderRunGroups(); });
  document.getElementById("btn-run-compare-mode").addEventListener("click", toggleRunCompareMode);
  document.getElementById("btn-compare-runs").addEventListener("click", compareSelectedRuns);
  document.getElementById("btn-export-runs-csv").addEventListener("click", exportRunsCsv);
  document.getElementById("btn-export-runs-json").addEventListener("click", exportRunsJson);
}

initRunsButtons();
