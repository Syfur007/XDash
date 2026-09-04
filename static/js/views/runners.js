// static/js/views/runners.js
//
// Runners tab (DASHBOARD_REDESIGN_PLAN.md §3.4): the fleet-management layer.
// The Kaggle account/worker panels, Machine Stats, and TensorBoard sections
// on this tab are Kaggle.js's/app.js's own pre-existing markup and render
// functions, relocated here under one nav item — see index.html. This file
// adds the one genuinely new piece: the mclab card, sourced from GET
// /api/runners (this runner's capacity) plus the system info app.js's
// loadSystem() already fetches at boot (no new fetch needed for repo_root/
// env_activate_cmd/tmux_available — reused, not re-requested).
//
// Same classic-<script>-sharing-global-scope model as every other view file.

async function loadRunnersOverview() {
  const body = document.getElementById("runner-local-body");
  if (!body) return;
  let data;
  try {
    data = await api("/api/runners");
  } catch (e) {
    return; // leave whatever was last rendered rather than blanking it on a transient poll failure
  }
  const local = (data.runners || []).find((r) => r.kind === "local");
  if (!local) return;
  const sys = state.system || {};
  const cap = local.capacity;
  const pct = cap.limit ? Math.min(100, Math.round((cap.used / cap.limit) * 100)) : 0;

  body.innerHTML = `
    <table class="kv-table">
      <tr><td>Repo root</td><td>${escapeHtml(sys.repo_root || "—")}</td></tr>
      <tr><td>Env activate</td><td>${escapeHtml(sys.env_activate_cmd || "(none configured — runs in the dashboard server's own environment)")}</td></tr>
      <tr><td>tmux</td><td>${cap.extra.tmux_available ? `<span class="badge emerald">available</span>` : `<span class="badge red">not found on PATH</span>`}</td></tr>
      <tr><td>Scheduler queue</td><td>${cap.extra.scheduler_paused ? `<span class="badge slate">paused</span>` : `<span class="badge emerald">running</span>`}</td></tr>
    </table>
    <div class="concurrency-row" style="padding:12px 0 0;">
      <span class="concurrency-label">Concurrency slots in use (Scheduler's own ceiling — adjustable on Experiments → Queue)</span>
      <span class="stepper-value">${cap.used} / ${cap.limit ?? "∞"}</span>
    </div>
    <div class="concurrency-bar-track"><div class="concurrency-bar-fill" style="width:${pct}%;"></div></div>
  `;
}
