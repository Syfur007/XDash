// ============================================================================
// Experiment Console — frontend
// No build step, no framework: plain fetch + DOM + CodeMirror + Chart.js.
// ============================================================================

const LOWER_IS_BETTER = new Set(["hd95", "asd", "mean_ms", "median_ms", "std_ms", "p95_ms", "eval_duration_s"]);
const RADAR_METRICS = ["dice", "miou", "precision", "recall", "specificity", "f2", "accuracy"];
const CHART_COLORS = ["#F5A623", "#4FD1C5", "#E5484D", "#8C97B0", "#7C9CF5", "#C77DFF"];

const state = {
  system: null,
  configs: [],
  selectedConfigPath: null,
  editor: null,
  editorDirty: false,

  terminals: [],
  selectedTerminal: null,
  terminalChart: null,
  renderedTerminalSession: null,
  configPreviewCache: {},
  configPreviewCollapsed: false,

  reportGroups: [],
  selectedReportPath: null,
  compareSelection: new Set(),
  reportRadarChart: null,
  compareRadarChart: null,

  historyTree: [],
  historyExpanded: new Set(),
  selectedHistoryFile: null,
  historySource: "logs",

  monitors: [],
  monitorExpanded: new Set(),
  monitorPrevAlive: {},

  builderConfig: {},
  creatorInitialized: false,

  pollTimer: null,
};

// ---------------------------------------------------------------- utilities
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer = null;
function toast(msg, kind = "") {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast show " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

function fmtNum(n) {
  if (n === undefined || n === null || typeof n !== "number") return n ?? "–";
  return Math.abs(n) < 10 ? n.toFixed(4) : n.toFixed(2);
}

function timeAgo(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function showConfirm(title, body) {
  return new Promise((resolve) => {
    document.getElementById("confirm-title").textContent = title;
    document.getElementById("confirm-body").textContent = body;
    const backdrop = document.getElementById("confirm-backdrop");
    const okBtn = document.getElementById("confirm-ok");
    const cancelBtn = document.getElementById("confirm-cancel");
    backdrop.classList.remove("hidden");
    function cleanup(result) {
      backdrop.classList.add("hidden");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      resolve(result);
    }
    function onOk() { cleanup(true); }
    function onCancel() { cleanup(false); }
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
  });
}

// ---------------------------------------------------------------- nav
function initNav() {
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.addEventListener("click", () => {
      switchView(item.dataset.view);
      closeSidebar();
    });
  });
  document.getElementById("btn-hamburger").addEventListener("click", toggleSidebar);
  document.getElementById("sidebar-backdrop").addEventListener("click", closeSidebar);
}

function toggleSidebar() {
  document.querySelector(".sidebar").classList.toggle("open");
  document.getElementById("sidebar-backdrop").classList.toggle("open");
}

function closeSidebar() {
  document.querySelector(".sidebar").classList.remove("open");
  document.getElementById("sidebar-backdrop").classList.remove("open");
}

function switchView(view) {
  document.querySelectorAll(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.view === view));
  document.querySelectorAll(".view").forEach((el) => el.classList.toggle("active", el.id === `view-${view}`));
  if (view === "tensorboard") refreshTensorboardStatus();
  if (view === "reports" && !state.reportGroups.length) loadReports();
  if (view === "history" && !state.historyTree.length) loadHistory();
  if (view === "monitors") loadMonitors();
  if (view === "creator") initCreatorView();
}

// ---------------------------------------------------------------- system info
async function loadSystem() {
  state.system = await api("/api/system");
  document.getElementById("footer-repo").textContent = state.system.repo_root;
  document.getElementById("tb-logdir").textContent = state.system.runs_dir;
  document.getElementById("history-logdir").textContent = historySourceDir();
  document.getElementById("history-dir-label").textContent = historySourceDir();
  document.getElementById("reports-dir-label").textContent = state.system.reports_dir;
  document.getElementById("footer-env").textContent = state.system.env_activate_cmd || "(none configured)";
  document.getElementById("footer-tmux-warning").classList.toggle("hidden", !!state.system.tmux_available);
}

// ============================================================================
// CONFIGS
// ============================================================================
async function loadConfigs() {
  const body = document.getElementById("config-tree-body");
  try {
    const data = await api("/api/configs");
    state.configs = data.groups;
    renderConfigTree();
  } catch (e) {
    body.innerHTML = `<div class="empty-state">Couldn't load configs: ${e.message}</div>`;
  }
}

function renderConfigTree() {
  const body = document.getElementById("config-tree-body");
  const countEl = document.getElementById("config-count");
  let total = 0;
  if (!state.configs.length) {
    body.innerHTML = `<div class="empty-state">No .yaml configs found under configs/</div>`;
    countEl.textContent = "";
    return;
  }
  let html = "";
  for (const group of state.configs) {
    total += group.configs.length;
    html += `<div class="category"><div class="category-label">${escapeHtml(group.category)}</div>`;
    for (const c of group.configs) {
      const active = c.path === state.selectedConfigPath ? "active" : "";
      html += `<div class="config-item ${active}" data-path="${escapeHtml(c.path)}" title="${escapeHtml(c.path)}">
        <span class="dot"></span><span>${escapeHtml(c.name)}</span>
      </div>`;
    }
    html += `</div>`;
  }
  body.innerHTML = html;
  countEl.textContent = `${total} file${total === 1 ? "" : "s"}`;
  body.querySelectorAll(".config-item").forEach((el) => el.addEventListener("click", () => selectConfig(el.dataset.path)));
}

async function selectConfig(path) {
  if (state.editorDirty && !confirm("Discard unsaved changes to the current config?")) return;
  state.selectedConfigPath = path;
  renderConfigTree();
  document.getElementById("editor-path").textContent = path;
  document.getElementById("run-bar").style.display = "flex";
  document.getElementById("btn-save-config").disabled = false;

  const editorBody = document.getElementById("editor-body");
  editorBody.innerHTML = `<textarea id="config-textarea"></textarea>`;

  try {
    const data = await api(`/api/config?path=${encodeURIComponent(path)}`);
    state.editor = CodeMirror.fromTextArea(document.getElementById("config-textarea"), {
      mode: "yaml", theme: "dracula", lineNumbers: true, tabSize: 2, indentUnit: 2, viewportMargin: Infinity,
    });
    state.editor.setValue(data.raw);
    state.editorDirty = false;
    setEditorStatus(true, "");
    state.editor.on("change", () => { state.editorDirty = true; validateEditorYaml(); });
  } catch (e) {
    editorBody.innerHTML = `<div class="empty-state">Failed to load config: ${e.message}</div>`;
  }
}

function validateEditorYaml() {
  if (!state.editor) return;
  try { jsyaml.load(state.editor.getValue()); setEditorStatus(true, "unsaved changes"); }
  catch (e) { setEditorStatus(false, "YAML error — " + e.message.split("\n")[0]); }
}

function setEditorStatus(ok, msg) {
  const el = document.getElementById("editor-status");
  el.textContent = msg;
  el.className = "editor-status " + (msg ? (ok ? "ok" : "err") : "");
}

