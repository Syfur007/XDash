// static/js/views/lab.js
//
// Lab (XDASH_V2_PLAN.md §6.3) — the control room. Answers exactly three
// questions, in this order: what is running? what is stuck, and why? what
// just finished? Backed entirely by GET /api/pulse (+ the slot detail
// already embedded in it) so opening this tab costs one request, not five —
// see backend/experiments.py's get_pulse() docstring for why that matters.
//
// Replaces the old Overview tab (formerly static/js/views/overview.js) —
// same nav slot, same "loads first / is the default landing view" role, now
// backed by Phase B's Experiment/Attempt model instead of GET /api/runners +
// a client-merged activity feed.
//
// Same classic-<script>-sharing-global-scope model as every other view file.

state.labPollTimer = null;

async function loadLab() {
  let pulse;
  try {
    pulse = await api("/api/pulse");
  } catch (e) {
    const body = document.getElementById("lab-slots-body");
    if (body) body.innerHTML = `<div class="empty-state">Couldn't load the Lab view: ${escapeHtml(e.message)}</div>`;
    return;
  }
  renderLabSlots(pulse.slots || []);
  renderLabRunning(pulse.running || []);
  renderLabBlocked(pulse.blocked || []);
  renderLabCounts(pulse);
  renderLabRecent(pulse.recent || []);
}

function startLabPolling() {
  stopLabPolling();
  state.labPollTimer = setInterval(loadLab, 5000);
}

function stopLabPolling() {
  if (state.labPollTimer) { clearInterval(state.labPollTimer); state.labPollTimer = null; }
}

// ---------------------------------------------------------------- capacity
function renderLabSlots(slots) {
  const body = document.getElementById("lab-slots-body");
  if (!body) return;
  if (!slots.length) {
    body.innerHTML = `<div class="empty-state">No slots configured — register a Kaggle account or check the local scheduler.</div>`;
    return;
  }
  body.innerHTML = slots.map(labSlotCardHtml).join("");
}

function labSlotCardHtml(s) {
  const pct = s.limit ? Math.min(100, Math.round((s.used / s.limit) * 100)) : 0;
  const full = s.limit != null && s.used >= s.limit;
  let sub;
  if (s.kind === "kaggle") {
    const remaining = s.remaining_hours;
    sub = `${fmtNum(s.hours_this_week)}h / ${fmtNum(s.weekly_budget_hours)}h this week`
      + (remaining != null && remaining <= 0 ? ` · resets ${escapeHtml(timeAgo(s.clears_at))}` : "");
  } else {
    sub = s.paused ? "paused" : `${s.used} / ${s.limit} slot${s.limit === 1 ? "" : "s"} in use`;
  }
  return `<div class="entity-card">
    <div class="entity-card-accent ${full ? "running" : "completed"}"></div>
    <div class="entity-card-body">
      <div class="entity-card-title">${escapeHtml(s.slot)}</div>
      <div class="entity-card-sub">${escapeHtml(sub)}</div>
      <div class="concurrency-bar-track"><div class="concurrency-bar-fill" style="width:${pct}%;"></div></div>
    </div>
  </div>`;
}

// ---------------------------------------------------------------- running
function renderLabRunning(running) {
  const body = document.getElementById("lab-running-body");
  const count = document.getElementById("lab-running-count");
  if (count) count.textContent = String(running.length);
  if (!body) return;
  body.innerHTML = running.length
    ? running.map(labRunningCardHtml).join("")
    : `<div class="empty-state">Nothing running.</div>`;
}

function labRunningCardHtml(exp) {
  const a = exp.current_attempt || {};
  const stages = (a.stages || [])
    .map((s) => `<span class="badge ${statusBadgeClass(s.status)}">${escapeHtml(s.name)}: ${escapeHtml(s.status)}</span>`)
    .join(" ");
  const seed = exp.seed === null || exp.seed === undefined ? "" : ` · seed ${escapeHtml(String(exp.seed))}`;
  return `<div class="entity-card">
    <div class="entity-card-accent running"></div>
    <div class="entity-card-body">
      <div class="entity-card-title">${escapeHtml(exp.experiment_id)}</div>
      <div class="entity-card-sub">${escapeHtml(a.slot || "")}${seed} · ${escapeHtml(timeAgo(a.started_at))}</div>
      <div class="entity-card-footer">${stages}</div>
    </div>
  </div>`;
}

// ---------------------------------------------------------------- blocked
// The single most important element of this view (XDASH_V2_PLAN.md §6.3) —
// converts §3.5's structured blocked codes into something a person can act
// on, instead of a mystery table cell.
function renderLabBlocked(blocked) {
  const body = document.getElementById("lab-blocked-body");
  const count = document.getElementById("lab-blocked-count");
  if (count) count.textContent = String(blocked.length);
  if (!body) return;
  body.innerHTML = blocked.length
    ? blocked.map(labBlockedRowHtml).join("")
    : `<div class="empty-state">Nothing blocked.</div>`;
}

function labBlockedRowHtml(exp) {
  const b = (exp.current_attempt || {}).blocked || {};
  const clears = b.clears_at ? ` · clears ${escapeHtml(timeAgo(b.clears_at))}` : "";
  return `<div class="kaggle-history-row">
    <span class="kaggle-history-time">${escapeHtml(timeAgo(b.since))}</span>
    <span class="badge red">${escapeHtml(b.code || "blocked")}</span>
    <strong>${escapeHtml(exp.experiment_id)}</strong>
    — ${escapeHtml(b.detail || "")}${clears}
  </div>`;
}

// ---------------------------------------------------------------- counts + recent
function renderLabCounts(pulse) {
  const set = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = String(value ?? 0); };
  set("lab-queued-count", pulse.queued_count);
  set("lab-done-count", pulse.done_count);
  set("lab-failed-count", pulse.failed_count);
}

function renderLabRecent(recent) {
  const body = document.getElementById("lab-recent-body");
  if (!body) return;
  body.innerHTML = recent.length
    ? recent.map(labRecentRowHtml).join("")
    : `<div class="empty-state">Nothing recent.</div>`;
}

function labRecentRowHtml(exp) {
  const a = exp.current_attempt || {};
  // A failed attempt's `blocked.detail` carries the actual reason (dispatch error, or "unit
  // ended: <status>") — showing only raw_status here left a failure just as undiagnosable from
  // Recent as it was from the Experiments table before that surfaced the same field.
  const why = exp.status === "failed" && a.blocked ? a.blocked.detail : a.raw_status;
  return `<div class="kaggle-history-row">
    <span class="kaggle-history-time">${escapeHtml(timeAgo(a.ended_at))}</span>
    ${renderStatusBadge(exp.status)} <strong>${escapeHtml(exp.experiment_id)}</strong>
    ${why ? `— ${escapeHtml(why)}` : ""}
  </div>`;
}
