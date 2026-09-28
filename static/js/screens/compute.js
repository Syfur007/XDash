// static/js/screens/compute.js
//
// XDASH_PLAN.md §8.4, Phase 5: the runtime board (every kind as one tile/list
// board, backed by GET /api/runtimes) and the runtime detail page
// (#/compute/<id>, a js/lib/router.js stub since Phase 3 — see its own
// comment on exactly what was left unread) with its five tabs: Now, Queue,
// History, Settings, Diagnostics. Plus the Add-runtime wizard (kind picker
// -> that kind's form -> a live test that must pass before Save) and the
// Colab Connect-account copy-paste OAuth flow.
//
// This screen shares #view-compute with the pre-Phase-5 Compute view
// (static/js/views/compute.js: the Queue/Machines/Kaggle/Colab/Monitors
// subtab strip) exactly the way Phase 4's dataset registry matrix shares
// #view-data with the older Data Studio feature — added alongside, not
// rebuilt. The one exception: the old GET /api/runners "Capacity" board is
// now a literal duplicate of this screen's own Runtimes board, so its panel
// is hidden (kept in the DOM, still populated by loadComputeCapacity(),
// exactly like #view-results was left reachable-by-code-but-not-by-nav in
// Phase 3 — see that section's own comment on why deleting instead would
// throw) rather than shown twice.
//
// Same classic-<script>-sharing-global-scope model as every other screen
// file: reuses js/views/lab.js's labRuntimeTileHtml()/labRuntimeRowHtml()
// (lab.js loads first) so a runtime tile/row looks and clicks the same way
// whether it's reached from Lab or from here.

// ---------------------------------------------------------------- state
state.computeRuntimes = [];        // GET /api/runtimes -> .runtimes
state.computeBoardPoller = null;
state.computeDetailPoller = null;  // XDASH_FIXES_PLAN.md F1.1 — Now/Queue/History refresh on their own, while visible
state.selectedRuntimeId = null;    // drives the detail panel; set from the URL
state.addRuntimeKind = "ssh";
state.addRuntimeTested = false;    // gates Save until POST /api/runtimes/test says ok
state.colabConnectPollers = {};    // account name -> createPoller, while a Connect-account flow is open
state.kaggleLogPoller = null;      // one at a time — only the open Now tab's account follows
// XDASH_FIXES_PLAN.md F1.1 — Diagnostics' probe/validate/quota/etc. results,
// keyed by runtime id then by which button produced them, so they survive
// a re-render (tab switch away and back) instead of living only in a DOM
// node's textContent that the next render blows away.
state.runtimeDiag = {};
// XDASH_FIXES_PLAN.md F3 — the Tools tab: the per-host tool catalog last
// fetched for the *currently open* runtime, which host it was fetched for
// (needed for every start/stop/output call — see runtimeHostId() below),
// which tool ids have their output drawer open, the last TensorBoard status
// fetched, and the poller that keeps both fresh only while this tab is the
// one showing.
state.toolsCatalog = [];
state.toolsHostId = null;
state.toolsExpanded = new Set();
state.toolsGridKey = null;   // last-rendered structural key (ids+alive+available+expanded) — see renderToolsGrid()
state.tbStatus = null;
state.toolsPoller = null;

function computeBoardViewMode() {
  try { return localStorage.getItem("xdash.compute.board.view") || "tiles"; }
  catch (e) { return "tiles"; }
}

function setComputeBoardViewMode(mode) {
  try { localStorage.setItem("xdash.compute.board.view", mode); }
  catch (e) { /* per-viewer convenience only — never load-bearing */ }
  renderComputeBoardToggle();
  renderRuntimeBoard();
}

function renderComputeBoardToggle() {
  const el = document.querySelector('[data-lab-toggle="computeboard"]');
  if (!el) return;
  const mode = computeBoardViewMode();
  el.innerHTML =
    `<button class="btn-icon${mode === "tiles" ? " active" : ""}" data-mode="tiles" title="Tiles">▦</button>` +
    `<button class="btn-icon${mode === "list" ? " active" : ""}" data-mode="list" title="List">☰</button>`;
}

// ---------------------------------------------------------------- the board
// XDASH_FIXES_PLAN.md F1.1 — this 5s poll now updates only the board and the
// detail header (both cheap, neither holds a form). It used to also call
// loadRuntimeDetailTab(currentRuntimeDetailTab()) on every tick, which is
// what wiped a typed Settings field or a Diagnostics result every 5s
// (§2/#6's root cause) — Now/Queue/History get their own poller
// (startComputeDetailPolling) that only runs those three tabs, and
// Settings/Diagnostics render once per open (see loadRuntimeDetailTab's
// other callers: applyComputeRouteParams, the subtab click handler).
async function loadRuntimeBoard() {
  try {
    const data = await api("/api/runtimes");
    state.computeRuntimes = data.runtimes || [];
  } catch (e) {
    const body = document.getElementById("runtime-board-body");
    if (body) body.innerHTML = `<div class="empty-state">Couldn't load runtimes: ${escapeHtml(e.message)}</div>`;
    return;
  }
  renderRuntimeBoard();
  if (state.selectedRuntimeId) renderRuntimeDetailHeader();
}

// The tabs that reflect live, un-editable state — safe to refresh on a
// short interval with no risk of clobbering a form (Settings/Diagnostics
// are deliberately excluded; see loadRuntimeBoard's own comment above).
const RUNTIME_DETAIL_LIVE_TABS = ["now", "queue", "history"];

function startComputeDetailPolling() {
  if (!state.computeDetailPoller) {
    state.computeDetailPoller = createPoller(() => {
      const tab = currentRuntimeDetailTab();
      if (RUNTIME_DETAIL_LIVE_TABS.includes(tab)) loadRuntimeDetailTab(tab);
    }, 5000);
  }
  state.computeDetailPoller.start();
}

function stopComputeDetailPolling() {
  if (state.computeDetailPoller) state.computeDetailPoller.stop();
}

function renderRuntimeBoard() {
  const body = document.getElementById("runtime-board-body");
  const countEl = document.getElementById("runtime-board-count");
  if (countEl) countEl.textContent = state.computeRuntimes.length ? String(state.computeRuntimes.length) : "";
  if (!body) return;
  if (!state.computeRuntimes.length) { body.innerHTML = `<div class="empty-state">No runtimes registered yet — click + Add runtime.</div>`; return; }
  const mode = computeBoardViewMode();
  body.className = mode === "tiles" ? "entity-grid" : "entity-list";
  body.innerHTML = state.computeRuntimes.map((r) => (mode === "tiles" ? labRuntimeTileHtml(r) : labRuntimeRowHtml(r))).join("");
  body.querySelectorAll("[data-lab-runtime]").forEach((el) => {
    el.addEventListener("click", () => navigateToRuntime(el.dataset.labRuntime));
  });
}

function startComputeBoardPolling() {
  if (!state.computeBoardPoller) state.computeBoardPoller = createPoller(loadRuntimeBoard, 5000);
  state.computeBoardPoller.start();
}

function stopComputeBoardPolling() {
  if (state.computeBoardPoller) state.computeBoardPoller.stop();
  Object.values(state.colabConnectPollers).forEach((p) => p.stop());
  stopKaggleLogPolling();
  stopToolsOutputPolling();
}

function findRuntime(id) {
  return state.computeRuntimes.find((r) => r.id === id) || null;
}

// A runtime id is "<kind>:<name>" for everything but local (registry.slot_id) —
// the name half is what /api/hosts, /api/kaggle/accounts and /api/colab/accounts
// key on.
function runtimeName(id) {
  return id.includes(":") ? id.slice(id.indexOf(":") + 1) : id;
}

// ---------------------------------------------------------------- routing (#/compute/<id>/<tab>)
// XDASH_FIXES_PLAN.md F1.6 — the tab is now part of the URL (js/lib/router.js's
// handleRoute() passes it through as the third path segment), so a reload or
// a shared link lands back on the same Now/Queue/History/Settings/Diagnostics
// tab instead of always resetting to "now". F2/F3's future "tools" tab reuses
// this exact scheme (#/compute/<id>/tools) — nothing here needs to change to
// add it, only another entry in RUNTIME_DETAIL_TABS and a subtab button.
const RUNTIME_DETAIL_TABS = ["now", "queue", "history", "settings", "diagnostics", "tools"];

