"""Notification dispatchers."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import datetime
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
            f"{emoji} FranklinWH: Switch to {action}\n"
            f"{rec.reason}\n"
            f"SoC {rec.details.get('soc_pct', 0):.0f}%  "
            f"Solar {rec.details.get('solar_kw', 0):.1f}kW  "
            f"Grid {rec.details.get('grid_use_kw', 0):+.1f}kW"
        )
    return f"{emoji} FranklinWH: Battery OK — {rec.reason}"


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


def notify_telegram(body: str, bot_token: str, chat_id: str) -> None:
    """Send a Telegram message via the Bot API (cross-platform, free)."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(
                url,
                json={"chat_id": chat_id, "text": body},
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
    try:
        requests.post(url, json=data, timeout=5)
        logger.debug("HA webhook posted to %s", url)
    except Exception as e:
        logger.warning("HA webhook failed: %s", e)


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
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(wait)
    return None


def _esc(s: str) -> str:
    """Escape double quotes for osascript strings."""
    return s.replace('"', '\\"')
