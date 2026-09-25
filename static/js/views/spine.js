// static/js/views/spine.js
//
// The new Experiments spine (XDASH_V2_PLAN.md §6.4) + Run Composer (§6.5) —
// backed by Phase B's GET/POST /api/experiments. Added ALONGSIDE the
// existing "Experiments" tab (static/js/views/experiments.js, itself backed
// by the old Assignments/scheduler/Kaggle-worker routes), not replacing it
// yet — see that nav item's own title attribute for why. Once this has been
// tried out, a follow-up change flips the default and removes the old tab,
// per the plan's own "strangler, not big-bang" migration (§6.8).
//
// Same classic-<script>-sharing-global-scope model as every other view file.

state.spineExperiments = [];
state.spineLoaded = false;
state.spineSelectedConfigs = new Set();   // config paths checked in the Run Composer
state.spineConfigFilter = "";

// ---------------------------------------------------------------- table
async function loadSpine() {
  state.spineLoaded = true;
  const status = document.getElementById("spine-status-filter")?.value || "";
  const body = document.getElementById("spine-table-body");
  try {
    const qs = status ? `?status=${encodeURIComponent(status)}` : "";
    const data = await api(`/api/experiments${qs}`);
    state.spineExperiments = data.experiments || [];
  } catch (e) {
    if (body) body.innerHTML = `<tr><td colspan="8" class="empty-state">Couldn't load experiments: ${escapeHtml(e.message)}</td></tr>`;
    return;
  }
  renderSpineTable();
}

function renderSpineTable() {
  const body = document.getElementById("spine-table-body");
  const countEl = document.getElementById("spine-count");
  const rows = state.spineExperiments;
  if (countEl) countEl.textContent = rows.length ? `${rows.length} experiment${rows.length === 1 ? "" : "s"}` : "";
  if (!body) return;
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="8" class="empty-state">No experiments yet — click + Run to launch some.</td></tr>`;
    return;
  }
  body.innerHTML = rows.map(spineRowHtml).join("");
  body.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.id;
      const action = btn.dataset.action;
      if (action === "spine-retry") retrySpineExperiment(id);
      else if (action === "spine-cancel") cancelSpineExperiment(id);
      else if (action === "spine-delete") openDeleteExperimentModal(id);
    });
  });
}

function spineRowHtml(exp) {
  const a = exp.current_attempt || {};
  const seed = exp.seed === null || exp.seed === undefined ? "—" : escapeHtml(String(exp.seed));
  let slotOrWhy = escapeHtml(a.slot || "—");
  // "blocked" means not-yet-dispatched-and-infeasible (§3.5); "failed" now also carries a
  // populated `blocked` — the reason THIS attempt didn't make it, kept even once retries are
  // exhausted, instead of being discarded the way a silently-requeued attempt used to be.
  if ((exp.status === "blocked" || exp.status === "failed") && a.blocked) {
    slotOrWhy = `<span class="badge red" title="${escapeHtml(a.blocked.detail || "")}">${escapeHtml(a.blocked.code || exp.status)}</span> ${escapeHtml(a.blocked.detail || "")}`;
    if (exp.attempt_count > 1) slotOrWhy += ` <span class="settings-profile-path">(${exp.attempt_count} attempts)</span>`;
  } else if (a.raw_status) {
    slotOrWhy += ` <span class="settings-profile-path">(${escapeHtml(a.raw_status)})</span>`;
  }
  // A chained (Multi_runner_XDash.md Phase 5) multi-leg experiment must read
  // as one row with its chain visible — leg_index > 1 means this attempt
  // is resuming a previous one on the same run_id, not a fresh start.
  if (a.leg_index && a.leg_index > 1) {
    const maxLegs = exp.max_legs || 6;
    slotOrWhy = `<span class="mode-tag" title="Leg ${a.leg_index} of at most ${maxLegs}, resumed from a previous attempt">leg ${a.leg_index}/${maxLegs}</span> ${slotOrWhy}`;
  }
  // Any non-in-flight status is deletable (2026-09-22: "failed" used to have no delete switch
  // at all — the backend only allowed pending/blocked — which left failed experiments with no
  // way to ever clear them). Only dispatching/running is refused, so this can never orphan a
  // live scheduler item or Kaggle push out from under the dashboard.
  const canDelete = exp.status !== "dispatching" && exp.status !== "running";
  const canCancel = exp.status === "dispatching" || exp.status === "running";
  const canRetry = exp.status === "done" || exp.status === "failed" || exp.status === "cancelled";
  return `<tr data-experiment-id="${escapeHtml(exp.experiment_id)}">
    <td>${escapeHtml(exp.experiment_id)}</td>
    <td>${escapeHtml(exp.config_path)}</td>
    <td>${seed}</td>
    <td>${escapeHtml(exp.batch_name || "—")}</td>
    <td>${renderStatusBadge(exp.status)}</td>
    <td>${slotOrWhy}</td>
    <td>${escapeHtml(timeAgo(a.updated_at || exp.created_at))}</td>
    <td>
      ${canRetry ? `<button class="btn-icon" data-action="spine-retry" data-id="${escapeHtml(exp.experiment_id)}" title="Retry">↻</button>` : ""}
      ${canCancel ? `<button class="btn-icon" data-action="spine-cancel" data-id="${escapeHtml(exp.experiment_id)}" title="Cancel">⏹</button>` : ""}
      ${canDelete ? `<button class="btn-icon" data-action="spine-delete" data-id="${escapeHtml(exp.experiment_id)}" title="Delete">✕</button>` : ""}
    </td>
  </tr>`;
}

async function retrySpineExperiment(experimentId) {
  try {
    await api(`/api/experiments/${encodeURIComponent(experimentId)}/retry`, { method: "POST" });
    toast(`Retrying '${experimentId}'`, "ok");
    loadSpine();
  } catch (e) { toast(`Couldn't retry: ${e.message}`, "err"); }
}

