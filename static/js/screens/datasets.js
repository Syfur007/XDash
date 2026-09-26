// static/js/screens/datasets.js
//
// XDASH_PLAN.md §8.5, Phase 4: the dataset × runtime binding matrix, its
// per-cell binding editor + Check action, and the dataset detail page
// (#/datasets/<name>, a js/lib/router.js stub since Phase 3 — see its own
// comment on exactly what was left unread). Reads the registry Phase 2
// built (backend/datasets.py, GET /api/datasets/registry — NOT the bare
// GET /api/datasets, which is the pre-existing, unrelated "Data Studio"
// fragment-card feature static/js/views/data.js already owns; see
// server.py's own comment on why those two routes don't collide).
//
// This screen shares #view-data with that older feature rather than
// getting a route of its own — switchView()'s existing `data` case already
// calls both loadDataView() (the old feature) and loadDatasetsScreen()
// (this one); js/lib/router.js's handleRoute() calls this file's
// applyDatasetsRouteParams() for the `second` path segment
// (#/datasets/<name>), same as it already did for experiments.
//
// Same classic-<script>-sharing-global-scope model as every other view file.

// ---------------------------------------------------------------- state
state.datasetRegistry = [];       // GET /api/datasets/registry -> .datasets
state.datasetDataAccount = null;
state.datasetsRuntimes = [];      // GET /api/runtimes -> .runtimes
state.datasetsLoaded = false;
state.selectedDatasetName = null; // drives the detail panel; set from the URL
state.datasetDetailConfigs = [];
state.datasetBindingEditor = null; // {dataset, runtimeId, runtimeKind} while the modal is open

const DATASET_MODE_ICON = { path: "✓", push: "⇡", fetch: "↓", attach: "⊕" };
const DATASET_MODE_LABEL = { path: "path", push: "push", fetch: "fetch", attach: "attach" };

// ================================================================== load + matrix
async function loadDatasetsScreen() {
  await Promise.all([loadDatasetRegistry(), loadDatasetsRuntimes()]);
  state.datasetsLoaded = true;
  renderDatasetsMatrix();
  if (state.selectedDatasetName) loadDatasetDetail(state.selectedDatasetName);
}

async function loadDatasetRegistry() {
  try {
    const data = await api("/api/datasets/registry");
    state.datasetRegistry = data.datasets || [];
    state.datasetDataAccount = data.data_account || null;
  } catch (e) {
    toast("Couldn't load the dataset registry: " + e.message, "err");
  }
}

async function loadDatasetsRuntimes() {
  try {
    const data = await api("/api/runtimes");
    state.datasetsRuntimes = data.runtimes || [];
  } catch (e) {
    // Non-fatal: the matrix just renders with whatever it already had (or
    // an empty column set on first load — the table still shows dataset
    // names and a clear "couldn't load runtimes" state).
  }
}

// Mirrors backend/datasets.py's resolve_binding() exactly (§5.2's order:
// exact runtime id -> kind:* -> the kind default), so the matrix shows the
// same answer a Check or a real dispatch would get, without a network
// round trip per cell. Doesn't replicate the config-specific
// dataset_map.json fallback (_resolve_binding_for_config) — this is a
// dataset-level display, not tied to one config.
function resolveDatasetBindingForDisplay(ds, runtime) {
  const bindings = ds.bindings || {};
  let binding = bindings[runtime.id] ? { ...bindings[runtime.id] } : null;
  if (!binding) {
    const wildcard = `${runtime.kind}:*`;
    binding = bindings[wildcard] ? { ...bindings[wildcard] } : null;
  }
  if (!binding) {
    if (runtime.kind === "local" || runtime.kind === "ssh") binding = { mode: "path" };
    else if (runtime.kind === "colab") binding = { mode: "fetch" };
    else if (runtime.kind === "kaggle") binding = { mode: "attach" };
    else return { mode: null };
  }
  if ((binding.mode === "fetch" || binding.mode === "attach") && !binding.source) {
    const slug = ((ds.sources || {}).kaggle || {}).slug;
    if (slug) binding.source = slug;
    else return { mode: null };
  }
  return binding;
}

