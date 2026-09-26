// static/js/views/lab.js
//
// Lab (XDASH_PLAN.md §8.1, §10 Phase 5) — the control room. Needs attention
// first (blocked/failed, with a one-click fix), then Studies, Runtimes,
// Running now and Recent, each with its own ▦/☰ tile-or-list toggle
// remembered per section in localStorage (a per-viewer convenience only —
// every read/write is wrapped in try/catch and never load-bearing). All of
// it comes from one GET /api/pulse, polled through Phase 3's shared
// createPoller (js/lib/poller.js) instead of a second bespoke timer — this
// file used to run its own raw setInterval; that's retired in favor of the
// one poller every other Phase 3+ screen already uses.
//
// Replaces the pre-Phase-5 Lab (local+kaggle "slots" only, one flat Blocked
// list, no Studies/Runtimes sections, no tile/list choice) in the same nav
// slot — same "loads first / is the default landing view" role.
//
// Same classic-<script>-sharing-global-scope convention as every other view
// file: app.js (loaded first) calls loadLab()/startLabPolling()/
// stopLabPolling() from switchView()/boot(), both of which only ever RUN
// later (after every script, including this one, has finished loading) —
// see js/lib/router.js's own comment on why a same-named forward reference
// inside a function body is safe, unlike one at top-level parse time.

state.labPoller = null;   // createPoller instance, started/stopped by switchView()/boot()
state.lastPulse = null;   // the last successful GET /api/pulse, so a view-mode toggle can re-render without re-fetching

const LAB_SECTIONS = ["studies", "runtimes", "running", "recent"];

// ---------------------------------------------------------------- per-section tile/list memory
function labViewMode(section) {
  try { return localStorage.getItem("xdash.lab.view." + section) || "tiles"; }
  catch (e) { return "tiles"; }
}

function setLabViewMode(section, mode) {
  try { localStorage.setItem("xdash.lab.view." + section, mode); }
  catch (e) { /* per-viewer convenience only — never load-bearing */ }
  renderLabViewToggle(section);
  renderLabFromLastPulse();
}

function renderLabViewToggle(section) {
  const el = document.querySelector(`[data-lab-toggle="${section}"]`);
  if (!el) return;
  const mode = labViewMode(section);
  el.innerHTML =
    `<button class="btn-icon${mode === "tiles" ? " active" : ""}" data-lab-mode="tiles" title="Tiles">▦</button>` +
    `<button class="btn-icon${mode === "list" ? " active" : ""}" data-lab-mode="list" title="List">☰</button>`;
}

function initLabViewToggles() {
  LAB_SECTIONS.forEach((section) => {
    renderLabViewToggle(section);
    const el = document.querySelector(`[data-lab-toggle="${section}"]`);
    if (!el) return;
    el.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-lab-mode]");
      if (btn) setLabViewMode(section, btn.dataset.labMode);
    });
  });
}

// A future timestamp read as "~2h" rather than timeAgo()'s past-only "2h ago"
// (timeAgo clamps a negative diff to 0s, which would misread an ETA as "now").
function labEtaLabel(iso) {
  if (!iso) return "";
  const s = (new Date(iso).getTime() - Date.now()) / 1000;
  if (s <= 0) return "due now";
  if (s < 3600) return `~${Math.ceil(s / 60)}m`;
  if (s < 86400) return `~${Math.ceil(s / 3600)}h`;
  return `~${Math.ceil(s / 86400)}d`;
}

// ---------------------------------------------------------------- load + fan-out
async function loadLab() {
  let pulse;
  try {
    pulse = await api("/api/pulse");
  } catch (e) {
    const body = document.getElementById("lab-studies-body");
    if (body) body.innerHTML = `<div class="empty-state">Couldn't load the Lab view: ${escapeHtml(e.message)}</div>`;
    return;
  }
  state.lastPulse = pulse;
  renderLabFromLastPulse();
}