async function cancelSpineExperiment(experimentId) {
  const ok = await showConfirm("Cancel experiment?", `Stops '${experimentId}''s in-flight attempt where possible.`);
  if (!ok) return;
  try {
    await api(`/api/experiments/${encodeURIComponent(experimentId)}/cancel`, { method: "POST" });
    toast(`Cancelled '${experimentId}'`, "ok");
    loadSpine();
  } catch (e) { toast(`Couldn't cancel: ${e.message}`, "err"); }
}

// Two tiers under one dialog (customizable, per the user's own framing): leaving both
// checkboxes unchecked is a soft delete (dashboard record only); checking either also removes
// that specific thing. Deliberately never offers to delete the underlying Kaggle kernel itself —
// kernel_slug_for_account() means one kernel is shared by every experiment on that account, so
// there's nothing "this experiment's own notebook" to safely delete anymore.
state.deleteExperimentTargetId = null;

function openDeleteExperimentModal(experimentId) {
  state.deleteExperimentTargetId = experimentId;
  document.getElementById("delete-experiment-body").textContent =
    `Delete '${experimentId}' from the dashboard.`;
  document.getElementById("delete-experiment-results").checked = false;
  document.getElementById("delete-experiment-ledger").checked = false;
  document.getElementById("delete-experiment-backdrop").classList.remove("hidden");
}

function closeDeleteExperimentModal() {
  document.getElementById("delete-experiment-backdrop").classList.add("hidden");
  state.deleteExperimentTargetId = null;
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
  try {
    await api(`/api/experiments/${encodeURIComponent(experimentId)}${qs}`, { method: "DELETE" });
    toast(`Deleted '${experimentId}'`, "ok");
    closeDeleteExperimentModal();
    loadSpine();
  } catch (e) {
    toast(`Couldn't delete: ${e.message}`, "err");
  }
}

// ---------------------------------------------------------------- Run Composer (§6.5)
// The single highest-leverage screen in the plan: one dialog collapses every launch path into
// one POST /api/experiments call. Reuses state.configs (already loaded by loadConfigs() at
// boot for the Configs tab) instead of fetching its own copy of the config list.
function openRunComposer() {
  state.spineSelectedConfigs = new Set();
  state.spineConfigFilter = "";
  document.getElementById("run-composer-config-filter").value = "";
  document.getElementById("run-composer-seeds").value = "";
  document.getElementById("run-composer-args").value = "";
  document.getElementById("run-composer-pool").value = "either";
  document.getElementById("run-composer-retries").value = "1";
  document.getElementById("run-composer-force-retry").checked = true;
  document.getElementById("run-composer-batch-name").value = "";
  renderRunComposerConfigList();
  updateRunComposerPreflight();
  document.getElementById("run-composer-backdrop").classList.remove("hidden");
}

function closeRunComposer() {
  document.getElementById("run-composer-backdrop").classList.add("hidden");
}

