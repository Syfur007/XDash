// static/js/views/compute.js
//
// Compute tab (Multi_runner_XDash.md Phase 6) — the fleet-management layer,
// replacing static/js/views/runners.js and the Kaggle-account half of
// kaggle.js (kaggle.js is deleted; its notification half moved to
// settings.js). A capacity board over every runner kind (GET /api/runners,
// which already carries capability flags — volatile/provisioned/
// budget_metered — per kind), then a Queue|Kaggle|Colab subtab strip.
// Queue is pre-existing scheduler markup relocated here unchanged — its JS
// stays in app.js, this file only adds the subtab wiring around it.
// Kaggle/Colab are this file's own. The Machines and Monitors subtabs that
// used to live here are gone (XDASH_FIXES_PLAN.md D1/F3): the runtime
// detail page's own Settings and Tools tabs (js/screens/compute.js) replace
// them, one host at a time instead of one global editor/board — Machines'
// host editor duplicated that Settings tab with fewer fields (and dropped
// `accelerator`); Monitors' host picker only ever applied to a *newly
// added* metric, never to starting an existing one (issue #5).
//
// Same classic-<script>-sharing-global-scope model as every other view file.

state.kaggleAccounts = [];        // carried over from the old kaggle.js verbatim
state.kaggleCredEditOpen = new Set();
state.kaggleNameEditOpen = new Set();
state.kaggleAutoRefreshTimer = null;
state.kaggleAutoRefresh = _computeGetPref("kaggleAutoRefresh");
state.kaggleSnapshotStatus = {};  // account name -> last-fetched GET .../snapshot result
state.colabAccounts = [];
state.colabSessionStatus = {};    // account name -> last-fetched GET .../session result

function _computeGetPref(key) {
  try { return localStorage.getItem(key) === "1"; } catch (e) { return false; }
}
function _computeSetPref(key, value) {
  try { localStorage.setItem(key, value ? "1" : "0"); } catch (e) {}
}

function initComputeSubtabs() {
  initSubtabStrip("compute-subtabs", (key) => {
    if (key === "kaggle") loadKaggle();
    else if (key === "colab") loadComputeColab();
    // "queue" is already kept warm by boot()'s own always-on pollTimer
    // (loadScheduler), so nothing to do the first time it opens. Machines
    // and Monitors (XDASH_FIXES_PLAN.md D1/F3) are gone — the runtime detail
    // page's own Settings and Tools tabs (js/screens/compute.js) replace
    // them, per host, instead of one global editor/board.
  });
}

// ---------------------------------------------------------------- capacity board
// Reuses GET /api/runners rather than /api/slots: /api/slots is deliberately
// still a dedicated local+kaggle-only shape (backend/experiments.py's own
// list_slots() docstring — kept that way through Phase 2 pending this exact
// unification), while /api/runners already reports every registered kind
// uniformly, capabilities included, which is what a fleet-wide board needs.
async function loadComputeCapacity() {
  const body = document.getElementById("compute-capacity-body");
  if (!body) return;
  let runners;
  try {
    const data = await api("/api/runners");
    runners = data.runners || [];
  } catch (e) {
    body.innerHTML = `<div class="empty-state">Couldn't load capacity: ${escapeHtml(e.message)}</div>`;
    return;
  }
  body.innerHTML = runners.length
    ? runners.map(computeCapacityCardHtml).join("")
    : `<div class="empty-state">No runners registered.</div>`;
}