function renderLabFromLastPulse() {
  const pulse = state.lastPulse;
  if (!pulse) return;
  renderLabAttention(pulse);
  renderLabStudies(pulse.studies || []);
  renderLabRuntimes(pulse.runtimes || []);
  renderLabRunning(pulse.running || []);
  renderLabCounts(pulse);
  renderLabRecent(pulse.recent || []);
}

function startLabPolling() {
  if (!state.labPoller) state.labPoller = createPoller(loadLab, 5000);
  state.labPoller.start();
}

function stopLabPolling() {
  if (state.labPoller) state.labPoller.stop();
}

// ---------------------------------------------------------------- needs attention
// The single most important thing Lab can show (XDASH_PLAN.md §8.1): every
// blocked experiment (pulse.blocked, the full list) plus a recently-failed
// one. /api/pulse has no dedicated "every failed experiment" route — only
// `failed_count` plus `recent` (done+failed, capped at the last 10
// completions, XDASH_PROGRESS.md's Phase 1 section) — so a failure older
// than the last 10 completions won't surface here until it's retried, or a
// fuller failed-list route is added. Documented deviation, not a bug.
function renderLabAttention(pulse) {
  const panel = document.getElementById("lab-attention-panel");
  const body = document.getElementById("lab-attention-body");
  const countEl = document.getElementById("lab-attention-count");
  const blocked = (pulse.blocked || []).map((v) => ({ ...v, _kind: "blocked" }));
  const failed = (pulse.recent || []).filter((v) => v.status === "failed").map((v) => ({ ...v, _kind: "failed" }));
  const items = blocked.concat(failed);
  if (countEl) countEl.textContent = String(items.length);
  if (!panel || !body) return;
  panel.classList.toggle("hidden", items.length === 0);
  if (!items.length) { body.innerHTML = ""; return; }
  body.innerHTML = items.map(labAttentionRowHtml).join("");
  body.querySelectorAll("[data-lab-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.experimentId;
      const action = btn.dataset.labAction;
      if (action === "retry") labQuickRetry(id);
      // attempt.blocked only ever carries {code, detail}, never the dataset
      // name itself (backend/runners/*.py's can_accept() drops the extra
      // "dataset" key data_mode_for_experiment() returns before it reaches
      // the attempt) — the deep link degrades to the bare Datasets matrix
      // rather than guessing a name out of free-text detail.
      else if (action === "bind-data") navigateToView("data");
      else navigateToExperiment(id, "overview");
    });
  });
}

function labAttentionRowHtml(v) {
  const a = v.current_attempt || {};
  const b = a.blocked || {};
  let detailHtml, actionsHtml;
  if (v._kind === "blocked") {
    detailHtml = `blocked · ${escapeHtml(b.code || "blocked")}${b.detail ? " — " + escapeHtml(b.detail) : ""}`;
    const isDataIssue = /dataset/.test(b.code || "");
    actionsHtml = isDataIssue
      ? `<button class="btn btn-sm btn-ghost" data-lab-action="bind-data" data-experiment-id="${escapeHtml(v.experiment_id)}">Bind data</button>`
      : `<button class="btn btn-sm btn-ghost" data-lab-action="open" data-experiment-id="${escapeHtml(v.experiment_id)}">Open</button>`;
  } else {
    const why = b.detail || a.raw_status || "failed";
    const of = v.max_retries != null ? v.max_retries + 1 : (v.attempt_count || 1);
    detailHtml = `failed · ${escapeHtml(why)} (attempt ${v.attempt_count || 1}/${of})`;
    actionsHtml =
      `<button class="btn btn-sm btn-ghost" data-lab-action="open" data-experiment-id="${escapeHtml(v.experiment_id)}">Open</button>` +
      `<button class="btn btn-sm btn-primary" data-lab-action="retry" data-experiment-id="${escapeHtml(v.experiment_id)}">Retry</button>`;
  }
  return `<div class="lab-attention-row">
    ${renderStatusBadge(v.status)} <strong>${experimentLink(v.experiment_id)}</strong>
    <span style="color:var(--text-muted);">${detailHtml}</span>
    <span class="job-actions">${actionsHtml}</span>
  </div>`;
}

