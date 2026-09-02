// static/js/views/kaggle.js
//
// Kaggle tab: multi-account push/status/download for notebook-based
// training kernels (backend/kaggle.py), plus a self-tracked GPU-hours
// estimate per account — Kaggle's API exposes no real quota endpoint, so
// this is only ever an estimate (see backend/kaggle.py's estimate_usage()
// docstring for why).
//
// Deliberately a plain classic <script>, like app.js/badge.js/runs.js —
// uses api()/toast()/escapeHtml()/timeAgo()/state/renderStatusBadge()/
// showConfirm() as page-global bindings rather than ES-module imports (see
// IMPLEMENTATION_PLAN.md Phase 7 for the eventual full split).

state.kaggleAccounts = [];
state.kaggleCredEditOpen = new Set();
state.kaggleNameEditOpen = new Set();
state.kaggleHistoryOpen = new Set();
state.kaggleLastFailedAction = {};   // worker_id -> "push" | "refresh" | "download", for the one-click Retry button
state.kaggleLastSeenStatus = {};     // worker_id -> status, so loadKaggle() can tell a *new* transition from a re-render
state.kaggleAutoRefreshTimer = null;
state.kaggleAutoRefresh = _kaggleGetPref("kaggleAutoRefresh");
state.kaggleNotify = _kaggleGetPref("kaggleNotify");

function _kaggleGetPref(key) {
  try { return localStorage.getItem(key) === "1"; } catch (e) { return false; }
}
function _kaggleSetPref(key, value) {
  try { localStorage.setItem(key, value ? "1" : "0"); } catch (e) {}
}

async function loadKaggle() {
  const accountsBody = document.getElementById("kaggle-accounts-body");
  const workersBody = document.getElementById("kaggle-workers-body");
  accountsBody.innerHTML = `<div class="empty-state">Loading…</div>`;
  workersBody.innerHTML = `<div class="empty-state">Loading…</div>`;
  try {
    const data = await api("/api/kaggle/accounts");
    state.kaggleAccounts = data.accounts || [];
    checkKaggleNotifications(state.kaggleAccounts);
    renderKaggleSummary();
    renderKaggleAccounts();
    renderKaggleWorkers();
    renderKaggleWorkerAccountOptions();
  } catch (e) {
    document.getElementById("kaggle-summary-strip").innerHTML = "";
    accountsBody.innerHTML = `<div class="empty-state">Failed to load accounts: ${escapeHtml(e.message)}</div>`;
    workersBody.innerHTML = "";
  }
}

function renderKaggleSummary() {
  const el = document.getElementById("kaggle-summary-strip");
  const accounts = state.kaggleAccounts;
  if (!accounts.length) { el.innerHTML = ""; return; }

  const allWorkers = accounts.flatMap((a) => a.workers || []);
  const totalHours = accounts.reduce((sum, a) => sum + ((a.usage_estimate || {}).hours_this_week || 0), 0);
  const running = allWorkers.filter((w) => ["queued", "preparing", "running"].includes(w.status)).length;
  const complete = allWorkers.filter((w) => w.status === "complete").length;
  const errored = allWorkers.filter((w) => w.status === "error" || w.status === "push_failed" || w.status === "unknown").length;

  el.innerHTML =
    `<div class="compute-summary-chip"><b>${accounts.length}</b>account${accounts.length === 1 ? "" : "s"}</div>` +
    `<div class="compute-summary-chip"><b>${allWorkers.length}</b>worker${allWorkers.length === 1 ? "" : "s"}</div>` +
    `<div class="compute-summary-chip"><b>${totalHours.toFixed(2)}</b>est. GPU-hours this week</div>` +
    (running ? `<div class="compute-summary-chip"><b style="color:var(--amber);">${running}</b>running</div>` : "") +
    (complete ? `<div class="compute-summary-chip"><b style="color:var(--emerald);">${complete}</b>complete</div>` : "") +
    (errored ? `<div class="compute-summary-chip"><b style="color:var(--red);">${errored}</b>need attention</div>` : "");
}