function computeCapacityCardHtml(r) {
  const cap = r.capacity || {};
  const extra = cap.extra || {};
  const caps = r.capabilities || {};
  const pct = cap.limit ? Math.min(100, Math.round((cap.used / cap.limit) * 100)) : 0;
  const full = cap.limit != null && cap.used >= cap.limit;
  const flags = [];
  if (caps.volatile) flags.push(`<span class="badge amber" title="Disappears if XDash stops mid-run">volatile</span>`);
  if (caps.provisioned) flags.push(`<span class="badge slate" title="Capacity is created on demand, not always-on">provisioned</span>`);
  if (caps.budget_metered) flags.push(`<span class="badge teal" title="Time/hour budget, not just a slot count">metered</span>`);
  if (extra.tmux_available === false) flags.push(`<span class="badge red" title="tmux not found on this host">no tmux</span>`);
  const sub = r.kind === "kaggle"
    ? `${fmtNum(extra.hours_this_week)}h this week`
    : `${cap.used} / ${cap.limit ?? "∞"} slot${cap.limit === 1 ? "" : "s"} in use`;
  return `<div class="entity-card">
    <div class="entity-card-accent ${full ? "running" : "completed"}"></div>
    <div class="entity-card-body">
      <div class="entity-card-title">${escapeHtml(r.label)} <span class="mode-tag">${escapeHtml(r.kind)}</span></div>
      <div class="entity-card-sub">${escapeHtml(sub)}</div>
      <div class="concurrency-bar-track"><div class="concurrency-bar-fill" style="width:${pct}%;"></div></div>
      ${flags.length ? `<div class="entity-card-footer">${flags.join(" ")}</div>` : ""}
    </div>
  </div>`;
}

// ---------------------------------------------------------------- Kaggle subtab
// The following (through the end of the notification-schema section this file
// deliberately does NOT include — see static/js/views/settings.js) is carried
// over from the old kaggle.js's account half essentially verbatim.

async function loadKaggle() {
  const accountsBody = document.getElementById("kaggle-accounts-body");
  if (!accountsBody) return;
  accountsBody.innerHTML = `<div class="empty-state">Loading…</div>`;
  try {
    const data = await api("/api/kaggle/accounts");
    state.kaggleAccounts = data.accounts || [];
    renderKaggleSummary();
    renderKaggleAccounts();
  } catch (e) {
    document.getElementById("kaggle-summary-strip").innerHTML = "";
    accountsBody.innerHTML = `<div class="empty-state">Failed to load accounts: ${escapeHtml(e.message)}</div>`;
  }
}

function renderKaggleSummary() {
  const el = document.getElementById("kaggle-summary-strip");
  const accounts = state.kaggleAccounts;
  if (!el) return;
  if (!accounts.length) { el.innerHTML = ""; return; }
  const totalHours = accounts.reduce((sum, a) => sum + ((a.usage_estimate || {}).hours_this_week || 0), 0);
  const budgeted = accounts.reduce((sum, a) => sum + ((a.usage_estimate || {}).weekly_budget_hours || 0), 0);
  const credentialled = accounts.filter((a) => a.has_legacy_key || a.has_api_token).length;
  el.innerHTML =
    `<div class="compute-summary-chip"><b>${accounts.length}</b>account${accounts.length === 1 ? "" : "s"}</div>` +
    `<div class="compute-summary-chip"><b>${accounts.length}</b>slot${accounts.length === 1 ? "" : "s"} (1 per account)</div>` +
    `<div class="compute-summary-chip"><b>${totalHours.toFixed(2)}</b>est. GPU-hours this week${budgeted ? ` / ${budgeted.toFixed(0)}` : ""}</div>` +
    (credentialled < accounts.length
      ? `<div class="compute-summary-chip"><b style="color:var(--red);">${accounts.length - credentialled}</b>missing credentials</div>`
      : "");
}

// Auto-refresh polls the (read-only, cheap) /api/kaggle/accounts endpoint —
// the actual Kaggle status checks are the dispatcher's own job, which keeps
// running server-side whether or not this tab is open or this toggle is on.
// XDASH_FIXES_PLAN.md F1.2 — loadKaggle()'s own renderKaggleAccounts()
// rebuilds every account card from scratch, including any open credential
// form (§2/#6: this auto-refresh used to wipe a mid-typed key/token every
// 30s). Only the *auto-refresh tick* is guarded — an explicit reload right
// after a save (e.g. saveKaggleCredentials()'s own loadKaggle() call) must
// still go through even though focus is still inside the container.
function kaggleAutoRefreshTick() {
  if (formPollGuard("kaggle-accounts-body")) return;
  loadKaggle();
}

