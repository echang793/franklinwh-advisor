"""SDG&E EV-TOU-5 time-of-use schedule and rates (effective Jan 2026)."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

# SDG&E revises rates roughly twice per year. If today is more than 180 days
# past this date, bill estimates may be stale — update _RATES below.
_RATES_EFFECTIVE_DATE = date(2026, 1, 1)


def rates_are_stale(today: date | None = None) -> bool:
    """Return True if rates are more than 180 days old."""
    if today is None:
        today = date.today()
    return (today - _RATES_EFFECTIVE_DATE).days > 180


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

# NEM 3.0 / Net Billing Tariff export credit rates ($/kWh).
# Aug/Sep have boosted evening export rates worth modeling; all other months
# use the NBT avoided-cost floor (~$0.05/kWh), far below the import rate.
# Source: user's SDG&E export schedule.
_NEM3_EXPORT_RATES: dict[int, dict[int, float]] = {
    8: {17: 0.907, 18: 1.022, 19: 0.920, 20: 0.996, 21: 0.895, 22: 0.885},
    9: {17: 0.253, 18: 0.595, 19: 0.673, 20: 0.380, 21: 0.154, 22: 0.154},
}
_NEM3_DEFAULT_EXPORT_RATE = 0.05  # $/kWh — NBT avoided-cost floor most months


def export_rate_at(dt: datetime) -> float:
    """Return NEM 3.0 export credit rate ($/kWh) for grid export at dt."""
    hour_rates = _NEM3_EXPORT_RATES.get(dt.month)
    if hour_rates:
        return hour_rates.get(dt.hour, _NEM3_DEFAULT_EXPORT_RATE)
    return _NEM3_DEFAULT_EXPORT_RATE


def peak_export_hour(month: int) -> tuple[int, float] | None:
    """Highest-value export (hour, $/kWh) for the month, or None outside Aug/Sep."""
    rates = _NEM3_EXPORT_RATES.get(month)
    if not rates:
        return None
    hour = max(rates, key=rates.__getitem__)
    return hour, rates[hour]


def _is_holiday(dt: datetime) -> bool:
    """Return True if dt is an SDG&E-observed holiday (treated as Sunday schedule)."""
    y, m, d = dt.year, dt.month, dt.day
    if (m, d) == (1, 1):   return True  # New Year's Day
    if (m, d) == (7, 4):   return True  # Independence Day
    if (m, d) == (11, 11): return True  # Veterans Day
    if (m, d) == (12, 25): return True  # Christmas
    if m == 2:  # Presidents Day — 3rd Monday of February
        mondays = [i for i in range(1, 29) if datetime(y, 2, i).weekday() == 0]
        if len(mondays) >= 3 and d == mondays[2]: return True
    if m == 5:  # Memorial Day — last Monday of May
        mondays = [i for i in range(1, 32) if datetime(y, 5, i).weekday() == 0]
        if mondays and d == mondays[-1]: return True
    if m == 9:  # Labor Day — 1st Monday of September
        mondays = [i for i in range(1, 31) if datetime(y, 9, i).weekday() == 0]
        if mondays and d == mondays[0]: return True
    if m == 11:  # Thanksgiving — 4th Thursday of November
        thursdays = [i for i in range(1, 31) if datetime(y, 11, i).weekday() == 3]
        if len(thursdays) >= 4 and d == thursdays[3]: return True
    return False


def base_service_cost(days: float) -> float:
    """Fixed basic-service charge over N days (EV-TOU-5)."""
    return max(0.0, days) * BASE_SERVICE_DAILY


def period_at(dt: datetime) -> TouPeriod:
    """Return the EV-TOU-5 period for a given datetime."""
    h = dt.hour
    if dt.weekday() >= 5 or _is_holiday(dt):   # Saturday, Sunday, or holiday
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
