"""SDG&E EV-TOU-5 time-of-use schedule and rates (effective Jan 2026)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum


class TouPeriod(str, Enum):
    SUPER_OFF_PEAK = "super_off_peak"
    OFF_PEAK       = "off_peak"
    ON_PEAK        = "on_peak"


_SUMMER_MONTHS = {6, 7, 8, 9, 10}  # June–October

_RATES = {
    "summer": {
        TouPeriod.SUPER_OFF_PEAK: 0.12424,
        TouPeriod.OFF_PEAK:       0.50245,
        TouPeriod.ON_PEAK:        0.79988,
    },
    "winter": {
        TouPeriod.SUPER_OFF_PEAK: 0.11686,
        TouPeriod.OFF_PEAK:       0.47267,
        TouPeriod.ON_PEAK:        0.52926,
    },
}

_ON_PEAK_START = 16  # 4 pm
_ON_PEAK_END   = 21  # 9 pm

# SDG&E EV-TOU-5 fixed Basic Service Fee — charged per day regardless of usage.
# ~$16/month ÷ 30. Verify against your bill; update if SDG&E changes it.
BASE_SERVICE_DAILY = 0.53


def base_service_cost(days: float) -> float:
    """Fixed basic-service charge over N days (EV-TOU-5)."""
    return max(0.0, days) * BASE_SERVICE_DAILY


def period_at(dt: datetime) -> TouPeriod:
    """Return the EV-TOU-5 period for a given datetime."""
    h = dt.hour
    if dt.weekday() >= 5:   # Saturday / Sunday
        if h < 14: return TouPeriod.SUPER_OFF_PEAK
        if h < 16: return TouPeriod.OFF_PEAK
        if h < 21: return TouPeriod.ON_PEAK
        return TouPeriod.OFF_PEAK
    # Weekday
    if h < 6:  return TouPeriod.SUPER_OFF_PEAK
    if h < 10: return TouPeriod.OFF_PEAK
    if h < 14: return TouPeriod.SUPER_OFF_PEAK  # mid-day super off-peak 10 am–2 pm
    if h < 16: return TouPeriod.OFF_PEAK
    if h < 21: return TouPeriod.ON_PEAK
    return TouPeriod.OFF_PEAK


def rate_at(dt: datetime) -> float:
    """Return $/kWh for grid import at dt."""
    season = "summer" if dt.month in _SUMMER_MONTHS else "winter"
    return _RATES[season][period_at(dt)]


def cheap_charge_deadline(dt: datetime) -> datetime | None:
    """
    Return the end of today's Super Off-Peak window (2 pm), or None if already past it.
    This is the latest time cheap grid import is available on EV-TOU-5.
    """
    cutoff = dt.replace(hour=14, minute=0, second=0, microsecond=0)
    return cutoff if dt < cutoff else None


def on_peak_window(dt: datetime) -> tuple[datetime, datetime]:
    """Return (start, end) of the on-peak window for the day containing dt."""
    base = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return base.replace(hour=_ON_PEAK_START), base.replace(hour=_ON_PEAK_END)
