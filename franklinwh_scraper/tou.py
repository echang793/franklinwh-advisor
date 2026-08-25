"""SDG&E EV-TOU-5 time-of-use schedule and rates (effective Jan 2026)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
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

# NOT independently verified against a real bill yet (unlike the export
# rate below, fixed 2026-08-24). The customer is actually on San Diego
# Community Power (SDCP, a CCA) — real SDCP generation-only rates from an
# itemized bill are on-peak $0.38242, off-peak $0.11828, super-off-peak
# $0.0368/kWh (summer), well below these numbers, because these are
# bundled SDG&E rates (delivery+generation) and SDCP splits the two.
# Reconstructing the correct combined delivery+generation rate needs a
# per-TOU-period delivery breakdown the bill summary doesn't show
# (sdge.com/SolarBillDetails has it) — on/off-peak here happen to be
# close to a rough combined estimate, but super-off-peak looks likely too
# high. Left alone rather than guessing at delivery's TOU structure from
# one bill; revisit with a SolarBillDetails export or a second bill.
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
# Per SDG&E's official "1-1-26 Schedule EV-TOU-5 Total Rates Table" tariff filing
# (sdge.com/sites/default/files/regulatory/), the Base Services Charge is
# $0.79343/day. Verify against your bill; update if SDG&E changes it.
BASE_SERVICE_DAILY = 0.79343

# Export credit rate ($/kWh) — actual customer is on San Diego Community
# Power (SDCP, a CCA), not bundled SDG&E generation. Real number confirmed
# 2026-08-24 from an itemized SDG&E/SDCP bill (cycle 7/21-8/18/26, "Legacy
# 2024 Pricing"): SDCP generation export credit $0.08201/kWh + $0.0075/kWh
# adder = $0.08951, plus SDG&E delivery export credit ~$0.0313/kWh
# (-$3.29 / 105 kWh) = ~$0.121/kWh combined. Flat, not hour-differentiated
# — the bill shows one rate applied to total monthly export, no per-hour
# schedule.
#
# This REPLACES a previous $0.885-1.022/kWh Aug/Sep "boosted evening rate"
# table that turned out to have no support in the real bill — source
# unclear, off by ~8-10x from the real CCA export credit, and had been
# driving every export-arbitrage/sundown-export-value $ estimate the app
# showed. If SDG&E's dynamic RIN wholesale pricing (mentioned on the bill,
# sdge.com's hourly-pricing-by-RIN tool) turns out to be a real, separate
# export mechanism on top of the CCA credit, this flat rate would need to
# become "CCA credit + RIN premium" instead — not established by this bill
# alone, so not modeled here.
_NEM3_DEFAULT_EXPORT_RATE = 0.121  # $/kWh — real SDCP+SDG&E combined export credit


def export_rate_at(dt: datetime) -> float:
    """Return the export credit rate ($/kWh) for grid export at dt.

    Flat year-round — see _NEM3_DEFAULT_EXPORT_RATE's docstring. `dt` is
    kept in the signature (unused) so every call site that reasonably
    expects a time-varying rate doesn't need touching if real hourly data
    ever replaces this.
    """
    return _NEM3_DEFAULT_EXPORT_RATE


def peak_export_hour(month: int) -> tuple[int, float]:
    """Highest-value export (hour, $/kWh). Flat rate now (see
    _NEM3_DEFAULT_EXPORT_RATE) so "peak" is nominal — returns a
    representative evening hour at the one real rate, kept as a
    (hour, rate) pair since callers display both.
    """
    return 18, _NEM3_DEFAULT_EXPORT_RATE


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


# ── Schedule DR-SES (residential-with-solar alternative to EV-TOU-5) ──────
# Used only for the rate-plan-comparison alert (savings.compare_rate_plans) —
# never for billing math above, which is all EV-TOU-5. Rates verified against
# SDG&E's official "1-1-26 Schedule DR-SES Total Rates Table" (sdge.com/
# sites/default/files/regulatory/1-1-26%20Schedule%20DR-SES%20Total%20Rates
# %20Table.pdf), fetched 2026-08-24 — same _RATES_EFFECTIVE_DATE staleness
# caveat as EV-TOU-5's _RATES applies here too.
_DRSES_RATES = {
    "summer": {
        TouPeriod.SUPER_OFF_PEAK: 0.35588,
        TouPeriod.OFF_PEAK:       0.44763,
        TouPeriod.ON_PEAK:        0.74506,
    },
    "winter": {
        TouPeriod.SUPER_OFF_PEAK: 0.34850,
        TouPeriod.OFF_PEAK:       0.41785,
        TouPeriod.ON_PEAK:        0.47444,
    },
}
# Base Services Charge is $0.79343/day on DR-SES too (same UDC line item as
# EV-TOU-5's BASE_SERVICE_DAILY) — identical in the counterfactual, so
# compare_rate_plans() ignores it rather than duplicating the constant.


def _drses_period_at(dt: datetime) -> TouPeriod:
    """Return the DR-SES period for dt — NOT the same schedule as period_at().

    Weekend/holiday periods match EV-TOU-5's exactly (midnight-2pm super
    off-peak, 2-4pm off-peak, 4-9pm on-peak, 9pm-midnight off-peak). Weekday
    periods differ: EV-TOU-5 carves 10am-2pm into super-off-peak year-round
    to incentivize midday EV charging; DR-SES only does that in March/April
    (and only reaches the tariff's "Winter" season definition, which runs
    Nov-May) — the rest of the year, weekday off-peak runs straight 6am-4pm
    with no midday carve-out. Source: SDG&E Schedule DR-SES tariff, Sheet 2,
    "Time Periods" table (sdge.com/sites/default/files/elec_elec-scheds_dr-
    ses.pdf), fetched 2026-08-24 — a tariff *structure* document, distinct
    from (and more stable than) the twice-yearly-revised rates table above.
    """
    h = dt.hour
    if dt.weekday() >= 5 or _is_holiday(dt):
        if h < 14:
            return TouPeriod.SUPER_OFF_PEAK
        if h < 16:
            return TouPeriod.OFF_PEAK
        if h < 21:
            return TouPeriod.ON_PEAK
        return TouPeriod.OFF_PEAK
    # Weekday
    if h < 6:
        return TouPeriod.SUPER_OFF_PEAK
    if dt.month in (3, 4) and 10 <= h < 14:
        return TouPeriod.SUPER_OFF_PEAK
    if h < 16:
        return TouPeriod.OFF_PEAK
    if h < 21:
        return TouPeriod.ON_PEAK
    return TouPeriod.OFF_PEAK


def drses_rate_at(dt: datetime) -> float:
    """Return $/kWh for grid import at dt under Schedule DR-SES."""
    season = "summer" if dt.month in _SUMMER_MONTHS else "winter"
    return _DRSES_RATES[season][_drses_period_at(dt)]


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


# Default day-of-month the utility billing cycle starts. SDG&E bills on a
# per-meter read date rather than a utility-wide constant, so this is only a
# default — Config.billing_cycle_start_day overrides it. Lives here, with the
# rest of the tariff logic, so every consumer (dashboard, digests, chatbot)
# derives its cycle from one implementation instead of three that disagree.
DEFAULT_CYCLE_START_DAY = 20


def _clamp_day(year: int, month: int, day: int) -> date:
    """date(year, month, day) with day clamped to the month's length.

    A start_day of 29-31 would otherwise raise in a shorter month
    (date(2026, 4, 31) is not a date), so the cycle silently rolls back to
    that month's last day instead.
    """
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last = (nxt - timedelta(days=1)).day
    return date(year, month, min(day, last))


def cycle_bounds(d: date, start_day: int = DEFAULT_CYCLE_START_DAY) -> tuple[date, date]:
    """Return (first_day, last_day) of the billing cycle containing `d`."""
    start_day = max(1, min(31, int(start_day)))
    anchor = _clamp_day(d.year, d.month, start_day)
    if d >= anchor:
        start = anchor
    else:
        prev = d.replace(day=1) - timedelta(days=1)
        start = _clamp_day(prev.year, prev.month, start_day)
    if start.month == 12:
        nxt = _clamp_day(start.year + 1, 1, start_day)
    else:
        nxt = _clamp_day(start.year, start.month + 1, start_day)
    return start, nxt - timedelta(days=1)