function applyComputeRouteParams(runtimeId, tab) {
  state.selectedRuntimeId = runtimeId || null;
  const boardPanel = document.getElementById("runtime-board-panel");
  const detailPanel = document.getElementById("runtime-detail-panel");
  if (!detailPanel) return;
  if (!state.selectedRuntimeId) {
    detailPanel.classList.add("hidden");
    if (boardPanel) boardPanel.classList.remove("hidden");
    stopComputeDetailPolling();
    stopToolsOutputPolling();
    return;
  }
  if (boardPanel) boardPanel.classList.add("hidden");
  detailPanel.classList.remove("hidden");
  activateSubtab("runtime-detail-subtabs", RUNTIME_DETAIL_TABS.includes(tab) ? tab : currentRuntimeDetailTab());
  renderRuntimeDetailHeader();
  loadRuntimeDetailTab(currentRuntimeDetailTab());
  startComputeDetailPolling();
  // XDASH_FIXES_PLAN.md F3.5 — the Tools tab's own poller (tool output +
  // alive/available refresh) only ever runs while that tab is the one
  // showing; excluded from RUNTIME_DETAIL_LIVE_TABS above because, like
  // Settings/Diagnostics, it holds a form ("Add tool") a blind re-render
  // would otherwise wipe mid-type.
  if (currentRuntimeDetailTab() === "tools") startToolsOutputPolling();
  else stopToolsOutputPolling();
}

function currentRuntimeDetailTab() {
  const active = document.querySelector("#runtime-detail-subtabs .subtab-btn.active");
  return active ? active.dataset.subtab : "now";
}

function renderRuntimeDetailHeader() {
  const r = findRuntime(state.selectedRuntimeId);
  const titleEl = document.getElementById("runtime-detail-title");
  const subEl = document.getElementById("runtime-detail-sub");
  const actionsEl = document.getElementById("runtime-detail-actions");
  if (titleEl) titleEl.textContent = r ? r.label : state.selectedRuntimeId;
  if (subEl) {
    if (!r) { subEl.textContent = "Loading…"; }
    else {
      const acc = r.accelerator ? `${r.accelerator.name}${r.accelerator.vram_gb ? " " + r.accelerator.vram_gb + "G" : ""}` : "no accelerator info";
      subEl.textContent = `${r.kind} · ${r.state} · ${acc}`;
    }
  }
  if (actionsEl) {
    actionsEl.innerHTML = (r && r.kind !== "local")
      ? `<button class="btn btn-sm btn-danger" data-action="remove-runtime">Remove</button>` : "";
    const btn = actionsEl.querySelector('[data-action="remove-runtime"]');
    if (btn) btn.addEventListener("click", () => removeRuntimeFromDetail(r));
  }
}

async function removeRuntimeFromDetail(r) {
  if (!r) return;
  const ok = await showConfirm("Remove this runtime?", `Deregisters '${r.label}' from the dashboard. Anything already in flight there will fail its next operation rather than being cancelled.`);
  if (!ok) return;
  const name = runtimeName(r.id);
  try {
    if (r.kind === "ssh") await api(`/api/hosts/${encodeURIComponent(name)}`, { method: "DELETE" });
    else if (r.kind === "kaggle") await api(`/api/kaggle/accounts/${encodeURIComponent(name)}`, { method: "DELETE" });
    else if (r.kind === "colab") await api(`/api/colab/accounts/${encodeURIComponent(name)}`, { method: "DELETE" });
    toast(`Removed '${r.label}'`, "ok");
    navigateToRuntime(null);
    loadRuntimeBoard();
  } catch (e) {
    toast("Couldn't remove: " + e.message, "err");
  }
}

function initRuntimeDetailTabs() {
  // Routed through navigateToRuntime() (not activateSubtab()+loadRuntimeDetailTab()
  // directly) so the URL always reflects the open tab (F1.6).
  document.querySelectorAll("#runtime-detail-subtabs .subtab-btn").forEach((btn) => {
    btn.addEventListener("click", () => navigateToRuntime(state.selectedRuntimeId, btn.dataset.subtab));
  });
  document.getElementById("btn-runtime-detail-back").addEventListener("click", () => navigateToRuntime(null));
}

function loadRuntimeDetailTab(key) {
  if (!state.selectedRuntimeId) return;
  if (key === "now") loadRuntimeNow();
  else if (key === "queue") loadRuntimeQueue();
  else if (key === "history") loadRuntimeHistory();
  else if (key === "settings") loadRuntimeSettings();
  else if (key === "diagnostics") loadRuntimeDiagnostics();
  else if (key === "tools") loadRuntimeTools();
}

// ---------------------------------------------------------------- Now
async function loadRuntimeNow() {
  const body = document.getElementById("runtime-detail-now-body");
  if (!body) return;
  body.innerHTML = `<div class="empty-state">Loading…</div>`;
  let running;
  try {
    const data = await api(`/api/experiments?runtime=${encodeURIComponent(state.selectedRuntimeId)}`);
    running = (data.experiments || []).filter((e) => e.status === "running" || e.status === "dispatching");
  } catch (e) {
    body.innerHTML = `<div class="empty-state">Couldn't load: ${escapeHtml(e.message)}</div>`;
    return;
  }
  body.innerHTML = running.length
    ? running.map((exp) => {
        const a = exp.current_attempt || {};
        return `<div class="entity-card" style="margin-bottom:10px;">
          <div class="entity-card-accent running"></div>
          <div class="entity-card-body">
            <div class="entity-card-title">${experimentLink(exp.experiment_id)}</div>
            <div class="entity-card-sub">${escapeHtml(a.raw_status || exp.status)} · ${escapeHtml(fmtDuration(a.started_at))} elapsed</div>
          </div>
        </div>`;
      }).join("")
    : `<div class="empty-state">Nothing running here right now.</div>`;
  const r = findRuntime(state.selectedRuntimeId);
  if (r && r.kind === "kaggle") {
    body.innerHTML += runtimeKaggleLiveLogHtml();
    wireRuntimeKaggleLiveLog();
  }
}

// Live `kernels logs -f` (CLI >= 2.0.2 — the installed 2.2.4 has it).
// Degrades to a "not available" message if the resolved 'kaggle' tool
// (Settings -> Tools) is missing or too old: start_kernel_log_follow's
// subprocess then just exits immediately with an argparse error, which
// shows up as empty/garbled output.
function runtimeKaggleLiveLogHtml() {
  return `<div class="panel" style="margin-top:12px;">
    <div class="panel-header"><span>Live log</span></div>
    <div style="padding:12px 16px;">
      <div class="job-actions" style="margin-bottom:8px;">
        <button class="btn btn-sm btn-ghost" id="rtd-kaggle-log-start">Follow</button>
        <button class="btn btn-sm btn-ghost" id="rtd-kaggle-log-stop">Stop</button>
      </div>
      <pre class="editor-body" id="rtd-kaggle-log-output" style="max-height:260px; overflow:auto; font-size:11px; white-space:pre-wrap;">(not following)</pre>
    </div>
  </div>`;
}

function wireRuntimeKaggleLiveLog() {
  const name = runtimeName(state.selectedRuntimeId);
  document.getElementById("rtd-kaggle-log-start").addEventListener("click", async () => {
    try {
      const result = await api(`/api/kaggle/accounts/${encodeURIComponent(name)}/logs/follow`, { method: "POST" });
      renderKaggleLogOutput(result);
      startKaggleLogPolling(name);
    } catch (e) { toast("Couldn't start following the log: " + e.message, "err"); }
  });
  document.getElementById("rtd-kaggle-log-stop").addEventListener("click", async () => {
    stopKaggleLogPolling();
    try { await api(`/api/kaggle/accounts/${encodeURIComponent(name)}/logs/follow`, { method: "DELETE" }); } catch (e) { /* best-effort */ }
    renderKaggleLogOutput({ active: false });
  });
}

function renderKaggleLogOutput(result) {
  const el = document.getElementById("rtd-kaggle-log-output");
  if (!el) return;
  el.textContent = result.active ? (result.output || "(no output yet)") : "(not following)";
}

function startKaggleLogPolling(name) {
  if (!state.kaggleLogPoller) {
    state.kaggleLogPoller = createPoller(async () => {
      const result = await api(`/api/kaggle/accounts/${encodeURIComponent(name)}/logs/follow`);
      renderKaggleLogOutput(result);
      if (!result.active) state.kaggleLogPoller.stop();
    }, 2000);
  }
  state.kaggleLogPoller.start();
}

function stopKaggleLogPolling() {
  if (state.kaggleLogPoller) state.kaggleLogPoller.stop();
}

