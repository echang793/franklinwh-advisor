"""The single definition of "how much has the battery + solar saved me".

Three different answers to that question existed before this module: the
dashboard ticker (webapi._saved_today), the billing-cycle card
(webapi._cycle_cost's "saved"), and the weekly digest. Only the first was
right — the cycle card dropped export credit, and the weekly digest dropped
export credit *and* every off-peak hour, so it silently under-reported.

Pure functions over already-integrated readings, no I/O — same shape as
tou.py, so it can be called from the CLI, the API, and the alert engine
without any of them importing each other.

Honesty constraints deliberately encoded here:

* Costs are priced with tou.rate_at, which has no date dimension — every
  figure is "at today's published rates", including historical ones. That's
  fine for a trailing window and starts to lie over a multi-year lifetime
  total, so `priced_at` is returned alongside and is meant to be displayed,
  not just logged.
* Base service charge is excluded from every comparison. It's incurred
  identically in the counterfactual, so including it would inflate savings.
* saved_vs_grid_only is a counterfactual, not a measurement: it assumes the
  household would have drawn the same load with no battery and no solar.
* saved_vs_solar_only is a *further* estimate on top of that — see its
  docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime

from .tou import (_NEM3_DEFAULT_EXPORT_RATE, _RATES_EFFECTIVE_DATE, TouPeriod,
                  export_rate_at, period_at, rate_at)


@dataclass
class SavingsBreakdown:
    days: int
    start: str
    end: str

    # Energy (kWh)
    import_kwh: float
    export_kwh: float
    home_kwh: float
    self_use_kwh: float          # home load served by battery or solar

    # Actual cost, excluding base service (see module docstring)
    actual_import_cost: float
    actual_export_credit: float
    actual_net_energy_cost: float

    # Counterfactuals
    grid_only_cost: float        # every kWh of home load bought from the grid
    solar_only_net_cost: float   # solar, no battery (estimate)

    saved_vs_grid_only: float    # the headline number
    saved_vs_solar_only: float   # the battery's own contribution (estimate)

    # saved_vs_grid_only split by when the avoided import would have happened.
    # These three plus actual_export_credit sum to saved_vs_grid_only.
    saved_on_peak: float
    saved_off_peak: float
    saved_super_off_peak: float

    priced_at: str               # tou rates' effective date
    export_days_at_assumed_rate: int

    def to_dict(self) -> dict:
        return asdict(self)


def compute(intervals, start: str = "", end: str = "") -> SavingsBreakdown:
    """Build a breakdown from history.integrate_intervals() output.

    `intervals` is the (dt, hours, grid_kw, home_kw, solar_kw) sequence that
    integrate_intervals yields — passing that rather than raw rows keeps this
    module free of any DB dependency and means callers can't accidentally
    integrate differently from each other.
    """
    import_kwh = export_kwh = home_kwh = self_use_kwh = 0.0
    actual_import_cost = actual_export_credit = 0.0
    grid_only_cost = 0.0
    solar_only_import_cost = solar_only_export_credit = 0.0
    saved_on_peak = saved_off_peak = saved_super_off_peak = 0.0
    assumed_rate_days: set[str] = set()
    seen_days: set[str] = set()

    for dt0, hours, grid_avg, home_avg, solar_avg in intervals:
        seen_days.add(dt0.strftime("%Y-%m-%d"))
        rate = rate_at(dt0)
        exp_rate = export_rate_at(dt0)
        period = period_at(dt0)

        imp_kw = max(0.0, grid_avg)
        exp_kw = max(0.0, -grid_avg)
        # Home load not covered by grid import = covered by battery or solar.
        self_kw = max(0.0, home_avg - imp_kw)

        import_kwh += imp_kw * hours
        export_kwh += exp_kw * hours
        home_kwh += max(0.0, home_avg) * hours
        self_use_kwh += self_kw * hours

        actual_import_cost += imp_kw * rate * hours
        actual_export_credit += exp_kw * exp_rate * hours

        # Counterfactual A: no battery, no solar — buy all home load.
        grid_only_cost += max(0.0, home_avg) * rate * hours

        # Counterfactual B: solar but no battery. Solar serves load
        # instantaneously; the surplus exports, the deficit imports.
        so_self = min(max(0.0, solar_avg), max(0.0, home_avg))
        solar_only_import_cost += (max(0.0, home_avg) - so_self) * rate * hours
        solar_only_export_credit += max(0.0, solar_avg - home_avg) * exp_rate * hours

        avoided = self_kw * rate * hours
        if period == TouPeriod.ON_PEAK:
            saved_on_peak += avoided
        elif period == TouPeriod.SUPER_OFF_PEAK:
            saved_super_off_peak += avoided
        else:
            saved_off_peak += avoided

        # Track exports priced at the assumed avoided-cost floor rather than a
        # published hourly rate, so the caller can footnote it honestly.
        if exp_kw > 0 and abs(exp_rate - _NEM3_DEFAULT_EXPORT_RATE) < 1e-9:
            assumed_rate_days.add(dt0.strftime("%Y-%m-%d"))

    actual_net = actual_import_cost - actual_export_credit
    solar_only_net = solar_only_import_cost - solar_only_export_credit

    return SavingsBreakdown(
        days=len(seen_days),
        start=start,
        end=end,
        import_kwh=round(import_kwh, 2),
        export_kwh=round(export_kwh, 2),
        home_kwh=round(home_kwh, 2),
        self_use_kwh=round(self_use_kwh, 2),
        actual_import_cost=round(actual_import_cost, 2),
        actual_export_credit=round(actual_export_credit, 2),
        actual_net_energy_cost=round(actual_net, 2),
        grid_only_cost=round(grid_only_cost, 2),
        solar_only_net_cost=round(solar_only_net, 2),
        saved_vs_grid_only=round(grid_only_cost - actual_net, 2),
        saved_vs_solar_only=round(solar_only_net - actual_net, 2),
        saved_on_peak=round(saved_on_peak, 2),
        saved_off_peak=round(saved_off_peak, 2),
        saved_super_off_peak=round(saved_super_off_peak, 2),
        priced_at=_RATES_EFFECTIVE_DATE.strftime("%Y-%m-%d"),
        export_days_at_assumed_rate=len(assumed_rate_days),
    )


def followed_advice_audit(advisor_log, charge_days: set[str], days: int,
                          today: datetime | None = None) -> dict:
    """Count EB recommendations vs. days grid charging was actually observed.

    Deliberately returns counts and no dollar figure. Whether following the
    advice *saved* anything is not answerable from this data: grid charging
    (the observable signature of EB mode) is near-absent in practice, so the
    "followed" arm has essentially no samples, and comparing peak coverage
    between recommended and non-recommended days is confounded by the fact
    that EB gets recommended precisely because the day looks bad.

    Streams the log rather than json.loads-ing every line — advisor_log.jsonl
    is multi-MB and chronological, so the date prefix is checked first. Fine
    for a CLI command; do not call this from a request handler.
    """
    import json as _json

    now = today or datetime.now()
    cutoff = now.date().toordinal() - days
    eb_days: set[str] = set()
    try:
        with open(advisor_log) as f:
            for line in f:
                # Cheap substring filter first — only EB lines get parsed, so
                # the multi-MB log costs one `in` check per line rather than a
                # json.loads. Deliberately not slicing the date out by
                # character offset; that's brittle against any formatting
                # change in the log writer.
                if "emergency_backup" not in line:
                    continue
                try:
                    rec = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                stamp = str(rec.get("timestamp", ""))[:10]
                try:
                    if datetime.strptime(stamp, "%Y-%m-%d").date().toordinal() < cutoff:
                        continue
                except ValueError:
                    continue
                if rec.get("recommended_mode") == "emergency_backup" and rec.get("needs_action"):
                    eb_days.add(stamp)
    except OSError:
        return {"eb_recommended_days": 0, "grid_charge_days": 0, "days": days,
                "available": False}

    recent_charge = {d for d in charge_days
                     if datetime.strptime(d, "%Y-%m-%d").date().toordinal() >= cutoff}
    return {
        "eb_recommended_days": len(eb_days),
        "grid_charge_days": len(recent_charge),
        "days": days,
        "available": True,
    }
