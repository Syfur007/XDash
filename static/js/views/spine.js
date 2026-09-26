// static/js/views/spine.js
//
// XDASH_PLAN.md Phase 3: the flat "Runs" spine table and its Run Composer
// (built in Phase 1, XDASH_PROGRESS.md's "Experiments β") are retired —
// static/js/screens/experiments2.js replaces both with the Studies-pane +
// grouped-by-config table + the real Composer that §8.2 describes. This
// file now holds only the one piece that's still generic infrastructure:
// the "delete experiment" confirmation modal (#delete-experiment-backdrop),
// which experiments2.js's row/bulk actions reuse as-is — same modal, same
// two opt-in checkboxes (downloaded Kaggle results / host-repo ledger row).
//
// Same classic-<script>-sharing-global-scope model as every other view file.

// Two tiers under one dialog (customizable, per the user's own framing): leaving both
// checkboxes unchecked is a soft delete (dashboard record only); checking either also removes
// that specific thing. Deliberately never offers to delete the underlying Kaggle kernel itself —
// kernel_slug_for_account() means one kernel is shared by every experiment on that account, so
// there's nothing "this experiment's own notebook" to safely delete anymore.
state.deleteExperimentTargetId = null;
state.deleteExperimentOnDone = null; // optional callback, set by the caller, run after a successful delete

function openDeleteExperimentModal(experimentId, onDone) {
  state.deleteExperimentTargetId = experimentId;
  state.deleteExperimentOnDone = onDone || null;
  document.getElementById("delete-experiment-body").textContent =
    `Delete '${experimentId}' from the dashboard.`;
  document.getElementById("delete-experiment-results").checked = false;
  document.getElementById("delete-experiment-ledger").checked = false;
  document.getElementById("delete-experiment-backdrop").classList.remove("hidden");
}

function closeDeleteExperimentModal() {
  document.getElementById("delete-experiment-backdrop").classList.add("hidden");
  state.deleteExperimentTargetId = null;
  state.deleteExperimentOnDone = null;
}

async function confirmDeleteExperiment() {
  const experimentId = state.deleteExperimentTargetId;
  if (!experimentId) return;
  const removeResults = document.getElementById("delete-experiment-results").checked;
  const removeLedger = document.getElementById("delete-experiment-ledger").checked;
  const params = new URLSearchParams();
  if (removeResults) params.set("remove_results", "1");
  if (removeLedger) params.set("remove_ledger", "1");
  const qs = params.toString() ? `?${params.toString()}` : "";
  const onDone = state.deleteExperimentOnDone;
  try {
    await api(`/api/experiments/${encodeURIComponent(experimentId)}${qs}`, { method: "DELETE" });
    toast(`Deleted '${experimentId}'`, "ok");
    closeDeleteExperimentModal();
    if (onDone) onDone();
  } catch (e) {
    toast(`Couldn't delete: ${e.message}`, "err");
  }
}

function initSpineButtons() {
  document.getElementById("delete-experiment-cancel").addEventListener("click", closeDeleteExperimentModal);
  document.getElementById("delete-experiment-confirm").addEventListener("click", confirmDeleteExperiment);
}

// Self-wiring, same convention as kaggle.js/data.js — every reference above is defined earlier
// in this same file, so calling this here (at script-load time) is safe regardless of whether
// app.js's boot() has run yet.
initSpineButtons();
