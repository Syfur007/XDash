// static/js/screens/datasets.js
//
// DATASETS_PLAN.md §8: one screen, list-left/detail-right, replacing both the
// old binding matrix and the separate "Registered dataset fragments" cards.
// GET /api/datasets is the single source (identity, tags, badges, sources,
// hosts, checks, and a per-runtime `plans` summary from cached checks —
// backend/datasets.py's plan_delivery()). There is no client-side resolver
// any more (DS11 fix): every state shown here is exactly what the backend
// computed, never re-derived in JS.
//
// Same classic-<script>-sharing-global-scope model as every other view file.
// Routes: #/datasets and #/datasets/<name>, wired by js/lib/router.js's
// applyDatasetsRouteParams(), same as before.

// ---------------------------------------------------------------- state
state.datasets = [];          // GET /api/datasets -> .datasets
state.datasetsDataAccount = null;
state.datasetsKaggleAccounts = [];
state.datasetsSearch = "";
state.datasetsTagFilter = new Set();
state.selectedDatasetName = null;
state.datasetDetail = null;   // GET /api/datasets/<name>
state.datasetSamplesPath = "";
state.datasetFsPicker = null; // {path, mode, onUse}

const DATASET_STATE_ICON = { ready: "✓", "will-transfer": "↻", partial: "⚠", blocked: "✗", unknown: "?" };
const DATASET_STATE_CLASS = { ready: "green", "will-transfer": "slate", partial: "amber", blocked: "red", unknown: "slate" };
const DATASET_STATE_ORDER = ["blocked", "partial", "unknown", "will-transfer", "ready"];

// ================================================================== load + list
async function loadDatasetsScreen() {
  await Promise.all([loadDatasets(), loadDatasetsKaggleAccounts()]);
  renderDatasetsList();
  renderDataAccountSelect();
  if (state.selectedDatasetName) loadDatasetDetail(state.selectedDatasetName);
}

async function loadDatasets() {
  try {
    const data = await api("/api/datasets");
    state.datasets = data.datasets || [];
    state.datasetsDataAccount = data.data_account || null;
  } catch (e) {
    toast("Couldn't load datasets: " + e.message, "err");
  }
}

async function loadDatasetsKaggleAccounts() {
  try {
    const data = await api("/api/kaggle/accounts");
    state.datasetsKaggleAccounts = data.accounts || [];
  } catch (e) {
    state.datasetsKaggleAccounts = [];
  }
}

function renderDataAccountSelect() {
  const sel = document.getElementById("datasets-data-account");
  if (!sel) return;
  const opts = ["<option value=\"\">(none)</option>"].concat(
    state.datasetsKaggleAccounts.map((a) => `<option value="${escapeHtml(a.name)}">${escapeHtml(a.name)}</option>`)
  );
  sel.innerHTML = opts.join("");
  sel.value = state.datasetsDataAccount || "";
}

function datasetWorstState(d) {
  let worst = "ready";
  for (const plan of Object.values(d.plans || {})) {
    const idx = DATASET_STATE_ORDER.indexOf(plan.state);
    if (idx > DATASET_STATE_ORDER.indexOf(worst)) worst = plan.state;
  }
  if (!Object.keys(d.plans || {}).length) worst = d.identity_source === "draft" ? "unknown" : "ready";
  return worst;
}

function datasetKindStrip(d) {
  const kinds = { local: [], ssh: [], kaggle: [], colab: [] };
  for (const [rid, plan] of Object.entries(d.plans || {})) {
    const kind = rid === "local" ? "local" : rid.split(":")[0];
    if (kinds[kind]) kinds[kind].push(plan);
  }
  const letter = { local: "L", ssh: "S", kaggle: "K", colab: "C" };
  return Object.entries(kinds).filter(([, plans]) => plans.length).map(([kind, plans]) => {
    const ready = plans.filter((p) => p.state === "ready").length;
    const blocked = plans.some((p) => p.state === "blocked");
    const transferring = plans.some((p) => p.state === "will-transfer");
    const mark = blocked ? "✗" : transferring ? "↻" : "✓";
    return kind === "local" ? `L${mark}` : `${letter[kind]} ${ready}/${plans.length}${transferring && !blocked ? " ↻" : ""}`;
  }).join(" ");
}

