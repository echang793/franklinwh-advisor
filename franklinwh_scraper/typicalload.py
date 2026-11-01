"""What a typical house load looks like on a given day of the week, learned from history.

Two ways to say "typical" disagree in ways that matter to a battery plan. The median of all
days ignores a dryer or an oven (lumps whose hour moves around), so on a day that always has
them it runs low (Sundays: about 2 kWh low from 2 PM to midnight). The mean of just that weekday
counts the lumps but, on days with the odd big spike, runs high (Saturdays and weekdays:
2-2.5 kWh high). Neither wins everywhere, so this checks which one has actually been right for
THIS weekday, by predicting each past same-weekday day from the others, and uses that one.
"""

import statistics
from dataclasses import dataclass
from datetime import date, timedelta

EVENING = range(14, 24)          # the part of the day the battery plan cares about
MIN_SAME = 4                     # same-weekday days needed before trusting the weekday's own mean
HISTORY_DAYS = 70
MIN_HOURS = 18                   # a day with fewer hours of readings is an outage, not a quiet day


@dataclass
class TypicalLoad:
    by_hour: dict[int, float]        # kW for each clock hour 0-23
    source: str                      # "same-weekday mean" | "quiet-day median"
    n_same: int                      # same-weekday days it learned from
    evening_kwh: float               # 2 PM to midnight under by_hour
    weekday_name: str
    low_kwh: float                   # the lightest and heaviest same-weekday evenings seen
    high_kwh: float


def typical_hourly_load(store, today: date, labels: dict | None = None,
                        days: int = HISTORY_DAYS) -> TypicalLoad | None:
    """None without enough history for this weekday (or no store). `labels` are the owner's
    "was that the car?" answers: car days are left out, since the car is planned separately."""
    if store is None:
        return None
    try:
        raw = store.daily_hourly_load_means((today - timedelta(days=days)).isoformat(), today.isoformat())
    except Exception:
        return None
    labels = labels or {}
    usable: dict[date, dict[int, float]] = {}
    for d, hours in raw.items():
        if len(hours) >= MIN_HOURS and not labels.get(d):
            usable[date.fromisoformat(d)] = hours
    same = sorted(d for d in usable if d.weekday() == today.weekday())
    if len(same) < MIN_SAME:
        return None

    def quiet_before(cutoff: date | None) -> dict[int, float]:
        pool = [usable[d] for d in usable if cutoff is None or d < cutoff] or list(usable.values())
        return {h: statistics.median(day.get(h, 0.4) for day in pool) for h in range(24)}

    quiet = quiet_before(None)

    def kwh(day_hours: dict[int, float], base: dict[int, float]) -> float:
        return sum(day_hours.get(h, base[h]) for h in EVENING)

    # Which estimate has been right for this weekday? Walk forward: predict each same-weekday day
    # from the ones before it, as it will be used. (Leaving one out of all of them would favour the
    # mean by construction and hide that old days no longer look like recent ones.)
    err_mean: list[float] = []
    err_quiet: list[float] = []
    for i in range(MIN_SAME - 1, len(same)):
        d, earlier = same[i], same[:i]
        base = quiet_before(d)
        mean_pred = sum(statistics.mean(usable[x].get(h, base[h]) for x in earlier) for h in EVENING)
        actual = kwh(usable[d], base)
        err_mean.append(mean_pred - actual)
        err_quiet.append(sum(base[h] for h in EVENING) - actual)
    use_mean = len(err_mean) >= 2 and abs(statistics.mean(err_mean)) < abs(statistics.mean(err_quiet))
    if use_mean:
        by_hour = {h: statistics.mean(usable[d].get(h, quiet[h]) for d in same) for h in range(24)}
    else:
        by_hour = quiet
    totals = [kwh(usable[d], quiet) for d in same]
    return TypicalLoad(by_hour=by_hour, source="same-weekday mean" if use_mean else "quiet-day median",
                       n_same=len(same), evening_kwh=sum(by_hour[h] for h in EVENING),
                       weekday_name=today.strftime("%A"), low_kwh=min(totals), high_kwh=max(totals))
