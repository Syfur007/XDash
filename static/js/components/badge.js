// static/js/components/badge.js
//
// Small status-pill renderer shared by every view that needs a consistent
// visual vocabulary for run/job status, instead of ad hoc colored spans.
// Reuses the existing .badge CSS class (styles.css) — this file just adds
// the run-status color mapping and a couple of new color modifiers
// (.emerald/.red/.slate) alongside the ones Reports already defined
// (.amber/.teal/.violet).
//
// Loaded as a plain classic <script> (no build step, no ES module
// wrapping), exactly like app.js — it shares the page's single global
// scope with app.js and every other view script, so escapeHtml() and the
// STATUS_BADGE_CLASS map declared here are usable as bare identifiers
// from runs.js and later views without an import. See
// IMPLEMENTATION_PLAN.md Phase 7 for the eventual full ES-module split;
// this file is written to be a trivial `export` away from that.

const STATUS_BADGE_CLASS = {
  running: "amber",
  pending: "slate",
  done: "emerald",
  completed: "emerald",
  stopped: "slate",
  failed: "red",
};

function statusBadgeClass(status) {
  return STATUS_BADGE_CLASS[status] || "slate";
}

function renderStatusBadge(status) {
  return `<span class="badge ${statusBadgeClass(status)}">${escapeHtml(status || "unknown")}</span>`;
}