function setKaggleAutoRefresh(enabled) {
  state.kaggleAutoRefresh = enabled;
  _computeSetPref("kaggleAutoRefresh", enabled);
  const btn = document.getElementById("btn-kaggle-toggle-autorefresh");
  if (btn) btn.textContent = `Auto-refresh: ${enabled ? "on" : "off"}`;
  if (state.kaggleAutoRefreshTimer) { clearInterval(state.kaggleAutoRefreshTimer); state.kaggleAutoRefreshTimer = null; }
  if (enabled) state.kaggleAutoRefreshTimer = setInterval(kaggleAutoRefreshTick, 30000);
}

function renderKaggleAccounts() {
  const body = document.getElementById("kaggle-accounts-body");
  const countEl = document.getElementById("kaggle-account-count");
  if (countEl) countEl.textContent = state.kaggleAccounts.length
    ? `${state.kaggleAccounts.length} account${state.kaggleAccounts.length === 1 ? "" : "s"}`
    : "";
  if (!body) return;

  if (!state.kaggleAccounts.length) {
    body.innerHTML = `<div class="empty-state">No accounts configured yet — add one below with a classic key, an API token, or both.</div>`;
    return;
  }

  body.innerHTML = state.kaggleAccounts.map((a) => {
    const usage = a.usage_estimate || {};
    const hours = usage.hours_this_week !== undefined ? usage.hours_this_week : "–";
    const credEditOpen = state.kaggleCredEditOpen.has(a.name);
    const nameEditOpen = state.kaggleNameEditOpen.has(a.name);
    const titleHtml = nameEditOpen
      ? `<input class="text-input grow" id="kaggle-name-input-${escapeHtml(a.name)}" value="${escapeHtml(a.name)}" autocomplete="off" />
         <button class="btn-icon save" data-action="save-name" data-account="${escapeHtml(a.name)}" title="Save">✓</button>
         <button class="btn-icon cancel" data-action="cancel-name" data-account="${escapeHtml(a.name)}" title="Cancel">✕</button>`
      : `<span class="kaggle-card-title" title="${escapeHtml(a.name)}">${escapeHtml(a.name)}</span>
         <button class="btn-icon" data-action="edit-name" data-account="${escapeHtml(a.name)}" title="Rename">✎</button>`;
    const snap = state.kaggleSnapshotStatus[a.name];
    const snapHtml = snap
      ? `<div class="kaggle-card-sub" title="${escapeHtml(snap.ref || "")}">snapshot: ${snap.exists ? escapeHtml(snap.status || "unknown") : "none yet"}</div>`
      : "";
    return `<div class="kaggle-card">
      <div class="kaggle-card-accent teal"></div>
      <div class="kaggle-card-body">
        <div class="kaggle-card-header">
          <div style="min-width:0;">
            <div class="kaggle-card-title-row">${titleHtml}</div>
            <div class="kaggle-card-sub">${escapeHtml(a.kaggle_username || "–")} · 1 slot</div>
          </div>
          <div class="kaggle-cred-chips">
            <span class="kaggle-chip on" title="${a.scope === "system"
              ? "System-wide: available under every repo profile, with one shared set of credentials and one weekly quota."
              : "Registered for this repo only — not visible under other repo profiles."}">${a.scope === "system" ? "system" : "this repo"}</span>
            <span class="kaggle-chip ${a.has_legacy_key ? "on" : ""}" title="Classic username/key pair stored">key</span>
            <span class="kaggle-chip ${a.has_api_token ? "on" : ""}" title="New-format API token stored">token</span>
          </div>
        </div>

        <div class="kaggle-stat-row">
          <div class="kaggle-stat" title="Self-tracked estimate, summed from this account's own downloaded runs this UTC week — not Kaggle's own quota figure.">
            <div class="kaggle-stat-label">Est. GPU-hrs / wk</div>
            <div class="kaggle-stat-value">${escapeHtml(String(hours))}</div>
          </div>
          ${renderKaggleSparkline(a.usage_history)}
        </div>
        ${snapHtml}

        ${credEditOpen ? renderKaggleCredentialForm(a) : ""}

        <div class="kaggle-card-footer">
          <button class="btn btn-sm btn-ghost" data-action="validate-account" data-account="${escapeHtml(a.name)}">Validate</button>
          <button class="btn btn-sm btn-ghost" data-action="check-snapshot" data-account="${escapeHtml(a.name)}" title="Resume-snapshot buffer status (Multi_runner_XDash.md Phase 5b) — a real kaggle datasets status call, checked on demand">Snapshot</button>
          <button class="btn btn-sm btn-ghost" data-action="toggle-credentials" data-account="${escapeHtml(a.name)}">${credEditOpen ? "Cancel" : "Credentials"}</button>
          <button class="btn btn-sm btn-danger" data-action="remove-account" data-account="${escapeHtml(a.name)}">Remove</button>
        </div>
      </div>
    </div>`;
  }).join("");

  body.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const account = btn.dataset.account;
      const action = btn.dataset.action;
      if (action === "validate-account") validateKaggleAccount(account);
      else if (action === "check-snapshot") checkKaggleSnapshot(account);
      else if (action === "remove-account") removeKaggleAccount(account);
      else if (action === "toggle-credentials") toggleKaggleCredentialForm(account);
      else if (action === "save-credentials") saveKaggleCredentials(account);
      else if (action === "remove-credential") removeKaggleCredential(account, btn.dataset.kind);
      else if (action === "edit-name") toggleKaggleNameEdit(account, true);
      else if (action === "cancel-name") toggleKaggleNameEdit(account, false);
      else if (action === "save-name") saveKaggleAccountName(account);
    });
  });
}

