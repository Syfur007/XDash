// static/js/views/experiments.js
//
// Experiments tab wiring (DASHBOARD_REDESIGN_PLAN.md §3.3): Active/Queue/
// Runs & Results are Terminals/Scheduler/Runs' own pre-existing markup and
// render functions, relocated under one nav item with a subtab strip — see
// index.html and app.js's initSubtabStrip(). This file only adds the one
// genuinely new piece: a compact "also running on Kaggle" summary at the
// top of Active, sourced from GET /api/experiments/active, so Active
// answers "what's executing right now" across *every* runner instead of
// just mclab's tmux sessions — Kaggle's own full worker cards (push/
// refresh/download) still live on the Runners tab, this is a read-only
// glance, not a duplicate control surface.
//
// Same classic-<script>-sharing-global-scope model as every other view file.

async function loadExperimentsKaggleActive() {
  const body = document.getElementById("experiments-kaggle-active-body");
  if (!body) return;
  let data;
  try {
    data = await api("/api/experiments/active");
  } catch (e) {
    return; // leave whatever was last rendered rather than blanking it on a transient poll failure
  }
  const kaggleUnits = (data.units || []).filter((u) => u.runner_id.startsWith("kaggle:"));
  if (!kaggleUnits.length) {
    body.innerHTML = `<div class="empty-state">Nothing in flight on Kaggle right now — push a worker from the Runners tab.</div>`;
    return;
  }
  body.innerHTML = kaggleUnits.map((u) => `
    <div class="entity-card">
      <div class="entity-card-accent ${statusBadgeClass(u.status)}"></div>
      <div class="entity-card-body">
        <div class="entity-card-title" title="${escapeHtml(u.label)}">${escapeHtml(u.label)}
          <span class="mode-tag">${escapeHtml(u.runner_id.replace("kaggle:", ""))}</span>
        </div>
        <div class="entity-card-sub" title="${escapeHtml(u.config_path || "")}">${escapeHtml(u.config_path || "(no config on record)")}${u.mode ? " · " + escapeHtml(u.mode) : ""}</div>
        <div class="entity-card-footer">
          <span class="entity-card-status">${renderStatusBadge(u.raw_status)}</span>
        </div>
      </div>
    </div>`
  ).join("");
}

function initExperimentsSubtabs() {
  initSubtabStrip("experiments-subtabs", (key) => {
    if (key === "runs" && !state.runGroups.length) loadRuns();
  });
}

initExperimentsSubtabs();