// ---------------------------------------------------------------- Queue
async function loadRuntimeQueue() {
  const body = document.getElementById("runtime-detail-queue-body");
  if (!body) return;
  body.innerHTML = `<div class="empty-state">Loading…</div>`;
  const r = findRuntime(state.selectedRuntimeId);
  let pinned = [], likelyNext = [];
  try {
    const pinnedData = await api(`/api/experiments?runtime=${encodeURIComponent(state.selectedRuntimeId)}&status=queued`);
    pinned = (pinnedData.experiments || []).filter((e) => (e.runtime || {}).mode === "pinned");
    const autoData = await api(`/api/experiments?status=queued`);
    likelyNext = (autoData.experiments || [])
      .filter((e) => (e.runtime || {}).mode !== "pinned")
      .filter((e) => {
        const allow = (e.runtime || {}).allow || [];
        return allow.includes("*") || (r && allow.includes(r.kind)) || allow.includes(state.selectedRuntimeId);
      })
      .sort((a, b) => (b.priority || 0) - (a.priority || 0) || new Date(a.created_at) - new Date(b.created_at))
      .slice(0, 5);
  } catch (e) {
    body.innerHTML = `<div class="empty-state">Couldn't load: ${escapeHtml(e.message)}</div>`;
    return;
  }
  const pinnedHtml = `<h4 class="run-detail-section-title">Pinned here</h4>` + (pinned.length
    ? pinned.map((e) => `<div class="kaggle-history-row">${experimentLink(e.experiment_id)} · priority ${e.priority || 0}</div>`).join("")
    : `<div class="empty-state">Nothing pinned here.</div>`);
  const likelyHtml = `<h4 class="run-detail-section-title">Likely next (auto-eligible, best-effort order)</h4>` + (likelyNext.length
    ? likelyNext.map((e) => `<div class="kaggle-history-row">${experimentLink(e.experiment_id)}</div>`).join("")
    : `<div class="empty-state">Nothing queued that could land here.</div>`);
  body.innerHTML = pinnedHtml + likelyHtml;
}

// ---------------------------------------------------------------- History
// A simple inline SVG success-rate sparkline (no charting library, per the
// plan's own note) — newest attempt on the right, green=done/red=anything else.
function runtimeSparklineSvg(experiments) {
  const items = experiments.slice(-30);
  if (!items.length) return "";
  const barW = 7, gap = 2, w = items.length * (barW + gap), h = 24;
  const bars = items.map((exp, i) => {
    const ok = exp.status === "done";
    const barH = ok ? 20 : 12;
    return `<rect x="${i * (barW + gap)}" y="${h - barH}" width="${barW}" height="${barH}" fill="${ok ? "var(--emerald)" : "var(--red)"}" />`;
  }).join("");
  const successRate = Math.round((100 * items.filter((e) => e.status === "done").length) / items.length);
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="vertical-align:middle;">${bars}</svg>
    <span class="entity-card-sub" style="margin-left:8px;">${successRate}% success (last ${items.length})</span>`;
}

async function loadRuntimeHistory() {
  const body = document.getElementById("runtime-detail-history-body");
  if (!body) return;
  body.innerHTML = `<div class="empty-state">Loading…</div>`;
  let experiments;
  try {
    const data = await api(`/api/experiments?runtime=${encodeURIComponent(state.selectedRuntimeId)}`);
    experiments = (data.experiments || []).filter((e) => ["done", "failed", "cancelled"].includes(e.status));
  } catch (e) {
    body.innerHTML = `<div class="empty-state">Couldn't load: ${escapeHtml(e.message)}</div>`;
    return;
  }
  if (!experiments.length) { body.innerHTML = `<div class="empty-state">No attempts have run here yet.</div>`; return; }
  const rows = experiments.map((exp) => {
    const a = exp.current_attempt || {};
    const hours = a.started_at && a.ended_at ? (((new Date(a.ended_at)) - (new Date(a.started_at))) / 3600000).toFixed(2) : "–";
    return `<tr><td>${experimentLink(exp.experiment_id)}</td><td>${renderStatusBadge(exp.status)}</td><td>${escapeHtml(String(hours))}h</td></tr>`;
  }).join("");
  body.innerHTML = `<div style="margin-bottom:12px;">${runtimeSparklineSvg(experiments)}</div>
    <table class="compare-table" style="width:100%;">
      <thead><tr><th>Experiment</th><th>Outcome</th><th>Hours</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ---------------------------------------------------------------- Settings
// XDASH_FIXES_PLAN.md F1.1 — this whole tab renders once per open (called
// only from loadRuntimeDetailTab, never from the board's 5s poll — see
// loadRuntimeBoard's own comment) plus this explicit Reload button.
// A runtime id alone ("ssh:box", "kaggle:acct", "local", …) already tells you
// its kind — used as a fallback below so Settings/Diagnostics don't have to
// wait on the board's own /api/runtimes fetch (which loadRuntimeBoard no
// longer re-triggers a tab render after — F1.1). Without this, a direct
// #/compute/<id>/settings deep-link opened before the board's first fetch
// resolves would render "Loading…" and then never retry.
function runtimeStubFromId(id) {
  if (!id) return null;
  return { id, kind: id === "local" ? "local" : id.split(":")[0] };
}

function loadRuntimeSettings() {
  const body = document.getElementById("runtime-detail-settings-body");
  if (!body) return;
  const r = findRuntime(state.selectedRuntimeId) || runtimeStubFromId(state.selectedRuntimeId);
  if (!r) { body.innerHTML = `<div class="empty-state">Loading…</div>`; return; }
  if (r.kind === "local") renderRuntimeSettingsLocal(body, r);
  else if (r.kind === "ssh") renderRuntimeSettingsSsh(body, r);
  else if (r.kind === "kaggle") renderRuntimeSettingsKaggle(body, r);
  else if (r.kind === "colab") renderRuntimeSettingsColab(body, r);
}

// XDASH_FIXES_PLAN.md F1.4 — "one complete machine settings form for SSH and
// local": Repo root / Env activate / Python interpreter / Max concurrent,
// shared between the two so they can never drift apart. `host.resolved`
// (backend/hosts.py's _Host.as_dict()) already carries the effective value
// for each — used as the placeholder so local shows this profile's own
// defaults (D5: an SSH host's placeholder is instead "(not set)"/"python",
// since it no longer inherits this machine's env/python at all).
function machineSettingsFieldsHtml(host, profileName) {
  const resolved = host.resolved || {};
  const profileRepo = (host.repos || {})[profileName] || {};
  return `
    <div class="field grow"><label>Repo root for this profile</label>
      <input class="text-input grow" id="rts-repo-root" placeholder="${escapeHtml(resolved.repo_root || "")}" value="${escapeHtml(profileRepo.repo_root || "")}" autocomplete="off" /></div>
    <div class="field grow"><label>Env activate</label>
      <input class="text-input grow" id="rts-env" placeholder="${escapeHtml(resolved.env_activate_cmd || "(not set)")}" value="${escapeHtml(host.env_activate_cmd || "")}" autocomplete="off" /></div>
    <div class="field grow"><label>Python interpreter</label>
      <input class="text-input grow" id="rts-python" placeholder="${escapeHtml(resolved.python_executable || "python")}" value="${escapeHtml(host.python_executable || "")}" autocomplete="off" /></div>
    <div class="field" style="width:130px;"><label>Max concurrent</label>
      <input class="text-input" id="rts-max" placeholder="${escapeHtml(resolved.max_concurrent != null ? String(resolved.max_concurrent) : "")}" value="${host.max_concurrent != null ? host.max_concurrent : ""}" autocomplete="off" /></div>`;
}

// "Verify environment" (F1.5/D6) + an explicit Reload — shared footer for
// both the SSH and local Settings forms.
function machineSettingsFootHtml() {
  return `<div class="job-actions" style="margin-top:10px; flex-wrap:wrap; gap:8px; align-items:center;">
    <button class="btn btn-primary" id="rts-save">Save</button>
    <button class="btn btn-ghost" id="rts-reload">Reload</button>
    <button class="btn btn-ghost" id="rts-verify-env">Verify environment</button>
    <span id="rts-verify-env-result" class="settings-profile-path"></span>
  </div>`;
}

function wireMachineSettingsSave(host, profileName, isLocal) {
  document.getElementById("rts-reload").addEventListener("click", () => loadRuntimeSettings());
  document.getElementById("rts-save").addEventListener("click", async () => {
    const repoRoot = document.getElementById("rts-repo-root").value.trim();
    const envActivate = document.getElementById("rts-env").value.trim();
    const python = document.getElementById("rts-python").value.trim();
    const maxRaw = document.getElementById("rts-max").value.trim();
    // PATCH, sending only what this form owns (XDASH_FIXES_PLAN.md F1.3) —
    // accelerator/tmux_session_prefix/another profile's repos entry are
    // never in this payload, so the merge on the backend leaves them alone.
    const patch = {
      max_concurrent: maxRaw ? Number(maxRaw) : null,
      env_activate_cmd: envActivate || null,
      python_executable: python || null,
      repos: { [profileName]: { repo_root: repoRoot || null } },
    };
    if (!isLocal) {
      const port = document.getElementById("rts-ssh-port").value.trim();
      patch.ssh = {
        host: document.getElementById("rts-ssh-host").value.trim(),
        user: document.getElementById("rts-ssh-user").value.trim(),
        identity_file: document.getElementById("rts-ssh-identity").value.trim(),
        port: port ? Number(port) : null,
      };
    }
    try {
      await api(`/api/hosts/${encodeURIComponent(host.id)}`, { method: "PATCH", body: JSON.stringify(patch) });
      toast("Saved", "ok");
      loadRuntimeBoard();
      loadRuntimeSettings();
    } catch (e) { toast("Couldn't save: " + e.message, "err"); }
  });
  const verifyBtn = document.getElementById("rts-verify-env");
  const verifyResultEl = document.getElementById("rts-verify-env-result");
  verifyBtn.addEventListener("click", async () => {
    verifyResultEl.textContent = "Checking…";
    try {
      // Always a fresh, synchronous check (server.py forces force=True) —
      // an explicit click means "tell me right now", never the dispatch
      // path's cached answer.
      const result = await api(`/api/hosts/${encodeURIComponent(host.id)}/verify-env`, { method: "POST" });
      verifyResultEl.textContent = result.ok ? "✓ environment looks fine" : "✗ " + (result.detail || "check failed");
    } catch (e) { verifyResultEl.textContent = "error: " + e.message; }
  });
}

async function renderRuntimeSettingsSsh(body, r) {
  body.innerHTML = `<div class="empty-state">Loading…</div>`;
  const hostId = runtimeName(r.id);
  let host;
  try {
    const data = await api("/api/hosts");
    host = (data.hosts || []).find((h) => h.id === hostId);
  } catch (e) { body.innerHTML = `<div class="empty-state">${escapeHtml(e.message)}</div>`; return; }
  if (!host) { body.innerHTML = `<div class="empty-state">Host record not found.</div>`; return; }
  const ssh = host.ssh || {};
  const profileName = (state.system && state.system.profile_name) || "";
  body.innerHTML = `<div class="scheduler-add-form" style="flex-wrap:wrap;">
    <div class="field grow"><label>SSH host</label><input class="text-input grow" id="rts-ssh-host" value="${escapeHtml(ssh.host || "")}" autocomplete="off" /></div>
    <div class="field grow"><label>User</label><input class="text-input grow" id="rts-ssh-user" value="${escapeHtml(ssh.user || "")}" autocomplete="off" /></div>
    <div class="field" style="width:80px;"><label>Port</label><input class="text-input" id="rts-ssh-port" value="${escapeHtml(ssh.port || "")}" autocomplete="off" /></div>
    <div class="field grow"><label>Identity file</label><input class="text-input grow" id="rts-ssh-identity" value="${escapeHtml(ssh.identity_file || "")}" autocomplete="off" /></div>
    ${machineSettingsFieldsHtml(host, profileName)}
  </div>
  ${machineSettingsFootHtml()}`;
  wireMachineSettingsSave(host, profileName, false);
}

// F1.4 — local now gets the same form (repo root/env activate/python/max
// concurrent), showing this profile's own defaults as placeholders: nothing
// typed here means "use the profile", exactly like backend/hosts.py's own
// _Host._fallback() already reads it for the local host.
async function renderRuntimeSettingsLocal(body, r) {
  body.innerHTML = `<div class="empty-state">Loading…</div>`;
  let host;
  try {
    const data = await api("/api/hosts");
    host = (data.hosts || []).find((h) => h.id === "local");
  } catch (e) { body.innerHTML = `<div class="empty-state">${escapeHtml(e.message)}</div>`; return; }
  if (!host) { body.innerHTML = `<div class="empty-state">Host record not found.</div>`; return; }
  const profileName = (state.system && state.system.profile_name) || "";
  body.innerHTML = `<div class="scheduler-add-form" style="flex-wrap:wrap;">
    ${machineSettingsFieldsHtml(host, profileName)}
  </div>
  ${machineSettingsFootHtml()}
  <div class="empty-state" style="margin-top:8px;">Blank fields use this profile's own settings (shown as placeholders) — only set one here to override it for this machine specifically.</div>`;
  wireMachineSettingsSave(host, profileName, true);
}

