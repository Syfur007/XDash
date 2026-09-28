// static/js/screens/settings_profile.js
//
// XDASH_PLAN.md §8.6, Phase 4: the dynamic profile form — shape generated
// from the profile YAML's own parsed tree (GET /api/profile, Phase 2),
// comments as help text, backend/profile_hints.py's optional type/enum
// hints, an unknown key still rendering via type inference — plus the raw
// YAML escape hatch, on-disk mtime polling, and the "+ New profile" wizard.
//
// Deliberately separate from static/js/views/settings.js (which owns the
// pre-existing Notifications/Alerts panel, kept as-is and just referenced
// from this screen's "Alerts" nav entry) — different domain, different
// file, same #view-settings section. js/lib/router.js's handleRoute()
// calls this file's applySettingsRouteParams() for the `second` path
// segment (#/settings/<section>).
//
// Save is scoped to the *currently open* section (XDASH_PLAN.md §8.6's
// mockup: one section's form, with its own [Revert] [Save]) — switching
// sections with unsaved edits confirms first (state.editorDirty's own
// discard-changes pattern, static/app.js's selectConfig()), rather than
// tracking a cross-section diff. Simpler, and matches how every other
// editor in this app already works.
//
// Same classic-<script>-sharing-global-scope model as every other view file.

// ---------------------------------------------------------------- state
state.profileDoc = null;            // last GET /api/profile response
state.profileActiveSection = null;  // a top-level profile key, or "alerts"/"about"
state.profileDirty = false;
state.profileDiskChanged = false;   // disk mtime moved while local edits were pending
state.profileRawMode = false;
state.profileRawEditor = null;
state.profileListValues = {};       // dottedKey -> working array, for the open section's chip editors
state.profileMtimePoller = null;
state.profilePathCheckTimer = {};   // dottedKey -> timer id, one per open path widget

// ================================================================== load
async function loadProfileScreen(silent) {
  let data;
  try {
    data = await api("/api/profile");
  } catch (e) {
    if (!silent) toast("Couldn't load the profile: " + e.message, "err");
    return;
  }

  const diskChanged = state.profileDoc && data.mtime !== state.profileDoc.mtime;
  if (diskChanged && state.profileDirty) {
    // Don't clobber in-progress edits — just flag it; Revert (or a save,
    // which reloads afterward) picks up the newer file.
    state.profileDiskChanged = true;
    updateProfileHeader();
    startProfileMtimePolling();
    return;
  }

  state.profileDoc = data;
  state.profileDiskChanged = false;
  if (!state.profileActiveSection) state.profileActiveSection = Object.keys(data.parsed || {})[0] || "about";

  renderProfileSectionNav();
  if (diskChanged || document.getElementById("profile-section-body")?.dataset.rendered !== "1") {
    renderProfileSectionBody();
    if (diskChanged && !silent) toast("Profile changed on disk — reloaded", "ok");
  }
  updateProfileHeader();
  startProfileMtimePolling();
}

function startProfileMtimePolling() {
  if (!state.profileMtimePoller) state.profileMtimePoller = createPoller(() => loadProfileScreen(true), 5000);
  state.profileMtimePoller.start();
}

function stopProfileMtimePolling() {
  if (state.profileMtimePoller) state.profileMtimePoller.stop();
}

// Called by js/lib/router.js's handleRoute() for #/settings/<section>.
function applySettingsRouteParams(section) {
  if (section) state.profileActiveSection = section;
  if (state.profileDoc) {
    renderProfileSectionNav();
    renderProfileSectionBody();
  }
}

// ================================================================== section nav
function renderProfileSectionNav() {
  const nav = document.getElementById("profile-section-nav");
  if (!nav || !state.profileDoc) return;
  const keys = Object.keys(state.profileDoc.parsed || {});
  const restartSet = new Set(state.profileDoc.restart_required || []);
  const hasRestart = (key) => restartSet.has(key) || [...restartSet].some((r) => r.startsWith(key + "."));
  const row = (key, label) => `<div class="config-item ${state.profileActiveSection === key ? "active" : ""}" data-profile-section="${escapeHtml(key)}">
    <span class="dot"></span><span style="flex:1; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(label)}</span>
    ${hasRestart(key) ? `<span class="badge amber" title="Contains a restart-required key">⟳</span>` : ""}
  </div>`;
  const rows = keys.map((k) => row(k, k)).join("") + row("tools", "Tools") + row("storage", "Storage") + row("alerts", "Alerts") + row("about", "About");
  nav.innerHTML = rows;
  nav.querySelectorAll("[data-profile-section]").forEach((el) => {
    el.addEventListener("click", () => trySwitchProfileSection(el.dataset.profileSection));
  });
}

