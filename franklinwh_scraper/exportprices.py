"""Hourly Solar Billing Plan export prices, from SDG&E's published schedule.

Export credits vary by hour and month (delivery ~$0.004/kWh at midday but
~$0.27 in the evening; generation above $2/kWh at 6-8 PM in September), so a
single flat $/kWh can be off 4x between bills. SDG&E publishes the hourly
values (sdge.com/solar/solar-billing-plan/export-pricing, "Legacy 2024" =
NBT24 vintage); this module reads a compact JSON built from that file by
scripts/build_export_prices.py. Replayed over the real Aug and Sep 2026
cycles the delivery schedule gives $3.31 vs the bill's $3.29 and $29.43 vs
$30.17.
"""

from __future__ import annotations

import csv
import json
import re
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "data" / "export_prices_legacy2024.json"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_VALUE_NAME = re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (Weekday|Weekend) HS(\d{1,2})$")


class ExportSchedule:
    """{year: {"delivery"|"generation": {month: {"Weekday"|"Weekend": [24 $/kWh]}}}}"""

    def __init__(self, years: dict[str, dict]):
        self._years = {int(y): v for y, v in years.items()}

    @classmethod
    def from_dict(cls, data: dict) -> "ExportSchedule":
        return cls(data.get("years", {}) if isinstance(data, dict) else {})

    def rates(self, dt: datetime, weekend: bool) -> tuple[float, float] | None:
        """(delivery, generation) $/kWh for an export at local time `dt`, or
        None if the schedule has no complete value for it. Years outside the
        schedule use its nearest year. `weekend` includes SDG&E holidays."""
        if not self._years:
            return None
        year = min(max(dt.year, min(self._years)), max(self._years))
        day = "Weekend" if weekend else "Weekday"
        try:
            block = self._years[year]
            return (float(block["delivery"][MONTHS[dt.month - 1]][day][dt.hour]),
                    float(block["generation"][MONTHS[dt.month - 1]][day][dt.hour]))
        except (KeyError, IndexError, TypeError, ValueError):
            return None


def _complete(block: dict) -> bool:
    return all(
        isinstance(block.get(kind, {}).get(m, {}).get(day), list)
        and len(block[kind][m][day]) == 24 and None not in block[kind][m][day]
        for kind in ("delivery", "generation") for m in MONTHS for day in ("Weekday", "Weekend"))


def build_from_csv(path: str | Path, require_complete: bool = True) -> dict:
    """Compact JSON-able schedule from SDG&E's "NBT Pricing Upload MIDAS" CSV.

    Rows are per hour per day, but the value depends only on (year, month,
    weekday/weekend, hour) — the ValueName, e.g. "Sep Weekday HS18" (HS = hour
    start, Pacific). The file's dates are UTC, so a stray row per slot can
    carry the previous December's value into January; the most common value
    per slot wins. Rows that don't parse are skipped. With `require_complete`
    (the default), years missing any month/day-type/hour are dropped — those
    are the boundary strays, not real years.
    """
    buckets: dict[tuple, list[float]] = defaultdict(list)
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                kind = "delivery" if "SDXX" in row["RIN"] else "generation" if "XXSD" in row["RIN"] else None
                m = _VALUE_NAME.match(row["ValueName"].strip())
                year = datetime.strptime(row["DateStart"], "%m/%d/%Y").year
                value = float(row["Value"])
            except (KeyError, ValueError, TypeError):
                continue
            hour = int(m.group(3)) if m else -1
            if kind and m and 0 <= hour <= 23:
                buckets[(str(year), kind, m.group(1), m.group(2), hour)].append(value)

    years: dict = {}
    for (year, kind, month, day, hour), values in buckets.items():
        try:
            best = statistics.mode(values)
        except statistics.StatisticsError:
            best = values[0]
        slot = years.setdefault(year, {}).setdefault(kind, {}).setdefault(month, {}).setdefault(day, [None] * 24)
        slot[hour] = round(best, 6)
    if require_complete:
        years = {y: b for y, b in years.items() if _complete(b)}
    return {
        "meta": {
            "source": "SDG&E Solar Billing Plan export pricing, Legacy 2024 (NBT24) — "
                      "sdge.com/solar/solar-billing-plan/export-pricing",
            "unit": "$/kWh exported",
            "note": "hour = Pacific local hour start; Weekend includes SDG&E holidays; values past the "
                    "customer's 9-year lock-in are illustrative",
        },
        "years": years,
    }


def load_default() -> ExportSchedule | None:
    """The bundled schedule, or None if the data file is missing or corrupt
    (callers then fall back to the flat export rate)."""
    try:
        return ExportSchedule.from_dict(json.loads(DEFAULT_PATH.read_text()))
    except (OSError, ValueError):
        return None