function datasetMatchesFilters(d) {
  const q = state.datasetsSearch.trim().toLowerCase();
  if (q) {
    const tagQuery = q.startsWith("tag:") ? q.slice(4) : null;
    if (tagQuery) {
      if (!(d.tags || []).some((t) => t.includes(tagQuery))) return false;
    } else if (!d.name.toLowerCase().includes(q) && !(d.tags || []).some((t) => t.includes(q))) {
      return false;
    }
  }
  if (state.datasetsTagFilter.size) {
    if (!(d.tags || []).some((t) => state.datasetsTagFilter.has(t))) return false;
  }
  return true;
}

function renderDatasetsList() {
  const body = document.getElementById("datasets-list-body");
  const countEl = document.getElementById("datasets-list-count");
  if (!body) return;
  countEl.textContent = state.datasets.length ? String(state.datasets.length) : "";

  renderDatasetTagChips();

  const rows = state.datasets.filter(datasetMatchesFilters);
  if (!rows.length) {
    body.innerHTML = `<div class="empty-state">${state.datasets.length ? "No datasets match this search/filter." : "No datasets yet — add one, or declare a configs/dataset/*.yaml fragment."}</div>`;
    return;
  }
  // Registered (fragment-backed, at least one source configured) first, per
  // §8's "registered datasets should be on top".
  const registered = (d) => d.identity_source === "fragment" && Object.keys(d.sources || {}).length;
  rows.sort((a, b) => (registered(b) - registered(a)) || a.name.localeCompare(b.name));

  body.innerHTML = rows.map((d) => {
    const worst = datasetWorstState(d);
    const active = d.name === state.selectedDatasetName ? "active" : "";
    const draftBadge = d.identity_source === "draft" ? `<span class="badge slate">draft</span>` : "";
    return `<div class="dataset-list-row ${active}" data-dataset-name="${escapeHtml(d.name)}">
      <div class="dataset-list-row-top">
        <strong>${escapeHtml(d.name)}</strong>
        <span>${draftBadge}<span title="${escapeHtml(worst)}">${DATASET_STATE_ICON[worst] || "?"}</span></span>
      </div>
      <div class="chip-row">${(d.tags || []).map((t) => `<span class="badge slate">${escapeHtml(t)}</span>`).join("")}</div>
      <div class="dataset-list-row-kinds">${escapeHtml(datasetKindStrip(d))}</div>
    </div>`;
  }).join("");

  body.querySelectorAll("[data-dataset-name]").forEach((el) => {
    el.addEventListener("click", () => navigateToDataset(el.dataset.datasetName));
  });
}

function renderDatasetTagChips() {
  const wrap = document.getElementById("datasets-tag-chips");
  if (!wrap) return;
  const allTags = new Set();
  state.datasets.forEach((d) => (d.tags || []).forEach((t) => allTags.add(t)));
  wrap.innerHTML = [...allTags].sort().map((t) => {
    const active = state.datasetsTagFilter.has(t);
    return `<span class="badge ${active ? "emerald" : "slate"}" data-tag-chip="${escapeHtml(t)}">${escapeHtml(t)}${active ? " ×" : ""}</span>`;
  }).join("");
  wrap.querySelectorAll("[data-tag-chip]").forEach((el) => {
    el.addEventListener("click", () => {
      const t = el.dataset.tagChip;
      if (state.datasetsTagFilter.has(t)) state.datasetsTagFilter.delete(t); else state.datasetsTagFilter.add(t);
      renderDatasetsList();
    });
  });
}

// ================================================================== detail (#/datasets/<name>)
function applyDatasetsRouteParams(name) {
  state.selectedDatasetName = name || null;
  const empty = document.getElementById("dataset-detail-empty");
  const body = document.getElementById("dataset-detail-body");
  if (!empty || !body) return;
  if (!state.selectedDatasetName) {
    empty.classList.remove("hidden");
    body.classList.add("hidden");
    renderDatasetsList();
    return;
  }
  empty.classList.add("hidden");
  body.classList.remove("hidden");
  if (state.datasets.length) loadDatasetDetail(state.selectedDatasetName);
  renderDatasetsList();
}

async function loadDatasetDetail(name) {
  try {
    state.datasetDetail = await api(`/api/datasets/${encodeURIComponent(name)}`);
  } catch (e) {
    toast("Couldn't load '" + name + "': " + e.message, "err");
    state.datasetDetail = null;
    return;
  }
  state.datasetSamplesPath = "";
  renderDatasetDetail();
  loadDatasetUsage(name);
  loadDatasetSamples();
}

