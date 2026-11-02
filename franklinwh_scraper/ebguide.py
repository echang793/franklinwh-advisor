"""When Emergency Backup is worth planning for, and the rules that time its start and stop.

EB only matters on a cloudy day whose sun is well below a good day's: on a clear day the
battery refills itself. The gate here is that test. The rest is small and pure so it can be
tested without a clock or a battery: the margin learned from how past plans turned out,
the start/stop timing, and the "this is a heavy day" check.
"""

import logging
import statistics
from datetime import date, timedelta

logger = logging.getLogger(__name__)

SIGNIFICANT_RATIO = 0.65     # forecast sun at or under this share of a good day counts as a big drop
BASELINE_DAYS = 30
MIN_BASELINE_DAYS = 7
BASE_MARGIN_PCT = 5.0
MARGIN_MIN_PLANS = 6         # followed plans needed before the margin moves
START_LEAD_H = 5 / 60        # say "start" this long before the start-by time
HEAVY_RATIO = 1.5
HEAVY_MIN_KW = 0.9


def good_day_baseline_kwh(store, today: date) -> float | None:
    """What a good solar day makes at this time of year: the median of the better half of the
    last 30 fully-recorded days (so cloudy stretches do not drag the yardstick down).
    None with fewer than a week of days or no store."""
    if store is None:
        return None
    vals: list[float] = []
    try:
        for i in range(1, BASELINE_DAYS + 1):
            d = (today - timedelta(days=i)).isoformat()
            if store.day_has_solar_coverage(d):
                kwh = store.daily_solar_kwh(d)
                if kwh > 1.0:
                    vals.append(float(kwh))
    except Exception:
        logger.debug("Good-day baseline failed", exc_info=True)
        return None
    if len(vals) < MIN_BASELINE_DAYS:
        return None
    vals.sort(reverse=True)
    return float(statistics.median(vals[: max(1, len(vals) // 2)]))


TUNE_MIN_DAYS = 10           # cloudy, un-mitigated days before the threshold moves
TUNE_MIN_NEEDED = 3          # of which the grid really had to supply the window
TUNE_NEED_GRID_KWH = 1.0
TUNE_BOUNDS = (0.5, 0.85)


def tuned_ratio(plan_log: list[dict], base: float = SIGNIFICANT_RATIO) -> float:
    """The "big drop in sun" threshold, learned from how past cloudy days went. Looks at cloudy
    days the plan log has a gate ratio for and where no EB ran (so the grid import shows what
    happened unaided). If on enough of them the grid still supplied the 2-11 PM window, the
    threshold moves up to cover the milder of those days (upper quartile of their ratios, plus
    0.02), clamped to 0.5-0.85. Otherwise the default."""
    days = [e for e in plan_log
            if e.get("gate_cloudy") and e.get("gate_ratio") is not None and (e.get("ran_eb_kwh") or 0) < 0.5]
    needed = sorted(float(e["gate_ratio"]) for e in days if (e.get("grid_kwh") or 0) > TUNE_NEED_GRID_KWH)
    if len(days) < TUNE_MIN_DAYS or len(needed) < TUNE_MIN_NEEDED:
        return base
    q = needed[round(0.75 * (len(needed) - 1))]
    return min(TUNE_BOUNDS[1], max(TUNE_BOUNDS[0], round(q + 0.02, 2)))


def eb_day_gate(outlook, store, today: date, ghi_threshold: float, *, peak_kw: float | None,
                perf_ratio: float = 1.0, hourly_bias=None, ratio_threshold: float = SIGNIFICANT_RATIO) -> dict:
    """Is today a cloudy day with a significant drop in generation?

    Returns {"known", "needed", "cloudy", "forecast_kwh", "baseline_kwh", "ratio"}. `needed`
    needs the sky to be cloudy AND, when a good-day baseline exists, the forecast sun at or
    under SIGNIFICANT_RATIO of it. Without a baseline the sky alone decides. `known` is False
    when there is no forecast to judge by, in which case callers should not suppress anything.
    Never raises."""
    out = {"known": False, "needed": False, "cloudy": None, "forecast_kwh": None,
           "baseline_kwh": None, "ratio": None, "threshold": ratio_threshold}
    if outlook is None:
        return out
    try:
        cloudy = bool(outlook.is_cloudy(12, ghi_threshold))
        forecast = None
        if peak_kw is not None:
            forecast = float(outlook.today_generation_kwh(peak_kw, perf_ratio, hourly_bias))
        baseline = good_day_baseline_kwh(store, today)
        ratio = forecast / baseline if (forecast is not None and baseline) else None
    except Exception:
        logger.debug("EB gate failed", exc_info=True)
        return out
    out.update(known=True, cloudy=cloudy, forecast_kwh=forecast, baseline_kwh=baseline, ratio=ratio,
               needed=cloudy and (ratio is None or ratio <= ratio_threshold))
    return out


def plan_margin(plan_log: list[dict], base: float = BASE_MARGIN_PCT) -> float:
    """The cushion (battery points) to plan with, learned from how past plans went: of the days
    the EB plan was followed (ran at least 80% of what it asked, and it asked for something),
    how often the grid still had to supply the window. Needs six such days."""
    followed = [e for e in plan_log
                if (e.get("planned_eb_kwh") or 0) >= 0.5 and (e.get("ran_eb_kwh") or 0) >= 0.8 * e["planned_eb_kwh"]]
    if len(followed) < MARGIN_MIN_PLANS:
        return base
    short = sum(1 for e in followed if (e.get("grid_kwh") or 0) > 0.5) / len(followed)
    if short >= 0.7:
        return base + 5.0
    if short >= 0.4:
        return base + 3.0
    return base


def timer_decision(*, running: bool, now_dec: float, start_by: float, eb_kwh: float,
                   started: bool = False, stopped: bool = False, deadline: float = 14.0) -> str | None:
    """"start" when it is time to switch to EB, "stop" when to switch back, else None.

    `running` is whether the battery is charging from the grid right now. `eb_kwh` is what the
    plan still asks for (0 once the target is reached). Start is said once, from five minutes
    before start-by until the deadline; stop is said once, when the target is reached or the
    deadline (2 PM, when super-off-peak ends) arrives."""
    if running:
        if not stopped and (eb_kwh < 0.1 or now_dec >= deadline):
            return "stop"
        return None
    if started or eb_kwh < 0.1 or now_dec >= deadline:
        return None
    return "start" if now_dec >= start_by - START_LEAD_H else None


def is_heavy(recent_kw: float | None, quiet_kw: float | None) -> bool:
    """Is the house running well above its usual load for these hours (dryer, oven, guests)?"""
    if recent_kw is None or not quiet_kw:
        return False
    return recent_kw >= HEAVY_MIN_KW and recent_kw >= HEAVY_RATIO * quiet_kw