async function trySwitchProfileSection(section) {
  if (section === state.profileActiveSection) return;
  if (state.profileDirty) {
    const ok = await showConfirm("Discard changes?", "Discard unsaved changes to this section?");
    if (!ok) return;
    state.profileDirty = false;
  }
  navigateToSettingsSection(section);
}

// ================================================================== section body
function inferProfileType(dottedKey, value) {
  if (typeof value === "boolean") return "bool";
  if (typeof value === "number") return "number";
  if (Array.isArray(value)) return "list";
  const leaf = dottedKey.split(".").pop();
  if (/(_dir|_path|root)$/i.test(leaf)) return "path";
  return "text";
}

function renderChipEditor(dottedKey, items) {
  const chips = items.map((v, i) => `<span class="badge slate" data-chip-item data-pf-key="${escapeHtml(dottedKey)}" data-index="${i}">${escapeHtml(String(v))} <span data-chip-remove>✕</span></span>`).join("");
  return `<div class="chip-list" data-pf-list="${escapeHtml(dottedKey)}">${chips}
    <input class="text-input" style="width:150px;" placeholder="+ add, Enter" data-chip-add data-pf-key="${escapeHtml(dottedKey)}" autocomplete="off" />
  </div>`;
}

function profileListValueFor(dottedKey) {
  if (dottedKey in state.profileListValues) return state.profileListValues[dottedKey];
  const parts = dottedKey.split(".");
  let node = state.profileDoc.parsed;
  for (const p of parts) node = (node || {})[p];
  const value = Array.isArray(node) ? node.slice() : [];
  state.profileListValues[dottedKey] = value;
  return value;
}

function renderFieldWidget(dottedKey, value) {
  const hints = (state.profileDoc.hints || {})[dottedKey] || {};
  const comment = (state.profileDoc.comments || {})[dottedKey];
  const restartSet = new Set(state.profileDoc.restart_required || []);
  const type = hints.type || inferProfileType(dottedKey, value);
  const id = "pf-" + dottedKey.replace(/[^a-zA-Z0-9_-]/g, "_");
  const restartBadge = restartSet.has(dottedKey) ? ` <span class="badge amber" title="Needs a server restart to take effect">⟳</span>` : "";
  const help = comment ? `<div class="settings-profile-path" style="white-space:normal; margin-bottom:4px;">${escapeHtml(comment)}</div>` : "";

  let control;
  if (type === "bool") {
    control = `<button class="toggle-switch ${value ? "on" : ""}" data-pf-key="${escapeHtml(dottedKey)}" id="${id}"><span class="toggle-knob"></span></button>`;
  } else if (type === "number") {
    control = `<input class="text-input" type="number" step="any" id="${id}" data-pf-key="${escapeHtml(dottedKey)}" data-pf-type="number" value="${escapeHtml(value === null || value === undefined ? "" : value)}" />`;
  } else if (type === "enum") {
    const choices = hints.choices || [];
    control = `<select class="text-input" id="${id}" data-pf-key="${escapeHtml(dottedKey)}" data-pf-type="text">${choices.map((c) => `<option value="${escapeHtml(c)}" ${c === value ? "selected" : ""}>${escapeHtml(c)}</option>`).join("")}</select>`;
  } else if (type === "secret") {
    // Never renders the current value — GET /api/profile returns the raw
    // secret, and putting it in the DOM at all (even as type="password")
    // is an unnecessary exposure for a field almost nobody needs to see
    // again. Blank means "leave unchanged" (Save skips an empty secret).
    control = `<input class="text-input" type="password" id="${id}" data-pf-key="${escapeHtml(dottedKey)}" data-pf-type="text" placeholder="•••• set — leave blank to keep" autocomplete="off" />`;
  } else if (type === "list") {
    control = renderChipEditor(dottedKey, profileListValueFor(dottedKey));
  } else if (type === "path") {
    control = `<div style="display:flex; gap:6px; align-items:center;">
      <input class="text-input grow" id="${id}" data-pf-key="${escapeHtml(dottedKey)}" data-pf-type="text" data-pf-path-check value="${escapeHtml(value === null || value === undefined ? "" : value)}" autocomplete="off" />
      <button class="btn btn-sm btn-ghost" type="button" data-path-picker="${id}" data-path-kind="directory">Browse</button>
      <span class="badge slate" id="${id}-exists"></span>
    </div>`;
  } else {
    const multiline = hints.multiline || (typeof value === "string" && value.length > 60);
    const text = value === null || value === undefined ? "" : String(value);
    control = multiline
      ? `<textarea class="text-input" rows="2" id="${id}" data-pf-key="${escapeHtml(dottedKey)}" data-pf-type="text" style="width:100%; font-family:var(--mono); resize:vertical;">${escapeHtml(text)}</textarea>`
      : `<input class="text-input" id="${id}" data-pf-key="${escapeHtml(dottedKey)}" data-pf-type="text" value="${escapeHtml(text)}" autocomplete="off" />`;
  }

  return `<div class="field" style="margin-bottom:14px; max-width:640px;">
    <label>${escapeHtml(dottedKey.split(".").pop())}${restartBadge}</label>
    ${help}
    ${control}
  </div>`;
}

