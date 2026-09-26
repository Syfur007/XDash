// static/js/lib/shortcuts.js
//
// XDASH_PLAN.md §10 Phase 6: a handful of single-key shortcuts, wired as one
// shared keydown handler rather than one per screen. `⌘/Ctrl-K` already
// opens the command palette (js/palette.js's own listener) — this file adds
// the rest:
//
//   /   focus the active screen's own filter/search input
//   j/k move the keyboard-focus highlight down/up in whichever list/table
//       is relevant to the active screen (the Experiments table, or the
//       Compare table when that subtab is open)
//   r   the focused Experiments-table row's own primary action button —
//       whichever one experiments2RowActionsHtml() already renders first for
//       that row's status (Queue for a draft, Run now is the second button
//       there so a draft's primary is "Queue"; Cancel while in flight;
//       Retry once terminal). Chosen over a fixed "r = retry" binding
//       because "run/retry the thing I'm looking at" is the one action that
//       makes sense across every status a row can be in, whereas a literal
//       always-retry binding would be a no-op (or wrong) on a draft/running
//       row. Documented here since the plan text left the exact meaning of
//       `r` to be inferred from U3's action set.
//
// Scoped so it never fires while typing (an input/textarea/select or a
// contenteditable element has focus) or while a modal is open — both the
// command palette's own listener and every existing per-screen `keydown`
// handler (e.g. the palette's ArrowUp/Down) already guard the same way, so
// this doesn't add a second, conflicting interpretation of the same keys.
//
// Same classic-<script>-sharing-global-scope model as every other file here
// — uses isPaletteOpen()/switchToSubtab()/currentExperimentsTab()/
// runExperiments2RowAction() as bare identifiers, all defined by earlier
// <script> tags and only ever called from inside this file's own event
// handlers (long after every script has loaded).

const KEYBOARD_FOCUS_CLASS = "kbd-focus";
state.keyboardFocusedRow = null; // the currently highlighted <tr>'s identifying key, if any

// Per-view/per-subtab "which input does `/` focus" — the first entry whose
// element actually exists and is visible (offsetParent !== null) wins, so a
// screen with no dedicated search input (Lab, Compute, Settings, the
// Experiment page) is simply a no-op rather than an error.
const SLASH_TARGET_CANDIDATES = [
  "experiments-search", "config-filter", "study-filter", "terminal-filter",
  "composer-config-filter", "report-filter", "run-group-filter",
];

function isTypingTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

function isAnyModalOpen() {
  return !!document.querySelector(".modal-backdrop:not(.hidden)");
}

function focusActiveScreenSearchInput() {
  for (const id of SLASH_TARGET_CANDIDATES) {
    const el = document.getElementById(id);
    if (el && el.offsetParent !== null) {
      el.focus();
      el.select();
      return true;
    }
  }
  return false;
}

// ---------------------------------------------------------------- j/k row focus
// Two known navigable tables (XDASH_PLAN.md §10 Phase 6's own list); each
// entry says how to find its rows and, for the Experiments table, how to
// read the row's own experiment id back out for the `r` binding.
function activeKeyboardTable() {
  const experimentsView = document.getElementById("view-experiments");
  if (!experimentsView || !experimentsView.classList.contains("active")) return null;
  const tab = currentExperimentsTab();
  if (tab === "experiments") {
    const rows = Array.from(document.querySelectorAll("#experiments-table-body tr[data-experiment-row]"));
    return rows.length ? { rows, kind: "experiments" } : null;
  }
  if (tab === "compare") {
    const rows = Array.from(document.querySelectorAll("#study-compare-table tbody tr[data-compare-row]"));
    return rows.length ? { rows, kind: "compare" } : null;
  }
  return null;
}

function clearKeyboardFocusRow() {
  document.querySelectorAll(`.${KEYBOARD_FOCUS_CLASS}`).forEach((el) => el.classList.remove(KEYBOARD_FOCUS_CLASS));
}

function moveKeyboardFocus(delta) {
  const table = activeKeyboardTable();
  if (!table) return;
  const { rows } = table;
  let idx = rows.findIndex((r) => r.classList.contains(KEYBOARD_FOCUS_CLASS));
  if (idx === -1) idx = delta > 0 ? -1 : rows.length;
  idx = Math.max(0, Math.min(rows.length - 1, idx + delta));
  clearKeyboardFocusRow();
  const row = rows[idx];
  row.classList.add(KEYBOARD_FOCUS_CLASS);
  row.scrollIntoView({ block: "nearest" });
  state.keyboardFocusedRow = row.dataset.experimentRow || row.dataset.compareRow || null;
}

function runFocusedRowPrimaryAction() {
  const table = activeKeyboardTable();
  if (!table || table.kind !== "experiments") return; // no per-row action on the Compare table
  const row = table.rows.find((r) => r.classList.contains(KEYBOARD_FOCUS_CLASS));
  if (!row) return;
  const btn = row.querySelector("button[data-row-action]");
  if (btn) btn.click();
}

function initKeyboardShortcuts() {
  document.addEventListener("keydown", (e) => {
    if (isTypingTarget(document.activeElement) || isAnyModalOpen() || isPaletteOpen()) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return; // leave ⌘K etc. to their own handlers

    if (e.key === "/") {
      if (focusActiveScreenSearchInput()) e.preventDefault();
    } else if (e.key === "j") {
      e.preventDefault();
      moveKeyboardFocus(1);
    } else if (e.key === "k") {
      e.preventDefault();
      moveKeyboardFocus(-1);
    } else if (e.key === "r") {
      e.preventDefault();
      runFocusedRowPrimaryAction();
    }
  });

  // A fresh render (poll tick, filter change, tab switch) rebuilds every row
  // from scratch, so the highlight class is gone even though state still
  // names the row — reapply it after the table body's own re-render.
  document.addEventListener("xdash:rows-rendered", () => {
    if (!state.keyboardFocusedRow) return;
    const row = document.querySelector(`[data-experiment-row="${CSS.escape(state.keyboardFocusedRow)}"], [data-compare-row="${CSS.escape(state.keyboardFocusedRow)}"]`);
    if (row) row.classList.add(KEYBOARD_FOCUS_CLASS);
  });
}

initKeyboardShortcuts();