async function saveConfig() {
  if (!state.editor || !state.selectedConfigPath) return;
  const raw = state.editor.getValue();
  try { jsyaml.load(raw); } catch (e) { toast("Fix the YAML error before saving", "err"); return; }
  try {
    await api("/api/config", { method: "POST", body: JSON.stringify({ path: state.selectedConfigPath, raw }) });
    state.editorDirty = false;
    setEditorStatus(true, "saved");
    toast("Config saved", "ok");
  } catch (e) {
    toast("Save failed: " + e.message, "err");
  }
}

async function runConfig() {
  if (!state.selectedConfigPath) return;
  if (state.editorDirty && !confirm("You have unsaved edits. Launch the last saved version anyway?")) return;
  const mode = document.getElementById("run-mode").value;
  const extra_args = document.getElementById("run-extra-args").value.trim();
  try {
    const term = await api("/api/terminals", {
      method: "POST",
      body: JSON.stringify({ config_path: state.selectedConfigPath, mode, extra_args }),
    });
    toast("Launched in a new terminal", "ok");
    state.selectedTerminal = term.session_name;
    switchView("terminals");
    await loadTerminals();
  } catch (e) {
    toast("Couldn't launch: " + e.message, "err");
  }
}

// ============================================================================
// TERMINALS
// ============================================================================
const STATUS_LABEL = {
  running: "Running", completed: "Completed", stopped: "Stopped",
  failed: "Failed", interrupted: "Interrupted", unmanaged: "External session",
};

async function loadTerminals() {
  let data;
  try { data = await api("/api/terminals"); } catch (e) { return; }
  state.terminals = data.terminals;
  renderTerminalList();
  updateTelemetry();
  if (state.selectedTerminal && state.terminals.some((t) => t.session_name === state.selectedTerminal)) {
    loadTerminalDetail(state.selectedTerminal);
  }
}

function renderTerminalList() {
  const body = document.getElementById("terminal-list-body");
  const countEl = document.getElementById("terminal-count");
  if (!state.terminals.length) {
    body.innerHTML = `<div class="empty-state">No terminals yet — launch one from the Configs tab.</div>`;
    countEl.textContent = "";
    return;
  }
  countEl.textContent = `${state.terminals.length}`;
  body.innerHTML = state.terminals.map((t) => {
    const active = t.session_name === state.selectedTerminal ? "active" : "";
    const title = escapeHtml(t.experiment_name || t.session_name);
    const modeTag = t.mode ? `<span class="mode-tag mode-${t.mode}">${t.mode}</span>` : "";
    const sub = t.managed
      ? `${escapeHtml(t.config_path || "")}${t.restart_count ? ` · restarted ${t.restart_count}×` : ""}`
      : "unmanaged tmux session";

    let metricChip = "";
    const m = t.latest_metrics;
    if (m) {
      const dice = m.metrics["Val Dice"] ?? m.metrics["Dice"];
      const label = dice !== undefined ? `dice ${fmtNum(dice)}` : Object.keys(m.metrics)[0];
      metricChip = `<span class="term-card-metric">epoch ${m.epoch}${label ? " · " + escapeHtml(label) : ""}</span>`;
    }

    return `<div class="term-card ${active}" data-session="${escapeHtml(t.session_name)}">
      <div class="term-card-accent ${t.status}"></div>
      <div class="term-card-body">
        <div class="term-card-title" title="${title}">${title}${modeTag}</div>
        <div class="term-card-sub" title="${escapeHtml(sub)}">${sub}</div>
        <div class="term-card-footer">
          <span class="term-card-status ${t.status}">${STATUS_LABEL[t.status] || t.status} · ${timeAgo(t.created_at)}</span>
          ${metricChip}
        </div>
      </div>
    </div>`;
  }).join("");
  body.querySelectorAll(".term-card").forEach((el) => {
    el.addEventListener("click", () => {
      state.selectedTerminal = el.dataset.session;
      renderTerminalList();
      loadTerminalDetail(state.selectedTerminal);
    });
  });
}

const LOG_LINE_RE = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*([A-Z]+)\s*\|\s*[^-]+-\s*(.*)$/;
const LEVEL_CLASS = { INFO: "lvl-INFO", WARNING: "lvl-WARNING", ERROR: "lvl-ERROR", DEBUG: "lvl-DEBUG" };

async function loadTerminalDetail(sessionName) {
  let term;
  try { term = await api(`/api/terminals/${encodeURIComponent(sessionName)}`); }
  catch (e) { return; }
  renderTerminalDetail(term);
}

function renderTerminalDetail(term) {
  document.getElementById("term-title").textContent = term.experiment_name || term.session_name;
  const bits = [];
  if (term.config_path) bits.push(term.config_path);
  if (term.mode) bits.push(term.mode);
  bits.push(`session ${term.session_name}`);
  if (term.created_at) bits.push(`started ${timeAgo(term.created_at)}`);
  bits.push(STATUS_LABEL[term.status] || term.status);
  document.getElementById("term-subtitle").textContent = bits.join(" · ");

  const actions = document.getElementById("terminal-actions");
  let actionHtml = "";
  if (term.status === "running") actionHtml += `<button class="btn btn-sm" id="btn-stop-term">Stop</button>`;
  if (term.restart_available) actionHtml += `<button class="btn btn-sm btn-primary" id="btn-restart-term">Restart</button>`;
  actionHtml += `<button class="btn btn-sm btn-danger" id="btn-kill-term">${term.alive ? "Kill session" : "Dismiss"}</button>`;
  actions.innerHTML = actionHtml;

  const stopBtn = document.getElementById("btn-stop-term");
  if (stopBtn) stopBtn.addEventListener("click", () => stopTerminal(term.session_name));
  const restartBtn = document.getElementById("btn-restart-term");
  if (restartBtn) restartBtn.addEventListener("click", () => restartTerminal(term.session_name));
  document.getElementById("btn-kill-term").addEventListener("click", () => killTerminal(term.session_name, term.alive));

  const body = document.getElementById("terminal-body");
  const hasChart = term.metrics_series && term.metrics_series.length > 0;
  const isNewSelection = state.renderedTerminalSession !== term.session_name;

  if (isNewSelection) {
    // Full rebuild only happens when switching to a different terminal —
    // rebuilding this on every poll was resetting the log's scroll position.
    state.renderedTerminalSession = term.session_name;
    body.innerHTML = `<div class="terminal-detail-grid">
        <div class="terminal-meta-line">Command: <b>${escapeHtml(term.command || "(unmanaged session)")}</b></div>
        <div class="terminal-top-row">
          ${term.config_path ? `<div class="config-preview ${state.configPreviewCollapsed ? "collapsed" : ""}" id="config-preview">
            <div class="config-preview-header" id="config-preview-header">
              <span>Config (read-only) — ${escapeHtml(term.config_path)}</span>
              <span class="chevron">▾</span>
            </div>
            <div class="config-preview-body" id="config-preview-body">Loading…</div>
          </div>` : ""}
          <div class="chart-wrap hidden" id="terminal-chart-wrap"><canvas id="terminal-chart"></canvas></div>
        </div>
        <div class="terminal-console-wrap"><div class="log-console" id="terminal-log"></div></div>
      </div>`;
    const header = document.getElementById("config-preview-header");
    if (header) header.addEventListener("click", () => {
      state.configPreviewCollapsed = !state.configPreviewCollapsed;
      document.getElementById("config-preview").classList.toggle("collapsed", state.configPreviewCollapsed);
    });
    if (term.config_path) loadConfigPreview(term.config_path);
  }

  document.getElementById("terminal-chart-wrap").classList.toggle("hidden", !hasChart);

  const logEl = document.getElementById("terminal-log");
  const text = term.log_text || "";
  const wasAtBottom = isNewSelection || (logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 20);
  if (!text.trim()) {
    logEl.innerHTML = `<span class="empty-log">No output yet.</span>`;
  } else {
    logEl.innerHTML = text.split("\n").map((raw) => {
      const m = raw.match(LOG_LINE_RE);
      if (m) {
        const cls = LEVEL_CLASS[m[2]] || "lvl-INFO";
        return `<div class="log-line"><span class="lvl ${cls}">${m[2]}</span><span class="msg">${escapeHtml(m[3])}</span></div>`;
      }
      return `<div class="log-line"><span class="lvl"></span><span class="msg">${escapeHtml(raw)}</span></div>`;
    }).join("");
    if (wasAtBottom) logEl.scrollTop = logEl.scrollHeight;
  }

  if (hasChart) renderTerminalChart(term.metrics_series);
}