async function renderRuntimeSettingsKaggle(body, r) {
  body.innerHTML = `<div class="empty-state">Loading…</div>`;
  const name = runtimeName(r.id);
  let account, kernel;
  try {
    const data = await api("/api/kaggle/accounts");
    account = (data.accounts || []).find((a) => a.name === name);
    kernel = await api(`/api/kaggle/accounts/${encodeURIComponent(name)}/kernel`);
  } catch (e) { body.innerHTML = `<div class="empty-state">${escapeHtml(e.message)}</div>`; return; }
  if (!account) { body.innerHTML = `<div class="empty-state">Account not found.</div>`; return; }
  const usage = account.usage_estimate || {};
  const secretLinkHtml = kernel.kernel_edit_url
    ? `<a href="${escapeHtml(kernel.kernel_edit_url)}" target="_blank" rel="noopener">open the kernel to attach it ↗</a>`
    : `(set a Kaggle username first)`;
  body.innerHTML = `<table class="kv-table">
      <tr><td>Kaggle username</td><td>${escapeHtml(account.kaggle_username || "–")}</td></tr>
      <tr><td>Weekly budget</td><td>
        <input class="text-input" id="rts-kaggle-budget" style="width:100px;" value="${usage.weekly_budget_hours != null ? usage.weekly_budget_hours : ""}" autocomplete="off" /> h/week
        <button class="btn btn-sm btn-ghost" id="rts-kaggle-budget-save">Save</button>
      </td></tr>
      <tr><td>GITHUB_TOKEN secret</td><td>One-time, web-UI only — ${secretLinkHtml}</td></tr>
      <tr><td>Kernel</td><td>${escapeHtml(kernel.kernel_slug)}</td></tr>
    </table>
    <div class="job-actions" style="margin-top:10px;">
      <button class="btn btn-sm btn-ghost" id="rts-kaggle-credentials-toggle">Credentials</button>
    </div>
    <div id="rts-kaggle-cred-form"></div>`;
  document.getElementById("rts-kaggle-budget-save").addEventListener("click", async () => {
    const hours = document.getElementById("rts-kaggle-budget").value.trim();
    try {
      await api(`/api/kaggle/accounts/${encodeURIComponent(name)}/weekly_budget`, { method: "POST", body: JSON.stringify({ hours: hours ? Number(hours) : null }) });
      toast("Saved", "ok");
      loadRuntimeBoard();
    } catch (e) { toast("Couldn't save: " + e.message, "err"); }
  });
  document.getElementById("rts-kaggle-credentials-toggle").addEventListener("click", () => {
    const el = document.getElementById("rts-kaggle-cred-form");
    if (el.innerHTML) { el.innerHTML = ""; return; }
    el.innerHTML = `<div class="scheduler-add-form" style="padding-top:9px; border-top:1px dashed var(--border-soft);">
      <div class="field grow"><label>New classic key (leave blank to keep current)</label><input class="text-input grow" id="rts-kaggle-key" type="password" autocomplete="off" /></div>
      <div class="field grow"><label>New API token (leave blank to keep current)</label><input class="text-input grow" id="rts-kaggle-token" type="password" autocomplete="off" /></div>
      <button class="btn btn-sm btn-primary" id="rts-kaggle-cred-save">Save</button>
    </div>`;
    document.getElementById("rts-kaggle-cred-save").addEventListener("click", async () => {
      const key = document.getElementById("rts-kaggle-key").value.trim();
      const api_token = document.getElementById("rts-kaggle-token").value.trim();
      if (!key && !api_token) { toast("Enter a new key, a new token, or both", "err"); return; }
      try {
        await api(`/api/kaggle/accounts/${encodeURIComponent(name)}/credentials`, { method: "PATCH", body: JSON.stringify({ username: "", key, api_token }) });
        toast("Credentials updated", "ok");
        el.innerHTML = "";
      } catch (e) { toast("Couldn't update: " + e.message, "err"); }
    });
  });
}

