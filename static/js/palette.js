// static/js/palette.js
//
// Command palette (Ctrl/Cmd-K): fuzzy-jump to any config, run, report, or
// tab by name, without hunting through the sidebar. Builds its search
// index from whatever's already in `state` — configs are loaded at boot,
// runs/reports are lazy-loaded on first open if the user hasn't visited
// those tabs yet (an explicit action, not a background fetch — keeps the
// "nothing happens unless you're looking at it" design the README
// documents; opening the palette IS looking at it).
//
// Same classic-<script>-sharing-global-scope model as every other view
// file — uses api()/state/switchView()/selectConfig()/loadRunGroups()/
// selectRun()/loadReports()/loadReportDetail()/escapeHtml() as bare
// identifiers.

state.paletteItems = [];
state.paletteFiltered = [];
state.paletteActiveIndex = 0;

async function ensurePaletteDataLoaded() {
  const tasks = [];
  if (!state.configs.length) tasks.push(loadConfigs());
  if (!state.runGroups.length) tasks.push(loadRunGroups());
  if (!state.reportGroups.length) tasks.push(loadReports());
  if (tasks.length) await Promise.all(tasks);
}

function buildPaletteIndex() {
  const items = [];

  document.querySelectorAll(".nav-item").forEach((el) => {
    items.push({ kind: "tab", label: el.textContent.trim(), sub: "Go to tab", action: () => switchView(el.dataset.view) });
  });

  for (const group of state.configs) {
    for (const c of group.configs) {
      items.push({ kind: "config", label: c.name, sub: c.path, action: () => { switchView("configs"); selectConfig(c.path); } });
    }
  }

  for (const group of state.runGroups) {
    for (const r of group.runs) {
      const logging = (r.resolved_config && r.resolved_config.logging) || {};
      const expLabel = logging.experiment_name || group.config_hash.slice(0, 7);
      items.push({
        kind: "run",
        label: r.run_id,
        sub: `${expLabel} · ${r.status}`,
        action: () => { switchView("runs"); selectRun(r.run_id); },
      });
    }
  }

  for (const group of state.reportGroups) {
    for (const rep of group.reports) {
      items.push({
        kind: "report",
        label: rep.experiment || rep.name,
        sub: rep.path,
        action: () => { switchView("reports"); loadReportDetail(rep.path); },
      });
    }
  }

  return items;
}

function filterPaletteItems(items, query) {
  const q = query.trim().toLowerCase();
  if (!q) return items.slice(0, 30);
  return items.filter((it) => `${it.label} ${it.sub || ""}`.toLowerCase().includes(q)).slice(0, 30);
}

function renderPaletteResults() {
  const el = document.getElementById("command-palette-results");
  if (!state.paletteFiltered.length) {
    el.innerHTML = `<div class="empty-state">No matches</div>`;
    return;
  }
  el.innerHTML = state.paletteFiltered.map((it, i) =>
    `<div class="palette-result-item ${i === state.paletteActiveIndex ? "active" : ""}" data-index="${i}">
      <span class="palette-kind">${escapeHtml(it.kind)}</span>
      <span class="palette-label">${escapeHtml(it.label)}</span>
      <span class="palette-sub">${escapeHtml(it.sub || "")}</span>
    </div>`
  ).join("");
  el.querySelectorAll(".palette-result-item").forEach((row) => {
    row.addEventListener("click", () => selectPaletteItem(Number(row.dataset.index)));
    row.addEventListener("mouseenter", () => {
      el.querySelectorAll(".palette-result-item.active").forEach((r) => r.classList.remove("active"));
      row.classList.add("active");
      state.paletteActiveIndex = Number(row.dataset.index);
    });
  });
}

function selectPaletteItem(index) {
  const item = state.paletteFiltered[index];
  if (!item) return;
  closePalette();
  item.action();
}

function scrollPaletteActiveIntoView() {
  const active = document.querySelector(".palette-result-item.active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

function isPaletteOpen() {
  return !document.getElementById("command-palette-backdrop").classList.contains("hidden");
}

async function openPalette() {
  document.getElementById("command-palette-backdrop").classList.remove("hidden");
  const input = document.getElementById("command-palette-input");
  input.value = "";
  input.focus();
  document.getElementById("command-palette-results").innerHTML = `<div class="empty-state">Loading…</div>`;
  await ensurePaletteDataLoaded();
  state.paletteItems = buildPaletteIndex();
  state.paletteFiltered = filterPaletteItems(state.paletteItems, "");
  state.paletteActiveIndex = 0;
  renderPaletteResults();
}

function closePalette() {
  document.getElementById("command-palette-backdrop").classList.add("hidden");
}

function initPalette() {
  const backdrop = document.getElementById("command-palette-backdrop");
  const input = document.getElementById("command-palette-input");

  // Ctrl/⌘K has no equivalent without a physical keyboard, so the mobile
  // topbar gets a tap target for the same entry point.
  document.getElementById("btn-open-palette").addEventListener("click", openPalette);

  document.addEventListener("keydown", (e) => {
    const isMac = navigator.platform.toUpperCase().includes("MAC");
    const modKey = isMac ? e.metaKey : e.ctrlKey;
    if (modKey && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (isPaletteOpen()) closePalette(); else openPalette();
      return;
    }
    if (isPaletteOpen() && e.key === "Escape") closePalette();
  });

  input.addEventListener("input", () => {
    state.paletteFiltered = filterPaletteItems(state.paletteItems, input.value);
    state.paletteActiveIndex = 0;
    renderPaletteResults();
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      state.paletteActiveIndex = Math.min(state.paletteActiveIndex + 1, state.paletteFiltered.length - 1);
      renderPaletteResults();
      scrollPaletteActiveIntoView();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      state.paletteActiveIndex = Math.max(state.paletteActiveIndex - 1, 0);
      renderPaletteResults();
      scrollPaletteActiveIntoView();
    } else if (e.key === "Enter") {
      e.preventDefault();
      selectPaletteItem(state.paletteActiveIndex);
    } else if (e.key === "Escape") {
      closePalette();
    }
  });

  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) closePalette();
  });
}

initPalette();
