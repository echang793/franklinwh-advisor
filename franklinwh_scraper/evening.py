"""The 4 PM to midnight load by weekday, and the battery level it asks for at sundown.

After sundown (about 4:45 PM in November) the sun is gone, so whatever the house runs until
midnight comes from the battery. This learns, per weekday, how much that is on a typical
evening and on a heavy one (the level four evenings in five stay under), so "what should the
battery be at by sunset?" has a number behind it.
"""

import statistics
from dataclasses import dataclass
from datetime import date, timedelta

HOURS = range(16, 24)
SEGMENTS = ((16, 19), (19, 22), (22, 24))
HISTORY_DAYS = 84
MIN_DAYS = 4                    # whole evenings needed before a weekday gets numbers
HEAVY_PERCENTILE = 0.8
HEAVY_NIGHT_KWH = 7.0           # an evening this big has the dryer, oven or car in it


@dataclass
class Evening:
    weekday: int                         # Monday = 0
    name: str
    n: int                               # evenings it learned from
    median_kwh: float
    heavy_kwh: float                     # four evenings in five use no more than this
    low_kwh: float
    max_kwh: float
    heavy_nights: int                    # evenings over HEAVY_NIGHT_KWH
    segments: tuple[float, float, float]  # mean kWh in 4-7, 7-10 and 10-12


def evening_profiles(store, today: date, days: int = HISTORY_DAYS) -> dict[int, Evening]:
    """Per weekday (Monday = 0) the evenings of the last `days` days, today excluded. A weekday
    with fewer than MIN_DAYS whole evenings (an outage leaves a partial one) is left out. {} with
    no store or on any error: this only ever adds detail, it never blocks."""
    if store is None:
        return {}
    try:
        raw = store.daily_hourly_load_means((today - timedelta(days=days)).isoformat(), today.isoformat())
    except Exception:
        return {}
    by_weekday: dict[int, list[dict[int, float]]] = {}
    for d, hours in raw.items():
        try:
            day = date.fromisoformat(d)
        except ValueError:
            continue
        if day >= today or not all(h in hours for h in HOURS):
            continue
        by_weekday.setdefault(day.weekday(), []).append(hours)
    out: dict[int, Evening] = {}
    for wd, nights in by_weekday.items():
        if len(nights) < MIN_DAYS:
            continue
        totals = sorted(sum(n[h] for h in HOURS) for n in nights)
        segs = tuple(statistics.mean(sum(n[h] for h in range(a, b)) for n in nights) for a, b in SEGMENTS)
        out[wd] = Evening(weekday=wd, name=date(2000, 1, 3 + wd).strftime("%A"), n=len(nights),
                          median_kwh=float(statistics.median(totals)),
                          heavy_kwh=float(totals[int(HEAVY_PERCENTILE * (len(totals) - 1))]),
                          low_kwh=float(totals[0]), max_kwh=float(totals[-1]),
                          heavy_nights=sum(1 for t in totals if t > HEAVY_NIGHT_KWH),
                          segments=segs)  # type: ignore[arg-type]
    return out


def target_pct(kwh: float, capacity_kwh: float, floor_pct: float, margin_pct: float) -> float:
    """Battery level to have at sundown to carry `kwh` to midnight without the grid: the load as a
    share of the battery, plus the reserve floor and the margin. Never above 100."""
    return min(100.0, floor_pct + margin_pct + kwh / capacity_kwh * 100.0)


def verdict(projected_pct: float, *, typical_pct: float, heavy_pct: float) -> tuple[str, float]:
    """("heavy" | "typical" | "short", points of room): which evenings the projected sundown level
    covers, and by how many points of the highest level it clears (negative when short of typical)."""
    if projected_pct >= heavy_pct:
        return "heavy", round(projected_pct - heavy_pct, 1)
    if projected_pct >= typical_pct:
        return "typical", round(projected_pct - typical_pct, 1)
    return "short", round(projected_pct - typical_pct, 1)