async function labQuickRetry(experimentId) {
  try {
    await api("/api/experiments/actions", { method: "POST", body: JSON.stringify({ action: "retry", ids: [experimentId] }) });
    toast(`Retrying ${experimentId}`, "ok");
    loadLab();
  } catch (e) {
    toast("Couldn't retry: " + e.message, "err");
  }
}

// ---------------------------------------------------------------- studies
function labStudyProgressPct(s) {
  const c = s.counts || {};
  const total = s.experiment_count || 0;
  if (!total) return 0;
  return Math.round(((c.done || 0) + (c.failed || 0) + (c.cancelled || 0)) / total * 100);
}

function labStudyAccent(s) {
  if (s.status === "attention") return "failed";
  if (s.status === "running") return "running";
  return "completed";
}

function labStudySubBits(s) {
  const c = s.counts || {};
  const bits = [];
  if (c.running) bits.push(`●${c.running} running`);
  if (c.blocked) bits.push(`⚠${c.blocked}`);
  if (c.draft) bits.push(`○${c.draft} draft`);
  return bits.join(" ");
}

function labStudyTileHtml(s) {
  const c = s.counts || {};
  const pct = labStudyProgressPct(s);
  const best = s.best_metric
    ? `best ${escapeHtml(s.best_metric.key)} ${fmtNum(s.best_metric.value)} ${experimentLink(s.best_metric.experiment_id)}` : "";
  return `<div class="entity-card runtime-tile" data-lab-study="${escapeHtml(s.study_id)}">
    <div class="entity-card-accent ${labStudyAccent(s)}"></div>
    <div class="entity-card-body">
      <div class="entity-card-title">${escapeHtml(s.name)}</div>
      <div class="entity-card-sub">${(c.done || 0) + (c.failed || 0) + (c.cancelled || 0)}/${s.experiment_count || 0}${s.autopilot && s.autopilot.enabled ? " · autopilot" : ""}</div>
      <div class="concurrency-bar-track"><div class="concurrency-bar-fill" style="width:${pct}%;"></div></div>
      <div class="entity-card-sub">${renderStatusBadge(s.status)} ${labStudySubBits(s)}</div>
      ${best ? `<div class="entity-card-sub">${best}</div>` : ""}
      ${s.eta ? `<div class="entity-card-sub">ETA ${labEtaLabel(s.eta)}</div>` : ""}
    </div>
  </div>`;
}

function labStudyRowHtml(s) {
  const c = s.counts || {};
  return `<div class="kaggle-history-row runtime-row" data-lab-study="${escapeHtml(s.study_id)}">
    ${renderStatusBadge(s.status)} <strong>${escapeHtml(s.name)}</strong>
    — ${(c.done || 0) + (c.failed || 0) + (c.cancelled || 0)}/${s.experiment_count || 0} ${labStudySubBits(s)}
    ${s.best_metric ? ` · best ${escapeHtml(s.best_metric.key)} ${fmtNum(s.best_metric.value)}` : ""}
    ${s.eta ? ` · ETA ${labEtaLabel(s.eta)}` : ""}
  </div>`;
}

function renderLabStudies(studies) {
  const countEl = document.getElementById("lab-studies-count");
  const body = document.getElementById("lab-studies-body");
  const active = studies.filter((s) => !s.archived);
  if (countEl) countEl.textContent = String(active.length);
  if (!body) return;
  if (!studies.length) { body.innerHTML = `<div class="empty-state">No studies yet — create one in Experiments.</div>`; return; }
  const mode = labViewMode("studies");
  body.className = mode === "tiles" ? "entity-grid" : "entity-list";
  body.innerHTML = active.map((s) => (mode === "tiles" ? labStudyTileHtml(s) : labStudyRowHtml(s))).join("")
    || `<div class="empty-state">No active studies (everything's archived).</div>`;
  body.querySelectorAll("[data-lab-study]").forEach((el) => {
    el.addEventListener("click", (e) => {
      if (e.target.closest("a")) return; // let a best_metric experiment link navigate itself
      navigateToView("experiments", { study: el.dataset.labStudy, tab: "experiments" });
    });
  });
}

