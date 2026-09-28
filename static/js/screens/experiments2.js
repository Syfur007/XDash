// static/js/screens/experiments2.js
//
// XDASH_PLAN.md §8.2/§8.3, Phase 3: the Experiments screen (Studies pane +
// grouped-by-config table + Compare + the real Composer) and the standalone
// Experiment page (#/x/<id>/<tab>). Both are addressed by js/lib/router.js,
// which is why this file — not switchView() — owns activating/tearing down
// #view-experiment-detail (see showExperimentDetailView() below).
//
// Every subtab strip in the app but this screen's own ("experiments-subtabs")
// is wired generically by app.js's initSubtabStrip() (click -> just flip CSS
// classes, load lazily). This one is wired by hand instead, because its
// active subtab is itself part of the address (#/experiments?tab=compare) —
// a plain click must go through navigateToView() so the URL stays the
// single source of truth, exactly as XDASH_PLAN.md §8's addressability rule
// asks for. Same reasoning for the Experiment page's own 7-tab strip
// (xdetail-subtabs) — see navigateToExperiment() in js/lib/router.js.
//
// Same classic-<script>-sharing-global-scope model as every other view file.

// ---------------------------------------------------------------- state
state.studies = [];
state.studiesLoaded = false;
state.selectedStudyId = null;     // null = "All experiments"; "unfiled" = the virtual Unfiled bucket
state.studyFilter = "";
state.studyEditId = null;         // study_id being edited in the modal, or "" for "new study"

state.experiments2 = [];          // GET /api/experiments, filtered to state.selectedStudyId
state.experiments2Loaded = false;
state.experiments2StatusFilter = null;
state.experiments2Search = "";
state.experiments2Selected = new Set();
state.experiments2Poller = null;

state.runtimesList = [];          // GET /api/runtimes, cached — the Composer's pin list + row runtime dropdowns
state.runtimesLoaded = false;

state.composerConfigFilter = "";
state.composerSelectedConfigs = new Set();
state.composerOverlayRows = [{ key: "", value: "" }];
state.composerPreflight = null;
state.composerPreflightTimer = null;
state.composerBusy = false;
// XDASH_FIXES_PLAN.md F4.1/F4.2: per-row Runtime picks, keyed by the
// preflight row's own experiment_id so they survive a re-preflight (e.g.
// an overlay edit) for any row that still exists. Only holds an entry for
// a row the user picked by hand; every other row falls back to the "Set
// all rows to…" control (composerBulkRuntimeValue()) — see
// composerRowRuntimeValue(). Cleared whenever that top control changes
// ("applies to every row"), and reset on every openComposer().
state.composerRowRuntime = new Map();

state.compareLoaded = false;

// The Experiment page (#/x/<id>/<tab>) — kept separate from the above so
// leaving the screen (stopExperimentDetailPolling(), called by
// switchView() — see app.js) can reset it without touching the spine.
state.xdetailId = null;
state.xdetailView = null;         // the last GET /api/experiments/<id> response
state.xdetailTab = "overview";
state.xdetailLogStage = null;
state.xdetailPoller = null;

const STUDY_STATUS_CLASS = { attention: "red", running: "amber", planning: "slate", complete: "emerald", archived: "slate" };

// ================================================================== Studies pane
async function loadStudies() {
  try {
    const data = await api("/api/studies");
    state.studies = data.studies || [];
    state.studiesLoaded = true;
  } catch (e) {
    toast("Couldn't load studies: " + e.message, "err");
  }
  renderStudiesList();
}

function renderStudiesList() {
  const body = document.getElementById("studies-list-body");
  if (!body) return;
  const filter = (state.studyFilter || "").trim().toLowerCase();
  const real = state.studies.filter((s) => !filter || s.name.toLowerCase().includes(filter));
  const rows = [];
  rows.push(studyRowHtml(null, "All experiments", null));
  rows.push(studyRowHtml("unfiled", "Unfiled", null));
  for (const s of real) rows.push(studyRowHtml(s.study_id, s.name, s));
  body.innerHTML = rows.join("");
  body.querySelectorAll("[data-study-row]").forEach((row) => {
    row.addEventListener("click", () => {
      const id = row.dataset.studyRow || null;
      const tab = currentExperimentsTab();
      state.compareAdhocIds = null; // picking a study leaves any ad-hoc "Compare selected" view
      navigateToView("experiments", { study: id || undefined, tab });
    });
  });
}

function studyRowHtml(id, name, study) {
  const active = (state.selectedStudyId || null) === (id || null) ? "active" : "";
  const badge = study ? `<span class="badge ${STUDY_STATUS_CLASS[study.status] || "slate"}">${escapeHtml(study.status)}</span>` : "";
  const count = study ? study.experiment_count : "";
  return `<div class="config-item ${active}" data-study-row="${escapeHtml(id || "")}" title="${escapeHtml(name)}">
    <span class="dot"></span><span style="flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(name)}</span>
    <span class="settings-profile-path">${count}</span>${badge}
  </div>`;
}

function currentExperimentsTab() {
  return document.querySelector("#experiments-subtabs .subtab-btn.active")?.dataset.subtab || "experiments";
}

function selectedStudy() {
  if (!state.selectedStudyId || state.selectedStudyId === "unfiled") return null;
  return state.studies.find((s) => s.study_id === state.selectedStudyId) || null;
}

function renderStudyHeader() {
  const info = document.getElementById("study-header-info");
  const editBtn = document.getElementById("btn-study-edit");
  const autopilotBtn = document.getElementById("btn-study-autopilot");
  const questionEl = document.getElementById("study-header-question");
  const study = selectedStudy();
  if (!state.selectedStudyId) {
    info.innerHTML = `<span>All experiments</span>`;
  } else if (state.selectedStudyId === "unfiled") {
    info.innerHTML = `<span>Unfiled</span>`;
  } else if (study) {
    info.innerHTML = `<span>${escapeHtml(study.name)}</span> <span class="badge ${STUDY_STATUS_CLASS[study.status] || "slate"}">${escapeHtml(study.status)}</span>`;
  } else {
    info.innerHTML = `<span>Unknown study</span>`;
  }
  editBtn.classList.toggle("hidden", !study);
  autopilotBtn.classList.toggle("hidden", !study);
  if (study) autopilotBtn.textContent = `Autopilot: ${study.autopilot && study.autopilot.enabled ? "on" : "off"}`;
  questionEl.classList.toggle("hidden", !(study && study.question));
  if (study && study.question) questionEl.textContent = study.question;
}

async function toggleSelectedStudyAutopilot() {
  const study = selectedStudy();
  if (!study) return;
  const enabled = !(study.autopilot && study.autopilot.enabled);
  try {
    await api(`/api/studies/${encodeURIComponent(study.study_id)}/autopilot`, {
      method: "POST", body: JSON.stringify({ enabled }),
    });
    toast(`Autopilot ${enabled ? "enabled" : "disabled"} for '${study.name}'`, "ok");
    await loadStudies();
    renderStudyHeader();
  } catch (e) {
    toast("Couldn't change autopilot: " + e.message, "err");
  }
}

// ---------------------------------------------------------------- study edit modal
function openStudyEditModal(study) {
  state.studyEditId = study ? study.study_id : "";
  document.getElementById("study-edit-title").textContent = study ? `Edit '${study.name}'` : "New study";
  document.getElementById("study-edit-name").value = study ? study.name : "";
  document.getElementById("study-edit-question").value = study ? (study.question || "") : "";
  document.getElementById("study-edit-metric").value = study ? ((study.primary_metric || {}).key || "") : "";
  document.getElementById("study-edit-metric-direction").value = study ? ((study.primary_metric || {}).direction || "max") : "max";
  document.getElementById("study-edit-baseline").value = study ? ((study.baseline || {}).config_path || "") : "";
  document.getElementById("study-edit-delete").classList.toggle("hidden", !study);
  document.getElementById("study-edit-backdrop").classList.remove("hidden");
}

function closeStudyEditModal() {
  document.getElementById("study-edit-backdrop").classList.add("hidden");
  state.studyEditId = null;
}

async function saveStudyEdit() {
  const name = document.getElementById("study-edit-name").value.trim();
  if (!name) { toast("A study needs a name", "err"); return; }
  const metricKey = document.getElementById("study-edit-metric").value.trim();
  const body = {
    name,
    question: document.getElementById("study-edit-question").value.trim(),
    primary_metric: metricKey ? { key: metricKey, direction: document.getElementById("study-edit-metric-direction").value } : null,
    baseline: document.getElementById("study-edit-baseline").value.trim() || null,
  };
  try {
    let result;
    if (state.studyEditId) result = await api(`/api/studies/${encodeURIComponent(state.studyEditId)}`, { method: "PATCH", body: JSON.stringify(body) });
    else result = await api("/api/studies", { method: "POST", body: JSON.stringify(body) });
    toast(`Saved '${result.name}'`, "ok");
    closeStudyEditModal();
    await loadStudies();
    if (!state.studyEditId) navigateToView("experiments", { study: result.study_id, tab: currentExperimentsTab() });
    else { renderStudyHeader(); }
  } catch (e) {
    toast("Couldn't save study: " + e.message, "err");
  }
}

async function deleteStudyEdit() {
  if (!state.studyEditId) return;
  const ok = await showConfirm("Delete study?", "Removes the study and every membership in it. The experiments themselves, their attempts and outputs are untouched.");
  if (!ok) return;
  try {
    await api(`/api/studies/${encodeURIComponent(state.studyEditId)}`, { method: "DELETE" });
    toast("Study deleted", "ok");
    closeStudyEditModal();
    if (state.selectedStudyId === state.studyEditId) navigateToView("experiments", { tab: currentExperimentsTab() });
    else { await loadStudies(); }
  } catch (e) {
    toast("Couldn't delete study: " + e.message, "err");
  }
}

// ================================================================== Experiments tab
async function refreshExperiments2List() {
  const params = new URLSearchParams();
  if (state.selectedStudyId) params.set("study", state.selectedStudyId);
  try {
    const data = await api(`/api/experiments?${params.toString()}`);
    state.experiments2 = data.experiments || [];
    state.experiments2Loaded = true;
  } catch (e) {
    const body = document.getElementById("experiments-table-body");
    if (body) body.innerHTML = `<tr><td colspan="7" class="empty-state">Couldn't load experiments: ${escapeHtml(e.message)}</td></tr>`;
    return;
  }
  renderExperiments2StatusFilters();
  renderExperiments2Table();
}