// ----------------------------------------------------------- auto-refresh / notify
// Auto-refresh polls the (read-only, cheap) /api/kaggle/accounts endpoint —
// the actual Kaggle status checks are the background poller's job
// (backend/kaggle.py's ensure_kaggle_worker_started/_tick), which keeps
// running server-side whether or not this tab is open or this toggle is on.
// This toggle only controls whether the *page* re-renders itself to show
// what the poller has already found.
function setKaggleAutoRefresh(enabled) {
  state.kaggleAutoRefresh = enabled;
  _kaggleSetPref("kaggleAutoRefresh", enabled);
  document.getElementById("btn-kaggle-toggle-autorefresh").textContent = `Auto-refresh: ${enabled ? "on" : "off"}`;
  if (state.kaggleAutoRefreshTimer) { clearInterval(state.kaggleAutoRefreshTimer); state.kaggleAutoRefreshTimer = null; }
  if (enabled) state.kaggleAutoRefreshTimer = setInterval(loadKaggle, 30000);
}

async function setKaggleNotify(enabled) {
  if (enabled && typeof Notification !== "undefined" && Notification.permission === "default") {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") { toast("Browser notification permission was not granted", "err"); enabled = false; }
  }
  state.kaggleNotify = enabled;
  _kaggleSetPref("kaggleNotify", enabled);
  document.getElementById("btn-kaggle-toggle-notify").textContent = `Notify: ${enabled ? "on" : "off"}`;
}

function checkKaggleNotifications(accounts) {
  accounts.forEach((a) => (a.workers || []).forEach((w) => {
    const prevStatus = state.kaggleLastSeenStatus[w.worker_id];
    if (prevStatus !== undefined && prevStatus !== w.status && (w.status === "complete" || w.status === "error")) {
      fireKaggleNotification(w, a.name);
    }
    state.kaggleLastSeenStatus[w.worker_id] = w.status;
  }));
}

function fireKaggleNotification(worker, accountName) {
  if (!state.kaggleNotify || typeof Notification === "undefined" || Notification.permission !== "granted") return;
  try {
    new Notification(`Kaggle: '${worker.worker_id}' is ${worker.status}`, {
      body: `${accountName} · ${worker.kernel_slug}`,
    });
  } catch (e) { /* some browsers throw if the page isn't foregrounded/focused-eligible — not worth surfacing */ }
}

