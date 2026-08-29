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

async function loadRuns() {
  const listBody = document.getElementById("run-group-list-body");
  listBody.innerHTML = `<div class="empty-state">Loading…</div>`;
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

  let html = "";
  state.runGroups.forEach((group, idx) => {
    const first = group.runs[0] || {};
    const logging = (first.resolved_config && first.resolved_config.logging) || {};
    const label = logging.experiment_name || group.config_hash.slice(0, 12);
    html += `<div class="category">
      <div class="category-label" title="config_hash ${escapeHtml(group.config_hash)}">
        ${escapeHtml(label)} <span style="color:var(--text-faint);">(${escapeHtml(group.config_hash.slice(0, 7))})</span>
      </div>
      <div class="run-group-grid" data-group-idx="${idx}">${renderSeedFoldGrid(group.runs)}</div>
    </div>`;
  });
  listBody.innerHTML = html;
  listBody.querySelectorAll(".run-group-grid").forEach((el) => wireSeedFoldGrid(el, selectRun));
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
  actionsEl.innerHTML = `<button class="btn btn-sm btn-ghost" id="btn-copy-resolved-config">Copy resolved config (YAML)</button>`;
  document.getElementById("btn-copy-resolved-config").addEventListener("click", () => copyResolvedConfig(run));

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

  let html = kvRows(rows);

  if (run.error) {
    html += `<div class="empty-state" style="color:var(--red); text-align:left; padding:10px 12px; border:1px solid rgba(229,72,77,0.35); border-radius:6px; margin:14px 0;">
      <b>Error</b><br>${escapeHtml(run.error)}
    </div>`;
  }

  if (run.nondeterministic_ops && run.nondeterministic_ops.length) {
    html += `<div class="empty-state" style="color:var(--red); text-align:left; padding:10px 12px; border:1px solid rgba(229,72,77,0.35); border-radius:6px; margin:14px 0;">
      <b>Non-deterministic ops recorded</b> — this run is not guaranteed bit-reproducible:<br>
      ${run.nondeterministic_ops.map((n) => escapeHtml(n)).join("<br>")}
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
}

initRunsButtons();