function renderDatasetDetail() {
  const d = state.datasetDetail;
  if (!d) return;
  document.getElementById("dataset-detail-title").textContent = d.name;
  document.getElementById("dataset-detail-sub").textContent = d.root
    ? `${d.fragment ? "configs/dataset/" + d.fragment + ".yaml" : ""} · root ${d.root}`
    : "draft — no dissert fragment yet";

  const badges = [];
  if (d.identity_source === "draft") badges.push(`<span class="badge slate">draft</span>`);
  if (d.badges && d.badges.modality) badges.push(`<span class="badge slate">${escapeHtml(d.badges.modality)}</span>`);
  if (d.badges && d.badges.channel_mode) badges.push(`<span class="badge slate">${escapeHtml(d.badges.channel_mode)}</span>`);
  if (d.badges && d.badges.dedup) badges.push(`<span class="badge emerald">dedup</span>`);
  if (d.badges && d.badges.external) badges.push(`<span class="badge red">external</span>`);
  document.getElementById("dataset-detail-badges").innerHTML = badges.join(" ");

  renderMigrationNotes(d);
  renderDraftChecklist(d);
  renderDatasetTagsEditor(d);
  renderDatasetSourcesEditor(d);
  renderDatasetHostOverrides(d);
  renderDatasetAvailability(d);
}

function renderMigrationNotes(d) {
  const el = document.getElementById("dataset-detail-migration-notes");
  if (!el) return;
  if (!d.migration_notes || !d.migration_notes.length) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="empty-state" style="text-align:left;">
    ${d.migration_notes.map(escapeHtml).join("<br/>")}
    <div class="job-actions" style="padding-top:6px;"><button class="btn btn-sm btn-ghost" id="btn-dataset-dismiss-notes">Dismiss</button></div>
  </div>`;
  document.getElementById("btn-dataset-dismiss-notes")?.addEventListener("click", async () => {
    try {
      await api(`/api/datasets/${encodeURIComponent(d.name)}`, { method: "PATCH", body: JSON.stringify({}) });
    } catch (e) { /* best-effort — the notes just won't clear server-side yet */ }
    el.innerHTML = "";
  });
}

function renderDraftChecklist(d) {
  const el = document.getElementById("dataset-detail-checklist");
  if (!el) return;
  if (d.identity_source !== "draft") { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="empty-state" style="text-align:left;">
    <strong>This is a draft.</strong> dissert still needs:
    <ul style="margin:6px 0 0 18px; padding:0;">
      <li>☐ Loader code for this dataset's format (not detectable — always shown until the fragment exists).</li>
      <li>☐ <span style="font-family:var(--mono)">configs/dataset/${escapeHtml(d.name.toLowerCase())}.yaml</span> declaring <span style="font-family:var(--mono)">dataset.name</span>/<span style="font-family:var(--mono)">dataset.root</span>.</li>
      <li>☐ That fragment committed and pushed (Kaggle clones the pinned commit).</li>
    </ul>
    <div class="job-actions" style="padding-top:8px;">
      <button class="btn btn-sm btn-ghost" id="btn-dataset-link-fragment">Link to fragment ▾</button>
    </div>
  </div>`;
  document.getElementById("btn-dataset-link-fragment")?.addEventListener("click", () => openLinkFragmentPrompt(d));
}

async function openLinkFragmentPrompt(d) {
  // Fragments with no XDash record of their own yet: identity_source is
  // "fragment" and it has no sources/tags/hosts recorded — the un-adopted set.
  const unclaimed = state.datasets.filter((x) => x.identity_source === "fragment" &&
    !Object.keys(x.sources || {}).length && !(x.tags || []).length && !Object.keys(x.hosts || {}).length);
  const fragmentName = window.prompt(
    "Link this draft to which fragment?\n" + (unclaimed.length ? unclaimed.map((x) => x.name).join(", ") : "(none unclaimed)")
  );
  if (!fragmentName) return;
  try {
    await api(`/api/datasets/${encodeURIComponent(d.name)}/link`, { method: "POST", body: JSON.stringify({ fragment: fragmentName }) });
    toast("Linked to " + fragmentName, "ok");
    await loadDatasetsScreen();
    navigateToDataset(fragmentName);
  } catch (e) { toast("Couldn't link: " + e.message, "err"); }
}