function renderExperiments2StatusFilters() {
  const el = document.getElementById("experiments-status-filters");
  if (!el) return;
  const counts = {};
  for (const v of state.experiments2) counts[v.status] = (counts[v.status] || 0) + 1;
  const order = ["draft", "queued", "blocked", "dispatching", "running", "done", "failed", "cancelled"];
  const chips = order.filter((s) => counts[s]).map((s) => {
    const active = state.experiments2StatusFilter === s ? "active" : "";
    return `<button class="subtab-btn ${active}" data-status-filter="${s}" style="padding:5px 10px; font-size:11px;">${escapeHtml(s)} <b>${counts[s]}</b></button>`;
  });
  el.innerHTML = chips.join("");
  el.querySelectorAll("[data-status-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const s = btn.dataset.statusFilter;
      state.experiments2StatusFilter = state.experiments2StatusFilter === s ? null : s;
      renderExperiments2StatusFilters();
      renderExperiments2Table();
    });
  });
}

// Groups by config_path + a stable (client-side, display-only) overlay key so
// seeds of the same config × overlay aggregate into one block — the exact
// digest is server-side (framework.overlay_digest, sha1); this only needs to
// be a stable grouping key, not the real id.
function overlayGroupKey(v) {
  const overlay = v.overlay || {};
  const keys = Object.keys(overlay).sort();
  return v.config_path + (keys.length ? "#" + JSON.stringify(keys.map((k) => [k, overlay[k]])) : "");
}

