// static/js/lib/router.js
//
// Hash router (XDASH_PLAN.md §8, §10 Phase 3): every screen is addressable
// by a URL, and native `hashchange` gives back/forward for free.
//
//   #/lab
//   #/experiments?study=<id>&tab=experiments|configs|compare
//   #/x/<experiment_id>/<tab>              -- the Experiment page (§8.3)
//   #/compute/<runtime_id>                 -- the Compute runtime detail (§8.4,
//   #/datasets/<name>                         Phase 5) and the Datasets/Settings
//   #/settings/<section>                      per-item pages (§8.5/§8.6, Phase 4).
//
// Loaded LAST of every script tag (see index.html): every function this file
// bare-references (switchView, loadExperiments2Screen, applyExperimentsRouteParams,
// showExperimentDetailView, loadExperimentDetail, stopExperimentDetailPolling,
// escapeHtml) is defined by an earlier <script> tag and already exists by the
// time this file's top-level code runs — classic scripts execute fully, in
// document order, before this one starts (see memory's load-order note and
// spine.js's own comment on the same convention). Same global-scope model as
// every other file here: no imports, no exports, just bare identifiers.

const NAV_VIEW_TO_ROUTE = { lab: "lab", experiments: "experiments", compute: "compute", data: "datasets", settings: "settings" };
const ROUTE_TO_NAV_VIEW = { lab: "lab", experiments: "experiments", compute: "compute", datasets: "data", settings: "settings" };

function parseHash() {
  let raw = location.hash || "#/lab";
  if (raw.startsWith("#")) raw = raw.slice(1);
  if (!raw.startsWith("/")) raw = "/" + raw;
  const [pathPart, queryPart] = raw.slice(1).split("?");
  const segments = pathPart.split("/").filter(Boolean);
  return { segments, query: new URLSearchParams(queryPart || "") };
}

// Every plain nav-item click and every "go to X" call in the app funnels
// through here instead of calling switchView() directly, so the URL bar
// always reflects what's on screen (XDASH_PLAN.md §8's addressability rule).
function navigateToView(view, query) {
  const route = NAV_VIEW_TO_ROUTE[view] || view;
  let hash = "#/" + route;
  if (query) {
    const qs = new URLSearchParams(query).toString();
    if (qs) hash += "?" + qs;
  }
  if (location.hash === hash) handleRoute();
  else location.hash = hash;
}

// The Experiment page is a route, not a nav item (XDASH_PLAN.md §8) — this
// is what experimentLink() below points at, and what every "open this
// experiment" button in the app should call instead of hand-building a hash.
function navigateToExperiment(experimentId, tab) {
  const hash = `#/x/${encodeURIComponent(experimentId)}/${tab || "overview"}`;
  if (location.hash === hash) handleRoute();
  else location.hash = hash;
}

// XDASH_PLAN.md §8's rule: "every experiment name rendered anywhere is a
// link to its page." A plain <a href="#/x/...">, so a click needs no JS at
// all — the browser's own hashchange does the rest. `extra` is any string to
// append after the id (e.g. a badge) that must NOT be part of the link text.
function experimentLink(experimentId, label) {
  if (!experimentId) return "";
  const text = escapeHtml(label || experimentId);
  const href = `#/x/${encodeURIComponent(experimentId)}/overview`;
  return `<a href="${href}" class="exp-link" data-experiment-link="${escapeHtml(experimentId)}">${text}</a>`;
}

// Phase 4's two per-item routes, filled in by js/screens/datasets.js and
// js/screens/settings_profile.js — same "id/section segment reached only
// via the URL" shape as navigateToExperiment() above (XDASH_PROGRESS.md's
// Phase 3 handoff: these were stub segments switchView() ignored; Phase 4
// is what reads them now).
function navigateToDataset(name) {
  const hash = `#/datasets/${encodeURIComponent(name)}`;
  if (location.hash === hash) handleRoute();
  else location.hash = hash;
}

function navigateToSettingsSection(section) {
  const hash = `#/settings/${encodeURIComponent(section)}`;
  if (location.hash === hash) handleRoute();
  else location.hash = hash;
}

// Phase 5's runtime detail route (XDASH_PLAN.md §8.4), filled in by
// js/screens/compute.js — same "id segment reached only via the URL" shape
// as navigateToDataset()/navigateToExperiment() above. Passing no *runtimeId*
// (or one equal to the current selection) returns to the bare board.
function navigateToRuntime(runtimeId) {
  const hash = runtimeId ? `#/compute/${encodeURIComponent(runtimeId)}` : "#/compute";
  if (location.hash === hash) handleRoute();
  else location.hash = hash;
}

function handleRoute() {
  const { segments, query } = parseHash();
  const [first, second, third] = segments;

  if (first === "x" && second) {
    showExperimentDetailView();
    loadExperimentDetail(decodeURIComponent(second), third || "overview");
    return;
  }

  const view = ROUTE_TO_NAV_VIEW[first] || "lab";
  switchView(view);

  if (view === "experiments") {
    applyExperimentsRouteParams(query, second ? decodeURIComponent(second) : null);
  } else if (view === "data") {
    // js/screens/datasets.js (Phase 4): `second` is a dataset name, or
    // undefined for the bare matrix.
    applyDatasetsRouteParams(second ? decodeURIComponent(second) : null);
  } else if (view === "settings") {
    // js/screens/settings_profile.js (Phase 4): `second` is a section key
    // (a top-level profile key, or "alerts"/"about"), or undefined for
    // whatever section was last open.
    applySettingsRouteParams(second ? decodeURIComponent(second) : null);
  } else if (view === "compute") {
    // js/screens/compute.js (Phase 5): `second` is a runtime id (#3.6's
    // `id`, e.g. "ssh:mclab-gpu2"), or undefined for the bare board. This
    // was the one segment Phase 3 left switchView(view) to ignore.
    applyComputeRouteParams(second ? decodeURIComponent(second) : null);
  }
}

function initRouter() {
  window.addEventListener("hashchange", handleRoute);
  handleRoute(); // paint whatever's already in the URL bar on first load
}

initRouter();