async function loadConfigPreview(configPath) {
  const el = document.getElementById("config-preview-body");
  if (!el) return;
  if (state.configPreviewCache[configPath]) {
    el.textContent = state.configPreviewCache[configPath];
    return;
  }
  try {
    const data = await api(`/api/config?path=${encodeURIComponent(configPath)}`);
    state.configPreviewCache[configPath] = data.raw;
    if (document.getElementById("config-preview-body")) el.textContent = data.raw;
  } catch (e) {
    el.textContent = `Couldn't load config: ${e.message}`;
  }
}

function renderTerminalChart(series) {
  const canvas = document.getElementById("terminal-chart");
  if (!canvas) return;
  const keys = Object.keys(series[series.length - 1].metrics).filter((k) => !/lr/i.test(k)).slice(0, 4);
  const labels = series.map((p) => p.epoch);
  const datasets = keys.map((key, i) => ({
    label: key,
    data: series.map((p) => p.metrics[key] ?? null),
    borderColor: CHART_COLORS[i % CHART_COLORS.length],
    backgroundColor: "transparent",
    borderWidth: 1.75, pointRadius: 0, tension: 0.25, spanGaps: true,
  }));
  if (state.terminalChart) { state.terminalChart.destroy(); }
  state.terminalChart = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: { labels, datasets },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { ticks: { color: "#5C6785", font: { family: "JetBrains Mono", size: 10 } }, grid: { color: "#1B2740" } },
        y: { ticks: { color: "#5C6785", font: { family: "JetBrains Mono", size: 10 } }, grid: { color: "#1B2740" } },
      },
      plugins: { legend: { labels: { color: "#8C97B0", font: { family: "JetBrains Mono", size: 10.5 }, boxWidth: 10 } } },
    },
  });
}

async function stopTerminal(sessionName) {
  try { await api(`/api/terminals/${encodeURIComponent(sessionName)}/stop`, { method: "POST" }); toast("Stop signal sent", "ok"); loadTerminals(); }
  catch (e) { toast("Couldn't stop: " + e.message, "err"); }
}

async function restartTerminal(sessionName) {
  try {
    const term = await api(`/api/terminals/${encodeURIComponent(sessionName)}/restart`, { method: "POST" });
    toast("Restarted in a new terminal", "ok");
    state.selectedTerminal = term.session_name;
    loadTerminals();
  } catch (e) {
    toast("Couldn't restart: " + e.message, "err");
  }
}

async function killTerminal(sessionName, alive) {
  const confirmed = await showConfirm(
    alive ? "Kill this session?" : "Dismiss this terminal?",
    alive
      ? "This ends the tmux session immediately. Any unsaved progress in the running command will be lost. This cannot be undone."
      : "This removes it from the list. Since the session is already gone, this is just housekeeping."
  );
  if (!confirmed) return;
  try {
    await api(`/api/terminals/${encodeURIComponent(sessionName)}`, { method: "DELETE" });
    if (state.selectedTerminal === sessionName) {
      state.selectedTerminal = null;
      state.renderedTerminalSession = null;
      document.getElementById("term-title").textContent = "No terminal selected";
      document.getElementById("term-subtitle").textContent = "";
      document.getElementById("terminal-actions").innerHTML = "";
      document.getElementById("terminal-body").innerHTML = `<div class="empty-state">Select a session on the left, or launch a new one from the Configs tab.</div>`;
    }
    toast(alive ? "Session killed" : "Dismissed", "ok");
    loadTerminals();
  } catch (e) {
    toast("Couldn't remove terminal: " + e.message, "err");
  }
}

// ---------------------------------------------------------------- telemetry / topbar
function updateTelemetry() {
  const counts = { running: 0, interrupted: 0, completed: 0, failed: 0 };
  let runningTerm = null;
  for (const t of state.terminals) {
    if (t.status === "running") { counts.running++; if (!runningTerm) runningTerm = t; }
    else if (t.status === "interrupted") counts.interrupted++;
    else if (t.status === "completed") counts.completed++;
    else if (t.status === "failed" || t.status === "stopped") counts.failed++;
  }
  document.getElementById("count-running").textContent = counts.running;
  document.getElementById("count-restart").textContent = counts.interrupted;
  document.getElementById("count-completed").textContent = counts.completed;
  document.getElementById("count-failed").textContent = counts.failed;

  const dot = document.getElementById("telemetry-dot");
  const stateLabel = document.getElementById("telemetry-state");
  const jobName = document.getElementById("telemetry-job");
  const metricChip = document.getElementById("telemetry-metric");

  if (runningTerm) {
    dot.classList.add("live");
    stateLabel.textContent = "RUNNING";
    jobName.textContent = runningTerm.experiment_name || runningTerm.session_name;
    const m = runningTerm.latest_metrics;
    if (m) {
      const dice = m.metrics["Val Dice"] ?? m.metrics["Dice"];
      metricChip.textContent = `epoch ${m.epoch}${dice !== undefined ? " · dice " + fmtNum(dice) : ""}`;
      metricChip.classList.remove("hidden");
    } else {
      metricChip.classList.add("hidden");
    }
  } else {
    dot.classList.remove("live");
    stateLabel.textContent = "IDLE";
    jobName.textContent = "";
    metricChip.classList.add("hidden");
  }
}

// ============================================================================
// REPORTS
// ============================================================================
async function loadReports() {
  const body = document.getElementById("report-list-body");
  try {
    const data = await api("/api/reports");
    state.reportGroups = data.groups;
    renderReportList();
  } catch (e) {
    body.innerHTML = `<div class="empty-state">Couldn't load reports: ${e.message}</div>`;
  }
}