function filteredExperiments2() {
  const search = (state.experiments2Search || "").trim().toLowerCase();
  return state.experiments2.filter((v) => {
    if (state.experiments2StatusFilter && v.status !== state.experiments2StatusFilter) return false;
    if (search) {
      const hay = `${v.experiment_id} ${v.name || ""} ${v.config_path} ${v.notes || ""}`.toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  });
}

function renderExperiments2Table() {
  const body = document.getElementById("experiments-table-body");
  if (!body) return;
  const list = filteredExperiments2();
  if (!list.length) {
    body.innerHTML = `<tr><td colspan="7" class="empty-state">No experiments match.</td></tr>`;
    renderExperiments2BulkBar();
    return;
  }
  const groups = new Map();
  for (const v of list) {
    const key = overlayGroupKey(v);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(v);
  }
  const html = [];
  for (const [key, members] of groups) {
    members.sort((a, b) => String(a.seed ?? "").localeCompare(String(b.seed ?? "")));
    const overlay = members[0].overlay || {};
    const overlayLabel = Object.keys(overlay).length ? Object.entries(overlay).map(([k, v]) => `${k}=${v}`).join(", ") : "";
    html.push(`<tr class="config-group-row" style="background:var(--surface-2);"><td colspan="7" style="font-family:var(--mono); font-size:11px; padding:8px 10px;">
      ${escapeHtml(members[0].config_path)}${overlayLabel ? ` <span style="color:var(--text-faint);">· ${escapeHtml(overlayLabel)}</span>` : ""}
      <span style="color:var(--text-faint);">(${members.length} seed${members.length === 1 ? "" : "s"})</span>
    </td></tr>`);
    for (const v of members) html.push(experiments2RowHtml(v));
  }
  body.innerHTML = html.join("");
  body.querySelectorAll("input[data-select-id]").forEach((cb) => {
    cb.checked = state.experiments2Selected.has(cb.dataset.selectId);
    cb.addEventListener("change", () => {
      if (cb.checked) state.experiments2Selected.add(cb.dataset.selectId);
      else state.experiments2Selected.delete(cb.dataset.selectId);
      renderExperiments2BulkBar();
    });
  });
  body.querySelectorAll("select[data-runtime-id]").forEach((sel) => {
    sel.addEventListener("change", () => setExperimentRuntime(sel.dataset.runtimeId, sel.value));
  });
  body.querySelectorAll("button[data-row-action]").forEach((btn) => {
    btn.addEventListener("click", () => runExperiments2RowAction(btn.dataset.rowAction, btn.dataset.id));
  });
  renderExperiments2BulkBar();
  document.dispatchEvent(new Event("xdash:rows-rendered")); // static/js/lib/shortcuts.js's j/k highlight survives this re-render
}

// XDASH_FIXES_PLAN.md F0.2 — the block code as a chip (with the detail as
// its tooltip, for a quick hover) plus the action line underneath, instead
// of a bare "⚠" whose only content was a title attribute nobody hovers on
// a live table. Shared by the table's status cell and the Experiment page
// Overview (renderXdetailOverview).
function blockedDetailHtml(blocked) {
  if (!blocked) return "";
  return ` <span class="badge red" title="${escapeHtml(blocked.detail || "")}">${escapeHtml(blocked.code || "blocked")}</span>` +
    (blocked.action ? `<div class="settings-profile-path" style="font-family:var(--mono); white-space:normal; margin-top:2px;">${escapeHtml(blocked.action)}</div>` : "");
}

function experiments2RowHtml(v) {
  const a = v.current_attempt || {};
  const seed = v.seed === null || v.seed === undefined ? "—" : escapeHtml(String(v.seed));
  const runtimeVal = v.runtime && v.runtime.mode === "pinned" ? v.runtime.slot : "auto";
  const runtimeOptions = [`<option value="auto" ${runtimeVal === "auto" ? "selected" : ""}>Auto</option>`]
    .concat(state.runtimesList.map((r) => `<option value="${escapeHtml(r.id)}" ${runtimeVal === r.id ? "selected" : ""}>${escapeHtml(r.label)}</option>`))
    .join("");
  return `<tr data-experiment-row="${escapeHtml(v.experiment_id)}">
    <td><input type="checkbox" data-select-id="${escapeHtml(v.experiment_id)}" /></td>
    <td>${experimentLink(v.experiment_id, v.name || v.experiment_id)}</td>
    <td>${seed}</td>
    <td><select class="text-input" data-runtime-id="${escapeHtml(v.experiment_id)}" style="font-size:11px; padding:3px 5px;">${runtimeOptions}</select></td>
    <td>${renderStatusBadge(v.status)}${blockedDetailHtml(a.blocked)}</td>
    <td>${escapeHtml(timeAgo(v.updated_at || a.ended_at || a.started_at || v.created_at))}</td>
    <td>${experiments2RowActionsHtml(v)}</td>
  </tr>`;
}

function experiments2RowActionsHtml(v) {
  const id = escapeHtml(v.experiment_id);
  if (v.status === "draft") return `<button class="btn btn-sm btn-ghost" data-row-action="queue" data-id="${id}">Queue</button>
    <button class="btn btn-sm btn-ghost" data-row-action="run_now" data-id="${id}">Run now</button>
    <button class="btn btn-sm btn-danger" data-row-action="delete" data-id="${id}">Delete</button>`;
  if (v.status === "queued" || v.status === "blocked") return `<button class="btn btn-sm btn-ghost" data-row-action="dequeue" data-id="${id}">Dequeue</button>
    <button class="btn btn-sm btn-danger" data-row-action="delete" data-id="${id}">Delete</button>`;
  if (v.status === "dispatching" || v.status === "running") return `<button class="btn btn-sm btn-danger" data-row-action="cancel" data-id="${id}">Cancel</button>`;
  return `<button class="btn btn-sm btn-ghost" data-row-action="retry" data-id="${id}">Retry</button>
    <button class="btn btn-sm btn-danger" data-row-action="delete" data-id="${id}">Delete</button>`;
}

// ================================================================== F0.1: action results are never silently "ok"
// `{action, ok: [...], skipped: [{id, reason, runtimes?, can_queue?}]}` —
// backend/experiments.py's apply_action()/create_experiments() shape.
// Before this, submitComposer/runExperiments2RowAction/…BulkAction/
// runXdetailAction each toasted a flat "ok" regardless of `skipped`, so a
// refused Kaggle dispatch (`code-not-pushed`) or a busy runtime looked
// identical to a real success.
// *collector*, when given, is an array reportActionResult appends this
// call's own (tagged) skipped entries to instead of opening the "why not"
// panel itself — XDASH_FIXES_PLAN.md F5's carry-over from F4.3: a Composer
// submit whose response carries skips in *both* `runtime_changes` and
// `then` used to call this twice, and the second openSkipReport() silently
// overwrote the first panel (only one exists). submitComposer() now passes
// one shared array to both calls and opens a single, merged panel itself
// once both are done — every other caller (row/bulk/xdetail actions, which
// only ever have one result to report) still omits *collector* and gets the
// exact same single-source behavior as before.
function reportActionResult(result, verb, collector) {
  if (!result) return true;
  const ok = result.ok || [];
  const skipped = result.skipped || [];
  if (ok.length) toast(`${verb}: ${ok.length} experiment${ok.length === 1 ? "" : "s"}`, "ok");
  if (skipped.length) {
    toast(`${skipped.length} couldn't ${verb} — see why`, "err");
    const tagged = skipped.map((s) => ({ ...s, _verb: verb }));
    if (collector) collector.push(...tagged);
    else openSkipReport(verb, tagged);
  }
  return skipped.length === 0;
}

function skipReportEntryHtml(s) {
  const runtimesHtml = (s.runtimes || []).map((r) => `
    <div class="kaggle-history-row" style="align-items:flex-start;">
      <span class="badge red">${escapeHtml(r.code || "blocked")}</span>
      <span><strong>${escapeHtml(r.runtime || "")}</strong>${r.detail ? ` — ${escapeHtml(r.detail)}` : ""}
      ${r.action ? `<div class="settings-profile-path" style="font-family:var(--mono);">${escapeHtml(r.action)}</div>` : ""}</span>
    </div>`).join("");
  // _verb (F5's merge fix) is only shown when a single combined panel is
  // reporting more than one distinct action — see openSkipReport() below.
  const verbTag = s._verb ? `<span class="badge slate">${escapeHtml(s._verb)}</span> ` : "";
  return `<div class="lab-attention-row" style="flex-direction:column; align-items:stretch; gap:6px;">
    <div>${verbTag}<strong>${experimentLink(s.id)}</strong> <span style="color:var(--text-muted);">${escapeHtml(s.reason || "")}</span></div>
    ${runtimesHtml}
    ${s.can_queue ? `<button class="btn btn-sm btn-ghost" data-skip-queue="${escapeHtml(s.id)}">Queue instead</button>` : ""}
  </div>`;
}

function openSkipReport(verb, skipped) {
  const body = document.getElementById("skip-report-body");
  // A merged call (submitComposer's shared collector) tags each entry with
  // its own source verb; the title stays specific when every entry agrees,
  // and only goes generic when this panel is actually combining more than
  // one kind of refusal in the same view.
  const verbs = new Set(skipped.map((s) => s._verb || verb));
  document.getElementById("skip-report-title").textContent =
    verbs.size <= 1 ? `Why not ${[...verbs][0] || verb}?` : "Why not?";
  body.innerHTML = skipped.map(skipReportEntryHtml).join("");
  body.querySelectorAll("[data-skip-queue]").forEach((btn) => {
    btn.addEventListener("click", () => queueInsteadFromSkipReport(btn.dataset.skipQueue));
  });
  document.getElementById("skip-report-backdrop").classList.remove("hidden");
}

function closeSkipReport() {
  document.getElementById("skip-report-backdrop").classList.add("hidden");
}

async function queueInsteadFromSkipReport(id) {
  try {
    await api("/api/experiments/actions", { method: "POST", body: JSON.stringify({ action: "queue", ids: [id] }) });
    toast(`Queued '${id}'`, "ok");
    closeSkipReport();
    if (state.xdetailId) refreshXdetail(); else refreshExperiments2AllData();
  } catch (e) {
    toast(`Couldn't queue: ${e.message}`, "err");
  }
}

async function runExperiments2RowAction(action, id) {
  if (action === "delete") { openDeleteExperimentModal(id, () => refreshExperiments2AllData()); return; }
  try {
    const result = await api("/api/experiments/actions", { method: "POST", body: JSON.stringify({ action, ids: [id] }) });
    reportActionResult(result, action.replace("_", " "));
    refreshExperiments2AllData();
  } catch (e) {
    toast(`Couldn't ${action}: ${e.message}`, "err");
  }
}

async function setExperimentRuntime(id, value) {
  const runtime = value === "auto" ? { mode: "auto" } : { mode: "pinned", slot: value };
  try {
    await api("/api/experiments/actions", { method: "POST", body: JSON.stringify({ action: "set_runtime", ids: [id], params: { runtime } }) });
    toast(`Runtime updated for '${id}'`, "ok");
    refreshExperiments2AllData();
  } catch (e) {
    toast("Couldn't change runtime: " + e.message, "err");
  }
}

function renderExperiments2BulkBar() {
  const bar = document.getElementById("experiments-bulk-bar");
  const selectAll = document.getElementById("experiments-select-all");
  if (!bar) return;
  const visible = filteredExperiments2().map((v) => v.experiment_id);
  const selected = visible.filter((id) => state.experiments2Selected.has(id));
  if (selectAll) selectAll.checked = visible.length > 0 && selected.length === visible.length;
  if (!selected.length) { bar.classList.add("hidden"); bar.innerHTML = ""; return; }
  bar.classList.remove("hidden");
  const studyOptions = `<option value="">— add to study —</option>` + state.studies.map((s) => `<option value="${escapeHtml(s.study_id)}">${escapeHtml(s.name)}</option>`).join("");
  bar.innerHTML = `
    <span><b>${selected.length}</b> selected</span>
    <button class="btn btn-sm btn-ghost" data-bulk="queue">Queue</button>
    <button class="btn btn-sm btn-ghost" data-bulk="cancel">Cancel</button>
    <button class="btn btn-sm btn-ghost" data-bulk="retry">Retry</button>
    <select class="text-input" id="bulk-add-to-study-select" style="font-size:11px;">${studyOptions}</select>
    <button class="btn btn-sm btn-ghost" data-bulk="add_to_study">Add</button>
    <button class="btn btn-sm btn-ghost" data-bulk="compare" title="Open Compare pre-filled with this selection, across any study">Compare</button>
    <button class="btn btn-sm btn-danger" data-bulk="delete">Delete</button>
  `;
  bar.querySelectorAll("[data-bulk]").forEach((btn) => btn.addEventListener("click", () => runExperiments2BulkAction(btn.dataset.bulk)));
}

async function runExperiments2BulkAction(action) {
  const ids = Array.from(state.experiments2Selected);
  if (!ids.length) return;
  if (action === "compare") {
    openCompareAdhoc(ids);
    return;
  }
  if (action === "delete") {
    const ok = await showConfirm("Delete selected?", `Deletes ${ids.length} experiment(s) from the dashboard (soft delete — see the per-row delete dialog for the opt-in extras this skips).`);
    if (!ok) return;
    try {
      await api("/api/experiments/actions", { method: "POST", body: JSON.stringify({ action: "delete", ids }) });
      toast(`Deleted ${ids.length} experiment(s)`, "ok");
      state.experiments2Selected.clear();
      refreshExperiments2AllData();
    } catch (e) { toast("Couldn't delete: " + e.message, "err"); }
    return;
  }
  if (action === "add_to_study") {
    const studyId = document.getElementById("bulk-add-to-study-select").value;
    if (!studyId) { toast("Pick a study first", "err"); return; }
    try {
      await api("/api/experiments/actions", { method: "POST", body: JSON.stringify({ action: "add_to_study", ids, params: { study_id: studyId } }) });
      toast(`Added ${ids.length} experiment(s) to study`, "ok");
      refreshExperiments2AllData();
    } catch (e) { toast("Couldn't add to study: " + e.message, "err"); }
    return;
  }
  try {
    const result = await api("/api/experiments/actions", { method: "POST", body: JSON.stringify({ action, ids }) });
    reportActionResult(result, action);
    refreshExperiments2AllData();
  } catch (e) {
    toast(`Couldn't ${action}: ${e.message}`, "err");
  }
}

async function refreshExperiments2AllData() {
  await Promise.all([loadStudies(), refreshExperiments2List()]);
  renderStudyHeader();
}

// ================================================================== screen entry (switchView -> here)
async function loadExperiments2Screen() {
  if (!state.runtimesLoaded) {
    try { state.runtimesList = (await api("/api/runtimes")).runtimes || []; state.runtimesLoaded = true; } catch (e) { /* row runtime dropdown just shows Auto */ }
  }
  await refreshExperiments2AllData();
  if (currentExperimentsTab() === "compare") loadCompareTab();
  if (!state.experiments2Poller) {
    state.experiments2Poller = createPoller(refreshExperiments2AllData, 6000);
  }
  state.experiments2Poller.start();
}

function stopExperiments2Polling() {
  if (state.experiments2Poller) state.experiments2Poller.stop();
}

// Called by js/lib/router.js's handleRoute() whenever the URL resolves to
// #/experiments — the one place study/tab selection is actually applied, so
// a bookmark, a back/forward navigation and a plain sidebar click all paint
// the same thing (XDASH_PLAN.md §8's addressability rule).
function applyExperimentsRouteParams(query) {
  state.selectedStudyId = query.get("study") || null;
  const tab = query.get("tab") || "experiments";
  document.querySelectorAll("#experiments-subtabs .subtab-btn").forEach((b) => b.classList.toggle("active", b.dataset.subtab === tab));
  document.querySelectorAll("#view-experiments > .subtab-panel").forEach((p) => p.classList.toggle("active", p.dataset.subtab === tab));
  renderStudiesList();
  renderStudyHeader();
  renderExperiments2Table();
  if (tab === "compare") loadCompareTab();
}

// ================================================================== Compare tab (XDASH_PLAN.md §8.2 U7, Phase 6)
//
// state.compareAdhocIds: set by "Compare selected" from the bulk bar (any
// selection, from any study or none) — when present, loadCompareTab() calls
// the ad-hoc POST /api/experiments/compare instead of a study's GET .../compare,
// and the study/group-by picker is replaced by a "back to a study" affordance.
// Both endpoints return the identical shape (backend/studies.py's
// _compare_rows()), so every render function below is agnostic to which one
// answered.
state.compareAdhocIds = null;
state.compareData = null;
state.compareMetric = null;
state.compareCurvesTag = null;
state.compareBarChart = null;
state.compareStripChart = null;
state.compareRadarChartInstance = null;
state.compareCurvesChart = null;
const COMPARE_CURVES_CAP = 12; // one HTTP round trip per experiment — keep it small

async function populateCompareStudySelect() {
  const sel = document.getElementById("compare-study-select");
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = `<option value="">— pick a study —</option>` + state.studies.map((s) => `<option value="${escapeHtml(s.study_id)}">${escapeHtml(s.name)}</option>`).join("");
  if (current && state.studies.some((s) => s.study_id === current)) sel.value = current;
  else if (state.selectedStudyId && state.selectedStudyId !== "unfiled") sel.value = state.selectedStudyId;
}

// Opens Compare pre-filled with an arbitrary experiment-id list, from any
// bulk-bar selection (the Experiments table's own selection, or later a
// Sessions/other-screen selection that reuses this same entry point) —
// §8.2's "any selection from any study can be compared ad hoc."
function openCompareAdhoc(ids) {
  state.compareAdhocIds = ids.slice();
  navigateToView("experiments", { tab: "compare" });
}

function clearCompareAdhoc() {
  state.compareAdhocIds = null;
  loadCompareTab();
}

async function loadCompareTab() {
  const table = document.getElementById("study-compare-table");
  const countEl = document.getElementById("study-compare-count");
  const isAdhoc = Array.isArray(state.compareAdhocIds) && state.compareAdhocIds.length > 0;
  document.getElementById("compare-study-select").classList.toggle("hidden", isAdhoc);
  document.getElementById("compare-adhoc-badge").classList.toggle("hidden", !isAdhoc);
  document.getElementById("btn-compare-clear-adhoc").classList.toggle("hidden", !isAdhoc);

  let data;
  if (isAdhoc) {
    const groupBy = document.getElementById("compare-group-by").value;
    table.innerHTML = `<tbody><tr><td class="empty-state">Loading…</td></tr></tbody>`;
    try {
      data = await api("/api/experiments/compare", {
        method: "POST", body: JSON.stringify({ ids: state.compareAdhocIds, group_by: groupBy }),
      });
    } catch (e) {
      table.innerHTML = `<tbody><tr><td class="empty-state">Couldn't load compare: ${escapeHtml(e.message)}</td></tr></tbody>`;
      return;
    }
    if (data.missing_ids && data.missing_ids.length) {
      toast(`${data.missing_ids.length} selected experiment(s) no longer exist`, "err");
    }
  } else {
    await populateCompareStudySelect();
    const studyId = document.getElementById("compare-study-select").value;
    if (!studyId) {
      table.innerHTML = `<tbody><tr><td class="empty-state">Pick a study above, or select experiments in the Experiments tab and click "Compare".</td></tr></tbody>`;
      if (countEl) countEl.textContent = "";
      hideCompareExtras();
      return;
    }
    const groupBy = document.getElementById("compare-group-by").value;
    table.innerHTML = `<tbody><tr><td class="empty-state">Loading…</td></tr></tbody>`;
    try {
      data = await api(`/api/studies/${encodeURIComponent(studyId)}/compare?group_by=${encodeURIComponent(groupBy)}`);
    } catch (e) {
      table.innerHTML = `<tbody><tr><td class="empty-state">Couldn't load compare: ${escapeHtml(e.message)}</td></tr></tbody>`;
      return;
    }
  }
  state.compareLoaded = true;
  state.compareData = data;
  if (countEl) countEl.textContent = `${data.groups.length} group${data.groups.length === 1 ? "" : "s"}`;
  if (!data.groups.length) {
    table.innerHTML = `<tbody><tr><td class="empty-state">No members with a reachable eval report yet.</td></tr></tbody>`;
    hideCompareExtras();
    return;
  }
  renderCompareTable(data);
  populateCompareMetricSelect(data);
  renderCompareCharts(data);
  renderCompareHeatmap(data);
  loadCompareCurves(data);
}

function hideCompareExtras() {
  document.getElementById("compare-charts-panel").classList.add("hidden");
  document.getElementById("compare-curves-panel").classList.add("hidden");
  document.getElementById("compare-heatmap-panel").classList.add("hidden");
}

// direction-aware "best in column": for each metric, the winning group's
// mean — never guessed for a metric absent from lower_is_better/the higher-
// is-better radar set (same rule renderCompare() already uses for Reports).
function compareBestPerMetric(data) {
  const best = {};
  for (const m of data.metrics) {
    const lowerBetter = data.lower_is_better.includes(m);
    const higherBetter = HIGHER_IS_BETTER.has(m);
    if (!lowerBetter && !higherBetter) { best[m] = null; continue; }
    const means = data.groups.map((g) => (g.metrics[m] || {}).mean).filter((v) => v != null);
    best[m] = means.length ? (lowerBetter ? Math.min(...means) : Math.max(...means)) : null;
  }
  return best;
}

function compareCellTooltip(g, m) {
  const s = g.metrics[m];
  if (!s || !s.values || !s.values.length) return "";
  return s.values.map((v) => `${v.experiment_id} (seed ${v.seed ?? "—"}): ${fmtNum(v.value)}`).join("\n");
}

function renderCompareTable(data) {
  const table = document.getElementById("study-compare-table");
  const metrics = data.metrics;
  const best = compareBestPerMetric(data);
  table.innerHTML = `
    <thead><tr><th>Group</th><th>n</th>${metrics.map((m) => {
      const dir = data.lower_is_better.includes(m) ? "↓" : HIGHER_IS_BETTER.has(m) ? "↑" : "";
      const primary = data.primary_metric && data.primary_metric.key === m;
      return `<th title="${dir === "↓" ? "Lower is better" : dir === "↑" ? "Higher is better" : ""}">${escapeHtml(m)}${dir ? ` <span style="color:var(--text-faint)">${dir}</span>` : ""}${primary ? ` <span class="badge amber" title="This study's primary metric">★</span>` : ""}</th>`;
    }).join("")}</tr></thead>
    <tbody>${data.groups.map((g) => `<tr data-compare-row="${escapeHtml(g.key)}" ${g.baseline ? 'style="background:var(--surface-2);"' : ""}>
      <td title="${escapeHtml(g.experiments.join(", "))}">${escapeHtml(g.key)}${g.baseline ? ` <span class="badge slate">baseline</span>` : ""}</td>
      <td>${g.with_report}/${g.n}</td>
      ${metrics.map((m) => {
        const s = g.metrics[m] || {};
        if (s.mean == null) return `<td>—</td>`;
        const isBest = best[m] != null && s.mean === best[m];
        const delta = s.delta_vs_baseline;
        const deltaHtml = delta != null && !g.baseline ? ` <span style="color:${(data.lower_is_better.includes(m) ? delta < 0 : delta > 0) ? "var(--teal)" : "var(--red)"};">${delta >= 0 ? "+" : ""}${fmtNum(delta)}</span>` : "";
        return `<td class="${isBest ? "winner" : ""}" title="${escapeHtml(compareCellTooltip(g, m))}">${fmtNum(s.mean)}${s.n > 1 ? ` ±${fmtNum(s.std)}` : ""}${deltaHtml}</td>`;
      }).join("")}
    </tr>`).join("")}</tbody>`;
  document.dispatchEvent(new Event("xdash:rows-rendered")); // static/js/lib/shortcuts.js's j/k highlight survives this re-render
}

function populateCompareMetricSelect(data) {
  const sel = document.getElementById("compare-metric-select");
  const current = state.compareMetric;
  sel.innerHTML = data.metrics.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("");
  state.compareMetric = (current && data.metrics.includes(current)) ? current : data.metrics[0];
  sel.value = state.compareMetric;
}

function compareGroupColors(data) {
  return data.groups.map((g, i) => CHART_COLORS[i % CHART_COLORS.length]);
}

function renderCompareCharts(data) {
  document.getElementById("compare-charts-panel").classList.remove("hidden");
  const metric = state.compareMetric;
  const groups = data.groups;
  const labels = groups.map((g) => g.key);
  const colors = compareGroupColors(data);

  // ---- bar ± std (a floating-bar dataset drawn behind the mean bar, since
  // Chart.js core ships no error-bar plugin and this app loads no plugin
  // CDNs beyond Chart.js itself — see index.html's script list).
  if (state.compareBarChart) state.compareBarChart.destroy();
  const means = groups.map((g) => (g.metrics[metric] || {}).mean);
  const stds = groups.map((g) => (g.metrics[metric] || {}).std || 0);
  state.compareBarChart = new Chart(document.getElementById("compare-bar-chart").getContext("2d"), {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          type: "bar", label: "mean ± std",
          data: means.map((m, i) => (m == null ? [0, 0] : [Math.max(0, m - stds[i]), m + stds[i]])),
          backgroundColor: colors.map((c) => c + "33"), borderColor: colors, borderWidth: 1,
        },
        {
          type: "bar", label: metric, data: means,
          backgroundColor: colors, barPercentage: 0.4, categoryPercentage: 0.6,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#8C97B0", font: { size: 9 } }, grid: { color: "#1B2740" } },
        y: { ticks: { color: "#5C6785", font: { size: 9 } }, grid: { color: "#1B2740" } },
      },
    },
  });

  // ---- per-seed strip plot: a scatter of every contributing experiment's
  // raw value at its group's x position, x-jittered by seed index.
  if (state.compareStripChart) state.compareStripChart.destroy();
  const stripDatasets = groups.map((g, gi) => {
    const values = (g.metrics[metric] || {}).values || [];
    return {
      type: "scatter", label: g.key,
      data: values.map((v, vi) => ({ x: gi + (vi - (values.length - 1) / 2) * 0.08, y: v.value })),
      backgroundColor: colors[gi], pointRadius: 4, pointHoverRadius: 6,
    };
  });
  state.compareStripChart = new Chart(document.getElementById("compare-strip-chart").getContext("2d"), {
    type: "scatter",
    data: { datasets: stripDatasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false } },
      scales: {
        x: {
          min: -0.5, max: groups.length - 0.5,
          ticks: { color: "#8C97B0", font: { size: 9 }, stepSize: 1, callback: (v) => labels[v] || "" },
          grid: { color: "#1B2740" },
        },
        y: { ticks: { color: "#5C6785", font: { size: 9 } }, grid: { color: "#1B2740" } },
      },
    },
  });

  // ---- radar: reuses app.js's radarOptions()/radarAxisKeys() — the same
  // component the (retired-from-nav) Reports "compare selected" feature
  // draws, so a radar looks identical everywhere in the app.
  if (state.compareRadarChartInstance) state.compareRadarChartInstance.destroy();
  const groupMetrics = groups.map((g) => {
    const m = {};
    for (const key of data.metrics) m[key] = (g.metrics[key] || {}).mean;
    return m;
  });
  const axisKeys = radarAxisKeys(groupMetrics);
  const radarCanvas = document.getElementById("compare-radar-chart");
  if (axisKeys.length) {
    radarCanvas.parentElement.classList.remove("hidden");
    state.compareRadarChartInstance = new Chart(radarCanvas.getContext("2d"), {
      type: "radar",
      data: {
        labels: axisKeys.map(radarAxisLabel),
        datasets: groups.map((g, i) => ({
          label: g.key, data: axisKeys.map((k) => radarAxisValue(groupMetrics[i], k)),
          borderColor: colors[i], backgroundColor: "transparent", pointBackgroundColor: colors[i],
        })),
      },
      options: radarOptions(),
    });
  } else {
    radarCanvas.parentElement.classList.add("hidden");
  }
}