function renderDatasetTagsEditor(d) {
  const el = document.getElementById("dataset-detail-tags");
  if (!el) return;
  const chips = (d.tags || []).map((t) => `<span class="badge slate" data-remove-tag="${escapeHtml(t)}">${escapeHtml(t)} ×</span>`).join("");
  el.innerHTML = `${chips}<input class="text-input" id="dataset-tag-input" placeholder="+ tag" style="width:110px;" autocomplete="off" />`;
  el.querySelectorAll("[data-remove-tag]").forEach((chip) => {
    chip.addEventListener("click", () => saveDatasetTags(d.name, (d.tags || []).filter((t) => t !== chip.dataset.removeTag)));
  });
  const input = document.getElementById("dataset-tag-input");
  input?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && input.value.trim()) {
      saveDatasetTags(d.name, [...(d.tags || []), input.value.trim()]);
    }
  });
}

async function saveDatasetTags(name, tags) {
  try {
    await api(`/api/datasets/${encodeURIComponent(name)}`, { method: "PATCH", body: JSON.stringify({ tags }) });
    await loadDatasetDetail(name);
    await loadDatasets();
    renderDatasetsList();
  } catch (e) { toast("Couldn't save tags: " + e.message, "err"); }
}

function renderDatasetSourcesEditor(d) {
  const el = document.getElementById("dataset-detail-sources");
  if (!el) return;
  const kaggleSlug = (d.sources.kaggle || {}).slug || "";
  const localPath = (d.sources.local || {}).path || (d.root ? "(default: this repo's own " + d.root + ")" : "(none)");
  el.innerHTML = `
    <tr><td>This machine</td><td>
      <input class="text-input grow" id="dataset-source-local-path" value="${escapeHtml((d.sources.local || {}).path || "")}" placeholder="${escapeHtml(localPath)}" autocomplete="off" ${d.root ? "" : "disabled"} />
      <button class="btn btn-sm btn-ghost" id="btn-dataset-source-local-pick">Pick folder…</button>
    </td></tr>
    <tr><td>Kaggle</td><td>
      <input class="text-input grow" id="dataset-source-kaggle-slug" value="${escapeHtml(kaggleSlug)}" placeholder="owner/dataset-slug (or paste a URL)" autocomplete="off" ${d.kaggle_slug_locked ? "disabled" : ""} />
      ${d.kaggle_slug_locked ? `<span class="entity-card-sub">declared in configs/dataset/${escapeHtml(d.fragment || "")}.yaml</span>` : `<button class="btn btn-sm btn-primary" id="btn-dataset-source-kaggle-save">Save</button>`}
    </td></tr>`;
  document.getElementById("btn-dataset-source-local-pick")?.addEventListener("click", () => {
    openFsPicker("dir", (path) => { document.getElementById("dataset-source-local-path").value = path; saveDatasetLocalPath(d.name, path); });
  });
  document.getElementById("btn-dataset-source-kaggle-save")?.addEventListener("click", () => {
    saveDatasetKaggleSlug(d.name, document.getElementById("dataset-source-kaggle-slug").value.trim());
  });
}

async function saveDatasetKaggleSlug(name, slug) {
  try {
    await api(`/api/datasets/${encodeURIComponent(name)}`, {
      method: "PATCH", body: JSON.stringify({ sources: { kaggle: slug ? { slug } : null } }),
    });
    toast("Kaggle slug saved — checking access…", "ok");
    await recheckDataset(name, ["kaggle:" + (state.datasetsDataAccount || "")].filter((t) => t !== "kaggle:"));
    await recheckDataset(name);
    await loadDatasetDetail(name);
    await loadDatasets();
    renderDatasetsList();
  } catch (e) { toast("Couldn't save slug: " + e.message, "err"); }
}

async function saveDatasetLocalPath(name, path) {
  try {
    await api(`/api/datasets/${encodeURIComponent(name)}`, {
      method: "PATCH", body: JSON.stringify({ sources: { local: path ? { path } : null } }),
    });
    await loadDatasetDetail(name);
    await loadDatasets();
    renderDatasetsList();
  } catch (e) { toast("Couldn't save local path: " + e.message, "err"); }
}