function renderDatasetsMatrix() {
  const table = document.getElementById("dataset-matrix-table");
  const countEl = document.getElementById("dataset-matrix-count");
  if (!table) return;
  if (!state.datasetsLoaded) {
    table.innerHTML = `<thead><tr><th>Dataset</th></tr></thead><tbody><tr><td class="empty-state">Loading…</td></tr></tbody>`;
    return;
  }
  countEl.textContent = state.datasetRegistry.length ? String(state.datasetRegistry.length) : "";
  if (!state.datasetsRuntimes.length) {
    table.innerHTML = `<thead><tr><th>Dataset</th></tr></thead><tbody><tr><td class="empty-state">No runtimes registered yet — see Compute.</td></tr></tbody>`;
    return;
  }
  if (!state.datasetRegistry.length) {
    table.innerHTML = `<thead><tr><th>Dataset</th></tr></thead><tbody><tr><td class="empty-state">No datasets declared yet (no configs/dataset/*.yaml fragments found).</td></tr></tbody>`;
    return;
  }

  const head = `<thead><tr><th>Dataset</th>${state.datasetsRuntimes.map((r) => `<th>${escapeHtml(r.label || r.id)}</th>`).join("")}</tr></thead>`;
  const body = state.datasetRegistry.map((ds) => {
    const nameCell = `<td class="matrix-name" data-dataset-row="${escapeHtml(ds.name)}" title="Open ${escapeHtml(ds.name)}'s detail page">${escapeHtml(ds.name)}</td>`;
    const cells = state.datasetsRuntimes.map((r) => datasetMatrixCellHtml(ds, r)).join("");
    return `<tr>${nameCell}${cells}</tr>`;
  }).join("");
  table.innerHTML = head + `<tbody>${body}</tbody>`;

  table.querySelectorAll("[data-dataset-row]").forEach((cell) => {
    cell.addEventListener("click", () => navigateToDataset(cell.dataset.datasetRow));
  });
  table.querySelectorAll("[data-binding-cell]").forEach((cell) => {
    cell.addEventListener("click", () => openBindingEditor(cell.dataset.dataset, cell.dataset.runtime, cell.dataset.runtimeKind));
  });
}

function datasetMatrixCellHtml(ds, runtime) {
  const resolved = resolveDatasetBindingForDisplay(ds, runtime);
  const check = (ds.checks || {})[runtime.id];
  let icon = resolved.mode ? DATASET_MODE_ICON[resolved.mode] : "✗";
  let cls = resolved.mode ? "slate" : "red";
  let title = resolved.mode
    ? `${DATASET_MODE_LABEL[resolved.mode]}${resolved.source ? " · " + resolved.source : ""}`
    : "no binding for this runtime and no default source to fall back to";
  if (check) {
    title += ` — last check: ${check.ok ? "ok" : "failed"}${check.detail ? " (" + check.detail + ")" : ""}`;
    if (!check.ok) { icon = "✗"; cls = "red"; }
    else if (resolved.mode) cls = "emerald";
  }
  return `<td class="matrix-cell ${cls}" data-binding-cell data-dataset="${escapeHtml(ds.name)}" data-runtime="${escapeHtml(runtime.id)}" data-runtime-kind="${escapeHtml(runtime.kind)}" title="${escapeHtml(title)}">${icon}</td>`;
}

// ================================================================== binding editor modal
function openBindingEditor(datasetName, runtimeId, runtimeKind) {
  const ds = state.datasetRegistry.find((d) => d.name === datasetName);
  if (!ds) return;
  const existing = (ds.bindings || {})[runtimeId] || (ds.bindings || {})[`${runtimeKind}:*`] || {};
  const resolved = resolveDatasetBindingForDisplay(ds, { id: runtimeId, kind: runtimeKind });
  state.datasetBindingEditor = { dataset: datasetName, runtimeId, runtimeKind };

  document.getElementById("binding-editor-title").textContent = `${datasetName} · ${runtimeId}`;
  document.getElementById("binding-editor-mode").value = existing.mode || resolved.mode || "path";
  document.getElementById("binding-editor-path").value = existing.path || "";
  document.getElementById("binding-editor-source").value = existing.source || resolved.source || "";
  document.getElementById("binding-editor-result").textContent = "";
  updateBindingEditorFieldVisibility();
  document.getElementById("dataset-binding-backdrop").classList.remove("hidden");
}

