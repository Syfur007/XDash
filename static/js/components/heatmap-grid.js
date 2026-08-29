// static/js/components/heatmap-grid.js
//
// Renders a run group's seed x fold status grid — one colored cell per
// run. Used by the Runs Hub (views/runs.js) today; written generically
// enough (plain {seed, fold, status, run_id} rows in, an HTML table out)
// to be reused by the Sweep Launcher's pre-flight matrix and later
// heat-table-style views without changes.
//
// Same classic-<script>-sharing-global-scope model as badge.js/app.js —
// uses escapeHtml()/statusBadgeClass() as bare identifiers.

function renderSeedFoldGrid(runs) {
  if (!runs.length) return `<div class="empty-state" style="padding:8px 0;">No runs</div>`;

  const seeds = Array.from(new Set(runs.map((r) => r.seed))).sort((a, b) => a - b);
  const foldKey = (r) => (r.fold === null || r.fold === undefined ? "-" : r.fold);
  const folds = Array.from(new Set(runs.map(foldKey))).sort((a, b) => {
    if (a === "-") return -1;
    if (b === "-") return 1;
    return a - b;
  });

  const byKey = {};
  for (const r of runs) byKey[`${r.seed}:${foldKey(r)}`] = r;

  let html = `<table class="seedfold-grid"><thead><tr><th></th>`;
  for (const s of seeds) html += `<th>s${escapeHtml(String(s))}</th>`;
  html += `</tr></thead><tbody>`;
  for (const f of folds) {
    html += `<tr><th>${f === "-" ? "—" : "f" + escapeHtml(String(f))}</th>`;
    for (const s of seeds) {
      const run = byKey[`${s}:${f}`];
      if (!run) {
        html += `<td class="seedfold-cell empty"></td>`;
        continue;
      }
      const cls = statusBadgeClass(run.status);
      const label = `${run.run_id} — ${run.status}`;
      html += `<td class="seedfold-cell ${cls}" data-run-id="${escapeHtml(run.run_id)}" title="${escapeHtml(label)}"></td>`;
    }
    html += `</tr>`;
  }
  html += `</tbody></table>`;
  return html;
}

function wireSeedFoldGrid(container, onCellClick) {
  container.querySelectorAll(".seedfold-cell[data-run-id]").forEach((el) => {
    el.addEventListener("click", () => onCellClick(el.dataset.runId));
  });
}