function renderDatasetHostOverrides(d) {
  const body = document.getElementById("dataset-detail-hosts");
  const select = document.getElementById("dataset-host-override-select");
  if (!body) return;
  const hosts = Object.entries(d.hosts || {});
  body.innerHTML = hosts.length
    ? hosts.map(([slot, ov]) => `<tr><td>${escapeHtml(slot)}</td><td>${escapeHtml(ov.path)}</td><td><button class="btn btn-sm btn-ghost" data-remove-host="${escapeHtml(slot)}">Remove</button></td></tr>`).join("")
    : `<tr><td colspan="3" class="empty-state">None set.</td></tr>`;
  body.querySelectorAll("[data-remove-host]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/datasets/${encodeURIComponent(d.name)}/hosts/${encodeURIComponent(btn.dataset.removeHost)}`, { method: "DELETE" });
        await loadDatasetDetail(d.name);
      } catch (e) { toast("Couldn't remove override: " + e.message, "err"); }
    });
  });
  if (select) {
    const sshSlots = Object.keys(d.plans || {}).filter((rid) => rid.startsWith("ssh:"));
    select.innerHTML = sshSlots.length
      ? sshSlots.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("")
      : `<option value="">(no SSH hosts registered)</option>`;
  }
}

function initDatasetHostOverrideForm() {
  document.getElementById("btn-dataset-host-override-save")?.addEventListener("click", async () => {
    const d = state.datasetDetail;
    if (!d) return;
    const slot = document.getElementById("dataset-host-override-select").value;
    const path = document.getElementById("dataset-host-override-path").value.trim();
    if (!slot || !path) { toast("Pick a host and a path first", "err"); return; }
    try {
      await api(`/api/datasets/${encodeURIComponent(d.name)}/hosts/${encodeURIComponent(slot)}`, { method: "PUT", body: JSON.stringify({ path }) });
      toast("Override saved — checking…", "ok");
      await recheckDataset(d.name, [slot]);
      await loadDatasetDetail(d.name);
    } catch (e) { toast("Couldn't save override: " + e.message, "err"); }
  });
}

function runtimeLabel(rid) {
  if (rid === "local") return "This machine";
  const [kind, name] = rid.split(":");
  if (kind === "colab" && name === undefined) return "Colab (all)";
  return `${kind}:${name}`;
}

function renderDatasetAvailability(d) {
  const body = document.getElementById("dataset-detail-availability");
  if (!body) return;
  const entries = Object.entries(d.plans || {});
  if (!entries.length) {
    body.innerHTML = `<tr><td colspan="3" class="empty-state">No runtimes registered yet — see Compute.</td></tr>`;
    return;
  }
  body.innerHTML = entries.map(([rid, plan]) => {
    const cls = DATASET_STATE_CLASS[plan.state] || "slate";
    const icon = DATASET_STATE_ICON[plan.state] || "?";
    return `<tr>
      <td>${escapeHtml(runtimeLabel(rid))}</td>
      <td><span class="badge ${cls}">${icon} ${escapeHtml(plan.state)}</span>${plan.code ? ` <span class="entity-card-sub">${escapeHtml(plan.code)}</span>` : ""}</td>
      <td>${escapeHtml(plan.detail || "")}</td>
    </tr>`;
  }).join("");
}

async function recheckDataset(name, targets) {
  try {
    const body = targets && targets.length ? { targets } : {};
    await api(`/api/datasets/${encodeURIComponent(name)}/check`, { method: "POST", body: JSON.stringify(body) });
  } catch (e) {
    toast("Check failed: " + e.message, "err");
  }
}

async function recheckAllDatasets() {
  toast("Re-checking every dataset…");
  for (const d of state.datasets) {
    await recheckDataset(d.name);
  }
  await loadDatasetsScreen();
  toast("Re-check complete", "ok");
}

// ================================================================== samples (§8.4)
async function loadDatasetSamples() {
  const d = state.datasetDetail;
  const crumbs = document.getElementById("dataset-samples-crumbs");
  const grid = document.getElementById("dataset-samples-grid");
  if (!d || !crumbs || !grid) return;
  if (!(d.sources.local || {}).path && !d.root) {
    crumbs.textContent = "";
    grid.innerHTML = `<div class="empty-state">No local copy configured.</div>`;
    return;
  }
  try {
    const data = await api(`/api/datasets/${encodeURIComponent(d.name)}/tree?path=${encodeURIComponent(state.datasetSamplesPath)}`);
    renderDatasetSamples(data);
  } catch (e) {
    grid.innerHTML = `<div class="empty-state">${escapeHtml(e.message)}</div>`;
  }
}

