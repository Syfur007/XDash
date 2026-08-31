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

async function loadKaggle() {
  const accountsBody = document.getElementById("kaggle-accounts-body");
  const workersBody = document.getElementById("kaggle-workers-body");
  accountsBody.innerHTML = `<div class="empty-state">Loading…</div>`;
  workersBody.innerHTML = `<div class="empty-state">Loading…</div>`;
  try {
    const data = await api("/api/kaggle/accounts");
    state.kaggleAccounts = data.accounts || [];
    renderKaggleAccounts();
    renderKaggleWorkers();
    renderKaggleWorkerAccountOptions();
  } catch (e) {
    accountsBody.innerHTML = `<div class="empty-state">Failed to load accounts: ${escapeHtml(e.message)}</div>`;
    workersBody.innerHTML = "";
  }
}

function renderKaggleAccounts() {
  const body = document.getElementById("kaggle-accounts-body");
  const countEl = document.getElementById("kaggle-account-count");
  countEl.textContent = state.kaggleAccounts.length
    ? `${state.kaggleAccounts.length} account${state.kaggleAccounts.length === 1 ? "" : "s"}`
    : "";

  if (!state.kaggleAccounts.length) {
    body.innerHTML = `<div class="empty-state">No accounts configured yet — add one below with a classic kaggle.json (username/key), not a newer-format token.</div>`;
    return;
  }

  body.innerHTML = state.kaggleAccounts.map((a) => {
    const usage = a.usage_estimate || {};
    const hours = usage.hours_this_week !== undefined ? usage.hours_this_week : "–";
    return `<div class="list-row">
      <div class="list-row-main">
        <div class="list-row-title">${escapeHtml(a.name)}</div>
        <div class="list-row-sub">Kaggle user: ${escapeHtml(a.kaggle_username || "–")} · ~${escapeHtml(String(hours))}h used this week (self-tracked estimate, not Kaggle's own quota)</div>
      </div>
      <div class="job-actions">
        <button class="btn btn-sm btn-ghost" data-action="validate-account" data-account="${escapeHtml(a.name)}">Validate</button>
        <button class="btn btn-sm btn-danger" data-action="remove-account" data-account="${escapeHtml(a.name)}">Remove</button>
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
    });
  });
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
    const budgetText = w.budget_hours ? `budget ${w.budget_hours}h` : "";
    const pushedText = w.pushed_at ? `pushed ${timeAgo(w.pushed_at)}` : "not pushed yet";
    const overBudget = w.over_budget ? `<span class="badge red" title="Running longer than its session budget">over budget</span>` : "";
    const errorText = w.last_error ? `<div class="list-row-sub" style="color:var(--red);">${escapeHtml(w.last_error)}</div>` : "";
    return `<div class="list-row">
      <div class="list-row-main">
        <div class="list-row-title">${escapeHtml(w.worker_id)} ${renderStatusBadge(status)} ${overBudget}</div>
        <div class="list-row-sub">${escapeHtml(w.account_name)} · ${escapeHtml(w.kernel_slug)} · ${pushedText}${budgetText ? " · " + escapeHtml(budgetText) : ""}</div>
        ${errorText}
      </div>
      <div class="job-actions">
        <button class="btn btn-sm btn-ghost" data-action="push-worker" data-id="${escapeHtml(w.worker_id)}">Push</button>
        <button class="btn btn-sm btn-ghost" data-action="refresh-worker" data-id="${escapeHtml(w.worker_id)}">Refresh</button>
        <button class="btn btn-sm btn-ghost" data-action="download-worker" data-id="${escapeHtml(w.worker_id)}">Download</button>
        <button class="btn btn-sm btn-danger" data-action="remove-worker" data-id="${escapeHtml(w.worker_id)}" data-account="${escapeHtml(w.account_name)}">Remove</button>
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
    });
  });
}

// ---------------------------------------------------------------- accounts
async function addKaggleAccount() {
  const name = document.getElementById("kaggle-new-account-name").value.trim();
  const username = document.getElementById("kaggle-new-account-username").value.trim();
  const key = document.getElementById("kaggle-new-account-key").value.trim();
  if (!name || !username || !key) { toast("Account name, Kaggle username and API key are all required", "err"); return; }
  // The backend still expects (and validates) the classic kaggle.json shape —
  // built here so the user only ever types the two plain fields Kaggle
  // itself hands out, never raw JSON.
  const kaggleJson = JSON.stringify({ username, key });
  try {
    await api("/api/kaggle/accounts", { method: "POST", body: JSON.stringify({ name, kaggle_json: kaggleJson }) });
    toast(`Account '${name}' added`, "ok");
    document.getElementById("kaggle-new-account-name").value = "";
    document.getElementById("kaggle-new-account-username").value = "";
    document.getElementById("kaggle-new-account-key").value = "";
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
    await api(`/api/kaggle/workers/${encodeURIComponent(workerId)}/push`, { method: "POST" });
    toast(`Pushed '${workerId}'`, "ok");
    loadKaggle();
  } catch (e) {
    toast(`Push failed: ${e.message}`, "err");
  }
}

async function refreshKaggleWorker(workerId) {
  try {
    const result = await api(`/api/kaggle/workers/${encodeURIComponent(workerId)}/status`, { method: "POST" });
    toast(`'${workerId}': ${result.status}`, "ok");
    loadKaggle();
  } catch (e) {
    toast(`Status check failed: ${e.message}`, "err");
  }
}

async function downloadKaggleWorker(workerId) {
  try {
    const result = await api(`/api/kaggle/workers/${encodeURIComponent(workerId)}/download`, { method: "POST" });
    const n = (result.registered_runs || []).length;
    toast(`Downloaded '${workerId}' — ${n} run${n === 1 ? "" : "s"} registered into the ledger`, "ok");
    loadKaggle();
  } catch (e) {
    toast(`Download failed: ${e.message}`, "err");
  }
}

// -------------------------------------------------------------------- bulk
async function pushAllKaggleWorkers() {
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

function initKaggleButtons() {
  document.getElementById("btn-kaggle-add-account").addEventListener("click", addKaggleAccount);
  document.getElementById("btn-kaggle-add-worker").addEventListener("click", addKaggleWorker);
  document.getElementById("btn-kaggle-push-all").addEventListener("click", pushAllKaggleWorkers);
  document.getElementById("btn-kaggle-refresh-all").addEventListener("click", refreshAllKaggleWorkers);
  document.getElementById("btn-kaggle-download-all").addEventListener("click", downloadAllKaggleWorkers);
}

initKaggleButtons();