function renderKaggleAccounts() {
  const body = document.getElementById("kaggle-accounts-body");
  const countEl = document.getElementById("kaggle-account-count");
  countEl.textContent = state.kaggleAccounts.length
    ? `${state.kaggleAccounts.length} account${state.kaggleAccounts.length === 1 ? "" : "s"}`
    : "";

  if (!state.kaggleAccounts.length) {
    body.innerHTML = `<div class="empty-state">No accounts configured yet — add one below with a classic key, an API token, or both.</div>`;
    return;
  }

  body.innerHTML = state.kaggleAccounts.map((a) => {
    const usage = a.usage_estimate || {};
    const hours = usage.hours_this_week !== undefined ? usage.hours_this_week : "–";
    const workerCount = (a.workers || []).length;
    const credEditOpen = state.kaggleCredEditOpen.has(a.name);
    const nameEditOpen = state.kaggleNameEditOpen.has(a.name);
    const titleHtml = nameEditOpen
      ? `<input class="text-input grow" id="kaggle-name-input-${escapeHtml(a.name)}" value="${escapeHtml(a.name)}" autocomplete="off" />
         <button class="btn-icon save" data-action="save-name" data-account="${escapeHtml(a.name)}" title="Save">✓</button>
         <button class="btn-icon cancel" data-action="cancel-name" data-account="${escapeHtml(a.name)}" title="Cancel">✕</button>`
      : `<span class="kaggle-card-title" title="${escapeHtml(a.name)}">${escapeHtml(a.name)}</span>
         <button class="btn-icon" data-action="edit-name" data-account="${escapeHtml(a.name)}" title="Rename">✎</button>`;
    return `<div class="kaggle-card">
      <div class="kaggle-card-accent teal"></div>
      <div class="kaggle-card-body">
        <div class="kaggle-card-header">
          <div style="min-width:0;">
            <div class="kaggle-card-title-row">${titleHtml}</div>
            <div class="kaggle-card-sub">${escapeHtml(a.kaggle_username || "–")} · ${workerCount} worker${workerCount === 1 ? "" : "s"}</div>
          </div>
          <div class="kaggle-cred-chips">
            <span class="kaggle-chip ${a.has_legacy_key ? "on" : ""}" title="Classic username/key pair stored">key</span>
            <span class="kaggle-chip ${a.has_api_token ? "on" : ""}" title="New-format API token stored">token</span>
          </div>
        </div>

        <div class="kaggle-stat-row">
          <div class="kaggle-stat" title="Self-tracked estimate, summed from this account's own downloaded runs this UTC week — not Kaggle's own quota figure (Kaggle exposes no API for that).">
            <div class="kaggle-stat-label">Est. GPU-hrs / wk</div>
            <div class="kaggle-stat-value">${escapeHtml(String(hours))}</div>
          </div>
          ${renderKaggleSparkline(a.usage_history)}
        </div>

        <div class="kaggle-auto-chain-row" title="When on, the background poller automatically pushes this account's next never-pushed worker once the current one reaches a final status.">
          <button class="toggle-switch ${a.auto_chain ? "on" : ""}" data-action="toggle-auto-chain" data-account="${escapeHtml(a.name)}"><span class="toggle-knob"></span></button>
          <span>Auto-chain next worker</span>
        </div>

        ${credEditOpen ? renderKaggleCredentialForm(a) : ""}

        <div class="kaggle-card-footer">
          <button class="btn btn-sm btn-ghost" data-action="validate-account" data-account="${escapeHtml(a.name)}">Validate</button>
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
      else if (action === "remove-account") removeKaggleAccount(account);
      else if (action === "toggle-credentials") toggleKaggleCredentialForm(account);
      else if (action === "save-credentials") saveKaggleCredentials(account);
      else if (action === "remove-credential") removeKaggleCredential(account, btn.dataset.kind);
      else if (action === "edit-name") toggleKaggleNameEdit(account, true);
      else if (action === "cancel-name") toggleKaggleNameEdit(account, false);
      else if (action === "save-name") saveKaggleAccountName(account);
      else if (action === "toggle-auto-chain") toggleKaggleAutoChain(account, !btn.classList.contains("on"));
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

async function toggleKaggleAutoChain(name, enabled) {
  try {
    await api(`/api/kaggle/accounts/${encodeURIComponent(name)}/auto_chain`, {
      method: "POST", body: JSON.stringify({ enabled }),
    });
    loadKaggle();
  } catch (e) {
    toast("Couldn't update auto-chain: " + e.message, "err");
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
  // Only send the username if it actually changed — update_credentials()
  // treats a bare username (no key) as a pure relabel, so an unchanged
  // value here should be a no-op, not a redundant rewrite.
  const username = current && typedUsername === current.kaggle_username ? "" : typedUsername;
  const key = document.getElementById(`kaggle-cred-key-${name}`).value.trim();
  const api_token = document.getElementById(`kaggle-cred-token-${name}`).value.trim();
  if (!username && !key && !api_token) { toast("Change the username, enter a new key, a new token, or some combination", "err"); return; }
  try {
    await api(`/api/kaggle/accounts/${encodeURIComponent(name)}/credentials`, {
      method: "PATCH", body: JSON.stringify({ username, key, api_token }),
    });
    toast(`Credentials updated for '${name}' — its workers were left untouched`, "ok");
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

function renderKaggleWorkerAccountOptions() {
  const select = document.getElementById("kaggle-new-worker-account");
  const current = select.value;
  select.innerHTML = `<option value="">— pick an account —</option>` +
    state.kaggleAccounts.map((a) => `<option value="${escapeHtml(a.name)}">${escapeHtml(a.name)}</option>`).join("");
  if (state.kaggleAccounts.some((a) => a.name === current)) select.value = current;
}

function renderKaggleWorkers() {
  const body = document.getElementById("kaggle-workers-body");
  const countEl = document.getElementById("kaggle-worker-count");
  const allWorkers = [];
  state.kaggleAccounts.forEach((a) => (a.workers || []).forEach((w) => allWorkers.push({ ...w, account_name: a.name })));

  countEl.textContent = allWorkers.length ? `${allWorkers.length} worker${allWorkers.length === 1 ? "" : "s"}` : "";

  if (!allWorkers.length) {
    body.innerHTML = `<div class="empty-state">No workers configured yet. Add one below once you've added an account — it should point at a notebook already committed in this repo.</div>`;
    return;
  }

  body.innerHTML = allWorkers.map((w) => {
    const status = w.status || "unconfigured";
    const accentClass = w.over_budget ? "red" : statusBadgeClass(status);
    const pushedText = w.pushed_at ? timeAgo(w.pushed_at) : "not pushed yet";
    const overBudgetBadge = w.over_budget ? `<span class="badge red" title="Running longer than its session budget">over budget</span>` : "";
    const notebookChangedBadge = w.notebook_changed
      ? `<span class="badge amber" title="The local notebook differs from what was last pushed">notebook changed</span>` : "";
    const retryBtn = state.kaggleLastFailedAction[w.worker_id]
      ? `<button class="btn-icon" data-action="retry-worker" data-id="${escapeHtml(w.worker_id)}" title="Retry the last failed action">↻</button>` : "";
    const errorHtml = w.last_error
      ? `<div class="kaggle-card-error">${escapeHtml(w.last_error)} ${retryBtn}</div>` : "";
    const historyOpen = state.kaggleHistoryOpen.has(w.worker_id);
    const historyEntries = (w.history || []).slice().reverse();
    const historyHtml = historyOpen
      ? `<div class="kaggle-history">${
          historyEntries.length
            ? historyEntries.map((h) => `<div class="kaggle-history-row"><span class="kaggle-history-time">${escapeHtml(timeAgo(h.at))}</span>${escapeHtml(h.event)}</div>`).join("")
            : `<div class="kaggle-history-row">No activity yet.</div>`
        }</div>`
      : "";
    return `<div class="kaggle-card">
      <div class="kaggle-card-accent ${accentClass}"></div>
      <div class="kaggle-card-body">
        <div class="kaggle-card-header">
          <div>
            <div class="kaggle-card-title">${escapeHtml(w.worker_id)}</div>
            <div class="kaggle-card-sub">${escapeHtml(w.account_name)} · ${escapeHtml(w.kernel_slug)}</div>
          </div>
          <div>${renderStatusBadge(status)} ${overBudgetBadge} ${notebookChangedBadge}</div>
        </div>

        <div class="kaggle-stat-row">
          <div class="kaggle-stat">
            <div class="kaggle-stat-label">Pushed</div>
            <div class="kaggle-stat-value" style="font-size:12.5px;">${escapeHtml(pushedText)}</div>
          </div>
          <div class="kaggle-stat">
            <div class="kaggle-stat-label">Budget</div>
            <div class="kaggle-stat-value" style="font-size:12.5px;">${w.budget_hours ? escapeHtml(String(w.budget_hours)) + "h" : "–"}</div>
          </div>
        </div>

        ${errorHtml}
        ${historyHtml}

        <div class="kaggle-card-footer">
          <button class="btn btn-sm btn-ghost" data-action="push-worker" data-id="${escapeHtml(w.worker_id)}">Push</button>
          <button class="btn btn-sm btn-ghost" data-action="refresh-worker" data-id="${escapeHtml(w.worker_id)}">Refresh</button>
          <button class="btn btn-sm btn-ghost" data-action="download-worker" data-id="${escapeHtml(w.worker_id)}">Download</button>
          <button class="btn btn-sm btn-ghost" data-action="toggle-history" data-id="${escapeHtml(w.worker_id)}">${historyOpen ? "Hide history" : "History"}</button>
          <button class="btn btn-sm btn-danger" data-action="remove-worker" data-id="${escapeHtml(w.worker_id)}" data-account="${escapeHtml(w.account_name)}">Remove</button>
        </div>
      </div>
    </div>`;
  }).join("");

  body.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const id = btn.dataset.id;
      const action = btn.dataset.action;
      if (action === "push-worker") pushKaggleWorker(id);
      else if (action === "refresh-worker") refreshKaggleWorker(id);
      else if (action === "download-worker") downloadKaggleWorker(id);
      else if (action === "remove-worker") removeKaggleWorker(btn.dataset.account, id);
      else if (action === "toggle-history") toggleKaggleWorkerHistory(id);
      else if (action === "retry-worker") retryKaggleAction(id);
    });
  });
}