function renderProfileAboutSection(body) {
  if (!state.system) {
    body.innerHTML = `<div class="empty-state">Loading…</div>`;
    loadSystem().then(() => { if (state.profileActiveSection === "about") renderProfileSectionBody(); });
    return;
  }
  const sys = state.system;
  const sizeKb = typeof sys.state_dir_size_bytes === "number" ? (sys.state_dir_size_bytes / 1024).toFixed(1) + " KB" : "–";
  const bridgeRows = Object.entries(sys.bridge || {}).map(([mod, ok]) =>
    `<tr><td>${escapeHtml(mod)}</td><td>${ok ? "✓ importable" : "✗ not importable"}</td></tr>`
  ).join("");
  body.innerHTML = `
    <table class="kv-table">
      <tr><td>Profile</td><td>${escapeHtml(sys.display_name || sys.profile_name || "")}</td></tr>
      <tr><td>Repo root</td><td>${escapeHtml(sys.repo_root || "")}</td></tr>
      <tr><td>Configs dir</td><td>${escapeHtml(sys.configs_dir || "")}</td></tr>
      <tr><td>Manifest layout</td><td>${escapeHtml(sys.manifest_layout || "")}</td></tr>
      <tr><td>tmux</td><td>${sys.tmux_available ? "available" : "not found"}</td></tr>
      <tr><td>Primary metric</td><td>${escapeHtml((sys.metrics || {}).primary || "(none set)")}</td></tr>
      <tr><td>XDash state dir</td><td>${escapeHtml(sys.state_dir || "")}</td></tr>
      <tr><td>XDash state dir size</td><td>${sizeKb}</td></tr>
    </table>
    <h4 class="run-detail-section-title">Bridge / hooks</h4>
    <table class="compare-table" style="width:100%;">
      <thead><tr><th>Module</th><th>Status</th></tr></thead>
      <tbody>${bridgeRows || `<tr><td colspan="2" class="empty-state">No bridge status reported.</td></tr>`}</tbody>
    </table>
  `;
}

