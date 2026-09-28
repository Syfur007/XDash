// static/js/lib/poller.js
//
// One shared poller (XDASH_PLAN.md §9 "one poller"): every screen that needs
// to refresh itself while visible gets a `createPoller(fn, intervalMs)`
// instead of its own bespoke setInterval. It pauses while the document is
// hidden (visibilitychange) and backs off (doubling, capped) after a failed
// tick, resetting to the base interval on the next success — so a screen
// left open overnight against a dead server doesn't hammer it every few
// seconds forever.
//
// Pre-existing pollers (lab.js's startLabPolling/stopLabPolling, the
// boot()-level setInterval for terminals/monitors/scheduler) are untouched
// this phase — they already work and touching them isn't this phase's job.
// New Phase 3 screens (the Experiments spine, the Experiment page) use this
// instead of writing a fourth copy of the same pattern.
//
// Classic script, same global-scope convention as every other file here:
// exposes `createPoller` as a bare top-level function, usable from any
// later-loaded script or module.

// XDASH_FIXES_PLAN.md F1.2 — one shared guard for a poller that re-renders a
// form: skip the re-render while *containerId* holds focus (mid-keystroke —
// e.g. the Experiment page Notes tab, the legacy Kaggle subtab's open
// credential form) or while *dirtyFlagKey* (a `state.<key>` boolean, same
// pattern as static/js/screens/settings_profile.js's own state.profileDirty)
// is set. Both are optional; a caller that only cares about focus passes no
// second argument. Returns true = "skip this render".
function formPollGuard(containerId, dirtyFlagKey) {
  const el = document.getElementById(containerId);
  if (el && document.activeElement && el.contains(document.activeElement) && document.activeElement !== document.body) return true;
  if (dirtyFlagKey && state[dirtyFlagKey]) return true;
  return false;
}

function createPoller(fn, intervalMs, opts = {}) {
  const maxIntervalMs = opts.maxIntervalMs || intervalMs * 8;
  let timer = null;
  let currentInterval = intervalMs;
  let running = false;
  let inFlight = false;

  function schedule() {
    clearTimeout(timer);
    timer = null;
    if (!running) return;
    timer = setTimeout(tick, currentInterval);
  }

  async function tick() {
    timer = null;
    if (!running || document.hidden || inFlight) return;
    inFlight = true;
    try {
      await fn();
      currentInterval = intervalMs;
    } catch (e) {
      currentInterval = Math.min(currentInterval * 2, maxIntervalMs);
    } finally {
      inFlight = false;
      schedule();
    }
  }

  function start() {
    if (running) return;
    running = true;
    currentInterval = intervalMs;
    tick();
  }

  function stop() {
    running = false;
    clearTimeout(timer);
    timer = null;
  }

  // Resume immediately (rather than waiting out whatever's left of a
  // back-off interval) the moment the tab becomes visible again — a
  // screen a user just switched back to should refresh right away, not
  // silently sit stale for up to maxIntervalMs.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(timer);
      timer = null;
    } else if (running && !inFlight && !timer) {
      tick();
    }
  });

  return { start, stop };
}