function renderReportList() {
  const body = document.getElementById("report-list-body");
  const countEl = document.getElementById("report-count");
  let total = 0;
  if (!state.reportGroups.length) {
    body.innerHTML = `<div class="empty-state">No evaluation reports found under logs/</div>`;
    countEl.textContent = "";
    updateCompareButton();
    return;
  }
  let html = "";
  for (const group of state.reportGroups) {
    total += group.reports.length;
    html += `<div class="category"><div class="category-label">${escapeHtml(group.category)}</div>`;
    for (const r of group.reports) {
      const active = r.path === state.selectedReportPath ? "active" : "";
      const checked = state.compareSelection.has(r.path) ? "checked" : "";
      html += `<div class="list-row ${active}" data-path="${escapeHtml(r.path)}" title="${escapeHtml(r.path)}">
        <input type="checkbox" class="compare-check" data-path="${escapeHtml(r.path)}" ${checked} />
        <span class="dot"></span>
        <div class="list-row-main">
          <div class="list-row-title">${escapeHtml(r.experiment || r.name)}${r.is_ensemble ? '<span class="mode-tag">ensemble</span>' : ""}</div>
          <div class="list-row-sub">${escapeHtml(r.model_name || "")} · dice ${fmtNum(r.dice)} · miou ${fmtNum(r.miou)}</div>
          <div class="list-row-sub">${r.timestamp ? new Date(r.timestamp).toLocaleString() : ""}</div>
        </div>
      </div>`;
    }
    html += `</div>`;
  }
  body.innerHTML = html;
  countEl.textContent = `${total}`;

  body.querySelectorAll(".list-row").forEach((el) => {
    el.addEventListener("click", (e) => {
      if (e.target.classList.contains("compare-check")) return;
      state.selectedReportPath = el.dataset.path;
      renderReportList();
      loadReportDetail(el.dataset.path);
    });
  });
  body.querySelectorAll(".compare-check").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      if (el.checked) state.compareSelection.add(el.dataset.path);
      else state.compareSelection.delete(el.dataset.path);
      updateCompareButton();
    });
  });
  updateCompareButton();
}

function updateCompareButton() {
  const btn = document.getElementById("btn-compare-reports");
  document.getElementById("compare-count").textContent = state.compareSelection.size;
  btn.classList.toggle("hidden", state.compareSelection.size < 2);
}

async function loadReportDetail(path) {
  const bodyEl = document.getElementById("report-body");
  bodyEl.innerHTML = `<div class="empty-state">Loading…</div>`;
  try {
    const data = await api(`/api/reports/${path.split("/").map(encodeURIComponent).join("/")}`);
    renderReportDetail(data);
  } catch (e) {
    bodyEl.innerHTML = `<div class="empty-state">Couldn't load report: ${e.message}</div>`;
  }
}

function flattenObj(d, prefix = "") {
  const flat = {};
  for (const k in (d || {})) {
    const key = prefix ? `${prefix}.${k}` : k;
    const v = d[k];
    if (v && typeof v === "object" && !Array.isArray(v)) Object.assign(flat, flattenObj(v, key));
    else flat[key] = v;
  }
  return flat;
}

function kvTable(obj) {
  return `<table class="kv-table">${Object.entries(obj).map(([k, v]) =>
    `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(Array.isArray(v) ? v.join(", ") : (v ?? "–"))}</td></tr>`
  ).join("")}</table>`;
}

function renderReportDetail(data) {
  const bodyEl = document.getElementById("report-body");
  const metrics = data.metrics || {};
  const model = data.model || {};
  const efficiency = data.efficiency || {};
  const environment = data.environment || {};

  const badges = [];
  if (data.is_ensemble) badges.push(`<span class="badge amber">ensemble</span>`);
  if (data.is_multiclass) badges.push(`<span class="badge">multiclass</span>`);
  if (model.name) badges.push(`<span class="badge teal">${escapeHtml(model.name)}</span>`);

  const hasRadar = RADAR_METRICS.some((k) => typeof metrics[k] === "number");

  bodyEl.innerHTML = `
    <div class="report-header">
      <h2>${escapeHtml(data.experiment || "Report")}</h2>
      <div class="sub">${data.timestamp ? new Date(data.timestamp).toLocaleString() : ""} ${data.num_samples ? `· ${data.num_samples} samples` : ""} ${data.eval_duration_s ? `· ${fmtNum(data.eval_duration_s)}s eval` : ""}</div>
      <div class="report-badges">${badges.join("")}</div>
    </div>

    <div class="metric-grid">
      ${Object.entries(metrics).map(([k, v]) => `<div class="metric-card"><div class="label">${escapeHtml(k)}</div><div class="value">${fmtNum(v)}</div></div>`).join("")}
    </div>

    ${hasRadar ? `<div class="report-section"><h3>Metrics overview</h3><div class="chart-wrap" style="height:280px;"><canvas id="report-radar"></canvas></div></div>` : ""}

    ${Object.keys(model).length ? `<div class="report-section"><h3>Model</h3>${kvTable(model)}</div>` : ""}
    ${Object.keys(efficiency).length ? `<div class="report-section"><h3>Efficiency</h3>${kvTable(flattenObj(efficiency))}</div>` : ""}
    ${Object.keys(environment).length ? `<div class="report-section"><h3>Environment</h3>${kvTable(environment)}</div>` : ""}
    ${data.checkpoint ? `<div class="report-section"><h3>Checkpoint</h3>${kvTable({ checkpoint: data.checkpoint })}</div>` : ""}
    ${data.config ? `<div class="report-section"><h3>Config</h3>${kvTable(flattenObj(data.config))}</div>` : ""}
  `;

  if (hasRadar) {
    if (state.reportRadarChart) state.reportRadarChart.destroy();
    const canvas = document.getElementById("report-radar");
    state.reportRadarChart = new Chart(canvas.getContext("2d"), {
      type: "radar",
      data: {
        labels: RADAR_METRICS.filter((k) => typeof metrics[k] === "number"),
        datasets: [{
          label: data.experiment || "report",
          data: RADAR_METRICS.filter((k) => typeof metrics[k] === "number").map((k) => metrics[k]),
          borderColor: CHART_COLORS[0], backgroundColor: "rgba(245,166,35,0.15)", pointBackgroundColor: CHART_COLORS[0],
        }],
      },
      options: radarOptions(),
    });
  }
}

function radarOptions() {
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    scales: {
      r: {
        angleLines: { color: "#1B2740" }, grid: { color: "#1B2740" },
        pointLabels: { color: "#8C97B0", font: { family: "JetBrains Mono", size: 10.5 } },
        ticks: { color: "#5C6785", backdropColor: "transparent", font: { size: 9 } },
        suggestedMin: 0, suggestedMax: 1,
      },
    },
    plugins: { legend: { labels: { color: "#8C97B0", font: { family: "JetBrains Mono", size: 10.5 }, usePointStyle: true, pointStyle: "circle" } } },
  };
}

async function compareReports() {
  const paths = Array.from(state.compareSelection);
  if (paths.length < 2) return;
  const bodyEl = document.getElementById("report-body");
  bodyEl.innerHTML = `<div class="empty-state">Comparing…</div>`;
  try {
    const data = await api("/api/reports/compare", { method: "POST", body: JSON.stringify({ paths }) });
    renderCompare(data);
  } catch (e) {
    bodyEl.innerHTML = `<div class="empty-state">Couldn't compare: ${e.message}</div>`;
  }
}

