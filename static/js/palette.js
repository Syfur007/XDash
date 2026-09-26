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

// XDASH_PLAN.md §8/§10 Phase 6: ⌘K search over studies/experiments/configs/
// runtimes/datasets, not just configs/runs/reports. A dedicated, unfiltered
// fetch (not state.experiments2, which is scoped to whatever study the
// Experiments screen last selected) — loaded once and cached, same
// lazy-on-first-open pattern as configs/runs/reports below.
state.paletteExperiments = [];

async function loadPaletteExperiments() {
  try { state.paletteExperiments = (await api("/api/experiments")).experiments || []; } catch (e) { /* index just skips experiments this time */ }
}

async function ensurePaletteDataLoaded() {
  const tasks = [];
  if (!state.configs.length) tasks.push(loadConfigs());
  if (!state.runGroups.length) tasks.push(loadRunGroups());
  if (!state.reportGroups.length) tasks.push(loadReports());
  if (!state.studies.length) tasks.push(loadStudies());
  if (!state.paletteExperiments.length) tasks.push(loadPaletteExperiments());
  if (!state.runtimesLoaded) {
    tasks.push(api("/api/runtimes").then((d) => { state.runtimesList = d.runtimes || []; state.runtimesLoaded = true; }).catch(() => {}));
  }
  if (!state.datasetsLoaded) tasks.push(loadDatasetsScreen());
  if (tasks.length) await Promise.all(tasks);
}

function buildPaletteIndex() {
  const items = [];

  document.querySelectorAll(".nav-item").forEach((el) => {
    items.push({ kind: "tab", label: el.textContent.trim(), sub: "Go to tab", action: () => switchView(el.dataset.view) });
  });

  // Compute subtabs (Multi_runner_XDash.md Phase 6) — the nav-item loop above
  // only reaches top-level tabs, so a fleet subsurface needs its own entries,
  // same as Experiments' Configs/Sessions subtabs get via switchToSubtab().
  [
    ["machines", "Machines"], ["kaggle", "Kaggle"], ["colab", "Colab"], ["monitors", "Monitors"],
  ].forEach(([key, label]) => {
    items.push({
      kind: "tab", label: `Compute · ${label}`, sub: "Go to subtab",
      action: () => switchToSubtab("compute", "compute-subtabs", key),
    });
  });

  for (const h of state.computeHosts || []) {
    items.push({
      kind: "host", label: h.label || h.id, sub: h.id,
      action: () => switchToSubtab("compute", "compute-subtabs", "machines"),
    });
  }
  for (const a of state.kaggleAccounts || []) {
    items.push({
      kind: "kaggle account", label: a.name, sub: a.kaggle_username || "",
      action: () => switchToSubtab("compute", "compute-subtabs", "kaggle"),
    });
  }

  for (const group of state.configs) {
    for (const c of group.configs) {
      items.push({ kind: "config", label: c.name, sub: c.path, action: () => { switchToSubtab("experiments", "experiments-subtabs", "configs"); selectConfig(c.path); } });
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
        action: () => { switchToSubtab("results", "results-subtabs", "runs"); selectRun(r.run_id); },
      });
    }
  }

  for (const group of state.reportGroups) {
    for (const rep of group.reports) {
      items.push({
        kind: "report",
        label: rep.experiment || rep.name,
        sub: rep.path,
        action: () => { switchToSubtab("results", "results-subtabs", "reports"); loadReportDetail(rep.path); },
      });
    }
  }

  // ---------------------------------------------------------- Phase 6: studies/experiments/runtimes/datasets
  for (const s of state.studies) {
    items.push({
      kind: "study", label: s.name, sub: `${s.status} · ${s.experiment_count ?? 0} experiments`,
      action: () => navigateToView("experiments", { study: s.study_id, tab: "experiments" }),
    });
  }

  for (const e of state.paletteExperiments) {
    items.push({
      kind: "experiment", label: e.experiment_id, sub: `${e.status} · ${e.config_path || ""}`,
      action: () => navigateToExperiment(e.experiment_id),
    });
  }

  for (const r of state.runtimesList) {
    items.push({
      kind: "runtime", label: r.label || r.id, sub: `${r.kind} · ${r.state}`,
      action: () => navigateToRuntime(r.id),
    });
  }

  for (const d of state.datasetRegistry) {
    items.push({ kind: "dataset", label: d.name, sub: d.root || "", action: () => navigateToDataset(d.name) });
  }

  // ---------------------------------------------------------- action verbs
  // Fuzzy-matched command-style entries on top of the search above, not a
  // parallel command system: each one resolves a study/selection and calls
  // the same Phase 1 /api/experiments/actions endpoint every button in the
  // app already uses. "queue size-ablation drafts" (XDASH_PLAN.md §10 Phase
  // 6's own example) matches any study by name via the fuzzy filter below.
  for (const s of state.studies) {
    const draftCount = (s.counts && s.counts.draft) || 0;
    if (draftCount > 0) {
      items.push({
        kind: "action", label: `Queue drafts — ${s.name}`, sub: `${draftCount} draft(s) in this study`,
        action: () => queueStudyDrafts(s.study_id, s.name),
      });
    }
    items.push({
      kind: "action", label: `Compare — ${s.name}`, sub: "Open Study Compare",
      action: () => navigateToView("experiments", { study: s.study_id, tab: "compare" }),
    });
    items.push({
      kind: "action", label: `Toggle autopilot — ${s.name}`, sub: s.autopilot && s.autopilot.enabled ? "currently on" : "currently off",
      action: async () => {
        try {
          await api(`/api/studies/${encodeURIComponent(s.study_id)}/autopilot`, {
            method: "POST", body: JSON.stringify({ enabled: !(s.autopilot && s.autopilot.enabled) }),
          });
          toast(`Autopilot ${s.autopilot && s.autopilot.enabled ? "stopped" : "started"} for '${s.name}'`, "ok");
          state.studies = []; // force the palette (and next Experiments-screen visit) to refetch
        } catch (e) { toast(`Couldn't toggle autopilot: ${e.message}`, "err"); }
      },
    });
  }
  items.push({ kind: "action", label: "New study", sub: "Open the new-study dialog", action: () => { switchToSubtab("experiments", "experiments-subtabs", "experiments"); openStudyEditModal(null); } });
  items.push({ kind: "action", label: "New experiments…", sub: "Open the Composer", action: () => { switchToSubtab("experiments", "experiments-subtabs", "experiments"); openComposer(); } });

  return items;
}

// "queue size-ablation drafts": resolves the named study and calls the
// Phase 1 actions endpoint with that scope — _act_queue (backend/
// experiments.py) already skips anything not a draft, so this is safe to
// run against a study with a mix of statuses, not just pure-draft ones.
async function queueStudyDrafts(studyId, studyName) {
  try {
    const result = await api("/api/experiments/actions", { method: "POST", body: JSON.stringify({ action: "queue", study_id: studyId }) });
    toast(`Queued ${result.ok.length} draft(s) in '${studyName}'`, "ok");
    if (document.getElementById("view-experiments").classList.contains("active")) {
      refreshExperiments2AllData();
    } else {
      state.studies = []; // force a refetch next time the palette or Experiments screen asks
    }
  } catch (e) {
    toast(`Couldn't queue drafts: ${e.message}`, "err");
  }
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
