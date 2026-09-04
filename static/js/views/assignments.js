// static/js/views/assignments.js
//
// Assignment board (DASHBOARD_REDESIGN_PLAN.md §7/Phase 7): a config x seed
// -> runner planning board — backend/assignments.py. Answers "which runner
// should run this not-yet-launched config", a different question from
// Experiments' Active ("what's running now") or Runs & Results ("what has
// run"). Retires the hand-maintained experiment_status.csv at the repo
// root — Import CSV below reads that exact shape.
//
// Same classic-<script>-sharing-global-scope model as kaggle.js/runs.js —
// uses api()/toast()/escapeHtml()/showConfirm()/state/renderStatusBadge()
// as page-global bindings.

state.assignmentRows = [];
state.assignmentsLoaded = false;
state.assignmentEditingId = null; // row_id being edited, or null = the form adds a new row
state.assignmentConfigGroups = [];
state.assignmentConfigsLoaded = false;
state.assignmentRunners = [];

async function loadAssignments() {
  state.assignmentsLoaded = true;
  try {
    const data = await api("/api/assignments");
    state.assignmentRows = data.rows || [];
  } catch (e) {
    toast("Couldn't load assignments: " + e.message, "err");
  }
  if (!state.assignmentRunners.length) {
    try {
      const data = await api("/api/runners");
      state.assignmentRunners = data.runners || [];
    } catch (e) { /* the runner <select> just falls back to free text */ }
  }
  renderAssignmentRunnerOptions();
  renderAssignments();
}

function renderAssignmentRunnerOptions() {
  const select = document.getElementById("assignment-runner-select");
  if (!select) return;
  const current = select.value;
  select.innerHTML = `<option value="">— pick a runner, or type a free label below —</option>` +
    state.assignmentRunners.map((r) => `<option value="${escapeHtml(r.id)}">${escapeHtml(r.label)} (${escapeHtml(r.id)})</option>`).join("");
  if (state.assignmentRunners.some((r) => r.id === current)) select.value = current;
}

async function populateAssignmentConfigSelect() {
  const select = document.getElementById("assignment-config-select");
  if (!select || state.assignmentConfigsLoaded) return;
  state.assignmentConfigsLoaded = true;
  try {
    const data = await api("/api/configs");
    state.assignmentConfigGroups = data.groups || [];
    for (const group of state.assignmentConfigGroups) {
      const optgroup = document.createElement("optgroup");
      optgroup.label = group.category;
      for (const c of group.configs) {
        const opt = document.createElement("option");
        opt.value = c.path; opt.textContent = c.name;
        optgroup.appendChild(opt);
      }
      select.appendChild(optgroup);
    }
  } catch (e) { /* the config field still accepts free text below */ }
}

