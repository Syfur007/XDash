// static/js/views/overview.js
//
// Overview (DASHBOARD_REDESIGN_PLAN.md §3.1) — the landing page. A fleet
// snapshot: one capacity card per runner (GET /api/runners) and a merged
// recent-activity feed. The activity feed is a client-side merge of two
// already-existing, already-timestamped event logs — the Kaggle poller's
// per-worker history[] (GET /api/kaggle/accounts) and Scheduler's finished
// items (GET /api/scheduler) — rather than a new backend log, since both
// already carry everything needed and merging them here adds no new state
// to keep in sync.
//
// Same classic-<script>-sharing-global-scope model as every other view file.

state.overviewLoaded = false;

async function loadOverview() {
  state.overviewLoaded = true;
  await Promise.all([loadOverviewRunners(), loadOverviewActivity()]);
}

async function loadOverviewRunners() {
  const body = document.getElementById("overview-runners-body");
  if (!body) return;
  let data;
  try {
    data = await api("/api/runners");
  } catch (e) {
    body.innerHTML = `<div class="empty-state">Couldn't load runners.</div>`;
    return;
  }
  const runners = data.runners || [];
  if (!runners.length) {
    body.innerHTML = `<div class="empty-state">No runners configured.</div>`;
    return;
  }
  body.innerHTML = runners.map((r) => {
    const cap = r.capacity;
    const pct = cap.limit ? Math.min(100, Math.round((cap.used / cap.limit) * 100)) : 0;
    const budgetLine = cap.extra && cap.extra.hours_this_week !== undefined
      ? `<div class="entity-card-sub">${fmtNum(cap.extra.hours_this_week)}h used this week (self-tracked estimate)</div>` : "";
    return `<div class="entity-card">
      <div class="entity-card-accent ${r.kind === "local" ? "running" : "completed"}"></div>
      <div class="entity-card-body">
        <div class="entity-card-title">${escapeHtml(r.label)}</div>
        <div class="entity-card-sub">${cap.used} / ${cap.limit ?? "∞"} ${cap.unit === "budget_hours" ? "budget hours" : "slots"} in use</div>
        <div class="concurrency-bar-track"><div class="concurrency-bar-fill" style="width:${pct}%;"></div></div>
        ${budgetLine}
        <div class="entity-card-footer">
          ${Object.entries(r.capabilities).filter(([, v]) => v === true).map(([k]) => `<span class="badge slate">${escapeHtml(k.replace(/_/g, " "))}</span>`).join(" ")}
        </div>
      </div>
    </div>`;
  }).join("");
}

async function loadOverviewActivity() {
  const body = document.getElementById("overview-activity-body");
  if (!body) return;
  const events = [];
  try {
    const kaggleData = await api("/api/kaggle/accounts");
    for (const account of kaggleData.accounts || []) {
      for (const w of account.workers || []) {
        for (const h of w.history || []) {
          events.push({ at: h.at, text: `Kaggle · ${account.name}/${w.worker_id}: ${h.event}` });
        }
      }
    }
  } catch (e) { /* activity feed just shows less — never blocks the rest of the page */ }
  try {
    const schedData = await api("/api/scheduler");
    for (const item of schedData.items || []) {
      if (item.ended_at) {
        const label = item.experiment_name || item.config_path;
        events.push({ at: item.ended_at, text: `Scheduler · ${label} (${item.mode}): ${item.status}` });
      }
    }
  } catch (e) { /* same — degrade quietly */ }

  events.sort((a, b) => new Date(b.at) - new Date(a.at));
  const recent = events.slice(0, 15);
  if (!recent.length) {
    body.innerHTML = `<div class="empty-state">No recent activity yet.</div>`;
    return;
  }
  body.innerHTML = recent.map((e) =>
    `<div class="kaggle-history-row"><span class="kaggle-history-time">${escapeHtml(timeAgo(e.at))}</span>${escapeHtml(e.text)}</div>`
  ).join("");
}