// ================================================================== tools (XDASH_FIXES_PLAN.md F2)
// backend/tools.py's registry — a deployment-level settings screen, not a
// profile one (kaggle_executable/colab_executable used to live in the
// profile form above; D2 moved them here). No Save/Revert footer (like
// Alerts/About): each row acts immediately (PUT to set/clear an override,
// POST .../test to re-resolve and re-validate one tool).
async function renderProfileToolsSection(body) {
  body.innerHTML = `<div class="empty-state">Loading…</div>`;
  let data;
  try {
    data = await api("/api/tools");
  } catch (e) {
    body.innerHTML = `<div class="empty-state">Couldn't load tools: ${escapeHtml(e.message)}</div>`;
    return;
  }
  if (state.profileActiveSection !== "tools") return;  // switched away while awaiting
  state.toolsData = data.tools || {};
  const rows = Object.entries(state.toolsData).map(([name, st]) => {
    const badgeClass = st.ok ? "emerald" : (st.required ? "red" : "amber");
    const badgeText = st.ok ? "ok" : (st.required ? "needs attention" : "not found (optional)");
    const versionText = st.version ? (st.min_version ? `${st.version} (>= ${st.min_version})` : st.version) : "–";
    const overrideValue = st.source === "override" ? st.path : "";
    return `<tr data-tool-row="${escapeHtml(name)}">
      <td>${escapeHtml(name)}<div class="settings-profile-path">${escapeHtml(st.used_for || "")}</div></td>
      <td><span class="badge ${badgeClass}" title="${escapeHtml(st.error || "")}">${badgeText}</span></td>
      <td><span class="settings-profile-path" title="${escapeHtml(st.path)}">${escapeHtml(st.path)}</span>
        <span class="badge slate">${escapeHtml(st.source)}</span></td>
      <td>${escapeHtml(versionText)}</td>
      <td><input class="text-input" style="width:220px;" data-tool-override="${escapeHtml(name)}"
            placeholder="auto-resolved" value="${escapeHtml(overrideValue)}" /></td>
      <td style="white-space:nowrap;">
        <button class="btn btn-sm btn-ghost" data-tool-save="${escapeHtml(name)}">Save</button>
        <button class="btn btn-sm btn-ghost" data-tool-test="${escapeHtml(name)}">Test</button>
      </td>
    </tr>`;
  }).join("");
  body.innerHTML = `<table class="compare-table" style="width:100%;">
    <thead><tr><th>Tool</th><th>Status</th><th>Resolved path</th><th>Version</th><th>Override</th><th></th></tr></thead>
    <tbody>${rows}</tbody>
  </table>
  <div class="settings-profile-path" style="margin-top:10px;">
    Resolution order: an explicit override above -> the bin/ directory next to whichever Python
    is running server.py -> PATH. A missing or too-old required tool puts its dependent
    runtimes into "needs attention" on the Lab/Compute tiles.
  </div>`;
  wireProfileToolsSection(body);
}

function wireProfileToolsSection(body) {
  body.querySelectorAll("[data-tool-save]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.toolSave;
      const input = body.querySelector(`[data-tool-override="${name}"]`);
      try {
        await api(`/api/tools/${encodeURIComponent(name)}`, {
          method: "PUT", body: JSON.stringify({ path: input.value.trim() || null }),
        });
        toast(`'${name}' override saved`, "ok");
      } catch (e) {
        toast(`Couldn't save '${name}': ${e.message}`, "err");
      }
      renderProfileToolsSection(body);
    });
  });
  body.querySelectorAll("[data-tool-test]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.toolTest;
      try {
        const st = await api(`/api/tools/${encodeURIComponent(name)}/test`, { method: "POST" });
        toast(st.ok ? `'${name}': ok (${st.path})` : `'${name}': ${st.error || "not found"}`, st.ok ? "ok" : "err");
      } catch (e) {
        toast(`Couldn't test '${name}': ${e.message}`, "err");
      }
      renderProfileToolsSection(body);
    });
  });
}

