"""Shared notification-channel infrastructure: Telegram, Discord, Slack,
email, and ntfy.sh — originally built for the Kaggle tab's worker-completion
alerts, now reused by the Scheduler tab too (see scheduler.py's
notify_on_finish setting), so it lives in its own module rather than under
either feature's name.

Settings live in their own gitignored JSON file (never dashboard_config.yaml
— these are runtime-editable from the UI, not deploy-time config) and secret
fields (bot tokens, webhook URLs, SMTP passwords) are never echoed back to a
caller once saved — see get_notification_settings()'s masking below.

Any caller that wants "notify someone when X happens" just calls
send_all(text) — best-effort, never raises, fans out to every channel
that's both enabled and configured.
"""
from __future__ import annotations

import json
import smtplib
import threading
import urllib.error
import urllib.request
from email.mime.text import MIMEText
from typing import Any, Dict

from .config import settings


class NotificationError(Exception):
    pass


NOTIFICATION_CHANNELS = ("telegram", "discord", "slack", "email", "ntfy")

_NOTIF_SECRET_FIELDS = {
    "telegram": {"bot_token"},
    "discord": {"webhook_url"},
    "slack": {"webhook_url"},
    "email": {"smtp_password"},
    "ntfy": set(),
}

# Fields a channel needs before it can be switched on — mirrors each
# channel's `required` list in kaggle.js's KAGGLE_NOTIF_CHANNELS (the
# frontend disables the toggle for the same reason), but this is the copy
# that's actually enforced: the frontend check is only a courtesy, since the
# API can be hit directly.
_NOTIF_REQUIRED_FIELDS = {
    "telegram": ("bot_token", "chat_id"),
    "discord": ("webhook_url",),
    "slack": ("webhook_url",),
    "email": ("smtp_host", "to_addr"),
    "ntfy": ("topic",),
}

_notif_lock = threading.Lock()


def _notif_channel_configured(channel: str, cfg: Dict[str, Any]) -> bool:
    return all(str(cfg.get(key) or "").strip() for key in _NOTIF_REQUIRED_FIELDS.get(channel, ()))


def _default_notifications() -> Dict[str, Any]:
    return {
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
        "discord": {"enabled": False, "webhook_url": ""},
        "slack": {"enabled": False, "webhook_url": ""},
        "email": {
            "enabled": False, "smtp_host": "", "smtp_port": 587, "smtp_user": "",
            "smtp_password": "", "from_addr": "", "to_addr": "", "use_tls": True,
        },
        "ntfy": {"enabled": False, "topic": "", "server": "https://ntfy.sh"},
    }


def _load_notifications() -> Dict[str, Any]:
    path = settings.notifications_file
    merged = _default_notifications()
    if not path.exists():
        return merged
    try:
        stored = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return merged
    for channel, cfg in merged.items():
        if isinstance(stored.get(channel), dict):
            cfg.update(stored[channel])
    return merged


def _save_notifications(data: Dict[str, Any]) -> None:
    path = settings.notifications_file
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def get_notification_settings() -> Dict[str, Any]:
    """Public view of every channel's settings — secret fields are collapsed
    to a `<field>_set` boolean so a real token/password/webhook URL never
    round-trips back to the caller once saved."""
    data = _load_notifications()
    out: Dict[str, Any] = {}
    for channel, cfg in data.items():
        secret_fields = _NOTIF_SECRET_FIELDS.get(channel, set())
        public = {}
        for key, value in cfg.items():
            if key in secret_fields:
                public[f"{key}_set"] = bool(value)
            else:
                public[key] = value
        out[channel] = public
    return out