async function renderRuntimeSettingsColab(body, r) {
  body.innerHTML = `<div class="empty-state">Loading…</div>`;
  const name = runtimeName(r.id);
  let account, dataAccount;
  try {
    const data = await api("/api/colab/accounts");
    account = (data.accounts || []).find((a) => a.name === name);
    const registry = await api("/api/datasets");
    dataAccount = registry.data_account;
  } catch (e) { body.innerHTML = `<div class="empty-state">${escapeHtml(e.message)}</div>`; return; }
  if (!account) { body.innerHTML = `<div class="empty-state">Account not found.</div>`; return; }
  body.innerHTML = `<table class="kv-table">
      <tr><td>Connected</td><td>${account.has_credentials ? "yes" : "no"}</td></tr>
      <tr><td>GPU preference</td><td>${escapeHtml(account.gpu || "(default)")} <span class="entity-card-sub">— set at Add-runtime time; remove and re-add to change it</span></td></tr>
      <tr><td>Session cap (hours)</td><td><input class="text-input" id="rts-colab-limit" style="width:90px;" value="${account.session_limit_hours != null ? account.session_limit_hours : ""}" autocomplete="off" /> <button class="btn btn-sm btn-ghost" id="rts-colab-limit-save">Save</button></td></tr>
      <tr><td>Data account (for dataset fetch)</td><td>${escapeHtml(dataAccount || "(none set)")} — <a href="#" id="rts-colab-data-account-link">set in Datasets ↗</a></td></tr>
    </table>
    <div class="panel" style="margin-top:12px;">
      <div class="panel-header"><span>Connect account</span></div>
      <div id="rts-colab-connect-body" style="padding:12px 16px;"></div>
    </div>`;
  document.getElementById("rts-colab-limit-save").addEventListener("click", async () => {
    const hours = document.getElementById("rts-colab-limit").value.trim();
    try {
      await api(`/api/colab/accounts/${encodeURIComponent(name)}/session_limit`, { method: "POST", body: JSON.stringify({ hours: hours ? Number(hours) : null }) });
      toast("Saved", "ok");
    } catch (e) { toast("Couldn't save: " + e.message, "err"); }
  });
  document.getElementById("rts-colab-data-account-link").addEventListener("click", (e) => {
    e.preventDefault();
    navigateToView("data");
  });
  renderColabConnectPanel(name, account.has_credentials);
}

// ---------------------------------------------------------------- Colab Connect-account (XDASH_PLAN.md §8.4)
// Drives the CLI's copy-paste OAuth from the dashboard. *** The one call in
// this whole file that, once clicked for real, opens a real browser-facing
// Google sign-in: beginColabConnect()'s POST /api/colab/accounts/<n>/connect,
// which runs backend/colab.py::begin_connect() -> procsession.start() against
// the real `colab` binary. Nothing before that click makes any network call. ***
function renderColabConnectPanel(name, connected) {
  const body = document.getElementById("rts-colab-connect-body");
  if (!body) return;
  body.innerHTML = `<div class="empty-state">${connected ? "Already connected (a CLI login exists under this account's HOME)." : "Not connected yet."}</div>
    <button class="btn btn-sm btn-primary" id="rts-colab-connect-start">${connected ? "Reconnect" : "Connect account"}</button>
    <div id="rts-colab-connect-flow" style="margin-top:8px;"></div>`;
  document.getElementById("rts-colab-connect-start").addEventListener("click", () => beginColabConnect(name));
}

async function beginColabConnect(name) {
  try {
    const status = await api(`/api/colab/accounts/${encodeURIComponent(name)}/connect`, { method: "POST" });
    renderColabConnectStatus(name, status);
    startColabConnectPolling(name);
  } catch (e) {
    toast("Couldn't start Connect account: " + e.message, "err");
  }
}

function startColabConnectPolling(name) {
  if (!state.colabConnectPollers[name]) {
    state.colabConnectPollers[name] = createPoller(async () => {
      const status = await api(`/api/colab/accounts/${encodeURIComponent(name)}/connect`);
      renderColabConnectStatus(name, status);
      if (status.done) state.colabConnectPollers[name].stop();
    }, 1500);
  }
  state.colabConnectPollers[name].start();
}

function renderColabConnectStatus(name, status) {
  const flow = document.getElementById("rts-colab-connect-flow");
  if (!flow) return;
  if (!status.active) { flow.innerHTML = ""; return; }
  const urlHtml = status.url
    ? `<div>Sign in: <a href="${escapeHtml(status.url)}" target="_blank" rel="noopener">${escapeHtml(status.url)}</a></div>`
    : `<div class="empty-state">Starting the login and waiting for a sign-in link…</div>`;
  const codeHtml = status.awaiting_code
    ? `<div class="job-actions" style="margin-top:8px;">
        <input class="text-input" id="rts-colab-code-input" placeholder="Paste the code shown after signing in" autocomplete="off" />
        <button class="btn btn-sm btn-primary" id="rts-colab-code-submit">Submit</button>
      </div>`
    : "";
  const doneHtml = status.done
    ? `<div style="margin-top:8px;">${status.connected ? "✓ connected" : `✗ login did not complete (exit ${status.returncode})`}</div>`
    : "";
  flow.innerHTML = `<div class="scheduler-add-form" style="flex-direction:column; align-items:flex-start; gap:8px;">${urlHtml}${codeHtml}${doneHtml}</div>`;
  const submitBtn = document.getElementById("rts-colab-code-submit");
  if (submitBtn) submitBtn.addEventListener("click", async () => {
    const code = document.getElementById("rts-colab-code-input").value.trim();
    if (!code) return;
    try {
      const result = await api(`/api/colab/accounts/${encodeURIComponent(name)}/connect/code`, { method: "POST", body: JSON.stringify({ code }) });
      renderColabConnectStatus(name, result);
    } catch (e) { toast("Couldn't submit code: " + e.message, "err"); }
  });
  if (status.done) loadRuntimeBoard();
}

// ---------------------------------------------------------------- Diagnostics
// XDASH_FIXES_PLAN.md F1.1 — results live in state.runtimeDiag[<runtime id>]
// (set by wireRuntimeDiagnosticsButtons below) and are re-applied here on
// every render, so switching tabs away and back (or Settings' own Reload
// triggering a board refresh) never wipes what the last probe/validate/quota
// check found — the same class of bug as the Settings tab, just for
// read-only results instead of typed input.
function loadRuntimeDiagnostics() {
  const body = document.getElementById("runtime-detail-diagnostics-body");
  if (!body) return;
  const r = findRuntime(state.selectedRuntimeId) || runtimeStubFromId(state.selectedRuntimeId);
  if (!r) { body.innerHTML = `<div class="empty-state">Loading…</div>`; return; }
  const hostId = r.kind === "ssh" ? runtimeName(r.id) : (r.kind === "local" ? "local" : null);
  const diag = state.runtimeDiag[r.id] || {};
  const parts = [];
  if (hostId) {
    parts.push(`<div class="job-actions" style="margin-bottom:10px;">
      <button class="btn btn-sm btn-ghost" id="rtd-test-connection">Test connection</button>
      <button class="btn btn-sm btn-ghost" id="rtd-probe-gpu">Probe GPU</button>
      <span id="rtd-diag-result" class="settings-profile-path">${escapeHtml(diag.hostResult || "")}</span>
    </div>`);
  }
  if (r.kind === "kaggle") {
    parts.push(`<div class="job-actions" style="margin-bottom:10px;">
      <button class="btn btn-sm btn-ghost" id="rtd-kaggle-validate">Validate credentials</button>
      <button class="btn btn-sm btn-ghost" id="rtd-kaggle-quota">Check measured quota</button>
      <button class="btn btn-sm btn-ghost" id="rtd-kaggle-quota-refresh" title="Bypass the 15-minute cache">Refresh</button>
      <span id="rtd-diag-result-kaggle" class="settings-profile-path">${escapeHtml(diag.kaggleResult || "")}</span>
    </div>`);
  } else if (r.kind === "colab") {
    parts.push(`<div class="job-actions" style="margin-bottom:10px;">
      <button class="btn btn-sm btn-ghost" id="rtd-colab-session">Check VM</button>
      <button class="btn btn-sm btn-ghost" id="rtd-colab-usage">Compute-unit balance</button>
      <span id="rtd-diag-result-colab" class="settings-profile-path">${escapeHtml(diag.colabResult || "")}</span>
    </div>`);
  }
  // XDASH_FIXES_PLAN.md F3.6 (issue #4) — "Machine stats →"/"TensorBoard →"
  // are gone; both are now just the Tools tab itself. toolsHostId (not the
  // narrower ssh/local-only `hostId` above) also covers a live Colab
  // account, which has a Tools tab too (F3.5) even though it never had a
  // Diagnostics test-connection/probe-gpu section here.
  const toolsHostId = runtimeHostId(r);
  if (hostId || toolsHostId) {
    parts.push(`<div class="job-actions">
      ${hostId ? `<button class="btn btn-sm btn-ghost" id="rtd-open-sessions">Sessions on this host →</button>` : ""}
      ${toolsHostId ? `<button class="btn btn-sm btn-ghost" id="rtd-open-tools">Tools →</button>` : ""}
    </div>`);
  }
  body.innerHTML = parts.join("") || `<div class="empty-state">No diagnostics for this runtime kind.</div>`;
  wireRuntimeDiagnosticsButtons(r, hostId);
}