// ---------------------------------------------------------------- accounts
async function addKaggleAccount() {
  const name = document.getElementById("kaggle-new-account-name").value.trim();
  const username = document.getElementById("kaggle-new-account-username").value.trim();
  const key = document.getElementById("kaggle-new-account-key").value.trim();
  const api_token = document.getElementById("kaggle-new-account-token").value.trim();
  if (!name || !username) { toast("Account name and Kaggle username are required", "err"); return; }
  if (!key && !api_token) { toast("Provide a classic API key, an API token, or both", "err"); return; }
  try {
    await api("/api/kaggle/accounts", { method: "POST", body: JSON.stringify({ name, username, key, api_token }) });
    toast(`Account '${name}' added`, "ok");
    ["kaggle-new-account-name", "kaggle-new-account-username", "kaggle-new-account-key", "kaggle-new-account-token"]
      .forEach((id) => (document.getElementById(id).value = ""));
    toggleKaggleAddForm("kaggle-add-account-form", "btn-kaggle-toggle-add-account", "+ Add account", "Cancel");
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
    `This deletes '${name}'s stored credentials and its worker assignments from the dashboard. It does not affect anything already on Kaggle.`
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

// ----------------------------------------------------------------- workers
async function addKaggleWorker() {
  const account_name = document.getElementById("kaggle-new-worker-account").value;
  const worker_id = document.getElementById("kaggle-new-worker-id").value.trim();
  const notebook_path = document.getElementById("kaggle-new-worker-notebook").value.trim();
  const kernel_slug = document.getElementById("kaggle-new-worker-slug").value.trim();
  const results_dir = document.getElementById("kaggle-new-worker-results").value.trim();
  const budgetRaw = document.getElementById("kaggle-new-worker-budget").value.trim();
  if (!account_name || !worker_id || !notebook_path || !kernel_slug || !results_dir) {
    toast("Account, worker id, notebook path, kernel slug and results dir are all required", "err");
    return;
  }
  const body = { worker_id, notebook_path, kernel_slug, results_dir };
  if (budgetRaw) body.budget_hours = parseFloat(budgetRaw);
  try {
    await api(`/api/kaggle/accounts/${encodeURIComponent(account_name)}/workers`, { method: "POST", body: JSON.stringify(body) });
    toast(`Worker '${worker_id}' added`, "ok");
    ["kaggle-new-worker-id", "kaggle-new-worker-notebook", "kaggle-new-worker-slug", "kaggle-new-worker-results", "kaggle-new-worker-budget"]
      .forEach((id) => (document.getElementById(id).value = ""));
    toggleKaggleAddForm("kaggle-add-worker-form", "btn-kaggle-toggle-add-worker", "+ Add worker", "Cancel");
    loadKaggle();
  } catch (e) {
    toast("Couldn't add worker: " + e.message, "err");
  }
}

async function removeKaggleWorker(accountName, workerId) {
  const ok = await showConfirm(
    "Remove worker?",
    `This only removes '${workerId}' from the dashboard's registry — it does not touch anything on Kaggle.`
  );
  if (!ok) return;
  try {
    await api(`/api/kaggle/accounts/${encodeURIComponent(accountName)}/workers/${encodeURIComponent(workerId)}`, { method: "DELETE" });
    toast(`Worker '${workerId}' removed`, "ok");
    loadKaggle();
  } catch (e) {
    toast("Couldn't remove worker: " + e.message, "err");
  }
}

async function pushKaggleWorker(workerId) {
  try {
    const result = await api(`/api/kaggle/workers/${encodeURIComponent(workerId)}/push`, { method: "POST" });
    delete state.kaggleLastFailedAction[workerId];
    toast(result.concurrent_warning ? `Pushed '${workerId}' — ${result.concurrent_warning}` : `Pushed '${workerId}'`, result.concurrent_warning ? "" : "ok");
    loadKaggle();
  } catch (e) {
    state.kaggleLastFailedAction[workerId] = "push";
    toast(`Push failed: ${e.message}`, "err");
    renderKaggleWorkers();
  }
}

async function refreshKaggleWorker(workerId) {
  try {
    const result = await api(`/api/kaggle/workers/${encodeURIComponent(workerId)}/status`, { method: "POST" });
    delete state.kaggleLastFailedAction[workerId];
    toast(`'${workerId}': ${result.status}`, "ok");
    loadKaggle();
  } catch (e) {
    state.kaggleLastFailedAction[workerId] = "refresh";
    toast(`Status check failed: ${e.message}`, "err");
    renderKaggleWorkers();
  }
}

async function downloadKaggleWorker(workerId) {
  try {
    const result = await api(`/api/kaggle/workers/${encodeURIComponent(workerId)}/download`, { method: "POST" });
    delete state.kaggleLastFailedAction[workerId];
    const n = (result.registered_runs || []).length;
    toast(`Downloaded '${workerId}' — ${n} run${n === 1 ? "" : "s"} registered into the ledger`, "ok");
    loadKaggle();
  } catch (e) {
    state.kaggleLastFailedAction[workerId] = "download";
    toast(`Download failed: ${e.message}`, "err");
    renderKaggleWorkers();
  }
}

const kaggleRetryHandlers = { push: pushKaggleWorker, refresh: refreshKaggleWorker, download: downloadKaggleWorker };

function retryKaggleAction(workerId) {
  const action = state.kaggleLastFailedAction[workerId];
  if (action) kaggleRetryHandlers[action](workerId);
}

function toggleKaggleWorkerHistory(workerId) {
  if (state.kaggleHistoryOpen.has(workerId)) state.kaggleHistoryOpen.delete(workerId);
  else state.kaggleHistoryOpen.add(workerId);
  renderKaggleWorkers();
}

// -------------------------------------------------------------------- bulk
async function pushAllKaggleWorkers() {
  const targets = state.kaggleAccounts.flatMap((a) => (a.workers || []).map((w) => `${w.worker_id} (${a.name})`));
  if (!targets.length) { toast("No workers configured", "err"); return; }
  const ok = await showConfirm(
    `Push ${targets.length} worker${targets.length === 1 ? "" : "s"}?`,
    `This pushes every configured worker, using real GPU quota on each account: ${targets.join(", ")}`
  );
  if (!ok) return;
  try {
    const { results } = await api("/api/kaggle/push_all", { method: "POST" });
    const failed = results.filter((r) => r.error).length;
    toast(`Pushed ${results.length - failed}/${results.length} worker(s)${failed ? `, ${failed} failed` : ""}`, failed ? "err" : "ok");
    loadKaggle();
  } catch (e) {
    toast("Push all failed: " + e.message, "err");
  }
}

async function refreshAllKaggleWorkers() {
  try {
    const { results } = await api("/api/kaggle/refresh_all", { method: "POST" });
    toast(`Refreshed ${results.length} worker(s)`, "ok");
    loadKaggle();
  } catch (e) {
    toast("Refresh all failed: " + e.message, "err");
  }
}

async function downloadAllKaggleWorkers() {
  try {
    const { results } = await api("/api/kaggle/download_all", { method: "POST" });
    if (!results.length) { toast("No workers currently marked complete", ""); return; }
    const failed = results.filter((r) => r.error).length;
    toast(`Downloaded ${results.length - failed}/${results.length} worker(s)${failed ? `, ${failed} failed` : ""}`, failed ? "err" : "ok");
    loadKaggle();
  } catch (e) {
    toast("Download all failed: " + e.message, "err");
  }
}

function toggleKaggleAddForm(formId, toggleBtnId, collapsedLabel, expandedLabel) {
  const form = document.getElementById(formId);
  const btn = document.getElementById(toggleBtnId);
  const nowHidden = !form.classList.toggle("hidden");
  btn.textContent = nowHidden ? collapsedLabel : expandedLabel;
  if (!nowHidden) form.querySelector("input, select")?.focus();
}

// ------------------------------------------------------------- export / import
async function exportKaggleRegistry() {
  try {
    const data = await api("/api/kaggle/registry/export");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "kaggle_registry.json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast("Registry exported — credentials are never included", "ok");
  } catch (e) {
    toast("Export failed: " + e.message, "err");
  }
}

function triggerKaggleImport() {
  document.getElementById("kaggle-import-file").click();
}

async function handleKaggleImportFile(e) {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  let payload;
  try {
    payload = JSON.parse(await file.text());
  } catch (err) {
    toast("That file isn't valid JSON", "err");
    return;
  }
  try {
    const summary = await api("/api/kaggle/registry/import", { method: "POST", body: JSON.stringify(payload) });
    const addedN = summary.workers_added.length;
    const skippedAccounts = summary.accounts_skipped.length;
    const skippedWorkers = summary.workers_skipped.length;
    toast(
      `Imported ${addedN} worker${addedN === 1 ? "" : "s"}` +
      (skippedAccounts ? ` · ${skippedAccounts} account(s) skipped — not configured locally yet, add credentials first` : "") +
      (skippedWorkers ? ` · ${skippedWorkers} worker(s) skipped` : ""),
      addedN ? "ok" : ""
    );
    loadKaggle();
  } catch (err) {
    toast("Import failed: " + err.message, "err");
  }
}

function initKaggleButtons() {
  document.getElementById("btn-kaggle-add-account").addEventListener("click", addKaggleAccount);
  document.getElementById("btn-kaggle-add-worker").addEventListener("click", addKaggleWorker);
  document.getElementById("btn-kaggle-push-all").addEventListener("click", pushAllKaggleWorkers);
  document.getElementById("btn-kaggle-refresh-all").addEventListener("click", refreshAllKaggleWorkers);
  document.getElementById("btn-kaggle-download-all").addEventListener("click", downloadAllKaggleWorkers);
  document.getElementById("btn-kaggle-toggle-add-account").addEventListener("click", () =>
    toggleKaggleAddForm("kaggle-add-account-form", "btn-kaggle-toggle-add-account", "+ Add account", "Cancel"));
  document.getElementById("btn-kaggle-toggle-add-worker").addEventListener("click", () =>
    toggleKaggleAddForm("kaggle-add-worker-form", "btn-kaggle-toggle-add-worker", "+ Add worker", "Cancel"));
  document.getElementById("btn-kaggle-toggle-autorefresh").addEventListener("click", () => setKaggleAutoRefresh(!state.kaggleAutoRefresh));
  document.getElementById("btn-kaggle-toggle-notify").addEventListener("click", () => setKaggleNotify(!state.kaggleNotify));
  document.getElementById("btn-kaggle-export").addEventListener("click", exportKaggleRegistry);
  document.getElementById("btn-kaggle-import").addEventListener("click", triggerKaggleImport);
  document.getElementById("kaggle-import-file").addEventListener("change", handleKaggleImportFile);

  // Reflect stored preferences in the toggle labels, and actually start the
  // auto-refresh timer if it was left on — but never auto-request
  // Notification permission on load (that must stay behind a click).
  setKaggleAutoRefresh(state.kaggleAutoRefresh);
  document.getElementById("btn-kaggle-toggle-notify").textContent = `Notify: ${state.kaggleNotify ? "on" : "off"}`;
}

initKaggleButtons();
