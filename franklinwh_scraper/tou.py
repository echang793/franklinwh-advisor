"""SDG&E EV-TOU-5 time-of-use schedule and rates (SDG&E delivery 6/1/2026, SDCP generation per the Sep 2026 bill)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import Enum

# SDG&E/SDCP revise rates roughly twice per year. If today is more than 180
# days past this date, bill estimates may be stale — update _RATES below.
_RATES_EFFECTIVE_DATE = date(2026, 6, 1)  # SDG&E EV-TOU-5 / DR-SES delivery table date


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

# Reconstructed from real, dated rate schedules plus the customer's own
# itemized bill — no bundled SDG&E number and no guess at delivery's TOU
# structure.
#
# Customer is on San Diego Community Power (SDCP, a CCA), not bundled
# SDG&E generation. Unbundled customers pay SDG&E delivery + SDCP
# generation separately (SDG&E's own EV-TOU-5 tariff, note 2: "Unbundled
# customers do not pay SDG&E's commodity rates").
#
# Delivery (SDG&E, "UDC Total" + "WF-NBC + DWR-BC Rate" columns of
# Schedule EV-TOU-5, effective 6/1/2026, sdge.com/sites/default/files/
# regulatory/6-1-26%20Schedule%20EV-TOU-5%20Total%20Rates%20Table.pdf) —
# same in summer and winter; on-peak and off-peak share one value, only
# super-off-peak drops. Raised from the 10/1/2025 table's 0.30120/0.02858
# UDC; the app was still on that older table and under-called import cost
# by ~22% on the Aug 19 - Sep 17 2026 bill.
#   on-peak = off-peak: 0.31711 (UDC) + 0.00591 (WF-NBC/DWR-BC) = 0.32302
#   super-off-peak:     0.04114 (UDC) + 0.00591 (WF-NBC/DWR-BC) = 0.04705
#
# Generation (SDCP EV-TOU-5, PowerBase). Summer is taken directly from the
# customer's Aug 19 - Sep 17 2026 SDCP itemization (0.38242 / 0.11828 /
# 0.0368 — within 0.4% of SDCP's published 1/1/2026 table, so SDCP's
# mid-year update is small). Winter is still SDCP's published 1/1/2026
# table (sdcommunitypower.org/wp-content/uploads/2026/01/Res_2021V_2026.pdf)
# — refresh from a winter bill when one is available.
#   summer: on-peak 0.38242, off-peak 0.11828, super-off-peak 0.0368
#   winter: on-peak 0.14237, off-peak 0.09205, super-off-peak 0.03039
#
# PCIA (Power Charge Indifference Adjustment, CCA 2021 vintage 0.03564
# $/kWh per the 6/1/2026 tariff): the bill's "Delivery Import Charges"
# ($15.86) exceed UDC + WF-NBC priced at the bill's kWh ($13.36) by ~$2.50.
# That residual is consistent with PCIA applying to net imports (import
# minus export, ~62 kWh this cycle) rather than all imports, so it is
# modeled as one small per-import-kWh adder (_PCIA_NET_ADDER) folded into
# every period. It is an empirical fit to ONE bill — it will drift with
# how much you export — so refine it from further bills.
_PCIA_NET_ADDER = 0.0133  # $/kWh imported (~PCIA 0.03564 x ~37% net-import share)

# SDG&E delivery (UDC + WF-NBC/DWR-BC), EV-TOU-5 effective 6/1/2026 — see above.
DELIVERY_ON_OFF = 0.31711 + 0.00591
DELIVERY_SUPER_OFF = 0.04114 + 0.00591

# SDCP generation by season/period (see above for provenance).
_GEN_DEFAULT = {
    "summer": {
        TouPeriod.SUPER_OFF_PEAK: 0.0368,
        TouPeriod.OFF_PEAK:       0.11828,
        TouPeriod.ON_PEAK:        0.38242,
    },
    "winter": {
        TouPeriod.SUPER_OFF_PEAK: 0.03039,
        TouPeriod.OFF_PEAK:       0.09205,
        TouPeriod.ON_PEAK:        0.14237,
    },
}


def _delivery(period: TouPeriod) -> float:
    return DELIVERY_SUPER_OFF if period == TouPeriod.SUPER_OFF_PEAK else DELIVERY_ON_OFF


# Values learned from the user's own bills (bill-record --from-text; installed
# by alerts._load_peak_state from state["learned_import"]). The tables above
# go stale every time SDCP/SDG&E revise rates; the newest bill is ground truth.
#   {"season": "summer", "gen": {"on_peak": r, "off_peak": r, "super_off_peak": r},
#    "pcia_adder": $/kWh, "base_daily": $/day}
# Each field is validated on its own, so one bad value can't discard the rest.
_learned_import: dict = {}
_LEARNED_GEN_MAX = 1.5      # $/kWh — above this is a parse error, not a rate
_LEARNED_ADDER_MAX = 0.06   # $/kWh — PCIA-scale; larger means the tariff table moved
_LEARNED_BASE_RANGE = (0.3, 2.0)  # $/day


def set_learned_import(learned) -> None:
    """Install (or, with None/invalid input, clear) bill-learned import terms."""
    global _learned_import
    clean: dict = {}
    if isinstance(learned, dict):
        season = learned.get("season")
        gen = learned.get("gen")
        if season in _GEN_DEFAULT and isinstance(gen, dict):
            ok = {}
            for period in TouPeriod:
                v = gen.get(period.value)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= _LEARNED_GEN_MAX:
                    ok[period] = float(v)
            if ok:
                clean["season"], clean["gen"] = season, ok
        adder = learned.get("pcia_adder")
        if isinstance(adder, (int, float)) and not isinstance(adder, bool) and 0 <= adder <= _LEARNED_ADDER_MAX:
            clean["pcia_adder"] = float(adder)
        base = learned.get("base_daily")
        if (isinstance(base, (int, float)) and not isinstance(base, bool)
                and _LEARNED_BASE_RANGE[0] <= base <= _LEARNED_BASE_RANGE[1]):
            clean["base_daily"] = float(base)
    _learned_import = clean


def _current_adder() -> float:
    return _learned_import.get("pcia_adder", _PCIA_NET_ADDER)


def _gen_rate(season: str, period: TouPeriod) -> float:
    if _learned_import.get("season") == season and period in _learned_import.get("gen", {}):
        return _learned_import["gen"][period]
    return _GEN_DEFAULT[season][period]


class _LiveRates:
    """`_RATES[season][period]` — all-in EV-TOU-5 $/kWh, always computed from
    the current (possibly bill-learned) generation, delivery and adder, so
    anything reading this table (the chatbot's prompt) can't go stale."""

    def __getitem__(self, season: str) -> dict:
        return {p: _gen_rate(season, p) + _delivery(p) + _current_adder() for p in TouPeriod}


_RATES = _LiveRates()


_ON_PEAK_START = 16  # 4 pm
_ON_PEAK_END   = 21  # 9 pm

# SDG&E EV-TOU-5 fixed charge per day, regardless of usage. The tariff's
# Base Services Charge is $0.79343/day (6/1/2026 table), but the Aug 19 -
# Sep 17 2026 bill's "Non-Nettable Charges" were $24.36 over 30 days =
# $0.812/day exactly, ~$0.56/cycle more than the tariff figure alone
# explains (cause not identified from one bill). Using the bill's observed
# figure so cycle estimates match it; re-verify against another bill.
BASE_SERVICE_DAILY = 0.812

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


# Effective export $/kWh learned from the user's latest real bill (set by
# alerts._load_peak_state from state["learned_export_rate"], recorded via
# `franklinwh bill-record --export-credit ... --export-kwh ...`). The flat
# default above matched the Aug 2026 bill ($0.121) but the Sep bill averaged
# ~$0.51/kWh — legacy 2024 export pricing varies by hour and season, so one
# hardcoded rate can't be right every cycle. Using the latest bill's
# effective rate tracks it with a one-cycle lag.
_learned_export_rate: float | None = None
_LEARNED_EXPORT_RATE_MAX = 2.0  # $/kWh — above this is a typo, not a rate


def set_learned_export_rate(rate) -> None:
    """Install (or, with None/invalid input, clear) the learned rate."""
    global _learned_export_rate
    if (isinstance(rate, (int, float)) and not isinstance(rate, bool)
            and 0.0 < rate <= _LEARNED_EXPORT_RATE_MAX):
        _learned_export_rate = float(rate)
    else:
        _learned_export_rate = None


def _current_export_rate() -> float:
    return _learned_export_rate if _learned_export_rate is not None else _NEM3_DEFAULT_EXPORT_RATE


def export_rate_at(dt: datetime) -> float:
    """Return the export credit rate ($/kWh) for grid export at dt.

    Flat — the latest bill's learned effective rate if one was recorded,
    else _NEM3_DEFAULT_EXPORT_RATE. `dt` is kept in the signature (unused)
    so every call site that reasonably expects a time-varying rate doesn't
    need touching if real hourly data ever replaces this.
    """
    return _current_export_rate()


def peak_export_hour(month: int) -> tuple[int, float]:
    """Highest-value export (hour, $/kWh). Flat rate now (see
    export_rate_at) so "peak" is nominal — returns a representative
    evening hour at the one rate, kept as a (hour, rate) pair since
    callers display both.
    """
    return 18, _current_export_rate()


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
    return max(0.0, days) * _learned_import.get("base_daily", BASE_SERVICE_DAILY)


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
    """Return $/kWh for grid import at dt: SDCP generation + SDG&E delivery
    + the PCIA adder (bill-learned values win over the built-in tables)."""
    season = "summer" if dt.month in _SUMMER_MONTHS else "winter"
    period = period_at(dt)
    return _gen_rate(season, period) + _delivery(period) + _current_adder()


# ── Schedule DR-SES (residential-with-solar alternative to EV-TOU-5) ──────
# Used only for the rate-plan-comparison alert (savings.compare_rate_plans) —
# never for billing math above, which is all EV-TOU-5.
#
# Reconstructed the same way as EV-TOU-5's _RATES above (2026-08-31), for
# the same reason: the previous version of this table was SDG&E's bundled
# DR-SES rate, but the customer is on SDCP (a CCA), same as for EV-TOU-5 —
# comparing a bundled DR-SES number against an unbundled EV-TOU-5 number
# would have silently made this comparison meaningless the moment EV-TOU-5
# switched to real unbundled numbers.
#
# Delivery (SDG&E, "UDC Total" + "WF-NBC + DWR-BC Rate" of Schedule DR-SES,
# effective 6/1/2026, sdge.com/sites/default/files/regulatory/6-1-26%20
# Schedule%20DR-SES%20Total%20Rates%20Table.pdf): flat across ALL periods
# and both seasons, unlike EV-TOU-5 — DR-SES has no time-varying delivery
# component at all, only Distribution differs from EV-TOU-5's.
#   every period, every season: 0.26328 (UDC) + 0.00591 (WF-NBC/DWR-BC) = 0.26919
#
# Generation (SDCP DR-SES, PowerBase column, SDCP's published 1/1/2026
# table — SDCP's small mid-year update isn't itemized for DR-SES on any
# bill, so this is not refreshed the way EV-TOU-5's summer column is):
#   summer: on-peak 0.38856, off-peak 0.12411, super-off-peak 0.04254
#   winter: on-peak 0.14795, off-peak 0.09763, super-off-peak 0.03597
#
# Same PCIA adder as EV-TOU-5 (_PCIA_NET_ADDER) so the two plans stay
# comparable — see that comment.
_DRSES_GEN = {
    "summer": {
        TouPeriod.SUPER_OFF_PEAK: 0.04254,
        TouPeriod.OFF_PEAK:       0.12411,
        TouPeriod.ON_PEAK:        0.38856,
    },
    "winter": {
        TouPeriod.SUPER_OFF_PEAK: 0.03597,
        TouPeriod.OFF_PEAK:       0.09763,
        TouPeriod.ON_PEAK:        0.14795,
    },
}
_DRSES_DELIVERY = 0.26328 + 0.00591
# Base Services Charge is $0.79343/day on DR-SES too (same tariff line item
# as EV-TOU-5's) — identical in the counterfactual, so
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
    return _DRSES_GEN[season][_drses_period_at(dt)] + _DRSES_DELIVERY + _current_adder()


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


# Real cycle boundaries learned from the user's bills (installed from
# state["bill_cycles"] / ["next_read_date"] by alerts._load_peak_state). SDG&E
# bills on the meter read date, which drifts day to day (cycles run 29-33
# days, starts landed on the 18th-21st), so a fixed day-of-month is only an
# approximation; recorded bills give the true boundaries.
_learned_cycles: list[tuple[date, date]] = []
_learned_next_read: date | None = None
_DEFAULT_CYCLE_DAYS = 30
_MAX_LEARNED_CYCLES = 24


def _as_date(v) -> date | None:
    try:
        return date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def set_learned_cycles(cycles, next_read=None) -> None:
    """Install (or clear, with None/invalid) real billing cycles: a list of
    {"start": iso, "end": iso} plus the bill's next scheduled read date.
    Malformed or inverted entries are dropped individually."""
    global _learned_cycles, _learned_next_read
    good: list[tuple[date, date]] = []
    for c in (cycles if isinstance(cycles, list) else []):
        if not isinstance(c, dict):
            continue
        st, en = _as_date(c.get("start")), _as_date(c.get("end"))
        if st and en and st <= en:
            good.append((st, en))
    _learned_cycles = sorted(set(good))[-_MAX_LEARNED_CYCLES:]
    nr = _as_date(next_read)
    _learned_next_read = nr if (nr and _learned_cycles and nr > _learned_cycles[-1][1]) else None


def _learned_cycle_bounds(d: date, start_day: int) -> tuple[date, date] | None:
    if not _learned_cycles:
        return None
    for st, en in _learned_cycles:
        if st <= d <= en:
            return st, en
    last_end = _learned_cycles[-1][1]
    if d > last_end:
        lengths = sorted((en - st).days + 1 for st, en in _learned_cycles)
        typical = lengths[len(lengths) // 2] or _DEFAULT_CYCLE_DAYS
        start = last_end + timedelta(days=1)
        # First projected cycle ends on the bill's own next read date, if given;
        # after that, chain at the typical length until it contains d.
        end = _learned_next_read or start + timedelta(days=typical - 1)
        for _ in range(240):
            if d <= end:
                return start, end
            start = end + timedelta(days=1)
            end = start + timedelta(days=typical - 1)
        return None
    # Earlier than any known cycle: fixed-day fallback, clamped so it can't
    # overlap the earliest real one.
    st, en = _fixed_day_cycle_bounds(d, start_day)
    earliest = _learned_cycles[0][0]
    return (st, min(en, earliest - timedelta(days=1))) if st < earliest else None


def cycle_bounds(d: date, start_day: int = DEFAULT_CYCLE_START_DAY) -> tuple[date, date]:
    """Return (first_day, last_day) of the billing cycle containing `d` —
    from real recorded bills when known, else the fixed `start_day`."""
    learned = _learned_cycle_bounds(d, start_day)
    return learned if learned else _fixed_day_cycle_bounds(d, start_day)


def _fixed_day_cycle_bounds(d: date, start_day: int) -> tuple[date, date]:
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