function renderRunComposerConfigList() {
  const list = document.getElementById("run-composer-config-list");
  const countEl = document.getElementById("run-composer-config-count");
  if (!list) return;
  const filter = state.spineConfigFilter.trim().toLowerCase();
  const flat = [];
  for (const group of state.configs || []) {
    for (const c of group.configs) {
      if (!filter || c.path.toLowerCase().includes(filter) || (c.experiment_name || "").toLowerCase().includes(filter)) {
        flat.push(c);
      }
    }
  }
  if (countEl) countEl.textContent = state.spineSelectedConfigs.size ? `(${state.spineSelectedConfigs.size} selected)` : "";
  if (!flat.length) {
    list.innerHTML = `<div class="empty-state">No configs match.</div>`;
    return;
  }
  list.innerHTML = flat.map((c) => {
    const checked = state.spineSelectedConfigs.has(c.path) ? "checked" : "";
    return `<label class="list-row" style="display:flex; align-items:center; gap:8px; padding:6px 10px; cursor:pointer;">
      <input type="checkbox" data-config-path="${escapeHtml(c.path)}" ${checked} />
      <span style="font-family:var(--mono); font-size:12px;">${escapeHtml(c.path)}</span>
    </label>`;
  }).join("");
  list.querySelectorAll("input[data-config-path]").forEach((input) => {
    input.addEventListener("change", () => {
      if (input.checked) state.spineSelectedConfigs.add(input.dataset.configPath);
      else state.spineSelectedConfigs.delete(input.dataset.configPath);
      updateRunComposerPreflight();
    });
  });
}

function parseRunComposerSeeds() {
  const raw = document.getElementById("run-composer-seeds").value.trim();
  if (!raw) return [null];
  return raw.split(",").map((s) => s.trim()).filter((s) => s.length).map((s) => {
    const n = Number(s);
    return Number.isFinite(n) ? n : s;
  });
}

// Best-effort client-side mirror of backend/experiments.py's experiment_id_for() — the config's
// own experiment_name when list_configs() already resolved one, else its filename stem. This is
// a *preview* only; the server computes the real id at creation time and is authoritative,
// including the idempotent-reuse rule (posting a (config, seed) that already exists reuses that
// Experiment rather than creating a duplicate — see create_experiments()'s docstring).
function findConfigMeta(path) {
  for (const group of state.configs || []) {
    const hit = group.configs.find((c) => c.path === path);
    if (hit) return hit;
  }
  return null;
}

