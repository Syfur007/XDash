// static/js/views/settings.js
//
// Settings tab additions (Multi_runner_XDash.md Phase 6): the 5-channel
// server-side notification panel, moved here from the old kaggle.js
// (KAGGLE_NOTIF_CHANNELS + its render/save/test functions), which is the
// right home for it now that it isn't Kaggle-specific — it fires on any
// Attempt transition (backend/experiments.py's notif.send_all() calls,
// including Phase 6's own new block/stall trigger) and on the scheduler's
// own "Notify on finish" toggle (still in Compute → Queue, next to the
// scheduler controls it actually describes — not moved, since it isn't a
// duplicate of this panel, just a consumer of the same channels).
//
// Function/id names kept exactly as they were in kaggle.js (renderKaggle-
// Notifications, kaggle-notif-body, ...) rather than renamed — this is a
// relocation, not a rewrite, and the DOM ids it's wired to are unchanged.
//
// Same classic-<script>-sharing-global-scope model as every other view file.

state.kaggleNotifications = {};       // channel -> settings, from GET /api/notifications
state.kaggleNotifEditOpen = new Set(); // channel keys with their edit form open

// The 5 server-side notification channels (backend/notifications.py) — sent by
// the dispatcher when an Attempt resolves or blocks (and, if enabled, the
// Scheduler's own tick), so they fire even when nobody has this tab open.
// `required` drives the "configured" chip; `secret` fields are masked
// round-trip (see get_notification_settings()).
const KAGGLE_NOTIF_CHANNELS = [
  {
    key: "telegram", label: "Telegram", icon: "✈️",
    required: ["bot_token", "chat_id"],
    fields: [
      { key: "bot_token", label: "Bot token", type: "password", secret: true, placeholder: "123456:AA… (from @BotFather)" },
      { key: "chat_id", label: "Chat ID", type: "text", placeholder: "e.g. 123456789" },
    ],
    hint: "Create a bot via @BotFather, message it once, then read the chat id from api.telegram.org/bot<token>/getUpdates.",
  },
  {
    key: "discord", label: "Discord", icon: "\u{1F3AE}",
    required: ["webhook_url"],
    fields: [{ key: "webhook_url", label: "Webhook URL", type: "password", secret: true, placeholder: "https://discord.com/api/webhooks/…" }],
    hint: "Server Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.",
  },
  {
    key: "slack", label: "Slack", icon: "\u{1F4AC}",
    required: ["webhook_url"],
    fields: [{ key: "webhook_url", label: "Webhook URL", type: "password", secret: true, placeholder: "https://hooks.slack.com/services/…" }],
    hint: "api.slack.com/apps → Incoming Webhooks → Add New Webhook to Workspace.",
  },
  {
    key: "email", label: "Email", icon: "✉️",
    required: ["smtp_host", "to_addr"],
    fields: [
      { key: "smtp_host", label: "SMTP host", type: "text", placeholder: "smtp.gmail.com" },
      { key: "smtp_port", label: "SMTP port", type: "text", placeholder: "587" },
      { key: "smtp_user", label: "SMTP username", type: "text", placeholder: "you@gmail.com" },
      { key: "smtp_password", label: "SMTP password", type: "password", secret: true, placeholder: "app password" },
      { key: "from_addr", label: "From address", type: "text", placeholder: "defaults to username" },
      { key: "to_addr", label: "To address", type: "text", placeholder: "alerts@example.com" },
      { key: "use_tls", label: "Use STARTTLS", type: "checkbox" },
    ],
    hint: "Most providers (Gmail included) need an app password here, not your normal login password.",
  },
  {
    key: "ntfy", label: "ntfy.sh", icon: "\u{1F514}",
    required: ["topic"],
    fields: [
      { key: "topic", label: "Topic", type: "text", placeholder: "a random hard-to-guess slug" },
      { key: "server", label: "Server", type: "text", placeholder: "https://ntfy.sh" },
    ],
    hint: "No account needed — install the ntfy app and subscribe to the same topic name.",
  },
];

async function loadKaggleNotifications() {
  try {
    state.kaggleNotifications = await api("/api/notifications");
  } catch (e) {
    // Non-critical panel — render whatever we already have.
  }
  renderKaggleNotifications();
}

function _kaggleNotifConfigured(cfg, c) {
  return c.required.every((key) => {
    const field = c.fields.find((f) => f.key === key);
    if (field && field.secret) return !!cfg[`${key}_set`];
    return !!(cfg[key] && String(cfg[key]).trim());
  });
}