// ================================================================== storage (XDASH_FIXES_PLAN.md F5)
// backend/housekeeping.py's inventory/clean — a deployment-level settings
// screen, same "no Save/Revert footer, each control acts immediately" shape
// as Tools above. Auto categories run unattended too (startup + daily); this
// panel is the manual/inspectable side of the exact same categories.
function fmtBytes(n) {
  if (typeof n !== "number" || !isFinite(n)) return "–";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

async function renderProfileStorageSection(body) {
  body.innerHTML = `<div class="empty-state">Loading…</div>`;
  let data;
  try {
    data = await api("/api/housekeeping");
  } catch (e) {
    body.innerHTML = `<div class="empty-state">Couldn't load storage inventory: ${escapeHtml(e.message)}</div>`;
    return;
  }
  if (state.profileActiveSection !== "storage") return;  // switched away while awaiting
  state.housekeepingData = data;
  const auto = new Set(data.auto_categories || []);
  const settingsRow = data.settings || {};
  const rows = Object.entries(data.categories || {}).map(([key, c]) => {
    const errRow = c.error ? `<div class="settings-profile-path" style="color:var(--danger, #e66);">${escapeHtml(c.error)}</div>` : "";
    return `<tr data-hk-row="${escapeHtml(key)}">
      <td>${escapeHtml(c.label || key)}${auto.has(key) ? "" : ` <span class="badge slate" title="Only runs when you click Clean">manual</span>`}${errRow}</td>
      <td>${c.total_count} item${c.total_count === 1 ? "" : "s"}<div class="settings-profile-path">${fmtBytes(c.total_bytes)}</div></td>
      <td>${c.eligible_count} item${c.eligible_count === 1 ? "" : "s"}<div class="settings-profile-path">${fmtBytes(c.eligible_bytes)}</div></td>
      <td style="white-space:nowrap;">
        <button class="btn btn-sm btn-ghost" data-hk-preview="${escapeHtml(key)}">Preview</button>
        <button class="btn btn-sm btn-ghost" data-hk-clean="${escapeHtml(key)}" ${c.eligible_count ? "" : "disabled"}>Clean</button>
      </td>
    </tr>`;
  }).join("");
  body.innerHTML = `
    <table class="compare-table" style="width:100%;">
      <thead><tr><th>Category</th><th>Total</th><th>Eligible now</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <h4 class="run-detail-section-title">Retention settings</h4>
    <div class="field" style="max-width:320px;">
      <label>Retention (days) — terminal records &amp; scheduler history</label>
      <input class="text-input" id="hk-retention-days" type="number" min="0" step="1" value="${escapeHtml(settingsRow.retention_days ?? "")}" />
    </div>
    <div class="field" style="max-width:320px;">
      <label>Thumbnail cache cap (MB)</label>
      <input class="text-input" id="hk-thumbnail-cap-mb" type="number" min="0" step="1" value="${escapeHtml(settingsRow.thumbnail_cap_mb ?? "")}" />
    </div>
    <div class="field" style="max-width:320px;">
      <label>Ad-hoc finished-session grace period (hours)</label>
      <input class="text-input" id="hk-adhoc-grace-hours" type="number" min="0" step="1" value="${escapeHtml(settingsRow.adhoc_session_grace_hours ?? "")}" />
    </div>
    <button class="btn btn-sm" id="btn-hk-save-settings">Save retention settings</button>
    <div id="hk-preview-body" class="settings-profile-path" style="margin-top:14px; white-space:pre-wrap;"></div>
  `;
  wireProfileStorageSection(body);
}

function wireProfileStorageSection(body) {
  body.querySelectorAll("[data-hk-preview]").forEach((btn) => {
    btn.addEventListener("click", () => previewHousekeepingCategory(btn.dataset.hkPreview));
  });
  body.querySelectorAll("[data-hk-clean]").forEach((btn) => {
    btn.addEventListener("click", () => cleanHousekeepingCategory(btn.dataset.hkClean));
  });
  const saveBtn = document.getElementById("btn-hk-save-settings");
  if (saveBtn) saveBtn.addEventListener("click", saveHousekeepingSettings);
}

async function previewHousekeepingCategory(key) {
  const out = document.getElementById("hk-preview-body");
  try {
    const r = await api("/api/housekeeping/clean", {
      method: "POST", body: JSON.stringify({ categories: [key], dry_run: true }),
    });
    const c = r.results[key] || {};
    if (out) out.textContent = `${key}: would remove ${c.removed_count || 0} item(s), freeing ${fmtBytes(c.freed_bytes || 0)}.`;
  } catch (e) {
    toast(`Couldn't preview '${key}': ${e.message}`, "err");
  }
}

async function cleanHousekeepingCategory(key) {
  const label = (state.housekeepingData?.categories?.[key] || {}).label || key;
  const ok = await showConfirm(`Clean '${label}'?`, "This permanently removes every currently-eligible item in this category. This can't be undone.");
  if (!ok) return;
  try {
    const r = await api("/api/housekeeping/clean", {
      method: "POST", body: JSON.stringify({ categories: [key], dry_run: false }),
    });
    const c = r.results[key] || {};
    const errNote = (c.errors || []).length ? ` (${c.errors.length} item(s) failed — see server log)` : "";
    toast(`'${label}': removed ${c.removed_count || 0} item(s), freed ${fmtBytes(c.freed_bytes || 0)}${errNote}`, "ok");
  } catch (e) {
    toast(`Couldn't clean '${key}': ${e.message}`, "err");
  }
  renderProfileStorageSection(document.getElementById("profile-section-body"));
}

async function saveHousekeepingSettings() {
  const patch = {
    retention_days: document.getElementById("hk-retention-days").value,
    thumbnail_cap_mb: document.getElementById("hk-thumbnail-cap-mb").value,
    adhoc_session_grace_hours: document.getElementById("hk-adhoc-grace-hours").value,
  };
  try {
    await api("/api/housekeeping/settings", { method: "PUT", body: JSON.stringify(patch) });
    toast("Retention settings saved", "ok");
  } catch (e) {
    toast("Couldn't save retention settings: " + e.message, "err");
  }
}

function renderProfileSectionBody() {
  const body = document.getElementById("profile-section-body");
  if (!body || !state.profileDoc) return;
  state.profileDirty = false;
  state.profileListValues = {};
  const section = state.profileActiveSection;

  if (section === "alerts") {
    body.innerHTML = `<div class="empty-state">See the Notifications panel below — the same server-side alert channels used across the whole app.</div>`;
    document.getElementById("kaggle-notif-body")?.scrollIntoView({ behavior: "smooth", block: "center" });
  } else if (section === "about") {
    renderProfileAboutSection(body);
  } else if (section === "tools") {
    renderProfileToolsSection(body);
  } else if (section === "storage") {
    renderProfileStorageSection(body);
  } else {
    const value = (state.profileDoc.parsed || {})[section];
    if (value !== null && typeof value === "object" && !Array.isArray(value)) {
      const entries = Object.entries(value);
      body.innerHTML = entries.length
        ? entries.map(([childKey, childValue]) => renderFieldWidget(`${section}.${childKey}`, childValue)).join("")
        : `<div class="empty-state">Empty section.</div>`;
    } else {
      body.innerHTML = renderFieldWidget(section, value);
    }
    wireProfileFieldWidgets(body);
  }

  body.dataset.rendered = "1";
  updateProfileSaveButtonState();
}

// Idempotent: safe to call again on a container that's already (partially)
// wired — rerenderChipEditor() below replaces just one chip-list's markup
// and re-runs this over the *whole* section body rather than tracking its
// own narrower listener set, so every element here is guarded by a
// "already wired" flag to avoid attaching a second listener (which would,
// for a toggle button, make one click flip it twice — a no-op the user
// would see as "stuck").
function wireProfileFieldWidgets(container) {
  const markDirty = () => { state.profileDirty = true; updateProfileSaveButtonState(); };
  const once = (el, event, handler) => {
    if (el.dataset.pfWired === event) return;
    el.dataset.pfWired = event;
    el.addEventListener(event, handler);
  };

  container.querySelectorAll('input[data-pf-key], textarea[data-pf-key], select[data-pf-key]').forEach((el) => {
    if (el.dataset.pfWiredChange) return;
    el.dataset.pfWiredChange = "1";
    el.addEventListener("input", markDirty);
    el.addEventListener("change", markDirty);
  });
  container.querySelectorAll("button.toggle-switch[data-pf-key]").forEach((btn) => {
    once(btn, "click", () => { btn.classList.toggle("on"); markDirty(); });
  });
  container.querySelectorAll("[data-chip-add]").forEach((input) => {
    once(input, "keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const v = input.value.trim();
      if (!v) return;
      const key = input.dataset.pfKey;
      const list = profileListValueFor(key);
      list.push(v);
      state.profileListValues[key] = list;
      markDirty();
      rerenderChipEditor(key);
    });
  });
  container.querySelectorAll("[data-chip-remove]").forEach((span) => {
    once(span, "click", () => {
      const chip = span.closest("[data-chip-item]");
      const key = chip.dataset.pfKey;
      const list = profileListValueFor(key);
      list.splice(Number(chip.dataset.index), 1);
      state.profileListValues[key] = list;
      markDirty();
      rerenderChipEditor(key);
    });
  });
  container.querySelectorAll("[data-pf-path-check]").forEach((el) => {
    if (el.dataset.pfWiredPath) return;
    el.dataset.pfWiredPath = "1";
    checkPathExists(el);
    el.addEventListener("input", () => {
      const key = el.dataset.pfKey;
      clearTimeout(state.profilePathCheckTimer[key]);
      state.profilePathCheckTimer[key] = setTimeout(() => checkPathExists(el), 400);
    });
  });
}