function renderCompare(data) {
  const bodyEl = document.getElementById("report-body");
  const reports = data.reports;
  const labels = reports.map((r) => r.experiment || r.path);
  // Same color-per-report assignment used for both table header swatches and
  // the radar chart datasets below, so a color always means the same report
  // everywhere on this page.
  const reportColors = reports.map((r, i) => CHART_COLORS[i % CHART_COLORS.length]);
  const headerCells = labels.map((l, i) =>
    `<th><span class="swatch" style="background:${reportColors[i]}"></span>${escapeHtml(l)}</th>`
  ).join("");

  const metricKeys = [];
  const seen = new Set();
  for (const r of reports) for (const k in r.metrics) if (!seen.has(k)) { seen.add(k); metricKeys.push(k); }

  const metricRows = metricKeys.map((k) => {
    const values = reports.map((r) => r.metrics[k]);
    const numeric = values.filter((v) => typeof v === "number");
    const lowerBetter = LOWER_IS_BETTER.has(k);
    const best = numeric.length ? (lowerBetter ? Math.min(...numeric) : Math.max(...numeric)) : null;
    return { key: k, values, best };
  });

  const flatConfigs = reports.map((r) => r.config_flat || {});
  const allKeys = [];
  const seenK = new Set();
  for (const fc of flatConfigs) for (const k in fc) if (!seenK.has(k)) { seenK.add(k); allKeys.push(k); }
  const diffRows = allKeys.map((k) => {
    const values = flatConfigs.map((fc) => fc[k]);
    const distinct = new Set(values.map((v) => JSON.stringify(v)));
    return { key: k, values, differs: distinct.size > 1 };
  }).filter((row) => row.differs);

  const hasRadar = reports.every((r) => RADAR_METRICS.some((k) => typeof r.metrics[k] === "number"));

  bodyEl.innerHTML = `
    <div class="report-header">
      <h2>Comparing ${reports.length} reports</h2>
      <div class="sub">${labels.map(escapeHtml).join(" · ")}</div>
    </div>

    <div class="report-section">
      <h3>Metrics <span style="text-transform:none; font-weight:400;">— the best value in each row is highlighted</span></h3>
      <table class="compare-table">
        <thead><tr><th>Metric</th>${headerCells}</tr></thead>
        <tbody>
          ${metricRows.map((row) => `<tr><td>${escapeHtml(row.key)}</td>${row.values.map((v) => {
            const isWinner = typeof v === "number" && v === row.best;
            return `<td class="${isWinner ? "winner" : ""}">${fmtNum(v)}</td>`;
          }).join("")}</tr>`).join("")}
        </tbody>
      </table>
    </div>

    ${hasRadar ? `<div class="report-section"><h3>Metrics overview</h3><div class="chart-wrap" style="height:300px;"><canvas id="compare-radar"></canvas></div></div>` : ""}

    <div class="report-section">
      <h3>Config differences (${diffRows.length} of ${allKeys.length} keys differ)</h3>
      ${diffRows.length ? `<table class="compare-table">
        <thead><tr><th>Key</th>${headerCells}</tr></thead>
        <tbody>${diffRows.map((row) => `<tr><td>${escapeHtml(row.key)}</td>${row.values.map((v) =>
          `<td class="differs">${escapeHtml(Array.isArray(v) ? v.join(", ") : (v ?? "–"))}</td>`
        ).join("")}</tr>`).join("")}</tbody>
      </table>` : `<div class="empty-state" style="height:auto;padding:20px 0;">These configs are identical.</div>`}
    </div>
  `;

  if (hasRadar) {
    if (state.compareRadarChart) state.compareRadarChart.destroy();
    const canvas = document.getElementById("compare-radar");
    state.compareRadarChart = new Chart(canvas.getContext("2d"), {
      type: "radar",
      data: {
        labels: RADAR_METRICS,
        datasets: reports.map((r, i) => ({
          label: r.experiment || r.path,
          data: RADAR_METRICS.map((k) => r.metrics[k] ?? null),
          borderColor: reportColors[i],
          backgroundColor: "transparent",
          pointBackgroundColor: reportColors[i],
        })),
      },
      options: radarOptions(),
    });
  }
}

// ============================================================================
// HISTORY
// ============================================================================
const ICON_FOLDER = `<svg class="tree-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>`;
const ICON_FILE = `<svg class="tree-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 3h9l5 5v13H6z"/><path d="M14 3v5h5"/></svg>`;
const ICON_IMAGE = `<svg class="tree-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>`;

