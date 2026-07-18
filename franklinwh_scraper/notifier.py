"""Notification dispatchers."""

from __future__ import annotations

import json
import logging
import smtplib
import subprocess
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests

from .advisor import Recommendation

logger = logging.getLogger(__name__)

_URGENCY_EMOJI = {
    "critical": "🔴",
    "warning":  "🟡",
    "info":     "🟢",
}


def rec_to_text(rec: Recommendation) -> str:
    """Format a Recommendation as a plain-text message body (used by all channels)."""
    emoji = _URGENCY_EMOJI.get(rec.urgency, "⚪")
    if rec.needs_action:
        action = rec.mode.value.replace("_", " ").upper()
        return (
            f"<b>{emoji} FranklinWH: Switch to {action}</b>\n"
            f"{rec.reason}\n"
            f"SoC {rec.details.get('soc_pct', 0):.0f}%  "
            f"Solar {rec.details.get('solar_kw', 0):.1f}kW  "
            f"Grid {rec.details.get('grid_use_kw', 0):+.1f}kW"
        )
    return f"<b>{emoji} FranklinWH: Battery OK</b> — {rec.reason}"


def notify_macos(rec: Recommendation) -> None:
    """Fire a macOS notification via osascript."""
    emoji = _URGENCY_EMOJI.get(rec.urgency, "⚪")
    if rec.needs_action:
        title = f"{emoji} FranklinWH — Switch to {rec.mode.value.replace('_', ' ').title()}"
    else:
        title = f"{emoji} FranklinWH — Battery OK"

    body = rec.reason[:200]
    script = (
        f'display notification "{_esc(body)}" '
        f'with title "{_esc(title)}" '
        f'sound name "Submarine"'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
        logger.debug("macOS notification sent: %s", title)
    except subprocess.CalledProcessError as e:
        logger.warning("macOS notification failed: %s", e.stderr.decode().strip())
    except FileNotFoundError:
        logger.warning("osascript not available (not macOS?)")


def notify_imessage(rec: Recommendation, phone: str) -> None:
    """Send an iMessage via AppleScript (macOS only, Messages app must be set up)."""
    notify_imessage_text(rec_to_text(rec), phone)


def notify_log(rec: Recommendation, log_path: Path) -> None:
    """Append a structured JSON record to a log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now().isoformat(),
        "urgency": rec.urgency,
        "recommended_mode": rec.mode.value,
        "needs_action": rec.needs_action,
        "reason": rec.reason,
        "details": rec.details,
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")
    logger.debug("Logged recommendation to %s", log_path)


def notify_imessage_text(body: str, phone: str) -> None:
    """Send a plain text iMessage (not tied to a Recommendation object)."""
    script = (
        f'tell application "Messages"\n'
        f'  set targetService to 1st service whose service type = iMessage\n'
        f'  set targetBuddy to buddy "{phone}" of targetService\n'
        f'  send "{_esc(body)}" to targetBuddy\n'
        f'end tell'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
        logger.debug("iMessage sent to %s", phone)
    except subprocess.CalledProcessError as e:
        logger.warning("iMessage failed: %s", e.stderr.decode().strip())
    except FileNotFoundError:
        logger.warning("osascript not available (not macOS?)")


def _with_retry(fn, label: str, attempts: int = 3, base_delay: float = 2.0) -> None:
    """Retry a zero-arg callable on any exception, with linear backoff.

    Email/webhook failures (SMTP hiccup, transient network error) used to be
    single-attempt and silently dropped — this gives them the same resilience
    Telegram already had, and escalates to ERROR once retries are exhausted so
    a lost alert is actually discoverable in advisor.log instead of buried at
    WARNING level.
    """
    for attempt in range(attempts):
        try:
            fn()
            return
        except Exception as e:
            logger.warning("%s failed (attempt %d/%d): %s", label, attempt + 1, attempts, e)
            if attempt < attempts - 1:
                time.sleep(base_delay)
    logger.error("%s failed after %d attempts — alert may be lost", label, attempts)


def notify_telegram(body: str, bot_token: str, chat_id: str) -> None:
    """Send a Telegram message via the Bot API (cross-platform, free)."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(
                url,
                json={"chat_id": chat_id, "text": body, "parse_mode": "HTML"},
                timeout=10,
            )
            if r.ok:
                logger.debug("Telegram message sent to chat %s", chat_id)
                return
            logger.warning("Telegram error %s: %s", r.status_code, r.text[:200])
            if r.status_code < 500:
                return  # 4xx — don't retry
        except Exception as e:
            logger.warning("Telegram notification failed (attempt %d/3): %s", attempt + 1, e)
        if attempt < 2:
            time.sleep(2)


def notify_ha_webhook(url: str, data: dict) -> None:
    """POST a JSON state payload to a Home Assistant webhook URL."""
    def _send():
        requests.post(url, json=data, timeout=5)
        logger.debug("HA webhook posted to %s", url)
    _with_retry(_send, "HA webhook")


def fetch_telegram_chat_id(bot_token: str, retries: int = 3, wait: int = 3) -> str | None:
    """Poll getUpdates to auto-detect the chat ID after user messages the bot.

    Retries up to `retries` times with `wait` seconds between attempts so the
    user has time to send a message during the setup wizard.
    """
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    for attempt in range(retries):
        try:
            data = requests.get(url, timeout=10).json()
            for upd in reversed(data.get("result", [])):
                for key in ("message", "edited_message", "channel_post"):
                    msg = upd.get(key)
                    if msg and "chat" in msg:
                        return str(msg["chat"]["id"])
                cq = upd.get("callback_query", {})
                msg = cq.get("message") if cq else None
                if msg and "chat" in msg:
                    return str(msg["chat"]["id"])
        except Exception as e:
            logger.warning("getUpdates attempt %d failed: %s", attempt + 1, e)
        if attempt < retries - 1:
            time.sleep(wait)
    return None


def _esc(s: str) -> str:
    """Escape a string for embedding in an osascript double-quoted literal.

    Backslash must be escaped first (or it would double-escape the
    subsequent quote/newline escaping); newlines and carriage returns are
    escaped too since raw control characters can distort the interpolated
    AppleScript string.
    """
    return (s.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\n", "\\n")
             .replace("\r", ""))


def notify_email(body: str, cfg: "Config") -> None:
    """Send alert via SMTP email. Uses STARTTLS on cfg.smtp_port (default 587)."""
    if not (cfg.smtp_host and cfg.email_to):
        return

    def _send():
        subject = body.splitlines()[0][:60] if body else "FranklinWH Alert"
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = cfg.email_from or cfg.email_to
        msg["To"] = cfg.email_to
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as s:
            s.ehlo()
            s.starttls()
            if cfg.smtp_user:
                s.login(cfg.smtp_user, cfg.smtp_password)
            s.sendmail(msg["From"], [cfg.email_to], msg.as_string())
        logger.debug("Email sent to %s", cfg.email_to)

    _with_retry(_send, "Email notification")


def notify_webhook(body: str, urgent: bool, cfg: "Config") -> None:
    """POST alert as JSON to cfg.webhook_url (Slack, Discord, custom endpoint, etc.)."""
    if not cfg.webhook_url:
        return

    def _send():
        requests.post(
            cfg.webhook_url,
            json={
                "alert":     body,
                "urgent":    urgent,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            timeout=10,
        )
        logger.debug("Webhook posted to %s", cfg.webhook_url)

    _with_retry(_send, "Webhook notification")