function _setRuntimeDiag(runtimeId, key, text) {
  state.runtimeDiag[runtimeId] = { ...(state.runtimeDiag[runtimeId] || {}), [key]: text };
}

function wireRuntimeDiagnosticsButtons(r, hostId) {
  const testBtn = document.getElementById("rtd-test-connection");
  if (testBtn) testBtn.addEventListener("click", async () => {
    const resultEl = document.getElementById("rtd-diag-result");
    resultEl.textContent = "Testing…";
    try {
      const result = await api(`/api/hosts/${encodeURIComponent(hostId)}/test`, { method: "POST" });
      resultEl.textContent = result.reachable ? `reachable${result.tmux_available ? " (tmux ok)" : " (no tmux)"}` : "unreachable";
    } catch (e) { resultEl.textContent = "error: " + e.message; }
    _setRuntimeDiag(r.id, "hostResult", resultEl.textContent);
  });
  const probeBtn = document.getElementById("rtd-probe-gpu");
  if (probeBtn) probeBtn.addEventListener("click", async () => {
    const resultEl = document.getElementById("rtd-diag-result");
    resultEl.textContent = "Probing…";
    try {
      const result = await api(`/api/hosts/${encodeURIComponent(hostId)}/probe-gpu`, { method: "POST" });
      resultEl.textContent = result.found ? `${result.accelerator.name} ${result.accelerator.vram_gb}G` : "no GPU found (or unreachable)";
      loadRuntimeBoard();
    } catch (e) { resultEl.textContent = "error: " + e.message; }
    _setRuntimeDiag(r.id, "hostResult", resultEl.textContent);
  });
  const validateBtn = document.getElementById("rtd-kaggle-validate");
  if (validateBtn) validateBtn.addEventListener("click", async () => {
    const name = runtimeName(r.id);
    const resultEl = document.getElementById("rtd-diag-result-kaggle");
    resultEl.textContent = "Validating…";
    try {
      const result = await api(`/api/kaggle/accounts/${encodeURIComponent(name)}/validate`, { method: "POST" });
      resultEl.textContent = result.ok ? "looks valid" : "failed: " + result.detail;
    } catch (e) { resultEl.textContent = "error: " + e.message; }
    _setRuntimeDiag(r.id, "kaggleResult", resultEl.textContent);
  });
  const checkQuota = async (force) => {
    const name = runtimeName(r.id);
    const resultEl = document.getElementById("rtd-diag-result-kaggle");
    resultEl.textContent = "Checking…";
    try {
      const qs = force ? "?refresh=1" : "";
      const result = await api(`/api/kaggle/accounts/${encodeURIComponent(name)}/quota${qs}`);
      resultEl.textContent = result.available
        ? `${fmtNum(result.used)}/${fmtNum(result.limit)} ${result.unit} (source: measured)`
        : `not available — ${result.detail} (check Settings -> Tools: 'kaggle' needs to resolve to CLI >= 2.2.1)`;
    } catch (e) { resultEl.textContent = "error: " + e.message; }
    _setRuntimeDiag(r.id, "kaggleResult", resultEl.textContent);
  };
  const quotaBtn = document.getElementById("rtd-kaggle-quota");
  if (quotaBtn) quotaBtn.addEventListener("click", () => checkQuota(false));
  const quotaRefreshBtn = document.getElementById("rtd-kaggle-quota-refresh");
  if (quotaRefreshBtn) quotaRefreshBtn.addEventListener("click", () => checkQuota(true));
  const sessionBtn = document.getElementById("rtd-colab-session");
  if (sessionBtn) sessionBtn.addEventListener("click", async () => {
    const name = runtimeName(r.id);
    const resultEl = document.getElementById("rtd-diag-result-colab");
    resultEl.textContent = "Checking…";
    try {
      const result = await api(`/api/colab/accounts/${encodeURIComponent(name)}/session`);
      resultEl.textContent = result.live ? "VM is live" : "no VM up right now";
    } catch (e) { resultEl.textContent = "error: " + e.message; }
    _setRuntimeDiag(r.id, "colabResult", resultEl.textContent);
  });
  const usageBtn = document.getElementById("rtd-colab-usage");
  if (usageBtn) usageBtn.addEventListener("click", async () => {
    const name = runtimeName(r.id);
    const resultEl = document.getElementById("rtd-diag-result-colab");
    resultEl.textContent = "Checking…";
    try {
      const result = await api(`/api/colab/accounts/${encodeURIComponent(name)}/usage`);
      resultEl.textContent = `${fmtNum(result.balance)} CU · ${fmtNum(result.burn_per_h)}/h`;
    } catch (e) { resultEl.textContent = "error: " + e.message; }
    _setRuntimeDiag(r.id, "colabResult", resultEl.textContent);
  });
  const sessionsLink = document.getElementById("rtd-open-sessions");
  if (sessionsLink) sessionsLink.addEventListener("click", () => {
    switchToSubtab("experiments", "experiments-subtabs", "sessions");
    const filter = document.getElementById("terminal-filter");
    if (filter) { filter.value = hostId; filter.dispatchEvent(new Event("input")); }
  });
  const toolsLink = document.getElementById("rtd-open-tools");
  if (toolsLink) toolsLink.addEventListener("click", () => navigateToRuntime(state.selectedRuntimeId, "tools"));
}

// ---------------------------------------------------------------- Tools (XDASH_FIXES_PLAN.md F3, issues #3/#4/#5)
// A host-agnostic catalog (GET/POST/DELETE /api/monitors — no host_id in an
// entry, ever) crossed with the *open runtime's* host at render time: the
// host always comes from here (the URL -> state.selectedRuntimeId -> its
// host_id), never from the catalog, which is the actual #5 fix. Local, SSH
// and a live Colab account all resolve to a real backend/hosts.py record
// (runtimeHostId() below); Kaggle doesn't, so it just shows a message.
//
// Renders once per tab open (loadRuntimeTools, same as Settings/Diagnostics
// — see loadRuntimeBoard's own F1.1 comment on why a blind re-render was
// the #6 bug); a dedicated poller (start/stopToolsOutputPolling, wired from
// applyComputeRouteParams above) then refreshes only the tool grid and the
// TensorBoard card in place while this tab stays open, leaving the
// "Add tool" form and any open output <pre> (and its scroll position)
// alone — same "structural change only" rebuild rule
// static/app.js's renderMonitorList() used for the exact same reason.
function runtimeHostId(r) {
  if (!r) return null;
  if (r.host_id !== undefined) return r.host_id; // real /api/runtimes data — authoritative
  if (r.kind === "local") return "local";
  if (r.kind === "ssh") return runtimeName(r.id);
  // A synthesized stub (runtimeStubFromId, before the board's own fetch has
  // resolved) can't guess a live Colab account's host id ("colab-<name>",
  // a different string from its "colab:<name>" runtime id) — the next
  // board tick fixes this once real data lands.
  return null;
}

async function loadRuntimeTools() {
  const r = findRuntime(state.selectedRuntimeId) || runtimeStubFromId(state.selectedRuntimeId);
  const hostId = runtimeHostId(r);
  state.toolsHostId = hostId;
  const grid = document.getElementById("rtd-tools-grid");
  const tbCard = document.getElementById("rtd-tb-card");
  if (!hostId) {
    if (grid) grid.innerHTML = `<div class="empty-state">No tools for this runtime kind.</div>`;
    if (tbCard) tbCard.innerHTML = `<div class="empty-state">No TensorBoard for this runtime kind.</div>`;
    return;
  }
  if (grid) grid.innerHTML = `<div class="empty-state">Loading…</div>`;
  if (tbCard) tbCard.innerHTML = `<div class="empty-state">Loading…</div>`;
  try {
    const data = await api(`/api/hosts/${encodeURIComponent(hostId)}/tools`);
    state.toolsCatalog = data.tools || [];
  } catch (e) {
    if (grid) grid.innerHTML = `<div class="empty-state">Couldn't load tools: ${escapeHtml(e.message)}</div>`;
    return;
  }
  try { state.tbStatus = await api(`/api/hosts/${encodeURIComponent(hostId)}/tensorboard/status`); }
  catch (e) { state.tbStatus = { running: false }; }
  state.toolsGridKey = null; // force the first render to fully build the grid
  renderToolsGrid();
  renderTensorboardCard();
  for (const id of state.toolsExpanded) loadToolOutput(id);
}