function previewExperimentIds(configPaths, seeds) {
  const ids = [];
  for (const path of configPaths) {
    const meta = findConfigMeta(path);
    const name = (meta && meta.experiment_name) || path.replace(/^.*\//, "").replace(/\.ya?ml$/, "");
    for (const seed of seeds) ids.push(seed === null ? name : `${name}-s${seed}`);
  }
  return ids;
}

// "Selected experiment calculation is omitting information" (user report, 2026-09-22): a bare
// "N configs × M seeds = X experiments" count doesn't say WHICH experiments are about to be
// created, or whether any of them already exist (POST /api/experiments is idempotent per
// (config, seed) — re-launching an existing one just queues a fresh Attempt on it, not a
// duplicate). Show the actual resolved ids instead of just a count.
//
// Also renders into its own #run-composer-preflight box (styles.css) instead of the shared
// .settings-profile-path class — that class is nowrap+ellipsis (built for a single truncated
// filesystem path), which was clipping this multi-line block down to nothing visible, i.e. the
// total-runs summary really was being "omitted" by CSS, not just under-emphasized.
async function updateRunComposerPreflight() {
  const el = document.getElementById("run-composer-preflight");
  if (!el) return;
  const configPaths = Array.from(state.spineSelectedConfigs);
  if (!configPaths.length) {
    el.innerHTML = `<span class="preflight-empty">Pick at least one config to see how many runs this launches.</span>`;
    return;
  }
  const seeds = parseRunComposerSeeds();
  const existingIds = new Set((state.spineExperiments || []).map((e) => e.experiment_id));
  const ids = previewExperimentIds(configPaths, seeds);
  const pool = document.getElementById("run-composer-pool")?.value || "either";

  // Allocation/queue estimate: which machine(s) these runs are eligible for (per the "Where"
  // pool choice), how many of each are free *right now*, and how many experiments are already
  // pending ahead of this batch — dispatch claims free slots roughly in creation order (see
  // experiments.py's _dispatch_tick), so a queue already occupying every free slot pushes this
  // whole batch behind it. Best-effort only (a live capacity read, not a scheduling guarantee)
  // and degrades quietly if either call fails.
  let capacityHtml = "";
  try {
    const [slotsData, pulseData] = await Promise.all([api("/api/slots"), api("/api/pulse")]);
    const slots = slotsData.slots || [];
    const local = slots.find((s) => s.kind === "local");
    const kaggleSlots = slots.filter((s) => s.kind === "kaggle");
    const localFree = local ? Math.max(0, local.limit - local.used) : 0;
    const kaggleFree = kaggleSlots.filter((s) => s.used < s.limit).length;
    const includeLocal = pool !== "kaggle_only";
    const includeKaggle = pool !== "local_only";
    const freeNow = (includeLocal ? localFree : 0) + (includeKaggle ? kaggleFree : 0);
    const queuedAhead = Number(pulseData.queued_count) || 0;
    const availableForNew = Math.max(0, freeNow - queuedAhead);
    const immediate = Math.min(ids.length, availableForNew);
    const queued = ids.length - immediate;

    const rows = [];
    if (includeLocal && local) rows.push(`<span class="preflight-capacity-row">Local device: <b>${localFree}/${local.limit}</b> free now</span>`);
    if (includeKaggle) rows.push(`<span class="preflight-capacity-row">Kaggle: <b>${kaggleFree}/${kaggleSlots.length}</b> account${kaggleSlots.length === 1 ? "" : "s"} free</span>`);
    const allocationNote = queued > 0
      ? `${immediate} of these should start immediately · ${queued} will queue behind ${queuedAhead ? `${queuedAhead} already-pending experiment${queuedAhead === 1 ? "" : "s"}` : "the rest of this batch"}`
      : `all ${ids.length} should start immediately`;
    capacityHtml = `<div class="preflight-capacity">${rows.join("")}<span class="preflight-queue-note">${allocationNote}</span></div>`;
  } catch (e) { /* preflight capacity block is a nicety — degrade quietly */ }

  const summary = `${configPaths.length} config${configPaths.length === 1 ? "" : "s"} × ${seeds.length} seed${seeds.length === 1 ? "" : "s"} = <span class="preflight-total-number">${ids.length}</span> run${ids.length === 1 ? "" : "s"}`;
  const shown = ids.slice(0, 12);
  const idList = shown.map((id) => {
    const reused = existingIds.has(id);
    return `<code title="${reused ? "Already exists — this queues a fresh attempt on it, not a duplicate" : "New"}"${reused ? ' style="color:var(--amber);"' : ""}>${escapeHtml(id)}</code>`;
  }).join(", ") + (ids.length > shown.length ? `, +${ids.length - shown.length} more` : "");
  const reusedCount = ids.filter((id) => existingIds.has(id)).length;
  const reusedNote = reusedCount ? `<div class="preflight-reused">${reusedCount} already exist${reusedCount === 1 ? "s" : ""} — queues a fresh attempt on ${reusedCount === 1 ? "it" : "them"} instead of duplicating</div>` : "";
  el.innerHTML = `<div class="preflight-total">${summary}</div>${capacityHtml}<div class="preflight-ids">${idList}</div>${reusedNote}`;
}

async function submitRunComposer() {
  if (!state.spineSelectedConfigs.size) { toast("Pick at least one config", "err"); return; }
  const body = {
    configs: Array.from(state.spineSelectedConfigs),
    seeds: parseRunComposerSeeds(),
    extra_args: document.getElementById("run-composer-args").value.trim(),
    pool: document.getElementById("run-composer-pool").value,
    max_retries: Number(document.getElementById("run-composer-retries").value) || 0,
    force_on_retry: document.getElementById("run-composer-force-retry").checked,
  };
  const batchName = document.getElementById("run-composer-batch-name").value.trim();
  if (batchName) body.batch_name = batchName;
  try {
    const result = await api("/api/experiments", { method: "POST", body: JSON.stringify(body) });
    const n = (result.experiments || []).length;
    toast(`Launched ${n} experiment${n === 1 ? "" : "s"}`, "ok");
    closeRunComposer();
    loadSpine();
  } catch (e) {
    toast(`Couldn't launch: ${e.message}`, "err");
  }
}

function initSpineButtons() {
  document.getElementById("btn-spine-refresh").addEventListener("click", loadSpine);
  document.getElementById("btn-spine-new-run").addEventListener("click", openRunComposer);
  document.getElementById("spine-status-filter").addEventListener("change", loadSpine);
  document.getElementById("run-composer-cancel").addEventListener("click", closeRunComposer);
  document.getElementById("run-composer-launch").addEventListener("click", submitRunComposer);
  document.getElementById("run-composer-seeds").addEventListener("input", updateRunComposerPreflight);
  document.getElementById("run-composer-pool").addEventListener("change", updateRunComposerPreflight);
  document.getElementById("run-composer-config-filter").addEventListener("input", (e) => {
    state.spineConfigFilter = e.target.value;
    renderRunComposerConfigList();
  });
  document.getElementById("delete-experiment-cancel").addEventListener("click", closeDeleteExperimentModal);
  document.getElementById("delete-experiment-confirm").addEventListener("click", confirmDeleteExperiment);
}

// Self-wiring, same convention as kaggle.js/data.js — every reference above is defined earlier
// in this same file, so calling this here (at script-load time) is safe regardless of whether
// app.js's boot() has run yet.
initSpineButtons();