function renderDatasetSamples(data) {
  const crumbs = document.getElementById("dataset-samples-crumbs");
  const grid = document.getElementById("dataset-samples-grid");
  const parts = (data.path || "").split("/").filter(Boolean);
  const crumbHtml = [`<a href="#" data-samples-crumb="">root</a>`].concat(
    parts.map((p, i) => `<a href="#" data-samples-crumb="${escapeHtml(parts.slice(0, i + 1).join("/"))}">${escapeHtml(p)}</a>`)
  ).join(" / ");
  crumbs.innerHTML = crumbHtml;
  crumbs.querySelectorAll("[data-samples-crumb]").forEach((a) => {
    a.addEventListener("click", (e) => { e.preventDefault(); state.datasetSamplesPath = a.dataset.samplesCrumb; loadDatasetSamples(); });
  });

  const dirs = (data.dirs || []).map((name) => {
    const rel = data.path ? `${data.path}/${name}` : name;
    return `<div class="dataset-list-row" data-samples-dir="${escapeHtml(rel)}" style="display:inline-block; padding:6px 10px;">📁 ${escapeHtml(name)}</div>`;
  }).join("");
  const name = state.datasetDetail.name;
  const images = (data.images || []).map((img) => {
    const rel = data.path ? `${data.path}/${img}` : img;
    const src = `/api/datasets/${encodeURIComponent(name)}/thumb?path=${encodeURIComponent(rel)}&size=160`;
    return `<img src="${src}" data-sample-image="${escapeHtml(rel)}" loading="lazy" alt="${escapeHtml(img)}" />`;
  }).join("");
  grid.innerHTML = dirs + images || `<div class="empty-state">Nothing here.</div>`;
  grid.querySelectorAll("[data-samples-dir]").forEach((el) => {
    el.addEventListener("click", () => { state.datasetSamplesPath = el.dataset.samplesDir; loadDatasetSamples(); });
  });
  grid.querySelectorAll("[data-sample-image]").forEach((el) => {
    el.addEventListener("click", () => previewDatasetSample(el.dataset.sampleImage));
  });
}

async function previewDatasetSample(relPath) {
  const d = state.datasetDetail;
  const out = document.getElementById("dataset-samples-preview");
  if (!d || !out) return;
  const name = d.name;
  let maskRel = null;
  try {
    const info = await api(`/api/datasets/${encodeURIComponent(name)}/file?path=${encodeURIComponent(relPath)}&mask_of=1`);
    maskRel = info.mask;
  } catch (e) { /* best-effort */ }
  const imgUrl = `/api/datasets/${encodeURIComponent(name)}/file?path=${encodeURIComponent(relPath)}`;
  const maskUrl = maskRel ? `/api/datasets/${encodeURIComponent(name)}/file?path=${encodeURIComponent(maskRel)}` : null;
  const modes = state.system && state.system.dataset_channel_modes ? Object.entries(state.system.dataset_channel_modes) : [];
  out.innerHTML = `
    <div class="job-actions" style="flex-wrap:wrap;">
      <img src="${imgUrl}" style="max-width:220px; max-height:220px; border-radius:var(--radius-sm);" />
      ${maskUrl ? `<img src="${maskUrl}" style="max-width:220px; max-height:220px; border-radius:var(--radius-sm);" title="mask" />` : ""}
    </div>
    ${modes.length ? `
      <div class="job-actions" style="margin-top:8px;">
        <select id="dataset-sample-channel-mode">${modes.map(([k, v]) => `<option value="${escapeHtml(k)}">${escapeHtml(v)}</option>`).join("")}</select>
        <button class="btn btn-sm btn-ghost" id="btn-dataset-sample-channel-preview">Channel preview ▸</button>
      </div>
      <div id="dataset-sample-channel-result"></div>
    ` : ""}`;
  document.getElementById("btn-dataset-sample-channel-preview")?.addEventListener("click", async () => {
    const mode = document.getElementById("dataset-sample-channel-mode").value;
    const resultEl = document.getElementById("dataset-sample-channel-result");
    resultEl.innerHTML = `<div class="empty-state">Building channels…</div>`;
    try {
      const result = await api(`/api/datasets/${encodeURIComponent(name)}/channel-preview`, {
        method: "POST", body: JSON.stringify({ path: relPath, mode }),
      });
      const tiles = (result.tiles || []).map((t) => `<div class="channel-tile"><img src="${t.png}" /><div class="channel-tile-label">${escapeHtml(t.group)}[${t.index_in_group}]</div></div>`).join("");
      resultEl.innerHTML = `<div class="channel-tile-grid">${tiles}</div>`;
    } catch (e) { resultEl.innerHTML = `<div class="empty-state">${escapeHtml(e.message)}</div>`; }
  });
}