function updateBindingEditorFieldVisibility() {
  const mode = document.getElementById("binding-editor-mode").value;
  document.getElementById("binding-editor-path-field").classList.toggle("hidden", mode !== "path" && mode !== "push");
  document.getElementById("binding-editor-source-field").classList.toggle("hidden", mode !== "fetch" && mode !== "attach");
}

function closeBindingEditor() {
  document.getElementById("dataset-binding-backdrop").classList.add("hidden");
  state.datasetBindingEditor = null;
}

function bindingEditorBody() {
  const mode = document.getElementById("binding-editor-mode").value;
  const body = { mode };
  const path = document.getElementById("binding-editor-path").value.trim();
  const source = document.getElementById("binding-editor-source").value.trim();
  if ((mode === "path" || mode === "push") && path) body.path = path;
  if ((mode === "fetch" || mode === "attach") && source) body.source = source;
  return body;
}

async function saveBindingEditor() {
  const ctx = state.datasetBindingEditor;
  if (!ctx) return;
  try {
    await api(`/api/datasets/${encodeURIComponent(ctx.dataset)}/bindings/${encodeURIComponent(ctx.runtimeId)}`, {
      method: "PUT", body: JSON.stringify(bindingEditorBody()),
    });
    toast("Binding saved", "ok");
    closeBindingEditor();
    await loadDatasetRegistry();
    renderDatasetsMatrix();
    if (state.selectedDatasetName === ctx.dataset) loadDatasetDetail(ctx.dataset);
  } catch (e) {
    toast("Couldn't save binding: " + e.message, "err");
  }
}

async function checkBindingEditor() {
  const ctx = state.datasetBindingEditor;
  if (!ctx) return;
  const resultEl = document.getElementById("binding-editor-result");
  resultEl.textContent = "Checking…";
  try {
    // A check runs against whatever binding is on record for this cell —
    // save first, so "Check" always verifies the values in the form, not
    // whatever was last saved.
    await api(`/api/datasets/${encodeURIComponent(ctx.dataset)}/bindings/${encodeURIComponent(ctx.runtimeId)}`, {
      method: "PUT", body: JSON.stringify(bindingEditorBody()),
    });
    const result = await api(`/api/datasets/${encodeURIComponent(ctx.dataset)}/check?runtime=${encodeURIComponent(ctx.runtimeId)}`, { method: "POST" });
    resultEl.textContent = `${result.ok ? "✓ ok" : "✗ failed"} — ${result.detail || ""}`;
    await loadDatasetRegistry();
    renderDatasetsMatrix();
  } catch (e) {
    resultEl.textContent = "Couldn't check: " + e.message;
  }
}

// ================================================================== dataset detail (#/datasets/<name>)
function applyDatasetsRouteParams(name) {
  state.selectedDatasetName = name || null;
  const panel = document.getElementById("dataset-registry-detail-panel");
  if (!panel) return;
  if (!state.selectedDatasetName) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  if (state.datasetsLoaded) loadDatasetDetail(state.selectedDatasetName);
  // else: loadDatasetsScreen() (kicked off by switchView()) will call
  // loadDatasetDetail() itself once the registry finishes loading.
}