function renderAssignments() {
  const countEl = document.getElementById("assignment-count");
  const body = document.getElementById("assignment-table-body");
  const rows = state.assignmentRows;
  countEl.textContent = rows.length ? `${rows.length} row${rows.length === 1 ? "" : "s"}` : "";

  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="7" class="empty-state">No assignments planned yet. Add one below, or Import CSV from a hand-maintained sheet like experiment_status.csv.</td></tr>`;
    return;
  }

  // Group by block for readability when a block column is in use — an
  // empty block just falls into one unlabeled group, so this degrades to a
  // flat table when nobody's using blocks at all.
  const byBlock = new Map();
  for (const r of rows) {
    const key = r.block || "";
    if (!byBlock.has(key)) byBlock.set(key, []);
    byBlock.get(key).push(r);
  }

  let html = "";
  for (const [block, blockRows] of byBlock) {
    if (block) html += `<tr class="assignment-block-row"><td colspan="7">Block ${escapeHtml(block)}</td></tr>`;
    for (const r of blockRows) {
      const extraKeys = Object.keys(r.extra || {});
      const extraTitle = extraKeys.length
        ? extraKeys.map((k) => `${k}: ${r.extra[k]}`).join("\n")
        : "";
      html += `<tr data-row-id="${escapeHtml(r.row_id)}">
        <td>${escapeHtml(r.config_path)}</td>
        <td>${r.seed === null || r.seed === undefined || r.seed === "" ? "—" : escapeHtml(String(r.seed))}</td>
        <td>${escapeHtml(r.runner_id || "—")}</td>
        <td>${renderStatusBadge(r.status)}</td>
        <td class="assignment-notes-cell" ${extraTitle ? `title="${escapeHtml(extraTitle)}"` : ""}>${escapeHtml(r.notes || "")}${extraTitle ? ` <span class="badge slate" title="${escapeHtml(extraTitle)}">+${extraKeys.length}</span>` : ""}</td>
        <td>${escapeHtml(timeAgo(r.updated_at))}</td>
        <td>
          <button class="btn-icon" data-action="edit-assignment" data-id="${escapeHtml(r.row_id)}" title="Edit">✎</button>
          <button class="btn-icon" data-action="delete-assignment" data-id="${escapeHtml(r.row_id)}" title="Delete">✕</button>
        </td>
      </tr>`;
    }
  }
  body.innerHTML = html;

  body.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const id = btn.dataset.id;
      if (btn.dataset.action === "edit-assignment") startEditAssignment(id);
      else if (btn.dataset.action === "delete-assignment") deleteAssignment(id);
    });
  });
}

function startEditAssignment(rowId) {
  const row = state.assignmentRows.find((r) => r.row_id === rowId);
  if (!row) return;
  state.assignmentEditingId = rowId;
  document.getElementById("assignment-config-select").value = row.config_path;
  document.getElementById("assignment-seed-input").value = row.seed === null || row.seed === undefined ? "" : row.seed;
  document.getElementById("assignment-block-input").value = row.block || "";
  document.getElementById("assignment-runner-select").value = row.runner_id || "";
  document.getElementById("assignment-runner-free").value = state.assignmentRunners.some((r) => r.id === row.runner_id) ? "" : (row.runner_id || "");
  document.getElementById("assignment-status-input").value = row.status || "planned";
  document.getElementById("assignment-notes-input").value = row.notes || "";
  document.getElementById("btn-assignment-submit").textContent = "Save changes";
  document.getElementById("btn-assignment-cancel-edit").classList.remove("hidden");
}

function cancelEditAssignment() {
  state.assignmentEditingId = null;
  ["assignment-seed-input", "assignment-block-input", "assignment-runner-free", "assignment-notes-input"]
    .forEach((id) => (document.getElementById(id).value = ""));
  document.getElementById("assignment-config-select").value = "";
  document.getElementById("assignment-runner-select").value = "";
  document.getElementById("assignment-status-input").value = "planned";
  document.getElementById("btn-assignment-submit").textContent = "Add row";
  document.getElementById("btn-assignment-cancel-edit").classList.add("hidden");
}

async function submitAssignmentForm() {
  const config_path = document.getElementById("assignment-config-select").value.trim();
  const seed = document.getElementById("assignment-seed-input").value.trim();
  const block = document.getElementById("assignment-block-input").value.trim();
  const runnerFree = document.getElementById("assignment-runner-free").value.trim();
  const runner_id = runnerFree || document.getElementById("assignment-runner-select").value;
  const status = document.getElementById("assignment-status-input").value.trim() || "planned";
  const notes = document.getElementById("assignment-notes-input").value.trim();
  if (!config_path) { toast("Pick or type a config first", "err"); return; }

  const body = { config_path, seed: seed === "" ? null : seed, block, runner_id, status, notes };
  try {
    if (state.assignmentEditingId) {
      await api(`/api/assignments/${encodeURIComponent(state.assignmentEditingId)}`, { method: "PATCH", body: JSON.stringify(body) });
      toast("Assignment updated", "ok");
    } else {
      await api("/api/assignments", { method: "POST", body: JSON.stringify(body) });
      toast("Assignment added", "ok");
    }
    cancelEditAssignment();
    loadAssignments();
  } catch (e) {
    toast("Couldn't save assignment: " + e.message, "err");
  }
}

async function deleteAssignment(rowId) {
  const ok = await showConfirm("Remove this assignment?", "This only removes the planning row — it never touches a config file, a runner, or the run ledger.");
  if (!ok) return;
  try {
    await api(`/api/assignments/${encodeURIComponent(rowId)}`, { method: "DELETE" });
    if (state.assignmentEditingId === rowId) cancelEditAssignment();
    loadAssignments();
  } catch (e) {
    toast("Couldn't delete: " + e.message, "err");
  }
}

async function importAssignmentsCsv() {
  const input = document.getElementById("assignment-import-path");
  const csv_path = input.value.trim();
  if (!csv_path) { toast("Enter a repo-relative CSV path first, e.g. experiment_status.csv", "err"); return; }
  try {
    const result = await api("/api/assignments/import_csv", { method: "POST", body: JSON.stringify({ csv_path }) });
    toast(`Imported ${result.imported} row(s) from ${csv_path}`, "ok");
    input.value = "";
    loadAssignments();
  } catch (e) {
    toast("Import failed: " + e.message, "err");
  }
}

function exportAssignmentsCsv() {
  // A plain navigation (not fetch) so the browser's own download/save-as
  // handling takes over — the response is a real CSV, not a JSON envelope
  // wrapped by api()'s JSON assumptions.
  window.open("/api/assignments/export_csv", "_blank");
}

function initAssignmentsButtons() {
  document.getElementById("btn-assignment-submit").addEventListener("click", submitAssignmentForm);
  document.getElementById("btn-assignment-cancel-edit").addEventListener("click", cancelEditAssignment);
  document.getElementById("btn-assignment-import").addEventListener("click", importAssignmentsCsv);
  document.getElementById("btn-assignment-export").addEventListener("click", exportAssignmentsCsv);
  document.getElementById("btn-refresh-assignments").addEventListener("click", loadAssignments);
  document.getElementById("assignment-config-select").addEventListener("focus", populateAssignmentConfigSelect, { once: true });
}

initAssignmentsButtons();