// ---- overlaid training curves, read straight from TB event files
// (GET /api/experiments/<id>/curves — new in Phase 6, backend/tb_curves.py).
async function loadCompareCurves(data) {
  const panel = document.getElementById("compare-curves-panel");
  const capEl = document.getElementById("compare-curves-cap");
  capEl.textContent = String(COMPARE_CURVES_CAP);
  const allIds = [];
  for (const g of data.groups) for (const id of g.experiments) allIds.push({ id, group: g.key });
  const picked = allIds.slice(0, COMPARE_CURVES_CAP);
  if (!picked.length) { panel.classList.add("hidden"); return; }

  // Tags come from the first experiment that actually has any — cheap
  // (one call), and every dissert run logs the same tag set.
  let tags = [];
  for (const { id } of picked) {
    try {
      const probe = await api(`/api/experiments/${encodeURIComponent(id)}/curves`);
      if (probe.available && probe.tags.length) { tags = probe.tags; break; }
    } catch (e) { /* try the next experiment */ }
  }
  if (!tags.length) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  const tagSel = document.getElementById("compare-curves-tag-select");
  const preferred = tags.find((t) => t.endsWith("/" + state.compareMetric)) || tags[0];
  const current = state.compareCurvesTag && tags.includes(state.compareCurvesTag) ? state.compareCurvesTag : preferred;
  tagSel.innerHTML = tags.map((t) => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join("");
  tagSel.value = current;
  state.compareCurvesTag = current;
  await renderCompareCurvesChart(picked, current);
}

async function renderCompareCurvesChart(picked, tag) {
  const colors = state.compareData ? compareGroupColors(state.compareData) : CHART_COLORS;
  const groupIndex = {};
  (state.compareData ? state.compareData.groups : []).forEach((g, i) => { groupIndex[g.key] = i; });
  const results = await Promise.all(picked.map(async ({ id, group }) => {
    try {
      const r = await api(`/api/experiments/${encodeURIComponent(id)}/curves?tags=${encodeURIComponent(tag)}`);
      return { id, group, points: (r.series && r.series[tag]) || [] };
    } catch (e) { return { id, group, points: [] }; }
  }));
  if (state.compareCurvesChart) state.compareCurvesChart.destroy();
  state.compareCurvesChart = new Chart(document.getElementById("compare-curves-chart").getContext("2d"), {
    type: "line",
    data: {
      datasets: results.filter((r) => r.points.length).map((r) => ({
        label: r.id, data: r.points.map(([step, value]) => ({ x: step, y: value })),
        borderColor: colors[groupIndex[r.group] % colors.length] || CHART_COLORS[0],
        backgroundColor: "transparent", pointRadius: 0, borderWidth: 1.5, tension: 0.15,
      })),
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      parsing: false,
      plugins: { legend: { labels: { color: "#8C97B0", font: { size: 9 } } } },
      scales: {
        x: { type: "linear", ticks: { color: "#8C97B0", font: { size: 9 } }, grid: { color: "#1B2740" } },
        y: { ticks: { color: "#5C6785", font: { size: 9 } }, grid: { color: "#1B2740" } },
      },
    },
  });
}

// ---- seed × fold heatmap, reusing heatmap-grid.js's renderSeedFoldGrid()
// (the Runs tab's own seed/fold status grid). Fold is parsed off the
// planned/collected run id (`R-<hash7>-s<seed>-f<fold|->` — §4.4); status
// comes from state.experiments2, already loaded for the main table.
function foldFromRunId(runId) {
  const m = /-f([^-]*)$/.exec(runId || "");
  return m && m[1] ? m[1] : null;
}

function renderCompareHeatmap(data) {
  const panel = document.getElementById("compare-heatmap-panel");
  const byId = {};
  for (const e of state.experiments2) byId[e.experiment_id] = e;
  const runs = [];
  for (const g of data.groups) {
    for (const eid of g.experiments) {
      const e = byId[eid];
      if (!e) continue;
      const runIds = (e.current_attempt && e.current_attempt.run && e.current_attempt.run.run_ids) || [];
      const runId = runIds[0] || null;
      runs.push({ seed: e.seed, fold: foldFromRunId(runId), status: e.status, run_id: runId || eid, _group: g.key });
    }
  }
  if (!runs.length) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  const groupsSeen = Array.from(new Set(runs.map((r) => r._group)));
  document.getElementById("compare-heatmap-body").innerHTML = groupsSeen.map((gname) => `
    <div style="margin-bottom:14px;">
      <div class="settings-profile-path" style="margin-bottom:4px;">${escapeHtml(gname)}</div>
      ${renderSeedFoldGrid(runs.filter((r) => r._group === gname))}
    </div>`).join("");
}

// ================================================================== Compare export (CSV / Markdown / LaTeX)
// All three build straight off the already-fetched state.compareData —
// client-side, no new export route (XDASH_PLAN.md §8.2: "client-side
// generation from the already-fetched data is fine").
function downloadTextFile(filename, text) {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function compareCellText(g, m) {
  const s = g.metrics[m];
  if (!s || s.mean == null) return "";
  return s.n > 1 ? `${fmtNum(s.mean)} ± ${fmtNum(s.std)}` : `${fmtNum(s.mean)}`;
}

function exportCompareCSV() {
  const data = state.compareData;
  if (!data) return;
  const header = ["group", "n"].concat(data.metrics);
  const rows = [header];
  for (const g of data.groups) {
    rows.push([g.key, `${g.with_report}/${g.n}`].concat(data.metrics.map((m) => compareCellText(g, m))));
  }
  const csv = rows.map((r) => r.map((c) => /[,"\n]/.test(String(c)) ? `"${String(c).replace(/"/g, '""')}"` : c).join(",")).join("\n");
  downloadTextFile("compare.csv", csv);
}

function exportCompareMarkdown() {
  const data = state.compareData;
  if (!data) return;
  const header = ["Group", "n"].concat(data.metrics);
  let out = `| ${header.join(" | ")} |\n| ${header.map(() => "---").join(" | ")} |\n`;
  for (const g of data.groups) {
    out += `| ${[g.key + (g.baseline ? " *(baseline)*" : ""), `${g.with_report}/${g.n}`].concat(data.metrics.map((m) => compareCellText(g, m) || "—")).join(" | ")} |\n`;
  }
  downloadTextFile("compare.md", out);
}

function exportCompareLatex() {
  const data = state.compareData;
  if (!data) return;
  const cols = "l" + "r".repeat(1 + data.metrics.length);
  let out = `\\begin{table}[h]\n\\centering\n\\begin{tabular}{${cols}}\n\\toprule\n`;
  out += `Group & n & ${data.metrics.map((m) => m.replace(/_/g, "\\_")).join(" & ")} \\\\\n\\midrule\n`;
  for (const g of data.groups) {
    const label = (g.key + (g.baseline ? " (baseline)" : "")).replace(/_/g, "\\_");
    out += `${label} & ${g.with_report}/${g.n} & ${data.metrics.map((m) => compareCellText(g, m).replace(/±/g, "$\\pm$") || "--").join(" & ")} \\\\\n`;
  }
  out += `\\bottomrule\n\\end{tabular}\n\\caption{Study comparison}\n\\end{table}\n`;
  downloadTextFile("compare.tex", out);
}

// ================================================================== Composer
async function openComposer(prefill) {
  prefill = prefill || {};
  state.composerSelectedConfigs = new Set(prefill.configPaths || []);
  state.composerOverlayRows = [{ key: "", value: "" }];
  state.composerPreflight = null;
  state.composerRowRuntime = new Map();
  document.getElementById("composer-config-filter").value = "";
  document.getElementById("composer-seeds").value = "42";
  document.getElementById("composer-group").value = "";
  document.getElementById("composer-runtime-mode").value = "auto";
  document.getElementById("composer-pin-field").classList.add("hidden");
  // Reachable before the Experiments screen has ever loaded runtimes (e.g. straight
  // from Configs Browse's "Create experiments from this config →") — fetch once if so,
  // rather than opening with an empty "Pin to…" list.
  if (!state.runtimesLoaded) {
    try { state.runtimesList = (await api("/api/runtimes")).runtimes || []; state.runtimesLoaded = true; } catch (e) { /* Pin stays empty; Auto still works */ }
  }
  populateComposerStudySelect();
  populateComposerPinSelect();
  if (prefill.studyId) document.getElementById("composer-study-select").value = prefill.studyId;
  else document.getElementById("composer-study-select").value = state.selectedStudyId || "";
  renderComposerConfigList();
  renderComposerOverlayRows();
  renderComposerPreflight();
  document.getElementById("composer-backdrop").classList.remove("hidden");
}

function closeComposer() {
  document.getElementById("composer-backdrop").classList.add("hidden");
  if (state.composerPreflightTimer) clearTimeout(state.composerPreflightTimer);
}

function populateComposerStudySelect() {
  const sel = document.getElementById("composer-study-select");
  sel.innerHTML = `<option value="">— none (unfiled) —</option>` + state.studies.map((s) => `<option value="${escapeHtml(s.study_id)}">${escapeHtml(s.name)}</option>`).join("");
}

function populateComposerPinSelect() {
  const sel = document.getElementById("composer-pin-select");
  sel.innerHTML = state.runtimesList.map((r) => `<option value="${escapeHtml(r.id)}">${escapeHtml(r.label)}</option>`).join("");
}

function renderComposerConfigList() {
  const body = document.getElementById("composer-config-list");
  const countEl = document.getElementById("composer-config-count");
  const filter = (document.getElementById("composer-config-filter").value || "").trim().toLowerCase();
  const all = [];
  for (const group of state.configs) for (const c of group.configs) all.push(c);
  const matches = filter ? all.filter((c) => c.name.toLowerCase().includes(filter) || c.path.toLowerCase().includes(filter)) : all;
  countEl.textContent = `${state.composerSelectedConfigs.size} selected`;
  body.innerHTML = matches.length ? matches.map((c) => `
    <label class="config-item" style="cursor:pointer;">
      <input type="checkbox" data-composer-config="${escapeHtml(c.path)}" ${state.composerSelectedConfigs.has(c.path) ? "checked" : ""} />
      <span>${escapeHtml(c.name)}</span>
    </label>`).join("") : `<div class="empty-state">No configs match.</div>`;
  body.querySelectorAll("[data-composer-config]").forEach((cb) => {
    cb.addEventListener("change", () => {
      if (cb.checked) state.composerSelectedConfigs.add(cb.dataset.composerConfig);
      else state.composerSelectedConfigs.delete(cb.dataset.composerConfig);
      document.getElementById("composer-config-count").textContent = `${state.composerSelectedConfigs.size} selected`;
      scheduleComposerPreflight();
    });
  });
}

function renderComposerOverlayRows() {
  const body = document.getElementById("composer-overlay-rows");
  body.innerHTML = state.composerOverlayRows.map((row, i) => `
    <div class="job-actions" style="margin-bottom:6px;">
      <input class="text-input" data-overlay-key="${i}" placeholder="training.epochs" value="${escapeHtml(row.key)}" style="width:45%;" autocomplete="off" />
      <input class="text-input grow" data-overlay-value="${i}" placeholder="50" value="${escapeHtml(row.value)}" autocomplete="off" />
      <button class="btn btn-sm btn-ghost" data-overlay-remove="${i}" type="button">✕</button>
    </div>`).join("");
  body.querySelectorAll("[data-overlay-key]").forEach((inp) => inp.addEventListener("input", () => { state.composerOverlayRows[Number(inp.dataset.overlayKey)].key = inp.value; scheduleComposerPreflight(); }));
  body.querySelectorAll("[data-overlay-value]").forEach((inp) => inp.addEventListener("input", () => { state.composerOverlayRows[Number(inp.dataset.overlayValue)].value = inp.value; scheduleComposerPreflight(); }));
  body.querySelectorAll("[data-overlay-remove]").forEach((btn) => btn.addEventListener("click", () => {
    state.composerOverlayRows.splice(Number(btn.dataset.overlayRemove), 1);
    if (!state.composerOverlayRows.length) state.composerOverlayRows.push({ key: "", value: "" });
    renderComposerOverlayRows();
    scheduleComposerPreflight();
  }));
}

function composerSeeds() {
  const raw = (document.getElementById("composer-seeds").value || "").trim();
  if (!raw) return [null];
  const seeds = raw.split(",").map((s) => s.trim()).filter(Boolean).map((s) => (isNaN(Number(s)) ? s : Number(s)));
  return seeds.length ? seeds : [null];
}

// dotted-key -> typed value; "50" -> 50, "true"/"false" -> boolean, else string —
// mirrors backend/framework.py's normalize_overlay(), which accepts any JSON
// scalar, not just strings; this just spares the user from typing quotes.
function composerOverlay() {
  const overlay = {};
  for (const row of state.composerOverlayRows) {
    const key = row.key.trim();
    if (!key) continue;
    let value = row.value;
    if (value === "true") value = true;
    else if (value === "false") value = false;
    else if (value.trim() !== "" && !isNaN(Number(value))) value = Number(value);
    overlay[key] = value;
  }
  return overlay;
}

function composerRuntime() {
  const mode = document.getElementById("composer-runtime-mode").value;
  if (mode === "pinned") return { mode: "pinned", slot: document.getElementById("composer-pin-select").value };
  return { mode: "auto" };
}

// XDASH_FIXES_PLAN.md F4.2: the "Set all rows to…" control's value, in the
// same "auto" | <runtime id> spelling every per-row select and
// setExperimentRuntime() use — the default a row falls back to when it has
// no override of its own in state.composerRowRuntime.
function composerBulkRuntimeValue() {
  const mode = document.getElementById("composer-runtime-mode").value;
  return mode === "pinned" ? document.getElementById("composer-pin-select").value : "auto";
}

// F4.1: a row's own pick if the user set one by hand, else the bulk
// control's current value — "per-row edits made after it win" (F4.2).
function composerRowRuntimeValue(experimentId) {
  return state.composerRowRuntime.has(experimentId) ? state.composerRowRuntime.get(experimentId) : composerBulkRuntimeValue();
}

function scheduleComposerPreflight() {
  if (state.composerPreflightTimer) clearTimeout(state.composerPreflightTimer);
  state.composerPreflightTimer = setTimeout(renderComposerPreflight, 350);
}

// F4.2: the "Set all rows to…" control changed — every row collapses onto
// that value (clearing any per-row overrides made before this), then a
// fresh preflight actually re-runs (the bug this fixes: today only the
// mode select re-preflights, never the pin select, so picking a different
// account left the table showing stale ✓/✗ and `best`/`reason` for the old
// one).
function composerBulkRuntimeChanged() {
  state.composerRowRuntime.clear();
  scheduleComposerPreflight();
}

async function renderComposerPreflight() {
  const el = document.getElementById("composer-preflight");
  const configs = Array.from(state.composerSelectedConfigs);
  if (!configs.length) {
    el.innerHTML = `<span class="preflight-empty">Pick at least one config to see a preview.</span>`;
    state.composerPreflight = null;
    return;
  }
  const seeds = composerSeeds();
  const overlay = composerOverlay();
  const runtime = composerRuntime();
  const specs = [];
  for (const path of configs) for (const seed of seeds) specs.push({ config_path: path, seed, overlay, runtime });
  el.innerHTML = `<span class="preflight-empty">Checking ${specs.length} experiment(s)…</span>`;
  let data;
  try {
    data = await api("/api/experiments/preflight", { method: "POST", body: JSON.stringify({ specs }) });
  } catch (e) {
    el.innerHTML = `<span class="preflight-empty">Couldn't preflight: ${escapeHtml(e.message)}</span>`;
    return;
  }
  state.composerPreflight = data;
  // Rows this response no longer carries (a config/seed/overlay edit
  // dropped or renamed them) leave nothing behind to key an override by.
  const liveIds = new Set(data.rows.map((r) => r.experiment_id));
  for (const id of Array.from(state.composerRowRuntime.keys())) if (!liveIds.has(id)) state.composerRowRuntime.delete(id);
  renderComposerPreflightTable();
}

// F4.1: one option per runtime, ✓/✗ from the preflight response's own
// `cells[runtime.id]` (block code inline, full detail on hover) — the same
// matrix every row already got, so picking a different row option is free.
function composerRuntimeOptionHtml(rt, cell, selectedValue) {
  const ok = cell ? cell.ok : true;
  const mark = ok ? "✓" : "✗";
  const codeSuffix = ok ? "" : ` (${escapeHtml(cell.code || "blocked")})`;
  const title = (cell && cell.detail) || "";
  return `<option value="${escapeHtml(rt.id)}" title="${escapeHtml(title)}" ${selectedValue === rt.id ? "selected" : ""}>${mark} ${escapeHtml(rt.label)}${codeSuffix}</option>`;
}

// F4.1/F4.2: builds the preflight table (and its per-row Runtime selects)
// from the cached state.composerPreflight — never an HTTP call by itself.
// Called after a real preflight fetch, and again whenever a row's own
// select changes (composerRowRuntimeValue() is all that changed).
function renderComposerPreflightTable() {
  const el = document.getElementById("composer-preflight");
  const data = state.composerPreflight;
  if (!data) return;
  el.innerHTML = `<table class="compare-table" style="width:100%;">
    <thead><tr><th>id</th><th>exists</th><th>est.</th><th>Runtime</th></tr></thead>
    <tbody>${data.rows.map((r) => {
      const selected = composerRowRuntimeValue(r.experiment_id);
      const autoLabel = r.best ? `Auto → ${escapeHtml(r.best)}` : `Auto → none${r.reason ? `: ${escapeHtml(r.reason)}` : ""}`;
      const options = [`<option value="auto" ${selected === "auto" ? "selected" : ""}>${autoLabel}</option>`]
        .concat((data.runtimes || []).map((rt) => composerRuntimeOptionHtml(rt, r.cells && r.cells[rt.id], selected)))
        .join("");
      return `<tr>
      <td style="font-family:var(--mono); font-size:11px;">${escapeHtml(r.experiment_id)}</td>
      <td>${r.exists ? `<span class="badge slate" title="Re-posting matches this existing experiment instead of creating a new one">matches ${escapeHtml(r.status || "")}</span>` : `<span class="badge emerald">new</span>`}</td>
      <td>${fmtNum(r.estimate && r.estimate.hours)}h</td>
      <td><select class="text-input" data-composer-row-runtime="${escapeHtml(r.experiment_id)}" style="font-size:11px; padding:3px 5px;">${options}</select></td>
    </tr>`;
    }).join("")}</tbody>
  </table>`;
  el.querySelectorAll("[data-composer-row-runtime]").forEach((sel) => {
    sel.addEventListener("change", () => state.composerRowRuntime.set(sel.dataset.composerRowRuntime, sel.value));
  });
}

// F4.3: the experiment_id a preflight row (config_path, seed) resolved to
// — how submitComposer() attaches each row's own Runtime pick to the
// matching configs[] entry it's about to submit. null before a preflight
// has ever run (e.g. the request never finished, or was never scheduled) —
// callers fall back to the bulk value for that row.
function composerRowExperimentId(configPath, seed) {
  const rows = (state.composerPreflight && state.composerPreflight.rows) || [];
  const row = rows.find((r) => r.config_path === configPath && String(r.seed ?? "") === String(seed ?? ""));
  return row ? row.experiment_id : null;
}

// "auto" | <runtime id> (a per-row select's value, or composerBulkRuntimeValue())
// -> the §6.3 policy shape POST /api/experiments' configs[].runtime expects.
function composerRuntimeFromValue(value) {
  return value === "auto" ? { mode: "auto" } : { mode: "pinned", slot: value };
}

async function submitComposer(then) {
  const configs = Array.from(state.composerSelectedConfigs);
  if (!configs.length) { toast("Pick at least one config", "err"); return; }
  if (state.composerBusy) return;
  state.composerBusy = true;
  const seeds = composerSeeds();
  // F4.3: always one configs[] entry per (config, seed) row — every row in
  // the preflight table maps 1:1 to one entry here, so its own Runtime pick
  // (composerRowRuntimeValue(), which already fell back to the bulk "Set
  // all rows to…" value if the row was never touched by hand) travels with
  // it as that entry's `runtime` override. Sent for every row, not only the
  // ones that differ from the request-level default — simpler than tracking
  // "did the user actually touch this one," and harmless: the backend only
  // ever *acts* on a matched row's override when it differs from what that
  // experiment already has stored (backend/experiments.py's create_experiments).
  const entryConfigs = [];
  for (const path of configs) {
    for (const seed of seeds) {
      const eid = composerRowExperimentId(path, seed);
      const value = eid ? composerRowRuntimeValue(eid) : composerBulkRuntimeValue();
      entryConfigs.push({ path, seeds: [seed], runtime: composerRuntimeFromValue(value) });
    }
  }
  const body = {
    configs: entryConfigs, overlay: composerOverlay(), runtime: composerRuntime(),
    study_id: document.getElementById("composer-study-select").value || null,
    group: document.getElementById("composer-group").value.trim() || null,
  };
  if (then) body.then = then;
  try {
    const result = await api("/api/experiments", { method: "POST", body: JSON.stringify(body) });
    const n = (result.created || []).length, m = (result.matched || []).length;
    toast(`${n} created${m ? `, ${m} matched existing` : ""}`, "ok");
    // F4.3: a matched row whose per-row Runtime pick differed from what
    // that experiment already had applies set_runtime backend-side —
    // reuses F0.1's exact {ok, skipped} shape, so the same
    // reportActionResult/"why not" panel covers it (e.g. it was in flight
    // and got refused instead).
    //
    // F5 carry-over fix: `result.runtime_changes` and `result.then` can
    // both carry skips from the very same submit (a matched row that's
    // simultaneously in flight *and* independently refused by Queue/Run
    // now). Both calls share one collector array instead of each opening
    // its own "why not" panel — the second no longer silently overwrites
    // the first — and a single merged panel opens once, after both, only
    // if either one actually found anything to report.
    const skipReportEntries = [];
    const runtimeOk = reportActionResult(result.runtime_changes, "update runtime", skipReportEntries);
    // reportActionResult (F0.1) toasts result.then's own skipped entries;
    // its return says whether every one of them actually dispatched/queued.
    const allOk = then ? reportActionResult(result.then, then.replace("_", " "), skipReportEntries) : true;
    if (skipReportEntries.length) openSkipReport(null, skipReportEntries);
    if (then === "run_now" && !allOk) {
      // Composer Run now with skips: stays open (the panel it just opened
      // says why), and the skipped ones are drafts, not gone.
      toast("Saved as drafts — see why not run now", "err");
    } else if (!runtimeOk) {
      // A matched row's runtime change was refused — stays open next to
      // the merged "why" panel above.
    } else {
      closeComposer();
    }
    refreshExperiments2AllData();
  } catch (e) {
    toast("Couldn't create experiments: " + e.message, "err");
  } finally {
    state.composerBusy = false;
  }
}

// ================================================================== init (subtabs, buttons, modals)
function experiments2SubtabClick(key) {
  // A plain subtab click (as opposed to openCompareAdhoc()'s own navigation)
  // always means "this study's real Compare", never a lingering ad-hoc
  // selection from an earlier "Compare selected" — see openCompareAdhoc().
  if (key === "compare") state.compareAdhocIds = null;
  navigateToView("experiments", { study: state.selectedStudyId || undefined, tab: key });
}

function initExperiments2Buttons() {
  document.querySelectorAll("#experiments-subtabs .subtab-btn").forEach((btn) => {
    btn.addEventListener("click", () => experiments2SubtabClick(btn.dataset.subtab));
  });

  document.getElementById("skip-report-close").addEventListener("click", closeSkipReport);
  document.getElementById("skip-report-backdrop").addEventListener("click", (e) => { if (e.target.id === "skip-report-backdrop") closeSkipReport(); });

  document.getElementById("btn-new-study").addEventListener("click", () => openStudyEditModal(null));
  document.getElementById("btn-study-edit").addEventListener("click", () => openStudyEditModal(selectedStudy()));
  document.getElementById("btn-study-autopilot").addEventListener("click", toggleSelectedStudyAutopilot);
  document.getElementById("study-filter").addEventListener("input", (e) => { state.studyFilter = e.target.value; renderStudiesList(); });

  document.getElementById("study-edit-cancel").addEventListener("click", closeStudyEditModal);
  document.getElementById("study-edit-save").addEventListener("click", saveStudyEdit);
  document.getElementById("study-edit-delete").addEventListener("click", deleteStudyEdit);
  document.getElementById("study-edit-backdrop").addEventListener("click", (e) => { if (e.target.id === "study-edit-backdrop") closeStudyEditModal(); });

  document.getElementById("experiments-search").addEventListener("input", (e) => { state.experiments2Search = e.target.value; renderExperiments2Table(); });
  document.getElementById("experiments-select-all").addEventListener("change", (e) => {
    const ids = filteredExperiments2().map((v) => v.experiment_id);
    if (e.target.checked) ids.forEach((id) => state.experiments2Selected.add(id));
    else ids.forEach((id) => state.experiments2Selected.delete(id));
    renderExperiments2Table();
  });

  document.getElementById("btn-open-composer").addEventListener("click", () => openComposer());
  document.getElementById("composer-cancel").addEventListener("click", closeComposer);
  document.getElementById("composer-backdrop").addEventListener("click", (e) => { if (e.target.id === "composer-backdrop") closeComposer(); });
  document.getElementById("composer-config-filter").addEventListener("input", renderComposerConfigList);
  document.getElementById("composer-seeds").addEventListener("input", scheduleComposerPreflight);
  document.getElementById("btn-composer-add-overlay-row").addEventListener("click", () => { state.composerOverlayRows.push({ key: "", value: "" }); renderComposerOverlayRows(); });
  document.getElementById("composer-runtime-mode").addEventListener("change", (e) => {
    document.getElementById("composer-pin-field").classList.toggle("hidden", e.target.value !== "pinned");
    composerBulkRuntimeChanged();
  });
  // F4.2 fix: changing the pin target itself used to never re-run preflight
  // or re-apply to the rows — only the mode select had a listener at all.
  document.getElementById("composer-pin-select").addEventListener("change", composerBulkRuntimeChanged);
  document.getElementById("composer-save-draft").addEventListener("click", () => submitComposer(null));
  document.getElementById("composer-queue").addEventListener("click", () => submitComposer("queue"));
  document.getElementById("composer-run-now").addEventListener("click", () => submitComposer("run_now"));

  document.getElementById("compare-study-select").addEventListener("change", loadCompareTab);
  document.getElementById("compare-group-by").addEventListener("change", loadCompareTab);
  document.getElementById("btn-compare-refresh").addEventListener("click", loadCompareTab);
  document.getElementById("btn-compare-clear-adhoc").addEventListener("click", clearCompareAdhoc);
  document.getElementById("compare-metric-select").addEventListener("change", (e) => {
    state.compareMetric = e.target.value;
    if (state.compareData) renderCompareCharts(state.compareData);
  });
  document.getElementById("compare-curves-tag-select").addEventListener("change", async (e) => {
    state.compareCurvesTag = e.target.value;
    if (state.compareData) {
      const picked = [];
      for (const g of state.compareData.groups) for (const id of g.experiments) picked.push({ id, group: g.key });
      await renderCompareCurvesChart(picked.slice(0, COMPARE_CURVES_CAP), state.compareCurvesTag);
    }
  });
  document.getElementById("btn-compare-export-csv").addEventListener("click", exportCompareCSV);
  document.getElementById("btn-compare-export-md").addEventListener("click", exportCompareMarkdown);
  document.getElementById("btn-compare-export-latex").addEventListener("click", exportCompareLatex);

  document.getElementById("btn-config-create-experiments").addEventListener("click", () => {
    if (!state.selectedConfigPath) return;
    switchToSubtab("experiments", "experiments-subtabs", "experiments");
    openComposer({ configPaths: [state.selectedConfigPath] });
  });
}

initExperiments2Buttons();

// ============================================================================
// The Experiment page — #/x/<experiment_id>/<tab> (XDASH_PLAN.md §8.3)
// ============================================================================
// Reached only through js/lib/router.js's handleRoute(), which calls
// showExperimentDetailView() once and then loadExperimentDetail(id, tab) on
// every hashchange (including a plain tab switch within the same
// experiment) — so this file never toggles #view-experiment-detail's own
// `.active` class itself outside of showExperimentDetailView(), the same
// division of responsibility switchView() and this screen's own subtab
// clicks keep everywhere else.

async function fetchExperimentDetail(id) {
  const view = await api(`/api/experiments/${encodeURIComponent(id)}`);
  state.xdetailView = view;
  return view;
}

function showExperimentDetailView() {
  document.querySelectorAll(".nav-item").forEach((el) => el.classList.remove("active"));
  document.querySelectorAll(".view").forEach((el) => el.classList.toggle("active", el.id === "view-experiment-detail"));
  // Leaving Lab/Experiments for this route the same way switchView() would
  // if this were a nav-item route (it deliberately isn't — see the note on
  // #view-experiment-detail in index.html).
  stopLabPolling();
  stopExperiments2Polling();
}

function stopExperimentDetailPolling() {
  if (state.xdetailPoller) state.xdetailPoller.stop();
}

function activateXdetailSubtab(tab) {
  document.querySelectorAll("#xdetail-subtabs .subtab-btn").forEach((b) => b.classList.toggle("active", b.dataset.subtab === tab));
  document.querySelectorAll("#view-experiment-detail > .subtab-panel").forEach((p) => p.classList.toggle("active", p.dataset.subtab === tab));
}

async function loadExperimentDetail(experimentId, tab) {
  const isNewExperiment = state.xdetailId !== experimentId;
  state.xdetailId = experimentId;
  state.xdetailTab = tab || "overview";
  activateXdetailSubtab(state.xdetailTab);
  if (isNewExperiment) {
    state.xdetailView = null;
    state.xdetailLogStage = null;
    document.getElementById("xdetail-title").textContent = experimentId;
    document.getElementById("xdetail-sub").textContent = "Loading…";
    document.getElementById("xdetail-actions").innerHTML = "";
  }
  try {
    await fetchExperimentDetail(experimentId);
  } catch (e) {
    document.getElementById("xdetail-sub").textContent = `Couldn't load: ${e.message}`;
    return;
  }
  renderXdetailHeader();
  renderXdetailTab(state.xdetailTab);
  if (!state.xdetailPoller) state.xdetailPoller = createPoller(refreshXdetail, 5000);
  state.xdetailPoller.start();
}

async function refreshXdetail() {
  if (!state.xdetailId) return;
  await fetchExperimentDetail(state.xdetailId);
  renderXdetailHeader();
  renderXdetailTab(state.xdetailTab);
}

function renderXdetailHeader() {
  const v = state.xdetailView;
  if (!v) return;
  document.getElementById("xdetail-title").textContent = v.name || v.experiment_id;
  document.getElementById("xdetail-sub").innerHTML =
    `${escapeHtml(v.experiment_id)} · ${escapeHtml(v.config_path)} · seed ${v.seed === null || v.seed === undefined ? "—" : escapeHtml(String(v.seed))}`;
  document.getElementById("xdetail-actions").innerHTML = renderStatusBadge(v.status) + " " + experiments2RowActionsHtml(v);
  document.querySelectorAll("#xdetail-actions button[data-row-action]").forEach((btn) => {
    btn.addEventListener("click", () => runXdetailAction(btn.dataset.rowAction, btn.dataset.id));
  });
  // The active tab's own render is renderXdetailTab()'s job (called right
  // after this by every caller) — not duplicated here.
}

async function runXdetailAction(action, id) {
  if (action === "delete") { openDeleteExperimentModal(id, () => navigateToView("experiments")); return; }
  try {
    const result = await api("/api/experiments/actions", { method: "POST", body: JSON.stringify({ action, ids: [id] }) });
    reportActionResult(result, action.replace("_", " "));
    refreshXdetail();
  } catch (e) {
    toast(`Couldn't ${action}: ${e.message}`, "err");
  }
}

function renderXdetailTab(tab) {
  const v = state.xdetailView;
  if (!v) return;
  if (tab === "overview") renderXdetailOverview(v);
  else if (tab === "live") loadXdetailLive();
  else if (tab === "metrics") loadXdetailMetrics();
  else if (tab === "artifacts") loadXdetailArtifacts();
  else if (tab === "config") renderXdetailConfig(v);
  else if (tab === "history") renderXdetailHistory(v);
  else if (tab === "notes") {
    // XDASH_FIXES_PLAN.md F1.2 — the 5s poller (refreshXdetail) calls this
    // same renderXdetailTab() on every tick; without the guard it wiped
    // whatever the user was mid-typing into the notes textarea (or the run
    // tag/note fields) every 5s (§2/#6's "same bug class elsewhere").
    if (!formPollGuard("xdetail-notes-panel")) renderXdetailNotes(v);
  }
}

// ---------------------------------------------------------------- Overview
// XDASH_FIXES_PLAN.md F0.2 — the Overview's own rendering of a.blocked (the
// table's blockedDetailHtml above uses a hover title for the detail, fine
// for a dense row; this page has the room to show it plainly instead).
function blockedOverviewHtml(blocked) {
  if (!blocked) return "—";
  return `<span class="badge red">${escapeHtml(blocked.code || "blocked")}</span> ${escapeHtml(blocked.detail || "")}` +
    (blocked.action ? `<div class="settings-profile-path" style="font-family:var(--mono); white-space:normal; margin-top:4px;">${escapeHtml(blocked.action)}</div>` : "");
}

function renderXdetailOverview(v) {
  const a = v.current_attempt || {};
  const code = a.code || {};
  const run = a.run || {};
  const rows = [
    ["Experiment id", v.experiment_id],
    ["Name", v.name || "—"],
    ["Config", v.config_path],
    ["Seed", v.seed === null || v.seed === undefined ? "—" : v.seed],
    ["Status", v.status],
    ["Studies", (v.studies || []).map((m) => m.name || m.study_id).join(", ") || "(unfiled)"],
    ["Priority", v.priority ?? 0],
    ["Runtime policy", v.runtime.mode === "pinned" ? `pinned: ${v.runtime.slot}` : "auto"],
    ["Placed on", a.slot || "—"],
    ["Attempt", a.attempt_id ? `${a.attempt_id} (leg ${a.leg_index}) — ${v.attempt_count} total` : "none yet"],
    ["Started", a.started_at ? timeAgo(a.started_at) : "—"],
    ["Ended", a.ended_at ? timeAgo(a.ended_at) : "—"],
    ["Code", code.commit ? `${String(code.commit).slice(0, 10)}${code.dirty ? " (dirty)" : ""}${code.pushed === false ? " (not pushed)" : ""}` : "—"],
    ["Run dir", run.collected_dir || run.run_dir || "—"],
  ];
  const html = rows.map(([k, val]) => `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(String(val))}</td></tr>`).join("")
    + (a.blocked ? `<tr><td>Blocked</td><td>${blockedOverviewHtml(a.blocked)}</td></tr>` : "");
  document.getElementById("xdetail-overview-kv").innerHTML = html;
}

// ---------------------------------------------------------------- Live
function renderXdetailLiveStages(v) {
  const a = v.current_attempt || {};
  const el = document.getElementById("xdetail-live-stages");
  const stages = a.stages || [];
  const blocked = a.blocked
    ? `<div class="kaggle-history-row"><span class="badge red">${escapeHtml(a.blocked.code || "blocked")}</span> ${escapeHtml(a.blocked.detail || "")}</div>`
    : "";
  el.innerHTML = (stages.length
    ? stages.map((s) => `<span class="badge ${statusBadgeClass(s.status)}">${escapeHtml(s.name)}: ${escapeHtml(s.status)}</span>`).join(" ")
    : `<div class="empty-state">No attempt in flight.</div>`) + blocked;
}

async function loadXdetailLive() {
  const v = state.xdetailView;
  renderXdetailLiveStages(v);
  const attempt = v.current_attempt || {};
  const attemptId = attempt.attempt_id;
  const btns = document.getElementById("xdetail-log-stage-buttons");
  const body = document.getElementById("xdetail-log-body");
  if (!attemptId) { btns.innerHTML = ""; body.textContent = "No attempt yet."; return; }
  let log;
  try {
    const qs = state.xdetailLogStage ? `&stage=${encodeURIComponent(state.xdetailLogStage)}` : "";
    log = await api(`/api/experiments/${encodeURIComponent(state.xdetailId)}/log?attempt_id=${encodeURIComponent(attemptId)}${qs}`);
  } catch (e) {
    body.textContent = `Couldn't load log: ${e.message}`;
    return;
  }
  state.xdetailLogStage = log.stage;
  btns.innerHTML = (log.available_stages.length ? log.available_stages : [log.stage]).map((s) =>
    `<button class="btn btn-sm ${s === log.stage ? "btn-primary" : "btn-ghost"}" data-log-stage="${escapeHtml(s)}">${escapeHtml(s)}</button>`
  ).join("");
  btns.querySelectorAll("[data-log-stage]").forEach((b) => b.addEventListener("click", () => {
    state.xdetailLogStage = b.dataset.logStage;
    loadXdetailLive();
  }));
  body.textContent = log.exists ? log.text : `(no ${log.stage}.log yet — ${log.path})`;
}

// ---------------------------------------------------------------- Metrics
// Reuses metricCardHtml() (app.js) — the same per-metric card the Reports
// tab renders, so a metric here looks exactly like it does everywhere else.
async function loadXdetailMetrics() {
  const body = document.getElementById("xdetail-metrics-body");
  const src = document.getElementById("xdetail-metrics-source");
  body.innerHTML = `<div class="empty-state">Loading…</div>`;
  let data;
  try {
    data = await api(`/api/experiments/${encodeURIComponent(state.xdetailId)}/report`);
  } catch (e) {
    body.innerHTML = `<div class="empty-state">Couldn't load metrics: ${escapeHtml(e.message)}</div>`;
    return;
  }
  if (!data.metrics) {
    body.innerHTML = `<div class="empty-state">No eval report on disk yet for this experiment.</div>`;
    if (src) src.textContent = "";
    return;
  }
  if (src) src.textContent = data.report_path || "";
  body.innerHTML = Object.entries(data.metrics).map(([k, val]) => metricCardHtml(k, val, data.metrics)).join("");
}

// ---------------------------------------------------------------- Artifacts
async function loadXdetailArtifacts() {
  const body = document.getElementById("xdetail-artifacts-body");
  const countEl = document.getElementById("xdetail-artifacts-count");
  body.innerHTML = `<tr><td colspan="3" class="empty-state">Loading…</td></tr>`;
  let data;
  try {
    data = await api(`/api/experiments/${encodeURIComponent(state.xdetailId)}/artifacts`);
  } catch (e) {
    body.innerHTML = `<tr><td colspan="3" class="empty-state">Couldn't load: ${escapeHtml(e.message)}</td></tr>`;
    return;
  }
  if (countEl) countEl.textContent = data.exists ? `${data.files.length} file${data.files.length === 1 ? "" : "s"}` : "no run dir yet";
  if (!data.files.length) {
    body.innerHTML = `<tr><td colspan="3" class="empty-state">${data.exists ? "No files yet." : "This experiment has no run directory on disk yet."}</td></tr>`;
    return;
  }
  const base = `/api/experiments/${encodeURIComponent(state.xdetailId)}/artifacts/`;
  body.innerHTML = data.files.map((f) => `<tr>
    <td style="font-family:var(--mono); font-size:11px;">${escapeHtml(f.rel_path)}</td>
    <td>${(f.size / 1024).toFixed(1)} KB</td>
    <td><a class="btn btn-sm btn-ghost" href="${base}${f.rel_path.split("/").map(encodeURIComponent).join("/")}" target="_blank" rel="noopener">Open ↗</a></td>
  </tr>`).join("");
}

// ---------------------------------------------------------------- Config
function renderXdetailConfig(v) {
  document.getElementById("xdetail-config-path").textContent = v.config_path;
  document.getElementById("xdetail-config-kv").innerHTML = [
    ["Config path", v.config_path], ["Seed", v.seed === null || v.seed === undefined ? "—" : v.seed],
  ].map(([k, val]) => `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(String(val))}</td></tr>`).join("");
  const overlay = v.overlay || {};
  const keys = Object.keys(overlay);
  document.getElementById("xdetail-overlay-body").innerHTML = keys.length
    ? keys.map((k) => `<tr><td style="font-family:var(--mono);">${escapeHtml(k)}</td><td style="font-family:var(--mono);">${escapeHtml(String(overlay[k]))}</td></tr>`).join("")
    : `<tr><td class="empty-state">No overrides — runs the base config as-is.</td></tr>`;
}

// ---------------------------------------------------------------- History
function renderXdetailHistory(v) {
  const body = document.getElementById("xdetail-history-body");
  const countEl = document.getElementById("xdetail-history-count");
  const attempts = v.attempts || [];
  if (countEl) countEl.textContent = `${attempts.length} attempt${attempts.length === 1 ? "" : "s"}`;
  body.innerHTML = attempts.length ? attempts.map((a) => `<tr>
    <td>${a.attempt_index}</td>
    <td>${a.leg_index}${a.resume_of ? " (resumed)" : ""}</td>
    <td>${renderStatusBadge(a.status)}</td>
    <td>${escapeHtml(a.queued_by || "")}</td>
    <td>${escapeHtml(timeAgo(a.started_at))}</td>
    <td>${escapeHtml(timeAgo(a.ended_at))}</td>
    <td>${escapeHtml(a.slot || "—")}</td>
  </tr>`).join("") : `<tr><td colspan="7" class="empty-state">No attempts yet.</td></tr>`;
}

// ---------------------------------------------------------------- Notes
// Two separate, both pre-existing stores — see index.html's comment on
// #xdetail-run-note-panel for why this doesn't invent a third.
function renderXdetailNotes(v) {
  document.getElementById("xdetail-notes-textarea").value = v.notes || "";
  const runId = v.run && v.run.run_id;
  const panel = document.getElementById("xdetail-run-note-panel");
  panel.classList.toggle("hidden", !runId);
  if (!runId) return;
  document.getElementById("xdetail-run-note-run-id").textContent = runId;
  api("/api/runs/notes").then((notes) => {
    const n = notes[runId] || { tag: "", note: "" };
    document.getElementById("xdetail-run-tag").value = n.tag || "";
    document.getElementById("xdetail-run-note").value = n.note || "";
  }).catch(() => { /* leave the fields blank rather than block the rest of the tab */ });
}

async function saveXdetailNotes() {
  try {
    await api(`/api/experiments/${encodeURIComponent(state.xdetailId)}`, {
      method: "PATCH", body: JSON.stringify({ notes: document.getElementById("xdetail-notes-textarea").value }),
    });
    toast("Notes saved", "ok");
    refreshXdetail();
  } catch (e) {
    toast("Couldn't save notes: " + e.message, "err");
  }
}

async function saveXdetailRunNote() {
  const runId = state.xdetailView && state.xdetailView.run && state.xdetailView.run.run_id;
  if (!runId) return;
  try {
    await api(`/api/runs/${encodeURIComponent(runId)}/note`, {
      method: "PUT",
      body: JSON.stringify({ tag: document.getElementById("xdetail-run-tag").value, note: document.getElementById("xdetail-run-note").value }),
    });
    toast("Run tag saved", "ok");
  } catch (e) {
    toast("Couldn't save run tag: " + e.message, "err");
  }
}

function initXdetailButtons() {
  document.getElementById("btn-xdetail-back").addEventListener("click", () => navigateToView("experiments"));
  document.querySelectorAll("#xdetail-subtabs .subtab-btn").forEach((btn) => {
    btn.addEventListener("click", () => navigateToExperiment(state.xdetailId, btn.dataset.subtab));
  });
  document.getElementById("btn-xdetail-save-notes").addEventListener("click", saveXdetailNotes);
  document.getElementById("btn-xdetail-save-run-note").addEventListener("click", saveXdetailRunNote);
}

initXdetailButtons();