function fmtBytes(n) {
  if (n === undefined || n === null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function historySourceDir() {
  if (!state.system) return "";
  return state.historySource === "images" ? state.system.plots_dir : state.system.logs_dir;
}

function switchHistorySource(source) {
  state.historySource = source;
  state.historyTree = [];
  state.historyExpanded = new Set();
  state.selectedHistoryFile = null;
  document.getElementById("history-dir-label").textContent = historySourceDir();
  document.getElementById("history-logdir").textContent = historySourceDir();
  document.getElementById("history-file-path").textContent = "No file selected";
  document.getElementById("history-file-meta").textContent = "";
  document.getElementById("history-file-body").innerHTML = `<div class="empty-state">Select a file on the left to view it.</div>`;
  loadHistory();
}

async function loadHistory() {
  const body = document.getElementById("history-tree-body");
  document.getElementById("history-logdir").textContent = historySourceDir();
  try {
    const data = await api(`/api/history/tree?source=${encodeURIComponent(state.historySource)}`);
    state.historyTree = data.tree;
    renderHistoryTree();
  } catch (e) {
    body.innerHTML = `<div class="empty-state">Couldn't load history: ${e.message}</div>`;
  }
}

function renderHistoryTree() {
  const body = document.getElementById("history-tree-body");
  if (!state.historyTree.length) {
    body.innerHTML = `<div class="empty-state">Nothing under ${escapeHtml(historySourceDir())} yet.</div>`;
    return;
  }
  body.innerHTML = renderTreeNodes(state.historyTree);
  wireTreeEvents(body);
}

function renderTreeNodes(nodes) {
  return nodes.map((n) => {
    if (n.type === "dir") {
      const open = state.historyExpanded.has(n.path);
      return `<div class="tree-node">
        <div class="tree-row dir" data-path="${escapeHtml(n.path)}" data-type="dir" title="${escapeHtml(n.path)}">
          <span class="chevron ${open ? "open" : ""}">▸</span>${ICON_FOLDER}<span class="tree-name">${escapeHtml(n.name)}</span>
        </div>
        ${open ? `<div class="tree-children">${renderTreeNodes(n.children)}</div>` : ""}
      </div>`;
    }
    const active = n.path === state.selectedHistoryFile ? "active" : "";
    const icon = n.is_image ? ICON_IMAGE : ICON_FILE;
    return `<div class="tree-node">
      <div class="tree-row file ${active}" data-path="${escapeHtml(n.path)}" data-type="file" title="${escapeHtml(n.path)}">
        <span class="chevron"></span>${icon}<span class="tree-name">${escapeHtml(n.name)}</span>
        <span class="tree-size">${fmtBytes(n.size)}</span>
      </div>
    </div>`;
  }).join("");
}

function wireTreeEvents(container) {
  container.querySelectorAll(".tree-row").forEach((el) => {
    el.addEventListener("click", () => {
      const path = el.dataset.path;
      if (el.dataset.type === "dir") {
        if (state.historyExpanded.has(path)) state.historyExpanded.delete(path);
        else state.historyExpanded.add(path);
        renderHistoryTree();
      } else {
        state.selectedHistoryFile = path;
        renderHistoryTree();
        loadHistoryFile(path);
      }
    });
  });
}

async function loadHistoryFile(path) {
  document.getElementById("history-file-path").textContent = path;
  document.getElementById("history-file-meta").textContent = "";
  const bodyEl = document.getElementById("history-file-body");
  bodyEl.innerHTML = `<div class="empty-state">Loading…</div>`;
  const encodedPath = path.split("/").map(encodeURIComponent).join("/");
  const source = encodeURIComponent(state.historySource);
  try {
    const data = await api(`/api/history/file/${source}/${encodedPath}`);
    document.getElementById("history-file-meta").textContent = `${fmtBytes(data.size)}${data.truncated ? " (truncated preview)" : ""}`;

    if (data.is_image) {
      const rawUrl = `/api/history/raw/${source}/${encodedPath}`;
      bodyEl.innerHTML = `<div class="history-image-view">
        <img src="${rawUrl}" alt="${escapeHtml(path)}" />
        <a class="btn btn-sm btn-ghost" href="${rawUrl}" target="_blank" rel="noopener">Open full size ↗</a>
      </div>`;
      return;
    }
    if (data.binary) {
      bodyEl.innerHTML = `<div class="empty-state">This is a binary file and can't be previewed here.</div>`;
      return;
    }
    let content = data.content;
    let cls = "history-file-view";
    if (path.endsWith(".json")) {
      try { content = JSON.stringify(JSON.parse(content), null, 2); cls += " json-view"; } catch (e) {}
    }
    bodyEl.innerHTML = `<div class="${cls}">${escapeHtml(content)}</div>`;
  } catch (e) {
    bodyEl.innerHTML = `<div class="empty-state">Couldn't load file: ${e.message}</div>`;
  }
}

// ============================================================================
// CONFIG CREATOR
// ============================================================================
function getAtPath(obj, path) {
  return path.reduce((o, k) => (o == null ? undefined : o[k]), obj);
}
function setAtPath(obj, path, value) {
  let cur = obj;
  for (let i = 0; i < path.length - 1; i++) {
    if (typeof cur[path[i]] !== "object" || cur[path[i]] === null) cur[path[i]] = {};
    cur = cur[path[i]];
  }
  cur[path[path.length - 1]] = value;
}
function deleteAtPath(obj, path) {
  let cur = obj;
  for (let i = 0; i < path.length - 1; i++) {
    if (cur[path[i]] == null) return;
    cur = cur[path[i]];
  }
  delete cur[path[path.length - 1]];
}
function fieldType(value) {
  if (typeof value === "boolean") return "bool";
  if (typeof value === "number") return "number";
  if (Array.isArray(value)) return "list";
  if (value !== null && typeof value === "object") return "section";
  return "string";
}
function hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) { h = (h << 5) - h + s.charCodeAt(i); h |= 0; }
  return Math.abs(h);
}
function cssId(s) { return (s || "root").replace(/[^a-zA-Z0-9]/g, "_"); }

async function initCreatorView() {
  document.getElementById("creator-filename-input").placeholder = "my_experiment.yaml";
  if (!state.creatorInitialized) {
    state.creatorInitialized = true;
    try {
      const data = await api("/api/configs");
      const baseSelect = document.getElementById("creator-base-select");
      const folderSelect = document.getElementById("creator-folder-select");
      const seenCategories = new Set(["general"]);
      for (const group of data.groups) {
        const optgroup = document.createElement("optgroup");
        optgroup.label = group.category;
        for (const c of group.configs) {
          const opt = document.createElement("option");
          opt.value = c.path; opt.textContent = c.name;
          optgroup.appendChild(opt);
        }
        baseSelect.appendChild(optgroup);
        if (group.category !== "general" && !seenCategories.has(group.category)) {
          seenCategories.add(group.category);
          const fopt = document.createElement("option");
          fopt.value = group.category; fopt.textContent = group.category;
          folderSelect.appendChild(fopt);
        }
      }
      const newOpt = document.createElement("option");
      newOpt.value = "__new__"; newOpt.textContent = "+ New folder…";
      folderSelect.appendChild(newOpt);
    } catch (e) {}
  }
  renderCreatorForm();
  renderCreatorPreview();
}

async function loadBaseConfig() {
  const path = document.getElementById("creator-base-select").value;
  if (!path) { toast("Pick a config first", "err"); return; }
  try {
    const data = await api(`/api/config?path=${encodeURIComponent(path)}`);
    state.builderConfig = (data.parsed && typeof data.parsed === "object") ? data.parsed : {};
    toast("Loaded as template — edit below", "ok");
    renderCreatorForm();
    renderCreatorPreview();
  } catch (e) {
    toast("Couldn't load: " + e.message, "err");
  }
}

function startBlankConfig() {
  state.builderConfig = {};
  document.getElementById("creator-base-select").value = "";
  renderCreatorForm();
  renderCreatorPreview();
}

function renderCreatorPreview() {
  const el = document.getElementById("creator-preview-body");
  try {
    el.textContent = Object.keys(state.builderConfig).length
      ? jsyaml.dump(state.builderConfig, { indent: 2, lineWidth: -1 })
      : "# Empty config — add a section below to get started.";
  } catch (e) {
    el.textContent = "(couldn't render preview: " + e.message + ")";
  }
}

function renderCreatorForm() {
  const container = document.getElementById("creator-sections");
  container.innerHTML = renderBuilderScope(state.builderConfig, []);
  wireCreatorEvents(container);
}

const FIELD_COLORS = ["amber", "teal", "violet", "blue", "red", "emerald"];

function renderBuilderScope(obj, path) {
  const keys = Object.keys(obj || {});
  const pathStr = path.join(".");
  const depth = path.length;
  const rows = keys.map((k) => {
    const value = obj[k];
    const childPath = [...path, k];
    const childPathStr = childPath.join(".");
    const type = fieldType(value);
    if (type === "section") {
      return `<div class="builder-section ${depth > 0 ? "nested" : ""}">
        <div class="builder-section-header">
          <span class="dot" style="background:var(--${FIELD_COLORS[hashStr(childPathStr) % FIELD_COLORS.length]})"></span>
          <span title="${escapeHtml(childPathStr)}">${escapeHtml(k)}</span>
          <button class="btn-icon-remove" data-remove-path="${escapeHtml(childPathStr)}" title="Remove section">✕</button>
        </div>
        <div class="builder-section-body">
          ${renderBuilderScope(value, childPath)}
        </div>
      </div>`;
    }
    return renderBuilderField(k, value, childPath, type);
  }).join("");

  return `${rows}
    <div class="add-field-row" data-scope="${escapeHtml(pathStr)}">
      <button class="btn btn-sm btn-ghost" data-add-field-toggle="${escapeHtml(pathStr)}">+ Add field</button>
      <div class="add-field-form hidden" id="add-field-form-${cssId(pathStr)}">
        <input class="text-input" placeholder="key name" data-new-key-input />
        <select data-new-type-input>
          <option value="string">Text</option>
          <option value="number">Number</option>
          <option value="bool">Toggle (true/false)</option>
          <option value="list">List</option>
          <option value="section">Section (group)</option>
        </select>
        <button class="btn btn-sm btn-primary" data-confirm-add="${escapeHtml(pathStr)}">Add</button>
      </div>
    </div>`;
}