// ---------------------------------------------------------------- runtimes
function labRuntimeAccent(state_) {
  if (state_ === "busy" || state_ === "online") return "running";
  if (state_ === "offline" || state_ === "unconfigured") return "failed";
  return "completed";
}

function labRuntimeBadgeClass(state_) {
  if (state_ === "busy" || state_ === "online") return "amber";
  if (state_ === "offline" || state_ === "unconfigured") return "red";
  return "slate";
}

function labRuntimeBarsHtml(r) {
  const cap = r.capacity || {};
  const capPct = cap.limit ? Math.min(100, Math.round((cap.used / cap.limit) * 100)) : 0;
  let quotaHtml = "";
  if (r.quota) {
    const qPct = r.quota.limit ? Math.min(100, Math.round(((r.quota.used || 0) / r.quota.limit) * 100)) : 0;
    quotaHtml = `<div class="entity-card-sub">${fmtNum(r.quota.used)} / ${fmtNum(r.quota.limit)} ${escapeHtml(r.quota.unit || "")} · ${escapeHtml(r.quota.source || "")}</div>
      <div class="concurrency-bar-track"><div class="concurrency-bar-fill" style="width:${qPct}%;"></div></div>`;
  }
  return `<div class="concurrency-bar-track"><div class="concurrency-bar-fill" style="width:${capPct}%;"></div></div>${quotaHtml}`;
}

function labRuntimeTileHtml(r) {
  const acc = r.accelerator ? `${escapeHtml(r.accelerator.name)}${r.accelerator.vram_gb ? " " + r.accelerator.vram_gb + "G" : ""}` : "no accelerator info";
  const cap = r.capacity || {};
  return `<div class="entity-card runtime-tile" data-lab-runtime="${escapeHtml(r.id)}">
    <div class="entity-card-accent ${labRuntimeAccent(r.state)}"></div>
    <div class="entity-card-body">
      <div class="entity-card-title">${escapeHtml(r.label)} <span class="mode-tag">${escapeHtml(r.kind)}</span></div>
      <div class="entity-card-sub">${escapeHtml(acc)} · ${escapeHtml(r.state)} · ${cap.used ?? 0}/${cap.limit ?? "∞"}</div>
      ${labRuntimeBarsHtml(r)}
      ${(r.running || []).length ? `<div class="entity-card-sub">running: ${r.running.map((x) => escapeHtml(x)).join(", ")}</div>` : ""}
    </div>
  </div>`;
}

function labRuntimeRowHtml(r) {
  const cap = r.capacity || {};
  return `<div class="kaggle-history-row runtime-row" data-lab-runtime="${escapeHtml(r.id)}">
    <span class="badge ${labRuntimeBadgeClass(r.state)}">${escapeHtml(r.state)}</span>
    <strong>${escapeHtml(r.label)}</strong> <span class="mode-tag">${escapeHtml(r.kind)}</span>
    — ${cap.used ?? 0}/${cap.limit ?? "∞"}${r.quota ? ` · ${fmtNum(r.quota.used)}/${fmtNum(r.quota.limit)} ${escapeHtml(r.quota.unit || "")}` : ""}
  </div>`;
}

function renderLabRuntimes(runtimes) {
  const countEl = document.getElementById("lab-runtimes-count");
  if (countEl) countEl.textContent = String(runtimes.length);
  const body = document.getElementById("lab-runtimes-body");
  if (!body) return;
  if (!runtimes.length) { body.innerHTML = `<div class="empty-state">No runtimes registered — see Compute.</div>`; return; }
  const mode = labViewMode("runtimes");
  body.className = mode === "tiles" ? "entity-grid" : "entity-list";
  body.innerHTML = runtimes.map((r) => (mode === "tiles" ? labRuntimeTileHtml(r) : labRuntimeRowHtml(r))).join("");
  body.querySelectorAll("[data-lab-runtime]").forEach((el) => {
    el.addEventListener("click", () => navigateToRuntime(el.dataset.labRuntime));
  });
}