function toolCardHtml(t) {
  const expanded = state.toolsExpanded.has(t.id);
  const statusClass = t.alive ? "running" : "stopped";
  const startBtn = t.available
    ? `<button class="btn btn-sm btn-primary" data-action="tool-start">Start</button>`
    : `<button class="btn btn-sm btn-primary" data-action="tool-start" disabled title="${escapeHtml(t.available_detail || "not available")}">Start</button>`;
  return `<div class="entity-card tool-card" data-id="${escapeHtml(t.id)}">
    <div class="entity-card-accent ${statusClass}"></div>
    <div class="entity-card-body">
      <div class="entity-card-title">${escapeHtml(t.name)}</div>
      <div class="entity-card-sub" title="${escapeHtml(t.command)}">${escapeHtml(t.command)}</div>
      <div class="entity-card-footer">
        <span class="term-card-status ${statusClass}">${t.alive ? "Running" : "Stopped"}</span>
        <div class="job-actions">
          ${t.alive ? `<button class="btn btn-sm" data-action="tool-stop">Stop</button>` : startBtn}
          <button class="btn btn-sm btn-ghost" data-action="tool-expand">${expanded ? "Hide output" : "Show output"}</button>
          ${!t.builtin ? `<button class="btn btn-sm btn-danger" data-action="tool-remove">Remove</button>` : ""}
        </div>
      </div>
      ${expanded ? `<pre class="log-console no-wrap" id="tool-output-${escapeHtml(t.id)}" style="margin-top:8px; max-height:220px;"></pre>` : ""}
    </div>
  </div>`;
}

function wireToolCard(card) {
  const id = card.dataset.id;
  card.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const action = btn.dataset.action;
      if (action === "tool-start") startRuntimeTool(id);
      else if (action === "tool-stop") stopRuntimeTool(id);
      else if (action === "tool-remove") removeRuntimeTool(id);
      else if (action === "tool-expand") {
        if (state.toolsExpanded.has(id)) state.toolsExpanded.delete(id);
        else state.toolsExpanded.add(id);
        state.toolsGridKey = null; // expand/collapse is itself a structural change — force a rebuild
        renderToolsGrid();
        if (state.toolsExpanded.has(id)) loadToolOutput(id);
      }
    });
  });
}

// Full rebuild only when something structural changed (a tool started/
// stopped, availability flipped, or a drawer was expanded/collapsed) — the
// exact reason static/app.js's own renderMonitorList() stopped rebuilding
// on every poll tick: constantly recreating an open output <pre> flickers
// it and loses its scroll position.
function renderToolsGrid() {
  const grid = document.getElementById("rtd-tools-grid");
  if (!grid) return;
  if (!state.toolsCatalog.length) {
    grid.innerHTML = `<div class="empty-state">No tools in the catalog.</div>`;
    state.toolsGridKey = "";
    return;
  }
  const key = state.toolsCatalog.map((t) => `${t.id}:${t.alive}:${t.available}:${state.toolsExpanded.has(t.id)}`).join(",");
  if (key === state.toolsGridKey) return;
  grid.innerHTML = state.toolsCatalog.map(toolCardHtml).join("");
  grid.querySelectorAll(".tool-card").forEach(wireToolCard);
  state.toolsGridKey = key;
}

async function startRuntimeTool(id) {
  try {
    await api(`/api/hosts/${encodeURIComponent(state.toolsHostId)}/tools/${encodeURIComponent(id)}/start`, { method: "POST" });
    toast("Tool started", "ok");
    loadRuntimeTools();
  } catch (e) { toast("Couldn't start: " + e.message, "err"); }
}

async function stopRuntimeTool(id) {
  try {
    await api(`/api/hosts/${encodeURIComponent(state.toolsHostId)}/tools/${encodeURIComponent(id)}/stop`, { method: "POST" });
    toast("Tool stopped", "ok");
    loadRuntimeTools();
  } catch (e) { toast("Couldn't stop: " + e.message, "err"); }
}

async function removeRuntimeTool(id) {
  const ok = await showConfirm("Remove this tool?", "Stops it on every host it happens to be running on, and removes it from the catalog entirely.");
  if (!ok) return;
  try {
    await api(`/api/monitors/${encodeURIComponent(id)}`, { method: "DELETE" });
    state.toolsExpanded.delete(id);
    toast("Removed", "ok");
    loadRuntimeTools();
  } catch (e) { toast("Couldn't remove: " + e.message, "err"); }
}

async function loadToolOutput(id) {
  const el = document.getElementById(`tool-output-${id}`);
  if (!el) return;
  try {
    const data = await api(`/api/hosts/${encodeURIComponent(state.toolsHostId)}/tools/${encodeURIComponent(id)}/output`);
    if (!data.alive) { el.textContent = "(not running — click Start)"; return; }
    const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
    el.textContent = data.output || "";
    if (wasAtBottom) el.scrollTop = el.scrollHeight;
  } catch (e) { /* transient — next tick retries */ }
}

async function addRuntimeTool() {
  const name = document.getElementById("rtd-tool-name").value.trim();
  const command = document.getElementById("rtd-tool-command").value.trim();
  const interval = parseInt(document.getElementById("rtd-tool-interval").value, 10) || 0;
  if (!name || !command) { toast("Name and command are both required", "err"); return; }
  try {
    await api("/api/monitors", { method: "POST", body: JSON.stringify({ name, command, watch_interval: interval }) });
    document.getElementById("rtd-tool-name").value = "";
    document.getElementById("rtd-tool-command").value = "";
    toast("Tool added to the catalog", "ok");
    loadRuntimeTools();
  } catch (e) { toast("Couldn't add tool: " + e.message, "err"); }
}

// TensorBoard's own card — always fully re-rendered (cheap, no persistent
// per-viewer state like an output drawer's scroll to lose).
function renderTensorboardCard() {
  const card = document.getElementById("rtd-tb-card");
  if (!card) return;
  const st = state.tbStatus || {};
  const running = !!st.running;
  const url = running && st.port ? `http://${window.location.hostname}:${st.port}/` : "#";
  card.innerHTML = `
    <div class="tb-status-row">
      <div class="pulse-dot${running ? " live" : ""}"></div>
      <div>
        <div class="tb-status-label">${running ? "Running" : "Not running"}</div>
        <div class="tb-status-sub">${running
          ? `Serving ${escapeHtml(st.logdir || "")} on port ${st.port}.`
          : `Starts tensorboard on this host, reading ${escapeHtml(st.logdir || "")}.`}</div>
      </div>
    </div>
    <div class="tb-toolbar">
      <button class="btn btn-primary${running ? " hidden" : ""}" id="rtd-tb-start">Start TensorBoard</button>
      <button class="btn btn-danger${running ? "" : " hidden"}" id="rtd-tb-stop">Stop</button>
      <a class="btn btn-ghost${running ? "" : " hidden"}" id="rtd-tb-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open ↗</a>
    </div>`;
  const startBtn = document.getElementById("rtd-tb-start");
  const stopBtn = document.getElementById("rtd-tb-stop");
  if (startBtn) startBtn.addEventListener("click", startRuntimeTensorboard);
  if (stopBtn) stopBtn.addEventListener("click", stopRuntimeTensorboard);
}

async function startRuntimeTensorboard() {
  try {
    state.tbStatus = await api(`/api/hosts/${encodeURIComponent(state.toolsHostId)}/tensorboard/start`, { method: "POST" });
    renderTensorboardCard();
  } catch (e) { toast("Couldn't start TensorBoard: " + e.message, "err"); }
}

async function stopRuntimeTensorboard() {
  try {
    state.tbStatus = await api(`/api/hosts/${encodeURIComponent(state.toolsHostId)}/tensorboard/stop`, { method: "POST" });
    renderTensorboardCard();
  } catch (e) { toast("Couldn't stop TensorBoard: " + e.message, "err"); }
}