function renderBuilderField(key, value, path, type) {
  const pathStr = path.join(".");
  let control;
  if (type === "bool") {
    control = `<button class="toggle-switch ${value ? "on" : ""}" data-path="${escapeHtml(pathStr)}" data-type="bool"><span class="toggle-knob"></span></button>`;
  } else if (type === "number") {
    control = `<input class="text-input builder-input" type="number" step="any" value="${value}" data-path="${escapeHtml(pathStr)}" data-type="number" />`;
  } else if (type === "list") {
    control = `<input class="text-input builder-input" type="text" value="${escapeHtml((value || []).join(", "))}" data-path="${escapeHtml(pathStr)}" data-type="list" placeholder="comma, separated, values" />`;
  } else {
    control = `<input class="text-input builder-input" type="text" value="${escapeHtml(value ?? "")}" data-path="${escapeHtml(pathStr)}" data-type="string" />`;
  }
  return `<div class="builder-field">
    <span class="builder-field-key" title="${escapeHtml(pathStr)}">${escapeHtml(key)}</span>
    <div class="builder-field-control">${control}</div>
    <button class="btn-icon-remove" data-remove-path="${escapeHtml(pathStr)}" title="Remove field">✕</button>
  </div>`;
}

function wireCreatorEvents(container) {
  container.querySelectorAll(".toggle-switch").forEach((btn) => {
    btn.addEventListener("click", () => {
      const path = btn.dataset.path.split(".");
      setAtPath(state.builderConfig, path, !getAtPath(state.builderConfig, path));
      btn.classList.toggle("on");
      renderCreatorPreview();
    });
  });
  container.querySelectorAll(".builder-input").forEach((input) => {
    input.addEventListener("input", () => {
      const path = input.dataset.path.split(".");
      const type = input.dataset.type;
      let value;
      if (type === "number") value = input.value === "" ? 0 : Number(input.value);
      else if (type === "list") {
        value = input.value.split(",").map((s) => s.trim()).filter((s) => s.length);
        if (value.length && value.every((v) => !isNaN(Number(v)))) value = value.map(Number);
      } else value = input.value;
      setAtPath(state.builderConfig, path, value);
      renderCreatorPreview();
    });
  });
  container.querySelectorAll("[data-remove-path]").forEach((btn) => {
    btn.addEventListener("click", () => {
      deleteAtPath(state.builderConfig, btn.dataset.removePath.split("."));
      renderCreatorForm();
      renderCreatorPreview();
    });
  });
  container.querySelectorAll("[data-add-field-toggle]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.getElementById("add-field-form-" + cssId(btn.dataset.addFieldToggle)).classList.toggle("hidden");
    });
  });
  container.querySelectorAll("[data-confirm-add]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const scope = btn.dataset.confirmAdd;
      const wrap = btn.closest(".add-field-form");
      const name = wrap.querySelector("[data-new-key-input]").value.trim();
      const chosenType = wrap.querySelector("[data-new-type-input]").value;
      if (!name) { toast("Enter a key name", "err"); return; }
      const path = scope ? scope.split(".").concat(name) : [name];
      const defaults = { bool: false, number: 0, list: [], section: {}, string: "" };
      setAtPath(state.builderConfig, path, defaults[chosenType]);
      renderCreatorForm();
      renderCreatorPreview();
    });
  });
}

async function saveCreatorConfig() {
  let filename = document.getElementById("creator-filename-input").value.trim();
  if (!filename) { toast("Enter a filename", "err"); return; }
  if (!/\.(ya?ml)$/i.test(filename)) filename += ".yaml";

  const folderSelect = document.getElementById("creator-folder-select");
  let folder = folderSelect.value;
  if (folder === "__new__") {
    folder = document.getElementById("creator-new-folder-input").value.trim();
    if (!folder) { toast("Enter a new folder name", "err"); return; }
  }
  const path = folder ? `${folder}/${filename}` : filename;

  let raw;
  try {
    raw = jsyaml.dump(state.builderConfig, { indent: 2, lineWidth: -1 });
  } catch (e) {
    toast("Couldn't serialize config: " + e.message, "err");
    return;
  }
  try {
    await api("/api/config", { method: "POST", body: JSON.stringify({ path, raw }) });
    toast(`Saved to configs/${path}`, "ok");
    loadConfigs();
  } catch (e) {
    toast("Couldn't save: " + e.message, "err");
  }
}

// ============================================================================
// MACHINE STATS (MONITORS)
// ============================================================================
async function loadMonitors() {
  let data;
  try { data = await api("/api/monitors"); } catch (e) { return; }
  const newMonitors = data.monitors;

  // Auto pop the dropdown open the moment a service starts, and auto-close
  // it the moment it stops. Manual toggles (see wireMonitorEvents) persist
  // across polls as long as the alive/not-alive state itself hasn't changed.
  for (const m of newMonitors) {
    const prevAlive = state.monitorPrevAlive[m.id];
    if (m.alive && prevAlive !== true) state.monitorExpanded.add(m.id);
    else if (!m.alive && prevAlive === true) state.monitorExpanded.delete(m.id);
    state.monitorPrevAlive[m.id] = m.alive;
  }

  state.monitors = newMonitors;
  renderMonitorList();
  for (const id of state.monitorExpanded) {
    if (state.monitors.some((m) => m.id === id)) loadMonitorOutput(id);
  }
}

function renderMonitorList() {
  const body = document.getElementById("monitor-list-body");
  if (!state.monitors.length) {
    body.innerHTML = `<div class="empty-state">No monitors configured.</div>`;
    return;
  }
  body.innerHTML = state.monitors.map((m) => {
    const expanded = state.monitorExpanded.has(m.id);
    const statusClass = m.alive ? "running" : "stopped";
    const statusLabel = m.alive ? "Running" : "Stopped";
    const intervalTag = m.watch_interval ? `<span class="mode-tag">watch ${m.watch_interval}s</span>` : `<span class="mode-tag">self-refreshing</span>`;
    return `<div class="monitor-card ${expanded ? "expanded" : ""}" data-id="${escapeHtml(m.id)}">
      <div class="monitor-card-row">
        <div class="term-card-accent ${statusClass}"></div>
        <div class="term-card-body">
          <div class="term-card-title" title="${escapeHtml(m.name)}">${escapeHtml(m.name)}${intervalTag}</div>
          <div class="term-card-sub" title="${escapeHtml(m.command)}">${escapeHtml(m.command)}</div>
          <div class="term-card-footer">
            <span class="term-card-status ${statusClass}">${statusLabel}</span>
            <div class="job-actions">
              ${m.alive
                ? `<button class="btn btn-sm" data-action="stop" data-id="${m.id}">Stop</button>`
                : `<button class="btn btn-sm btn-primary" data-action="start" data-id="${m.id}">Start</button>`}
              ${!m.builtin ? `<button class="btn btn-sm btn-danger" data-action="remove" data-id="${m.id}">Remove</button>` : ""}
            </div>
          </div>
        </div>
        <div class="monitor-chevron">▾</div>
      </div>
      <div class="monitor-output-drawer">
        <div class="log-console no-wrap" id="monitor-output-${m.id}"></div>
      </div>
    </div>`;
  }).join("");

  body.querySelectorAll(".monitor-card-row").forEach((row) => {
    row.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      const card = row.closest(".monitor-card");
      const id = card.dataset.id;
      const nowExpanded = !state.monitorExpanded.has(id);
      if (nowExpanded) { state.monitorExpanded.add(id); loadMonitorOutput(id); }
      else state.monitorExpanded.delete(id);
      card.classList.toggle("expanded", nowExpanded);
    });
  });
  body.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const id = btn.dataset.id;
      if (btn.dataset.action === "start") startMonitor(id);
      else if (btn.dataset.action === "stop") stopMonitor(id);
      else if (btn.dataset.action === "remove") removeMonitor(id);
    });
  });
}