function renderKaggleSparkline(history) {
  if (!history || !history.length) return "";
  const w = 110, h = 30, pad = 2;
  const max = Math.max(...history.map((p) => p.hours), 0.01);
  const stepX = (w - pad * 2) / Math.max(history.length - 1, 1);
  const points = history.map((p, i) => {
    const x = pad + i * stepX;
    const y = h - pad - (p.hours / max) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return `<div class="kaggle-stat kaggle-sparkline-cell" title="Est. GPU-hours per week, past ${history.length} weeks">
    <div class="kaggle-stat-label">Trend</div>
    <svg class="kaggle-sparkline" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <polyline points="${points}" fill="none" stroke="var(--amber)" stroke-width="1.5" />
    </svg>
  </div>`;
}

async function checkKaggleSnapshot(name) {
  try {
    const result = await api(`/api/kaggle/accounts/${encodeURIComponent(name)}/snapshot`);
    state.kaggleSnapshotStatus[name] = result;
    renderKaggleAccounts();
    toast(result.exists ? `Snapshot for '${name}': ${result.status}` : `No snapshot created yet for '${name}'`, "ok");
  } catch (e) {
    toast(`Couldn't check snapshot: ${e.message}`, "err");
  }
}

function toggleKaggleNameEdit(name, open) {
  if (open) state.kaggleNameEditOpen.add(name);
  else state.kaggleNameEditOpen.delete(name);
  renderKaggleAccounts();
}

async function saveKaggleAccountName(name) {
  const input = document.getElementById(`kaggle-name-input-${name}`);
  const newName = input.value.trim();
  if (!newName) { toast("Account name can't be empty", "err"); return; }
  if (newName === name) { toggleKaggleNameEdit(name, false); return; }
  try {
    await api(`/api/kaggle/accounts/${encodeURIComponent(name)}/rename`, {
      method: "POST", body: JSON.stringify({ name: newName }),
    });
    toast(`Renamed '${name}' to '${newName}'`, "ok");
    state.kaggleNameEditOpen.delete(name);
    loadKaggle();
  } catch (e) {
    toast("Couldn't rename account: " + e.message, "err");
  }
}

function renderKaggleCredentialForm(a) {
  return `<div class="scheduler-add-form" style="padding-top:9px; border-top:1px dashed var(--border-soft);">
    <div class="field grow">
      <label>Kaggle username</label>
      <input class="text-input grow" id="kaggle-cred-username-${escapeHtml(a.name)}" autocomplete="off" value="${escapeHtml(a.kaggle_username || "")}" />
    </div>
    <div class="field grow">
      <label>New classic key (leave blank to keep current)</label>
      <input class="text-input grow" id="kaggle-cred-key-${escapeHtml(a.name)}" type="password" autocomplete="off" placeholder="paste the key from kaggle.json" />
    </div>
    <div class="field grow">
      <label>New API token (leave blank to keep current)</label>
      <input class="text-input grow" id="kaggle-cred-token-${escapeHtml(a.name)}" type="password" autocomplete="off" placeholder="paste the new-format token" />
    </div>
    <button class="btn btn-sm btn-primary" data-action="save-credentials" data-account="${escapeHtml(a.name)}">Save</button>
    ${a.has_legacy_key ? `<button class="btn btn-sm btn-danger" data-action="remove-credential" data-account="${escapeHtml(a.name)}" data-kind="legacy">Remove key</button>` : ""}
    ${a.has_api_token ? `<button class="btn btn-sm btn-danger" data-action="remove-credential" data-account="${escapeHtml(a.name)}" data-kind="token">Remove token</button>` : ""}
  </div>`;
}

function toggleKaggleCredentialForm(name) {
  if (state.kaggleCredEditOpen.has(name)) state.kaggleCredEditOpen.delete(name);
  else state.kaggleCredEditOpen.add(name);
  renderKaggleAccounts();
}

async function saveKaggleCredentials(name) {
  const usernameInput = document.getElementById(`kaggle-cred-username-${name}`);
  const current = state.kaggleAccounts.find((a) => a.name === name);
  const typedUsername = usernameInput.value.trim();
  const username = current && typedUsername === current.kaggle_username ? "" : typedUsername;
  const key = document.getElementById(`kaggle-cred-key-${name}`).value.trim();
  const api_token = document.getElementById(`kaggle-cred-token-${name}`).value.trim();
  if (!username && !key && !api_token) { toast("Change the username, enter a new key, a new token, or some combination", "err"); return; }
  try {
    await api(`/api/kaggle/accounts/${encodeURIComponent(name)}/credentials`, {
      method: "PATCH", body: JSON.stringify({ username, key, api_token }),
    });
    toast(`Credentials updated for '${name}'`, "ok");
    state.kaggleCredEditOpen.delete(name);
    loadKaggle();
  } catch (e) {
    toast("Couldn't update credentials: " + e.message, "err");
  }
}

async function removeKaggleCredential(name, kind) {
  const ok = await showConfirm("Remove this credential?", `This removes '${name}'s stored ${kind === "legacy" ? "classic key" : "API token"}. The other credential (if any) is left in place.`);
  if (!ok) return;
  try {
    await api(`/api/kaggle/accounts/${encodeURIComponent(name)}/credentials/${kind}`, { method: "DELETE" });
    toast(`Removed ${kind === "legacy" ? "classic key" : "API token"} from '${name}'`, "ok");
    loadKaggle();
  } catch (e) {
    toast("Couldn't remove credential: " + e.message, "err");
  }
}

async function addKaggleAccount() {
  const name = document.getElementById("kaggle-new-account-name").value.trim();
  const username = document.getElementById("kaggle-new-account-username").value.trim();
  const key = document.getElementById("kaggle-new-account-key").value.trim();
  const api_token = document.getElementById("kaggle-new-account-token").value.trim();
  const scope = document.getElementById("kaggle-new-account-scope").value;
  if (!name || !username) { toast("Account name and Kaggle username are required", "err"); return; }
  if (!key && !api_token) { toast("Provide a classic API key, an API token, or both", "err"); return; }
  try {
    await api("/api/kaggle/accounts", { method: "POST", body: JSON.stringify({ name, username, key, api_token, scope }) });
    toast(`Account '${name}' added`, "ok");
    ["kaggle-new-account-name", "kaggle-new-account-username", "kaggle-new-account-key", "kaggle-new-account-token"]
      .forEach((id) => (document.getElementById(id).value = ""));
    toggleComputeAddForm("kaggle-add-account-form", "btn-kaggle-toggle-add-account", "+ Add account", "Cancel");
    loadKaggle();
  } catch (e) {
    toast("Couldn't add account: " + e.message, "err");
  }
}

async function validateKaggleAccount(name) {
  try {
    const result = await api(`/api/kaggle/accounts/${encodeURIComponent(name)}/validate`, { method: "POST" });
    toast(
      result.ok ? `'${name}' credentials look valid` : `'${name}' failed to authenticate: ${result.detail}`,
      result.ok ? "ok" : "err"
    );
  } catch (e) {
    toast("Validation failed: " + e.message, "err");
  }
}

async function removeKaggleAccount(name) {
  const ok = await showConfirm(
    "Remove account?",
    `This deletes '${name}'s stored credentials from the dashboard. It does not affect anything already on Kaggle, and past runs stay in Results.`
  );
  if (!ok) return;
  try {
    await api(`/api/kaggle/accounts/${encodeURIComponent(name)}`, { method: "DELETE" });
    toast(`Account '${name}' removed`, "ok");
    loadKaggle();
  } catch (e) {
    toast("Couldn't remove account: " + e.message, "err");
  }
}

// Shared by both the Kaggle and Colab "+ Add account" toggle buttons.
function toggleComputeAddForm(formId, toggleBtnId, collapsedLabel, expandedLabel) {
  const form = document.getElementById(formId);
  const btn = document.getElementById(toggleBtnId);
  const nowHidden = !form.classList.toggle("hidden");
  btn.textContent = nowHidden ? collapsedLabel : expandedLabel;
  if (!nowHidden) form.querySelector("input, select")?.focus();
}

// ---------------------------------------------------------------- Colab subtab
async function loadComputeColab() {
  const body = document.getElementById("compute-colab-body");
  if (!body) return;
  let accounts;
  try {
    const data = await api("/api/colab/accounts");
    accounts = data.accounts || [];
  } catch (e) {
    body.innerHTML = `<div class="empty-state">Couldn't load Colab accounts: ${escapeHtml(e.message)}</div>`;
    return;
  }
  state.colabAccounts = accounts;
  renderComputeColab();
}

function renderComputeColab() {
  const body = document.getElementById("compute-colab-body");
  const countEl = document.getElementById("compute-colab-count");
  if (countEl) countEl.textContent = state.colabAccounts.length
    ? `${state.colabAccounts.length} account${state.colabAccounts.length === 1 ? "" : "s"}`
    : "";
  if (!body) return;

  if (!state.colabAccounts.length) {
    body.innerHTML = `<div class="empty-state">No Colab accounts registered yet. Add one below, then run <code>colab login</code> into its credentials directory (<code>data/colab_accounts/&lt;name&gt;/</code>) out of band — see backend/colab.py's own module docstring for why this dashboard doesn't (yet) capture that interactively.</div>`;
    return;
  }

  body.innerHTML = state.colabAccounts.map((a) => {
    const session = state.colabSessionStatus[a.name];
    const sessionHtml = session
      ? `<div class="kaggle-card-sub">VM: ${session.live ? `<span class="kaggle-chip on">live</span>` : "idle (no VM)"}</div>`
      : "";
    return `<div class="kaggle-card">
      <div class="kaggle-card-accent ${a.has_credentials ? "emerald" : "slate"}"></div>
      <div class="kaggle-card-body">
        <div class="kaggle-card-header">
          <div style="min-width:0;">
            <div class="kaggle-card-title-row"><span class="kaggle-card-title">${escapeHtml(a.label || a.name)}</span></div>
            <div class="kaggle-card-sub">${escapeHtml(a.gpu || "default GPU")} · session cap ${a.session_limit_hours != null ? fmtNum(a.session_limit_hours) + "h" : "(default)"}</div>
          </div>
          <span class="kaggle-chip ${a.has_credentials ? "on" : ""}" title="${a.has_credentials ? "sessions.json found" : "no credentials captured yet — see the note above"}">${a.has_credentials ? "connected" : "not connected"}</span>
        </div>
        ${sessionHtml}
        <div class="kaggle-card-footer">
          <button class="btn btn-sm btn-ghost" data-action="colab-check-session" data-account="${escapeHtml(a.name)}" title="colab sessions — a real CLI call, checked on demand">Check VM</button>
          <button class="btn btn-sm btn-ghost" data-action="colab-stop-session" data-account="${escapeHtml(a.name)}" title="Tear down this account's VM now, if one is up">Stop VM</button>
          <button class="btn btn-sm btn-danger" data-action="colab-remove" data-account="${escapeHtml(a.name)}">Remove</button>
        </div>
      </div>
    </div>`;
  }).join("");

  body.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const account = btn.dataset.account;
      const action = btn.dataset.action;
      if (action === "colab-check-session") checkComputeColabSession(account);
      else if (action === "colab-stop-session") stopComputeColabSession(account);
      else if (action === "colab-remove") removeComputeColabAccount(account);
    });
  });
}

