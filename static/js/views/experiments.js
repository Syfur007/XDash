// static/js/views/experiments.js
//
// Experiments tab wiring (XDASH_V2_PLAN.md §6.4): the "Sessions" subtab is
// Terminals' own pre-existing markup and render functions, relocated under
// the Experiments nav item alongside the spine (Runs) and Configs (browse/
// create) subtabs — see index.html and app.js's initSubtabStrip(). This
// file only adds the one genuinely new piece: a compact "also running on
// Kaggle" / "also running under other repos" summary at the top of
// Sessions, sourced from GET /api/experiments/active and
// GET /api/repos/sessions, so Sessions answers "what's executing right
// now" across *every* runner and repo instead of just the local device's tmux
// sessions — Kaggle's own full account cards (push/refresh/download) still
// live on the Compute tab, this is a read-only glance, not a duplicate
// control surface.
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
    body.innerHTML = `<div class="empty-state">Nothing in flight on Kaggle right now — launch one from the Runs subtab's "+ Run".</div>`;
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

// Cross-repo visibility (MULTI_REPO_PLAN.md §6 option B): switching the
// active repo never hides another repo's live work, so this reads the
// backend's own cross-profile session list (backend/repos.py) rather than
// /api/terminals or /api/kaggle/accounts, both of which are scoped to
// whichever profile is currently active. Read-only glance, same spirit as
// loadExperimentsKaggleActive() above — full control over another repo's
// session still requires switching to it first.
async function loadExperimentsOtherRepos() {
  const body = document.getElementById("experiments-other-repos-body");
  if (!body) return;
  let data;
  try {
    data = await api("/api/repos/sessions");
  } catch (e) {
    return; // leave whatever was last rendered rather than blanking it on a transient poll failure
  }
  const activeProfile = state.system ? state.system.profile_name : null;
  const others = (data.sessions || []).filter((s) => s.profile && s.profile !== activeProfile);
  if (!others.length) {
    body.innerHTML = `<div class="empty-state">Nothing running under any other repo right now.</div>`;
    return;
  }
  body.innerHTML = others.map((s) => {
    const sub = s.kind === "kaggle" ? (s.account || "kaggle") : (s.config_path || "unmanaged session");
    return `
    <div class="entity-card">
      <div class="entity-card-accent ${statusBadgeClass(s.status)}"></div>
      <div class="entity-card-body">
        <div class="entity-card-title" title="${escapeHtml(s.label)}">${escapeHtml(s.label)}
          <span class="mode-tag" title="repo profile">${escapeHtml(s.profile)}</span>
        </div>
        <div class="entity-card-sub" title="${escapeHtml(sub)}">${escapeHtml(sub)}${s.mode ? " · " + escapeHtml(s.mode) : ""}</div>
        <div class="entity-card-footer">
          <span class="entity-card-status">${renderStatusBadge(s.status)}</span>
        </div>
      </div>
    </div>`;
  }).join("");
}

function initExperimentsSubtabs() {
  initSubtabStrip("experiments-subtabs", () => {});
}

initExperimentsSubtabs();