async function loadMonitorOutput(id) {
  const el = document.getElementById(`monitor-output-${id}`);
  if (!el) return;
  try {
    const data = await api(`/api/monitors/${encodeURIComponent(id)}/output`);
    if (!data.alive) { el.innerHTML = `<span class="empty-log">Not running — click Start to launch it.</span>`; return; }
    const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
    el.textContent = data.output || "";
    if (wasAtBottom) el.scrollTop = el.scrollHeight;
  } catch (e) {}
}

async function startMonitor(id) {
  try { await api(`/api/monitors/${encodeURIComponent(id)}/start`, { method: "POST" }); toast("Monitor started", "ok"); loadMonitors(); }
  catch (e) { toast("Couldn't start: " + e.message, "err"); }
}

async function stopMonitor(id) {
  try { await api(`/api/monitors/${encodeURIComponent(id)}/stop`, { method: "POST" }); toast("Monitor stopped", "ok"); loadMonitors(); }
  catch (e) { toast("Couldn't stop: " + e.message, "err"); }
}

async function removeMonitor(id) {
  const confirmed = await showConfirm("Remove this metric?", "This stops it (if running) and removes it from your list permanently.");
  if (!confirmed) return;
  try {
    await api(`/api/monitors/${encodeURIComponent(id)}`, { method: "DELETE" });
    state.monitorExpanded.delete(id);
    delete state.monitorPrevAlive[id];
    toast("Removed", "ok");
    loadMonitors();
  } catch (e) {
    toast("Couldn't remove: " + e.message, "err");
  }
}

async function addMonitor() {
  const name = document.getElementById("monitor-name-input").value.trim();
  const command = document.getElementById("monitor-command-input").value.trim();
  const interval = parseInt(document.getElementById("monitor-interval-input").value, 10) || 0;
  if (!name || !command) { toast("Name and command are both required", "err"); return; }
  try {
    await api("/api/monitors", { method: "POST", body: JSON.stringify({ name, command, watch_interval: interval }) });
    document.getElementById("monitor-name-input").value = "";
    document.getElementById("monitor-command-input").value = "";
    toast("Metric added", "ok");
    loadMonitors();
  } catch (e) {
    toast("Couldn't add metric: " + e.message, "err");
  }
}

// ============================================================================
// TENSORBOARD
// ============================================================================
async function refreshTensorboardStatus() {
  const status = await api("/api/tensorboard/status").catch(() => ({ running: false }));
  applyTensorboardStatus(status);
}

function applyTensorboardStatus(status) {
  const dot = document.getElementById("tb-dot");
  const label = document.getElementById("tb-status-label");
  const sub = document.getElementById("tb-status-sub");
  const startBtn = document.getElementById("btn-tb-start");
  const stopBtn = document.getElementById("btn-tb-stop");
  const openLink = document.getElementById("btn-tb-open");

  if (status.running) {
    const url = `http://${window.location.hostname}:${status.port}/`;
    dot.classList.add("live");
    label.textContent = "Running";
    sub.textContent = `Serving ${status.logdir || "runs/"} on port ${status.port}.`;
    startBtn.classList.add("hidden");
    stopBtn.classList.remove("hidden");
    openLink.classList.remove("hidden");
    openLink.href = url;
  } else {
    dot.classList.remove("live");
    label.textContent = "Not running";
    sub.textContent = "Starts a tensorboard process on the server and opens it in a new browser tab.";
    startBtn.classList.remove("hidden");
    stopBtn.classList.add("hidden");
    openLink.classList.add("hidden");
  }
}

async function startTensorboard() {
  const confirmed = await showConfirm(
    "Start TensorBoard?",
    "This starts a tensorboard process on the server (reading runs/) and opens it in a new browser tab."
  );
  if (!confirmed) return;
  toast("Starting TensorBoard…");
  try {
    const status = await api("/api/tensorboard/start", { method: "POST" });
    applyTensorboardStatus(status);
    window.open(`http://${window.location.hostname}:${status.port}/`, "_blank", "noopener");
  } catch (e) {
    toast("Couldn't start TensorBoard: " + e.message, "err");
  }
}

async function stopTensorboard() {
  const status = await api("/api/tensorboard/stop", { method: "POST" }).catch(() => null);
  if (status) applyTensorboardStatus(status);
}

// ---------------------------------------------------------------- boot
function initButtons() {
  document.getElementById("btn-refresh-configs").addEventListener("click", loadConfigs);
  document.getElementById("btn-save-config").addEventListener("click", saveConfig);
  document.getElementById("btn-run").addEventListener("click", runConfig);

  document.getElementById("btn-refresh-terminals").addEventListener("click", loadTerminals);

  document.getElementById("btn-refresh-reports").addEventListener("click", loadReports);
  document.getElementById("btn-compare-reports").addEventListener("click", compareReports);

  document.getElementById("btn-refresh-history").addEventListener("click", loadHistory);
  document.getElementById("history-source").addEventListener("change", (e) => switchHistorySource(e.target.value));

  document.getElementById("btn-refresh-monitors").addEventListener("click", loadMonitors);
  document.getElementById("btn-add-monitor").addEventListener("click", addMonitor);

  document.getElementById("btn-load-base").addEventListener("click", loadBaseConfig);
  document.getElementById("btn-blank-base").addEventListener("click", startBlankConfig);
  document.getElementById("btn-save-creator-config").addEventListener("click", saveCreatorConfig);
  document.getElementById("creator-folder-select").addEventListener("change", (e) => {
    document.getElementById("creator-new-folder-input").classList.toggle("hidden", e.target.value !== "__new__");
  });

  document.getElementById("btn-tb-start").addEventListener("click", startTensorboard);
  document.getElementById("btn-tb-stop").addEventListener("click", stopTensorboard);

  window.addEventListener("beforeunload", (e) => {
    if (state.editorDirty) { e.preventDefault(); e.returnValue = ""; }
  });
}

async function boot() {
  initNav();
  initButtons();
  await loadSystem();
  await loadConfigs();
  await loadTerminals();
  const interval = (state.system && state.system.poll_interval_ms) || 2000;
  state.pollTimer = setInterval(() => { loadTerminals(); loadMonitors(); }, interval);
}

boot();