function rerenderChipEditor(dottedKey) {
  const wrap = document.querySelector(`[data-pf-list="${dottedKey}"]`);
  if (!wrap) return;
  wrap.outerHTML = renderChipEditor(dottedKey, state.profileListValues[dottedKey] || []);
  wireProfileFieldWidgets(document.getElementById("profile-section-body"));
}

async function checkPathExists(el) {
  const indicator = document.getElementById(el.id + "-exists");
  if (!indicator) return;
  const value = el.value.trim();
  if (!value) { indicator.textContent = ""; indicator.className = "badge slate"; return; }
  try {
    const r = await api(`/api/paths/exists?scope=repo&path=${encodeURIComponent(value)}`);
    indicator.textContent = r.exists ? "✓" : "✗";
    indicator.className = "badge " + (r.exists ? "emerald" : "red");
  } catch (e) {
    indicator.textContent = "";
  }
}

function updateProfileSaveButtonState() {
  const saveBtn = document.getElementById("btn-profile-form-save");
  const revertBtn = document.getElementById("btn-profile-form-revert");
  const isFormSection = !["alerts", "about", "tools", "storage"].includes(state.profileActiveSection);
  if (saveBtn) saveBtn.disabled = !isFormSection || !state.profileDirty;
  if (revertBtn) revertBtn.disabled = !isFormSection || !state.profileDirty;
}