// ================================================================== usage ("Used by")
async function loadDatasetUsage(name) {
  const el = document.getElementById("dataset-detail-usage");
  if (!el) return;
  el.textContent = "Loading…";
  try {
    const data = await api(`/api/datasets/${encodeURIComponent(name)}/configs`);
    const configs = data.configs || [];
    let experiments = [];
    for (const path of configs) {
      try {
        const r = await api(`/api/experiments?config=${encodeURIComponent(path)}`);
        experiments = experiments.concat(r.experiments || []);
      } catch (e) { /* best-effort */ }
    }
    const studies = new Set();
    experiments.forEach((e) => (e.studies || []).forEach((s) => studies.add(s.study_id)));
    el.textContent = `${configs.length} config${configs.length === 1 ? "" : "s"} · ${experiments.length} experiment${experiments.length === 1 ? "" : "s"} · ${studies.size} stud${studies.size === 1 ? "y" : "ies"}`;
  } catch (e) {
    el.textContent = "Couldn't load usage: " + e.message;
  }
}

// ================================================================== remove / data account
async function removeDatasetFromXDash() {
  const d = state.datasetDetail;
  if (!d) return;
  const ok = await showConfirm("Remove from XDash", `Remove "${d.name}" from XDash's own store? Sources, tags, overrides and checks are deleted. Nothing in dissert or on disk is touched.`);
  if (!ok) return;
  try {
    await api(`/api/datasets/${encodeURIComponent(d.name)}`, { method: "DELETE" });
    toast("Removed", "ok");
    navigateToDataset("");
    location.hash = "#/datasets";
    await loadDatasetsScreen();
  } catch (e) { toast("Couldn't remove: " + e.message, "err"); }
}

async function saveDataAccount(name) {
  try {
    await api("/api/datasets/data-account", { method: "PUT", body: JSON.stringify({ name: name || null }) });
    state.datasetsDataAccount = name || null;
    toast("Data account set", "ok");
  } catch (e) { toast("Couldn't set data account: " + e.message, "err"); }
}

// ================================================================== Add-dataset wizard (§6)
function openAddDatasetWizard() {
  document.getElementById("dataset-add-name").value = "";
  document.getElementById("dataset-add-tags").value = "";
  document.getElementById("dataset-add-kaggle").value = "";
  document.getElementById("dataset-add-local-path").value = "";
  document.getElementById("dataset-add-fragment-notice").textContent = "";
  document.getElementById("dataset-add-backdrop").classList.remove("hidden");
}

function closeAddDatasetWizard() {
  document.getElementById("dataset-add-backdrop").classList.add("hidden");
}

async function createDatasetFromWizard() {
  const name = document.getElementById("dataset-add-name").value.trim();
  if (!name) { toast("Name is required", "err"); return; }
  const tags = document.getElementById("dataset-add-tags").value.split(",").map((t) => t.trim()).filter(Boolean);
  const kaggleRaw = document.getElementById("dataset-add-kaggle").value.trim();
  const localPath = document.getElementById("dataset-add-local-path").value.trim();
  const sources = {};
  if (kaggleRaw) sources.kaggle = { slug: kaggleRaw };
  if (localPath) sources.local = { path: localPath };
  try {
    const result = await api("/api/datasets", { method: "POST", body: JSON.stringify({ name, tags, sources }) });
    if (result.exists === "fragment") {
      document.getElementById("dataset-add-fragment-notice").textContent = `dissert already declares "${result.name}" — opening it instead.`;
      closeAddDatasetWizard();
      navigateToDataset(result.name);
      await loadDatasetsScreen();
      return;
    }
    toast("Draft created", "ok");
    closeAddDatasetWizard();
    await loadDatasetsScreen();
    navigateToDataset(result.name);
    if (kaggleRaw) await recheckDataset(result.name);
  } catch (e) { toast("Couldn't create: " + e.message, "err"); }
}