async function checkComputeColabSession(name) {
  try {
    const result = await api(`/api/colab/accounts/${encodeURIComponent(name)}/session`);
    state.colabSessionStatus[name] = result;
    renderComputeColab();
    toast(result.live ? `'${name}' has a live VM` : `'${name}' has no VM up right now`, "ok");
  } catch (e) {
    toast(`Couldn't check '${name}': ${e.message}`, "err");
  }
}

async function stopComputeColabSession(name) {
  const ok = await showConfirm("Stop this VM?", `Tears down '${name}'s Colab VM now, if one is running. Nothing to undo if none is up.`);
  if (!ok) return;
  try {
    const result = await api(`/api/colab/accounts/${encodeURIComponent(name)}/stop`, { method: "POST" });
    toast(result.stopped ? `Stopped '${name}'` : `Nothing to stop for '${name}'`, "ok");
    checkComputeColabSession(name);
  } catch (e) {
    toast(`Couldn't stop '${name}': ${e.message}`, "err");
  }
}

async function addComputeColabAccount() {
  const name = document.getElementById("colab-new-account-name").value.trim();
  const label = document.getElementById("colab-new-account-label").value.trim();
  const gpu = document.getElementById("colab-new-account-gpu").value.trim();
  const limitRaw = document.getElementById("colab-new-account-limit").value.trim();
  if (!name) { toast("Account name is required", "err"); return; }
  try {
    const body = { name, label, gpu };
    if (limitRaw) body.session_limit_hours = Number(limitRaw);
    await api("/api/colab/accounts", { method: "POST", body: JSON.stringify(body) });
    toast(`Colab account '${name}' registered`, "ok");
    ["colab-new-account-name", "colab-new-account-label", "colab-new-account-gpu", "colab-new-account-limit"]
      .forEach((id) => (document.getElementById(id).value = ""));
    toggleComputeAddForm("colab-add-account-form", "btn-colab-toggle-add-account", "+ Add account", "Cancel");
    loadComputeColab();
  } catch (e) {
    toast("Couldn't register account: " + e.message, "err");
  }
}