function updateProfileHeader() {
  if (!state.profileDoc) return;
  const pathEl = document.getElementById("profile-form-path");
  if (pathEl) pathEl.textContent = `repos/${state.profileDoc.profile_name}.yaml`;
  const savedEl = document.getElementById("profile-form-saved-at");
  if (savedEl && state.profileDoc.mtime) savedEl.textContent = `saved ${timeAgo(new Date(state.profileDoc.mtime * 1000).toISOString())}`;
  const bannerEl = document.getElementById("profile-form-disk-changed");
  if (bannerEl) bannerEl.classList.toggle("hidden", !state.profileDiskChanged);
}

// ================================================================== save / revert
async function saveProfileSection() {
  if (!state.profileDoc) return;
  const section = state.profileActiveSection;
  if (section === "alerts" || section === "about" || section === "tools" || section === "storage") return;
  const body = document.getElementById("profile-section-body");
  const patch = {};

  body.querySelectorAll("[data-pf-key]").forEach((el) => {
    const key = el.dataset.pfKey;
    if (el.classList.contains("toggle-switch")) {
      patch[key] = el.classList.contains("on");
    } else if (el.tagName === "SELECT") {
      patch[key] = el.value;
    } else if (el.dataset.pfType === "number") {
      const n = parseFloat(el.value);
      if (!Number.isNaN(n)) patch[key] = n;
    } else if (el.type === "password") {
      if (el.value.trim()) patch[key] = el.value;
    } else if (!el.hasAttribute("data-chip-add")) {
      patch[key] = el.value;
    }
  });
  Object.keys(state.profileListValues).forEach((key) => {
    if (key === section || key.startsWith(section + ".")) patch[key] = state.profileListValues[key];
  });

  if (!Object.keys(patch).length) { toast("Nothing to save", "err"); return; }

  try {
    const data = await api("/api/profile", { method: "PATCH", body: JSON.stringify({ patch }) });
    state.profileDoc = data;
    state.profileDirty = false;
    state.profileDiskChanged = false;
    renderProfileSectionNav();
    renderProfileSectionBody();
    updateProfileHeader();
    toast("Profile saved", "ok");
  } catch (e) {
    toast("Couldn't save: " + e.message, "err");
  }
}

function revertProfileSection() {
  state.profileDirty = false;
  renderProfileSectionBody();
}