function renderKaggleNotifications() {
  const body = document.getElementById("kaggle-notif-body");
  if (!body) return;
  const settingsByChannel = state.kaggleNotifications || {};
  const configuredCount = KAGGLE_NOTIF_CHANNELS.filter((c) => _kaggleNotifConfigured(settingsByChannel[c.key] || {}, c)).length;
  const countEl = document.getElementById("kaggle-notif-count");
  if (countEl) countEl.textContent = configuredCount ? `${configuredCount} of ${KAGGLE_NOTIF_CHANNELS.length} configured` : "";

  body.innerHTML = KAGGLE_NOTIF_CHANNELS.map((c) => {
    const cfg = settingsByChannel[c.key] || {};
    const enabled = !!cfg.enabled;
    const configured = _kaggleNotifConfigured(cfg, c);
    const editOpen = state.kaggleNotifEditOpen.has(c.key);
    // Only ever locks out turning ON an unconfigured channel — turning an
    // already-on one off stays available regardless.
    const toggleLocked = !configured && !enabled;
    const toggleTitle = toggleLocked ? `Configure ${escapeHtml(c.label)} before enabling it` : `Enable ${escapeHtml(c.label)} alerts`;
    return `<div class="kaggle-card">
      <div class="kaggle-card-accent ${enabled ? "emerald" : "slate"}"></div>
      <div class="kaggle-card-body">
        <div class="kaggle-card-header">
          <div style="min-width:0;">
            <div class="kaggle-card-title-row"><span class="kaggle-card-title">${c.icon} ${escapeHtml(c.label)}</span></div>
            <div class="kaggle-card-sub"><span class="kaggle-chip ${configured ? "on" : ""}">${configured ? "configured" : "not set"}</span></div>
          </div>
          <button class="toggle-switch ${enabled ? "on" : ""}" ${toggleLocked ? "disabled" : ""} data-action="toggle-notif-enabled" data-channel="${c.key}" title="${toggleTitle}"><span class="toggle-knob"></span></button>
        </div>
        ${editOpen ? renderKaggleNotifForm(c, cfg) : ""}
        <div class="kaggle-card-footer">
          <button class="btn btn-sm btn-ghost" data-action="toggle-notif-edit" data-channel="${c.key}">${editOpen ? "Cancel" : "Configure"}</button>
          <button class="btn btn-sm btn-ghost" data-action="test-notif" data-channel="${c.key}">Send test</button>
        </div>
      </div>
    </div>`;
  }).join("");

  body.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const channel = btn.dataset.channel;
      const action = btn.dataset.action;
      if (action === "toggle-notif-enabled") toggleKaggleNotifEnabled(channel, !btn.classList.contains("on"));
      else if (action === "toggle-notif-edit") toggleKaggleNotifEdit(channel);
      else if (action === "save-notif") saveKaggleNotification(channel);
      else if (action === "test-notif") testKaggleNotification(channel);
    });
  });
}

function renderKaggleNotifForm(c, cfg) {
  const rows = c.fields.map((f) => {
    const id = `kaggle-notif-${c.key}-${f.key}`;
    if (f.type === "checkbox") {
      const checked = cfg[f.key] !== false; // default on (matches _default_notifications' use_tls: True)
      return `<div class="field"><label class="checkbox-label"><input type="checkbox" id="${id}" ${checked ? "checked" : ""} /> ${escapeHtml(f.label)}</label></div>`;
    }
    const isSecretSet = f.secret && cfg[`${f.key}_set`];
    const placeholder = isSecretSet ? "•••• saved — leave blank to keep" : (f.placeholder || "");
    const value = f.secret ? "" : (cfg[f.key] != null ? cfg[f.key] : "");
    return `<div class="field grow">
      <label>${escapeHtml(f.label)}</label>
      <input class="text-input grow" id="${id}" type="${f.type}" placeholder="${escapeHtml(placeholder)}" value="${escapeHtml(String(value))}" autocomplete="off" />
    </div>`;
  }).join("");
  return `<div class="scheduler-add-form kaggle-notif-form" style="padding-top:9px; border-top:1px dashed var(--border-soft);">
    ${rows}
    <div class="kaggle-card-sub">${escapeHtml(c.hint || "")}</div>
    <button class="btn btn-sm btn-primary" data-action="save-notif" data-channel="${c.key}">Save</button>
  </div>`;
}

function toggleKaggleNotifEnabled(channel, enabled) {
  api(`/api/notifications/${channel}`, { method: "PATCH", body: JSON.stringify({ enabled }) })
    .then(() => loadKaggleNotifications())
    .catch((e) => toast(`Couldn't update ${channel}: ${e.message}`, "err"));
}

function toggleKaggleNotifEdit(channel) {
  if (state.kaggleNotifEditOpen.has(channel)) state.kaggleNotifEditOpen.delete(channel);
  else state.kaggleNotifEditOpen.add(channel);
  renderKaggleNotifications();
}

async function saveKaggleNotification(channel) {
  const c = KAGGLE_NOTIF_CHANNELS.find((x) => x.key === channel);
  const patch = {};
  c.fields.forEach((f) => {
    const el = document.getElementById(`kaggle-notif-${channel}-${f.key}`);
    if (!el) return;
    patch[f.key] = f.type === "checkbox" ? el.checked : el.value.trim();
  });
  try {
    const result = await api(`/api/notifications/${channel}`, { method: "PATCH", body: JSON.stringify(patch) });
    state.kaggleNotifications[channel] = result;
    state.kaggleNotifEditOpen.delete(channel);
    renderKaggleNotifications();
    toast(`${c.label} notification settings saved`, "ok");
  } catch (e) {
    toast(`Couldn't save ${c.label} settings: ${e.message}`, "err");
  }
}

async function testKaggleNotification(channel) {
  const c = KAGGLE_NOTIF_CHANNELS.find((x) => x.key === channel);
  try {
    await api(`/api/notifications/${channel}/test`, { method: "POST" });
    toast(`Test message sent via ${c.label} — check it arrived`, "ok");
  } catch (e) {
    toast(`${c.label} test failed: ${e.message}`, "err");
  }
}