def update_notification_settings(channel: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    if channel not in NOTIFICATION_CHANNELS:
        raise NotificationError(f"Unknown notification channel: {channel}")
    with _notif_lock:
        data = _load_notifications()
        cfg = data[channel]
        secret_fields = _NOTIF_SECRET_FIELDS.get(channel, set())
        for key, value in (patch or {}).items():
            if key == "enabled":
                cfg["enabled"] = bool(value)
            elif key not in cfg:
                continue  # ignore unknown fields rather than silently growing the schema
            elif key in secret_fields:
                # Blank means "leave the stored secret alone" — mirrors
                # update_credentials()'s same convention for account keys.
                if isinstance(value, str) and value.strip() == "":
                    continue
                cfg[key] = value
            else:
                cfg[key] = value
        if cfg.get("enabled") and not _notif_channel_configured(channel, cfg):
            missing = [k for k in _NOTIF_REQUIRED_FIELDS[channel] if not str(cfg.get(k) or "").strip()]
            raise NotificationError(
                f"Can't enable {channel} — still missing: {', '.join(missing)}. "
                f"Configure it first, then enable it."
            )
        data[channel] = cfg
        _save_notifications(data)
    return get_notification_settings()[channel]


def _send_telegram(cfg: Dict[str, Any], text: str) -> None:
    token = (cfg.get("bot_token") or "").strip()
    chat_id = str(cfg.get("chat_id") or "").strip()
    if not token or not chat_id:
        raise NotificationError("Telegram is missing a bot token or chat ID.")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10).close()


def _send_discord(cfg: Dict[str, Any], text: str) -> None:
    url = (cfg.get("webhook_url") or "").strip()
    if not url:
        raise NotificationError("Discord is missing a webhook URL.")
    # Discord's webhook API reads the message body from "content", not
    # "text" — a generic Slack-style {"text": ...} webhook silently posts
    # nothing on Discord despite a 2xx response.
    req = urllib.request.Request(
        url, data=json.dumps({"content": text[:2000]}).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10).close()


def _send_slack(cfg: Dict[str, Any], text: str) -> None:
    url = (cfg.get("webhook_url") or "").strip()
    if not url:
        raise NotificationError("Slack is missing a webhook URL.")
    req = urllib.request.Request(
        url, data=json.dumps({"text": text}).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10).close()


def _send_email(cfg: Dict[str, Any], text: str) -> None:
    host = (cfg.get("smtp_host") or "").strip()
    to_addr = (cfg.get("to_addr") or "").strip()
    if not host or not to_addr:
        raise NotificationError("Email is missing an SMTP host or a recipient address.")
    user = (cfg.get("smtp_user") or "").strip()
    password = cfg.get("smtp_password") or ""
    from_addr = (cfg.get("from_addr") or "").strip() or user or to_addr
    port = int(cfg.get("smtp_port") or 587)
    msg = MIMEText(text)
    msg["Subject"] = "XDash notification"
    msg["From"] = from_addr
    msg["To"] = to_addr
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        if cfg.get("use_tls", True):
            smtp.starttls()
        if user and password:
            smtp.login(user, password)
        smtp.sendmail(from_addr, [to_addr], msg.as_string())


def _send_ntfy(cfg: Dict[str, Any], text: str) -> None:
    topic = (cfg.get("topic") or "").strip()
    if not topic:
        raise NotificationError("ntfy is missing a topic.")
    server = (cfg.get("server") or "https://ntfy.sh").strip().rstrip("/")
    req = urllib.request.Request(f"{server}/{topic}", data=text.encode(), method="POST")
    urllib.request.urlopen(req, timeout=10).close()


_NOTIF_SENDERS = {
    "telegram": _send_telegram,
    "discord": _send_discord,
    "slack": _send_slack,
    "email": _send_email,
    "ntfy": _send_ntfy,
}


def test_notification(channel: str) -> Dict[str, Any]:
    """Sends one real test message through *channel* right now, using
    whatever's currently saved for it — independent of its `enabled` flag,
    so a channel can be verified before being switched on."""
    if channel not in NOTIFICATION_CHANNELS:
        raise NotificationError(f"Unknown notification channel: {channel}")
    cfg = _load_notifications()[channel]
    try:
        _NOTIF_SENDERS[channel](cfg, "XDash test notification — if you can read this, the channel is wired up correctly.")
    except NotificationError:
        raise
    except Exception as e:
        raise NotificationError(f"{channel} test failed: {e}")
    return {"ok": True}


def send_all(text: str) -> None:
    """Best-effort fan-out to every enabled, configured channel. Never
    raises — a notification failure must never take down whatever background
    tick called it (the Kaggle poller, the scheduler's own tick, ...)."""
    data = _load_notifications()
    for channel, sender in _NOTIF_SENDERS.items():
        cfg = data.get(channel, {})
        if not cfg.get("enabled"):
            continue
        try:
            sender(cfg, text)
        except Exception:
            pass