// ================================================================== folder/file picker (§8.5)
async function openFsPicker(mode, onUse) {
  state.datasetFsPicker = { path: null, mode, onUse };
  document.getElementById("fs-picker-backdrop").classList.remove("hidden");
  await fsPickerNavigate(null);
}

async function fsPickerNavigate(path) {
  const p = state.datasetFsPicker;
  if (!p) return;
  try {
    const data = await api(`/api/fs/list?mode=${encodeURIComponent(p.mode)}${path ? "&path=" + encodeURIComponent(path) : ""}`);
    p.path = data.path;
    document.getElementById("fs-picker-crumbs").textContent = data.path;
    const list = document.getElementById("fs-picker-list");
    const rows = [];
    if (data.parent) rows.push(`<button class="path-picker-item" data-fs-nav="${escapeHtml(data.parent)}">.. (up)</button>`);
    (data.entries || []).forEach((e) => {
      rows.push(`<button class="path-picker-item" data-fs-nav="${e.type === "dir" ? escapeHtml(e.path) : ""}" data-fs-file="${e.type === "file" ? escapeHtml(e.path) : ""}">${e.type === "dir" ? "📁" : "🖼"} ${escapeHtml(e.name)}</button>`);
    });
    list.innerHTML = rows.join("") || `<div class="empty-state">Empty.</div>`;
    list.querySelectorAll("[data-fs-nav]").forEach((btn) => {
      if (btn.dataset.fsNav) btn.addEventListener("click", () => fsPickerNavigate(btn.dataset.fsNav));
    });
    list.querySelectorAll("[data-fs-file]").forEach((btn) => {
      if (btn.dataset.fsFile) btn.addEventListener("click", () => { closeFsPicker(); state.datasetFsPicker.onUse(btn.dataset.fsFile); });
    });
  } catch (e) {
    document.getElementById("fs-picker-list").innerHTML = `<div class="empty-state">${escapeHtml(e.message)}</div>`;
  }
}

function closeFsPicker() {
  document.getElementById("fs-picker-backdrop").classList.add("hidden");
}

function useFsPickerFolder() {
  const p = state.datasetFsPicker;
  if (!p) return;
  closeFsPicker();
  p.onUse(p.path);
}

// ================================================================== init
function initDatasetsScreenButtons() {
  document.getElementById("btn-refresh-data")?.addEventListener("click", loadDatasetsScreen);
  document.getElementById("btn-datasets-recheck-all")?.addEventListener("click", recheckAllDatasets);
  document.getElementById("btn-dataset-add")?.addEventListener("click", openAddDatasetWizard);
  document.getElementById("btn-dataset-remove")?.addEventListener("click", removeDatasetFromXDash);
  document.getElementById("btn-dataset-recheck")?.addEventListener("click", () => {
    if (state.datasetDetail) { recheckDataset(state.datasetDetail.name).then(() => loadDatasetDetail(state.datasetDetail.name)); }
  });
  document.getElementById("datasets-search")?.addEventListener("input", (e) => { state.datasetsSearch = e.target.value; renderDatasetsList(); });
  document.getElementById("datasets-data-account")?.addEventListener("change", (e) => saveDataAccount(e.target.value));

  document.getElementById("dataset-add-cancel")?.addEventListener("click", closeAddDatasetWizard);
  document.getElementById("dataset-add-create")?.addEventListener("click", createDatasetFromWizard);
  document.getElementById("btn-dataset-add-pick-folder")?.addEventListener("click", () => {
    openFsPicker("dir", (path) => { document.getElementById("dataset-add-local-path").value = path; });
  });
  document.getElementById("dataset-add-backdrop")?.addEventListener("click", (e) => {
    if (e.target.id === "dataset-add-backdrop") closeAddDatasetWizard();
  });

  document.getElementById("fs-picker-cancel")?.addEventListener("click", closeFsPicker);
  document.getElementById("fs-picker-use")?.addEventListener("click", useFsPickerFolder);
  document.getElementById("fs-picker-backdrop")?.addEventListener("click", (e) => {
    if (e.target.id === "fs-picker-backdrop") closeFsPicker();
  });

  initDatasetHostOverrideForm();
}

initDatasetsScreenButtons();