// ================================================================== raw YAML
function toggleProfileRawMode() {
  if (!state.profileDoc) return;
  state.profileRawMode = !state.profileRawMode;
  document.getElementById("profile-form-layout").classList.toggle("hidden", state.profileRawMode);
  document.getElementById("profile-raw-wrap").classList.toggle("hidden", !state.profileRawMode);
  document.getElementById("btn-profile-form-raw-toggle").textContent = state.profileRawMode ? "Form view" : "Raw YAML";
  document.getElementById("btn-profile-form-revert").classList.toggle("hidden", state.profileRawMode);
  document.getElementById("btn-profile-form-save").classList.toggle("hidden", state.profileRawMode);

  if (state.profileRawMode) {
    if (state.profileRawEditor) { state.profileRawEditor.toTextArea(); state.profileRawEditor = null; }
    const textarea = document.getElementById("profile-raw-textarea");
    textarea.value = state.profileDoc.text;
    state.profileRawEditor = CodeMirror.fromTextArea(textarea, {
      mode: "yaml", theme: "dracula", lineNumbers: true, tabSize: 2, indentUnit: 2, viewportMargin: Infinity,
    });
  }
}

async function saveProfileRaw() {
  if (!state.profileRawEditor) return;
  const text = state.profileRawEditor.getValue();
  try {
    const data = await api("/api/profile/raw", { method: "PUT", body: JSON.stringify({ text }) });
    state.profileDoc = data;
    state.profileDirty = false;
    state.profileDiskChanged = false;
    toast("Raw profile saved", "ok");
    renderProfileSectionNav();
    renderProfileSectionBody();
    updateProfileHeader();
  } catch (e) {
    toast("Couldn't save raw profile: " + e.message, "err");
  }
}

// ================================================================== "+ New profile" wizard
function openNewProfileWizard() {
  document.getElementById("new-profile-name").value = "";
  document.getElementById("new-profile-display-name").value = "";
  document.getElementById("new-profile-repo-root").value = "";
  document.getElementById("new-profile-detect-result").textContent = "";
  document.getElementById("new-profile-backdrop").classList.remove("hidden");
}

function closeNewProfileWizard() {
  document.getElementById("new-profile-backdrop").classList.add("hidden");
}

async function detectNewProfileRepo() {
  const root = document.getElementById("new-profile-repo-root").value.trim();
  const resultEl = document.getElementById("new-profile-detect-result");
  if (!root) { resultEl.textContent = "Enter a repo root first"; return; }
  resultEl.textContent = "Detecting…";
  try {
    const d = await api("/api/repos/detect", { method: "POST", body: JSON.stringify({ repo_root: root }) });
    resultEl.textContent = d.exists
      ? `Found (${d.resolved}) — configs: ${d.configs_dir || "not found"} · train.py ${d.train_py ? "✓" : "✗"} · eval.py ${d.eval_py ? "✓" : "✗"} · output layout guess: ${d.manifest_layout}`
      : `Not found (resolved to ${d.resolved})`;
  } catch (e) {
    resultEl.textContent = "Couldn't detect: " + e.message;
  }
}

async function createNewProfile() {
  const name = document.getElementById("new-profile-name").value.trim();
  const displayName = document.getElementById("new-profile-display-name").value.trim();
  const root = document.getElementById("new-profile-repo-root").value.trim();
  if (!name || !root) { toast("A profile id and a repo root are required", "err"); return; }
  try {
    await api("/api/repos", { method: "POST", body: JSON.stringify({ name, display_name: displayName, repo_root: root }) });
    toast(`Profile '${name}' created — use "Use profile" below to switch to it`, "ok");
    closeNewProfileWizard();
    await loadRepos();
    renderSettings();
  } catch (e) {
    toast("Couldn't create profile: " + e.message, "err");
  }
}

// ================================================================== init
function initProfileScreenButtons() {
  document.getElementById("btn-profile-form-save").addEventListener("click", saveProfileSection);
  document.getElementById("btn-profile-form-revert").addEventListener("click", revertProfileSection);
  document.getElementById("btn-profile-form-raw-toggle").addEventListener("click", toggleProfileRawMode);
  document.getElementById("btn-profile-raw-save").addEventListener("click", saveProfileRaw);

  document.getElementById("btn-new-profile").addEventListener("click", openNewProfileWizard);
  document.getElementById("new-profile-cancel").addEventListener("click", closeNewProfileWizard);
  document.getElementById("new-profile-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "new-profile-backdrop") closeNewProfileWizard();
  });
  document.getElementById("btn-new-profile-detect").addEventListener("click", detectNewProfileRepo);
  document.getElementById("new-profile-create").addEventListener("click", createNewProfile);
}

initProfileScreenButtons();
