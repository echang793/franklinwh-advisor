"""Read-only history tools the Telegram chatbot can call.

The chatbot's context block (chatbot.build_context) only describes *right
now*, so "what was my import last Tuesday?" had no answer. These tools let
Claude look a past day or moment up in history.db. They are deliberately
narrow: each takes a date/timestamp, goes through existing HistoryStore
methods (never model-written SQL), and returns small JSON-safe dicts. Bad
input yields {"error": ...} for the model to relay, never an exception.
"""

from __future__ import annotations

import math
import re
from datetime import datetime

from .history import integrate_intervals
from .savings import compute as savings_compute

TOOLS = [
    {
        "name": "daily_summary",
        "description": (
            "Energy totals for one past calendar day from the home's history: solar produced, "
            "home use, grid import/export, battery charge/discharge, and the import cost and "
            "export credit at the day's TOU rates. Use for any question about a specific past "
            "day (yesterday, last Tuesday, a date). Not for right now — the data block has that."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD, today or earlier"}},
            "required": ["date"],
        },
    },
    {
        "name": "soc_at",
        "description": (
            "Battery state of charge (%) at a past moment, from the nearest reading within "
            "30 minutes. Use for 'what was my battery at 7am yesterday'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"timestamp": {"type": "string", "description": "ISO local time, e.g. 2026-09-30T07:00"}},
            "required": ["timestamp"],
        },
    },
]

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean(v: float) -> float:
    """Round and keep NaN/Inf out of the JSON the model (and Telegram) will see."""
    return round(v, 2) if math.isfinite(v) else 0.0


def daily_summary(store, date_str: str, now: datetime) -> dict:
    if not isinstance(date_str, str) or not _DATE_RE.match(date_str):
        return {"error": "date must be YYYY-MM-DD"}
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return {"error": "not a real calendar date"}
    if day > now.date():
        return {"error": "that date is in the future"}

    # weekly_readings' end date is inclusive (it filters timestamp < next_day(end)),
    # so a single day is (date, date) — passing the next day would add its readings.
    readings = store.weekly_readings(date_str, date_str)
    if not readings:
        return {"error": f"no history recorded for {date_str}"}

    sv = savings_compute(integrate_intervals(readings))
    charged, discharged = store.daily_battery_kwh(date_str)
    return {
        "date": date_str,
        "readings": len(readings),
        # False means polling stopped before the solar day ended (an outage), so
        # the day's totals are a lower bound — say so rather than present them as complete.
        "covers_solar_day": bool(store.day_has_solar_coverage(date_str)),
        "solar_kwh": _clean(store.daily_solar_kwh_api(date_str)),
        "home_use_kwh": _clean(sv.home_kwh),
        "grid_import_kwh": _clean(sv.import_kwh),
        "grid_export_kwh": _clean(sv.export_kwh),
        "battery_charged_kwh": _clean(charged),
        "battery_discharged_kwh": _clean(discharged),
        "import_cost_usd": _clean(sv.actual_import_cost),
        "export_credit_usd": _clean(sv.actual_export_credit),
    }


def soc_at(store, timestamp: str) -> dict:
    try:
        ts = datetime.fromisoformat(str(timestamp).strip().replace(" ", "T"))
    except ValueError:
        return {"error": "timestamp must be an ISO time like 2026-09-30T07:00"}
    soc = store.soc_near(ts.isoformat())
    if soc is None:
        return {"error": "no reading within 30 minutes of that time"}
    return {"timestamp": ts.isoformat(), "soc_pct": _clean(soc)}


def run_tool(store, name: str, args: dict, now: datetime | None = None) -> dict:
    """Dispatch one tool call. Never raises: failures come back as {"error": ...}."""
    if store is None:
        return {"error": "history isn't available right now"}
    args = args if isinstance(args, dict) else {}
    try:
        if name == "daily_summary":
            return daily_summary(store, args.get("date", ""), now or datetime.now())
        if name == "soc_at":
            return soc_at(store, args.get("timestamp", ""))
    except Exception as e:  # a bad history DB must not take the chat down
        return {"error": f"lookup failed: {e}"}
    return {"error": f"unknown tool {name!r}"}
