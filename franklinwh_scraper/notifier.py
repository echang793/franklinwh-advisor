"""Notification dispatchers."""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

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

    # Keep body short for the banner
    body = rec.reason[:200]

    script = (
        f'display notification "{_esc(body)}" '
        f'with title "{_esc(title)}" '
        f'sound name "Submarine"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
        )
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
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
        )
        logger.debug("iMessage sent to %s", phone)
    except subprocess.CalledProcessError as e:
        logger.warning("iMessage failed: %s", e.stderr.decode().strip())
    except FileNotFoundError:
        logger.warning("osascript not available (not macOS?)")


def notify_telegram(body: str, bot_token: str, chat_id: str) -> None:
    """Send a Telegram message via the Bot API (cross-platform, free)."""
    import json as _json
    from urllib.request import Request as _Req, urlopen as _open
    from urllib.error import URLError as _URLError

    url  = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = _json.dumps({"chat_id": chat_id, "text": body}).encode()
    req  = _Req(url, data=data, headers={"Content-Type": "application/json"})
    try:
        _open(req, timeout=10)
        logger.debug("Telegram message sent to chat %s", chat_id)
    except _URLError as e:
        logger.warning("Telegram notification failed: %s", e)
    except Exception as e:
        logger.warning("Telegram notification error: %s", e)


def fetch_telegram_chat_id(bot_token: str) -> str | None:
    """Poll getUpdates to auto-detect the chat ID after user messages the bot."""
    import json as _json
    from urllib.request import urlopen as _open
    from urllib.error import URLError as _URLError

    try:
        url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
        with _open(url, timeout=10) as r:
            data = _json.loads(r.read())
        results = data.get("result", [])
        if results:
            msg = results[-1].get("message") or results[-1].get("channel_post", {})
            return str(msg["chat"]["id"])
    except (_URLError, KeyError, IndexError, Exception):
        pass
    return None


def _esc(s: str) -> str:
    """Escape double quotes for osascript strings."""
    return s.replace('"', '\\"')
