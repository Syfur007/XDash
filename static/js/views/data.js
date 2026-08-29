// static/js/views/data.js
//
// Data Studio: registered datasets (configs/dataset/*.yaml fragments — see
// backend/datasets_info.py), a channel-mode montage preview (bridge-backed,
// via backend/bridge_scripts/channel_preview.py — makes m1..m5 a real
// picture instead of an abstract config string, per the spec's S2 gate
// artifact), and the guarded test-set evaluation audit trail straight off
// artifacts/ledger/test_evals.csv (backend/ledger.py — already built in
// Phase 1, reused here as-is).
//
// Same classic-<script>-sharing-global-scope model as the other view files.

state.datasetList = [];
state.selectedDatasetFragment = null;

async function loadDataView() {
  await Promise.all([loadDatasetCards(), loadTestEvalsTable()]);
}

async function loadDatasetCards() {
  const listEl = document.getElementById("dataset-card-list");
  listEl.innerHTML = `<div class="empty-state">Loading…</div>`;
  try {
    const data = await api("/api/datasets");
    state.datasetList = data.datasets || [];
    renderDatasetCards();
  } catch (e) {
    listEl.innerHTML = `<div class="empty-state">Couldn't load datasets: ${escapeHtml(e.message)}</div>`;
  }
}

function renderDatasetCards() {
  const listEl = document.getElementById("dataset-card-list");
  const countEl = document.getElementById("dataset-count");
  countEl.textContent = state.datasetList.length ? String(state.datasetList.length) : "";

  if (!state.datasetList.length) {
    listEl.innerHTML = `<div class="empty-state">No configs/dataset/*.yaml fragments found.</div>`;
    return;
  }

  listEl.innerHTML = state.datasetList.map((ds) => {
    const active = ds.fragment === state.selectedDatasetFragment ? "active" : "";
    const mode = ds.channel_mode || "m1";
    const modality = ds.modality || "colour";
    const badges = [`<span class="badge slate">${escapeHtml(mode)} · ${escapeHtml(modality)}</span>`];
    if (ds.dedup) badges.push(`<span class="badge emerald">dedup</span>`);
    if (ds.external) badges.push(`<span class="badge red">external</span>`);
    return `<div class="dataset-card ${active}" data-fragment="${escapeHtml(ds.fragment)}">
      <div class="dataset-card-name">${escapeHtml(ds.name || ds.fragment)}</div>
      <div class="dataset-card-root" title="${escapeHtml(ds.root || "")}">${escapeHtml(ds.root || "")}</div>
      <div class="dataset-card-badges">${badges.join("")}</div>
    </div>`;
  }).join("");

  listEl.querySelectorAll(".dataset-card").forEach((el) => {
    el.addEventListener("click", () => selectDataset(el.dataset.fragment));
  });
}

function selectDataset(fragment) {
  state.selectedDatasetFragment = fragment;
  renderDatasetCards();

  const ds = state.datasetList.find((d) => d.fragment === fragment);
  const panel = document.getElementById("dataset-detail-panel");
  if (!ds) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  document.getElementById("dataset-detail-title").textContent = ds.name || ds.fragment;

  const kvEl = document.getElementById("dataset-detail-kv");
  const rows = Object.entries(ds).filter(([k]) => k !== "fragment");
  kvEl.innerHTML = rows.map(([k, v]) =>
    `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(typeof v === "object" ? JSON.stringify(v) : String(v))}</td></tr>`
  ).join("");

  document.getElementById("channel-preview-mode").value = ds.channel_mode || "m1";
  document.getElementById("channel-preview-modality").value = ds.modality || "colour";
  document.getElementById("channel-preview-result").innerHTML = "";
}

async function previewChannels() {
  const btn = document.getElementById("btn-channel-preview");
  const resultEl = document.getElementById("channel-preview-result");
  const imagePath = document.getElementById("channel-preview-image-path").value.trim();
  const mode = document.getElementById("channel-preview-mode").value;
  const modality = document.getElementById("channel-preview-modality").value;

  if (!imagePath) {
    toast("Enter a sample image path first", "err");
    return;
  }

  const originalLabel = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Rendering…";
  resultEl.innerHTML = `<div class="empty-state">Building channels…</div>`;
  try {
    const result = await api("/api/datasets/channel-preview", {
      method: "POST",
      body: JSON.stringify({ image_path: imagePath, mode, modality }),
    });
    renderChannelTiles(result);
  } catch (e) {
    resultEl.innerHTML = `<div class="empty-state">${escapeHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = originalLabel;
  }
}

function renderChannelTiles(result) {
  const resultEl = document.getElementById("channel-preview-result");
  const meta = `<div class="channel-preview-meta">${escapeHtml(result.mode)} · ${escapeHtml(result.modality)} · ${result.effective_channels} effective channels · groups: ${result.groups.map(escapeHtml).join(", ")} · source ${result.source_size[1]}×${result.source_size[0]}</div>`;
  const tiles = result.tiles.map((t) =>
    `<div class="channel-tile">
      <img src="${t.png}" alt="${escapeHtml(t.group)} channel ${t.index_in_group}" />
      <div class="channel-tile-label">${escapeHtml(t.group)}[${t.index_in_group}]</div>
    </div>`
  ).join("");
  resultEl.innerHTML = `${meta}<div class="channel-tile-grid">${tiles}</div>`;
}

async function loadTestEvalsTable() {
  const tableEl = document.getElementById("test-evals-table");
  const countEl = document.getElementById("test-evals-count");
  try {
    const data = await api("/api/ledger/test_evals");
    const rows = data.rows || [];
    countEl.textContent = rows.length ? String(rows.length) : "";
    if (!rows.length) {
      tableEl.innerHTML = `<tr><td colspan="5" class="empty-state" style="padding:20px 0;">No test-set evaluations recorded yet in artifacts/ledger/test_evals.csv — every issue_test_token() call appends a row here, so this is a real audit trail of every touch of the guarded test set, not a convention anyone has to remember.</td></tr>`;
      return;
    }
    const header = `<tr><th>Run ID</th><th>Token</th><th>Issued</th><th>Config hash</th><th>Checkpoint</th></tr>`;
    const body = rows.map((r) => `<tr>
      <td>${escapeHtml(r.run_id || "")}</td>
      <td title="${escapeHtml(r.token || "")}">${escapeHtml((r.token || "").slice(0, 10))}…</td>
      <td>${escapeHtml(r.issued_time || "")}</td>
      <td title="${escapeHtml(r.config_hash || "")}">${escapeHtml((r.config_hash || "").slice(0, 10))}…</td>
      <td>${escapeHtml(r.checkpoint_path || "–")}</td>
    </tr>`).join("");
    tableEl.innerHTML = `<thead>${header}</thead><tbody>${body}</tbody>`;
  } catch (e) {
    tableEl.innerHTML = `<tr><td colspan="5" class="empty-state" style="padding:20px 0;">${escapeHtml(e.message)}</td></tr>`;
  }
}

function initDataButtons() {
  document.getElementById("btn-refresh-data").addEventListener("click", loadDataView);
  document.getElementById("btn-channel-preview").addEventListener("click", previewChannels);
}

initDataButtons();