// ---------------------------------------------------------------- running now
function labRunningStagesHtml(a) {
  return (a.stages || [])
    .map((s) => `<span class="badge ${statusBadgeClass(s.status)}">${escapeHtml(s.name)}: ${escapeHtml(s.status)}</span>`)
    .join(" ");
}

function labRunningCardHtml(exp) {
  const a = exp.current_attempt || {};
  const seed = exp.seed === null || exp.seed === undefined ? "" : ` · seed ${escapeHtml(String(exp.seed))}`;
  const study = (exp.studies || [])[0];
  return `<div class="entity-card">
    <div class="entity-card-accent running"></div>
    <div class="entity-card-body">
      <div class="entity-card-title">${experimentLink(exp.experiment_id)}</div>
      <div class="entity-card-sub">${study ? escapeHtml(study.name || study.study_id) + " · " : ""}${escapeHtml(a.slot || "")}${seed} · ${escapeHtml(fmtDuration(a.started_at))} elapsed</div>
      <div class="entity-card-footer">${labRunningStagesHtml(a)}</div>
    </div>
  </div>`;
}

function labRunningRowHtml(exp) {
  const a = exp.current_attempt || {};
  return `<div class="kaggle-history-row">
    ${renderStatusBadge(exp.status)} <strong>${experimentLink(exp.experiment_id)}</strong>
    — ${escapeHtml(a.slot || "")} · ${escapeHtml(fmtDuration(a.started_at))} elapsed
  </div>`;
}

function renderLabRunning(running) {
  const count = document.getElementById("lab-running-count");
  if (count) count.textContent = String(running.length);
  const body = document.getElementById("lab-running-body");
  if (!body) return;
  if (!running.length) { body.innerHTML = `<div class="empty-state">Nothing running.</div>`; return; }
  const mode = labViewMode("running");
  body.className = mode === "tiles" ? "entity-grid" : "entity-list";
  body.innerHTML = running.map((exp) => (mode === "tiles" ? labRunningCardHtml(exp) : labRunningRowHtml(exp))).join("");
}

// ---------------------------------------------------------------- counts + recent
function renderLabCounts(pulse) {
  const set = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = String(value ?? 0); };
  set("lab-queued-count", pulse.queued_count);
  set("lab-done-count", pulse.done_count);
  set("lab-failed-count", pulse.failed_count);
}

function labRecentRowHtml(exp) {
  const a = exp.current_attempt || {};
  const why = exp.status === "failed" && a.blocked ? a.blocked.detail : a.raw_status;
  return `<div class="kaggle-history-row">
    <span class="kaggle-history-time">${escapeHtml(timeAgo(a.ended_at))}</span>
    ${renderStatusBadge(exp.status)} <strong>${experimentLink(exp.experiment_id)}</strong>
    ${why ? `— ${escapeHtml(why)}` : ""}
  </div>`;
}

function labRecentTileHtml(exp) {
  const a = exp.current_attempt || {};
  const why = exp.status === "failed" && a.blocked ? a.blocked.detail : a.raw_status;
  return `<div class="entity-card">
    <div class="entity-card-accent ${exp.status === "done" ? "completed" : "failed"}"></div>
    <div class="entity-card-body">
      <div class="entity-card-title">${experimentLink(exp.experiment_id)}</div>
      <div class="entity-card-sub">${renderStatusBadge(exp.status)} ${escapeHtml(timeAgo(a.ended_at))}</div>
      ${why ? `<div class="entity-card-sub">${escapeHtml(why)}</div>` : ""}
    </div>
  </div>`;
}

function renderLabRecent(recent) {
  const body = document.getElementById("lab-recent-body");
  if (!body) return;
  if (!recent.length) { body.innerHTML = `<div class="empty-state">Nothing recent.</div>`; return; }
  const mode = labViewMode("recent");
  body.className = mode === "tiles" ? "entity-grid" : "";
  body.innerHTML = recent.map((exp) => (mode === "tiles" ? labRecentTileHtml(exp) : labRecentRowHtml(exp))).join("");
}

// ---------------------------------------------------------------- init
initLabViewToggles();