async function loadDatasetDetail(name) {
  const panel = document.getElementById("dataset-registry-detail-panel");
  if (!panel) return;
  const ds = state.datasetRegistry.find((d) => d.name.toLowerCase() === name.toLowerCase());
  document.getElementById("dataset-registry-detail-title").textContent = name;
  const kv = document.getElementById("dataset-registry-detail-kv");

  if (!ds) {
    kv.innerHTML = `<tr><td colspan="2" class="empty-state">Unknown dataset '${escapeHtml(name)}' — no registry record or identity fragment declares it.</td></tr>`;
    document.getElementById("dataset-registry-detail-configs").innerHTML = "";
    document.getElementById("dataset-registry-detail-experiments").innerHTML = "";
    document.getElementById("dataset-registry-detail-preview-link").classList.add("hidden");
    return;
  }

  const kaggleSource = (ds.sources || {}).kaggle || {};
  kv.innerHTML = [
    ["Root", ds.root || "(no dataset.root declared)"],
    ["Kaggle source", kaggleSource.slug || (state.datasetDataAccount ? `(none — data account: ${state.datasetDataAccount})` : "(none)")],
    ["Bindings set", Object.keys(ds.bindings || {}).length ? Object.keys(ds.bindings).join(", ") : "(none — every runtime uses its kind default)"],
  ].map(([k, v]) => `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(v)}</td></tr>`).join("");

  // "Open in Data Studio" — relocates/links the pre-existing channel-preview
  // + audit feature (static/js/views/data.js) rather than duplicating it.
  const linkWrap = document.getElementById("dataset-registry-detail-preview-link");
  const fragmentMatch = (state.datasetList || []).find((d) => (d.name || "").toLowerCase() === ds.name.toLowerCase());
  if (fragmentMatch) {
    linkWrap.classList.remove("hidden");
    document.getElementById("btn-dataset-registry-open-preview").onclick = () => {
      selectDataset(fragmentMatch.fragment);
      document.getElementById("dataset-detail-panel").scrollIntoView({ behavior: "smooth", block: "start" });
    };
  } else {
    linkWrap.classList.add("hidden");
  }

  const configsBody = document.getElementById("dataset-registry-detail-configs");
  configsBody.innerHTML = `<tr><td class="empty-state">Loading…</td></tr>`;
  try {
    const data = await api(`/api/datasets/${encodeURIComponent(ds.name)}/configs`);
    state.datasetDetailConfigs = data.configs || [];
  } catch (e) {
    state.datasetDetailConfigs = [];
  }
  configsBody.innerHTML = state.datasetDetailConfigs.length
    ? state.datasetDetailConfigs.map((path) => `<tr><td>${escapeHtml(path)}</td></tr>`).join("")
    : `<tr><td class="empty-state">No configs found using this dataset.</td></tr>`;

  // Best-effort "which experiments/studies use it" (XDASH_PLAN.md §8.5):
  // every experiment whose config_path is one of the configs above, one
  // /api/experiments?config= call per config (there are only ever a
  // handful of configs per dataset) — not a new backend route, since
  // GET /api/experiments already supports this exact filter (§7).
  const expBody = document.getElementById("dataset-registry-detail-experiments");
  expBody.innerHTML = `<tr><td colspan="3" class="empty-state">Loading…</td></tr>`;
  let experiments = [];
  for (const path of state.datasetDetailConfigs) {
    try {
      const data = await api(`/api/experiments?config=${encodeURIComponent(path)}`);
      experiments = experiments.concat(data.experiments || []);
    } catch (e) {
      // best-effort — one bad config shouldn't blank the rest
    }
  }
  expBody.innerHTML = experiments.length
    ? experiments.map((e) => `<tr>
        <td>${experimentLink(e.experiment_id)}</td>
        <td>${escapeHtml(e.status)}</td>
        <td>${escapeHtml((e.studies || []).map((s) => s.study_id).join(", ") || "—")}</td>
      </tr>`).join("")
    : `<tr><td colspan="3" class="empty-state">No experiments reference these configs yet.</td></tr>`;
}

// ================================================================== init
function initDatasetsScreenButtons() {
  document.getElementById("btn-refresh-data")?.addEventListener("click", loadDatasetsScreen);
  document.getElementById("btn-dataset-registry-back")?.addEventListener("click", () => navigateToView("data"));

  document.getElementById("binding-editor-mode").addEventListener("change", updateBindingEditorFieldVisibility);
  document.getElementById("binding-editor-cancel").addEventListener("click", closeBindingEditor);
  document.getElementById("binding-editor-save").addEventListener("click", saveBindingEditor);
  document.getElementById("btn-binding-editor-check").addEventListener("click", checkBindingEditor);
  document.getElementById("dataset-binding-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "dataset-binding-backdrop") closeBindingEditor();
  });
}

initDatasetsScreenButtons();