// Only while the Tools tab is the one actually showing (checked on every
// tick, same pattern startComputeDetailPolling() uses for Now/Queue/
// History above) — and skipped entirely while the Add-tool form has focus,
// reusing js/lib/poller.js's formPollGuard exactly like F1.2's Kaggle
// auto-refresh guard, even though this poller never touches that form's
// container itself (it only ever rewrites #rtd-tools-grid/#rtd-tb-card) —
// cheap insurance against a future change coupling the two.
function startToolsOutputPolling() {
  if (!state.toolsPoller) {
    state.toolsPoller = createPoller(() => {
      if (currentRuntimeDetailTab() !== "tools") return;
      if (formPollGuard("rtd-tools-add-form")) return;
      refreshRuntimeTools();
    }, 4000);
  }
  state.toolsPoller.start();
}

function stopToolsOutputPolling() {
  if (state.toolsPoller) state.toolsPoller.stop();
}

async function refreshRuntimeTools() {
  if (!state.toolsHostId) return;
  try {
    const data = await api(`/api/hosts/${encodeURIComponent(state.toolsHostId)}/tools`);
    state.toolsCatalog = data.tools || [];
    state.tbStatus = await api(`/api/hosts/${encodeURIComponent(state.toolsHostId)}/tensorboard/status`).catch(() => state.tbStatus);
    renderToolsGrid();
    renderTensorboardCard();
    for (const id of state.toolsExpanded) loadToolOutput(id);
  } catch (e) { /* transient — next tick retries */ }
}

// ---------------------------------------------------------------- Add-runtime wizard
function openAddRuntimeWizard() {
  state.addRuntimeKind = "ssh";
  state.addRuntimeTested = false;
  document.querySelectorAll(".kind-picker [data-runtime-kind]").forEach((b) => b.classList.toggle("active", b.dataset.runtimeKind === "ssh"));
  document.querySelectorAll("#add-runtime-backdrop .wizard-step").forEach((s) => s.classList.toggle("active", s.dataset.wizardStep === "kind"));
  document.getElementById("add-runtime-test-result").textContent = "";
  document.getElementById("add-runtime-save").disabled = true;
  [
    "ar-ssh-id", "ar-ssh-label", "ar-ssh-host", "ar-ssh-user", "ar-ssh-port", "ar-ssh-identity",
    "ar-ssh-repo-root", "ar-ssh-env", "ar-ssh-python",
    "ar-kaggle-name", "ar-kaggle-username", "ar-kaggle-key", "ar-kaggle-token",
    "ar-colab-name", "ar-colab-label", "ar-colab-gpu", "ar-colab-limit",
  ].forEach((id) => { const el = document.getElementById(id); if (el) el.value = ""; });
  document.getElementById("add-runtime-backdrop").classList.remove("hidden");
}

function closeAddRuntimeWizard() {
  document.getElementById("add-runtime-backdrop").classList.add("hidden");
}

function addRuntimeGoToForm() {
  document.querySelectorAll("#add-runtime-backdrop .wizard-step").forEach((s) => s.classList.toggle("active", s.dataset.wizardStep === "form"));
  ["ssh", "kaggle", "colab"].forEach((k) => document.getElementById(`add-runtime-form-${k}`).classList.toggle("hidden", k !== state.addRuntimeKind));
}

function addRuntimeFields() {
  if (state.addRuntimeKind === "ssh") {
    const port = document.getElementById("ar-ssh-port").value.trim();
    const fields = {
      host: document.getElementById("ar-ssh-host").value.trim(),
      user: document.getElementById("ar-ssh-user").value.trim(),
      identity_file: document.getElementById("ar-ssh-identity").value.trim(),
    };
    if (port) fields.port = Number(port);
    return fields;
  }
  if (state.addRuntimeKind === "kaggle") {
    return {
      username: document.getElementById("ar-kaggle-username").value.trim(),
      key: document.getElementById("ar-kaggle-key").value.trim(),
      api_token: document.getElementById("ar-kaggle-token").value.trim(),
    };
  }
  return { gpu: document.getElementById("ar-colab-gpu").value.trim() };
}

// F1.4 — the SSH wizard step's Repo root/Env activate/Python, collected
// separately from addRuntimeFields() above: /api/runtimes/test (what
// addRuntimeFields() feeds) is a live reachability probe and never touches
// these, only saveAddRuntime() does.
function addRuntimeMachineFields() {
  return {
    repoRoot: document.getElementById("ar-ssh-repo-root").value.trim(),
    env: document.getElementById("ar-ssh-env").value.trim(),
    python: document.getElementById("ar-ssh-python").value.trim(),
  };
}

async function testAddRuntime() {
  const resultEl = document.getElementById("add-runtime-test-result");
  resultEl.textContent = "Testing…";
  state.addRuntimeTested = false;
  document.getElementById("add-runtime-save").disabled = true;
  try {
    const result = await api("/api/runtimes/test", {
      method: "POST", body: JSON.stringify({ kind: state.addRuntimeKind, fields: addRuntimeFields() }),
    });
    resultEl.textContent = (result.ok ? "✓ " : "✗ ") + (result.detail || "");
    state.addRuntimeTested = !!result.ok;
    document.getElementById("add-runtime-save").disabled = !result.ok;
  } catch (e) {
    resultEl.textContent = "✗ " + e.message;
  }
}

async function saveAddRuntime() {
  if (!state.addRuntimeTested) return;
  try {
    if (state.addRuntimeKind === "ssh") {
      const id = document.getElementById("ar-ssh-id").value.trim();
      if (!id) { toast("Host id is required", "err"); return; }
      const label = document.getElementById("ar-ssh-label").value.trim();
      const machine = addRuntimeMachineFields();
      const profileName = (state.system && state.system.profile_name) || "";
      const record = { id, kind: "ssh", label: label || id, ssh: addRuntimeFields() };
      if (machine.env) record.env_activate_cmd = machine.env;
      if (machine.python) record.python_executable = machine.python;
      if (machine.repoRoot && profileName) record.repos = { [profileName]: { repo_root: machine.repoRoot } };
      await api("/api/hosts", { method: "POST", body: JSON.stringify(record) });
    } else if (state.addRuntimeKind === "kaggle") {
      const name = document.getElementById("ar-kaggle-name").value.trim();
      if (!name) { toast("Account name is required", "err"); return; }
      await api("/api/kaggle/accounts", { method: "POST", body: JSON.stringify({ name, ...addRuntimeFields() }) });
    } else {
      const name = document.getElementById("ar-colab-name").value.trim();
      if (!name) { toast("Account name is required", "err"); return; }
      const label = document.getElementById("ar-colab-label").value.trim();
      const limitRaw = document.getElementById("ar-colab-limit").value.trim();
      const body = { name, label, ...addRuntimeFields() };
      if (limitRaw) body.session_limit_hours = Number(limitRaw);
      await api("/api/colab/accounts", { method: "POST", body: JSON.stringify(body) });
    }
    toast("Runtime added", "ok");
    closeAddRuntimeWizard();
    loadRuntimeBoard();
  } catch (e) {
    toast("Couldn't save: " + e.message, "err");
  }
}

function initAddRuntimeWizard() {
  document.getElementById("btn-add-runtime").addEventListener("click", openAddRuntimeWizard);
  document.getElementById("add-runtime-cancel-1").addEventListener("click", closeAddRuntimeWizard);
  document.getElementById("add-runtime-back").addEventListener("click", () => {
    document.querySelectorAll("#add-runtime-backdrop .wizard-step").forEach((s) => s.classList.toggle("active", s.dataset.wizardStep === "kind"));
  });
  document.getElementById("add-runtime-next").addEventListener("click", addRuntimeGoToForm);
  document.getElementById("add-runtime-test").addEventListener("click", testAddRuntime);
  document.getElementById("add-runtime-save").addEventListener("click", saveAddRuntime);
  document.querySelectorAll(".kind-picker [data-runtime-kind]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.addRuntimeKind = btn.dataset.runtimeKind;
      document.querySelectorAll(".kind-picker [data-runtime-kind]").forEach((b) => b.classList.toggle("active", b === btn));
    });
  });
  document.getElementById("add-runtime-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "add-runtime-backdrop") closeAddRuntimeWizard();
  });
}

// ---------------------------------------------------------------- init
function initComputeScreenButtons() {
  renderComputeBoardToggle();
  document.querySelector('[data-lab-toggle="computeboard"]').addEventListener("click", (e) => {
    const btn = e.target.closest("[data-mode]");
    if (btn) setComputeBoardViewMode(btn.dataset.mode);
  });
  initRuntimeDetailTabs();
  initAddRuntimeWizard();
  document.getElementById("rtd-tools-reload").addEventListener("click", loadRuntimeTools);
  document.getElementById("rtd-tool-add").addEventListener("click", addRuntimeTool);
}

initComputeScreenButtons();