async function removeComputeColabAccount(name) {
  const ok = await showConfirm("Remove account?", `Deregisters '${name}' from the dashboard. Doesn't touch anything on Google's side.`);
  if (!ok) return;
  try {
    await api(`/api/colab/accounts/${encodeURIComponent(name)}`, { method: "DELETE" });
    toast(`Account '${name}' removed`, "ok");
    loadComputeColab();
  } catch (e) {
    toast("Couldn't remove account: " + e.message, "err");
  }
}

function initComputeButtons() {
  document.getElementById("btn-kaggle-add-account").addEventListener("click", addKaggleAccount);
  document.getElementById("btn-kaggle-toggle-add-account").addEventListener("click", () =>
    toggleComputeAddForm("kaggle-add-account-form", "btn-kaggle-toggle-add-account", "+ Add account", "Cancel"));
  document.getElementById("btn-kaggle-toggle-autorefresh").addEventListener("click", () => setKaggleAutoRefresh(!state.kaggleAutoRefresh));
  document.getElementById("btn-colab-add-account").addEventListener("click", addComputeColabAccount);
  document.getElementById("btn-colab-toggle-add-account").addEventListener("click", () =>
    toggleComputeAddForm("colab-add-account-form", "btn-colab-toggle-add-account", "+ Add account", "Cancel"));

  // Reflect the stored preference in the toggle label, and actually start
  // the auto-refresh timer if it was left on.
  setKaggleAutoRefresh(state.kaggleAutoRefresh);
  initComputeSubtabs();
}

initComputeButtons();
