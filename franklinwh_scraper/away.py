"""Noticing that nobody is home: the house load sits at fridge-and-standby level for days.

While you are away the advisor's nags (the EB timer, the "tonight may run short" alert, "was that
the car?") are noise, so alerts.py holds them until the load picks up again. Detection is on whole
past days only: two days in a row at under half your usual daily energy with no hour above 1 kW.
"""

import statistics
from datetime import date, timedelta

USUAL_DAYS = 35                 # how far back "your usual day" looks
MIN_USUAL_DAYS = 10             # whole days needed to know what usual is
QUIET_DAYS = 2                  # in a row, right up to yesterday
QUIET_SHARE = 0.5               # of the usual daily kWh
QUIET_PEAK_KW = 1.0             # no hour of the day above this
MIN_HOURS = 20                  # an outage leaves holes: a day with fewer hours is not evidence
BACK_KW = 1.5                   # an hour at this level today means someone is home


def _day_kwh(hours: dict[int, float]) -> float | None:
    if len(hours) < MIN_HOURS:
        return None
    return statistics.mean(hours.values()) * 24.0


def detect(store, today: date) -> dict | None:
    """{"days": QUIET_DAYS, "kwh": mean quiet-day kWh, "usual": your median day} when the last
    QUIET_DAYS whole days were all quiet, else None (also with no store or too little history)."""
    if store is None:
        return None
    try:
        raw = store.daily_hourly_load_means((today - timedelta(days=USUAL_DAYS + QUIET_DAYS)).isoformat(),
                                            today.isoformat())
    except Exception:
        return None
    quiet_days = [today - timedelta(days=i) for i in range(1, QUIET_DAYS + 1)]
    quiet_kwh = []
    for d in quiet_days:
        hours = raw.get(d.isoformat())
        kwh = _day_kwh(hours) if hours else None
        if kwh is None or max(hours.values()) > QUIET_PEAK_KW:
            return None
        quiet_kwh.append(kwh)
    quiet_set = {d.isoformat() for d in quiet_days}
    usual = [k for day, hours in raw.items() if day not in quiet_set and (k := _day_kwh(hours)) is not None]
    if len(usual) < MIN_USUAL_DAYS:
        return None
    median = statistics.median(usual)
    mean_quiet = statistics.mean(quiet_kwh)
    if mean_quiet >= QUIET_SHARE * median:
        return None
    return {"days": QUIET_DAYS, "kwh": mean_quiet, "usual": median}


def is_back(store, today: date) -> bool:
    """True once some hour today has run at BACK_KW or more: someone is home again."""
    if store is None:
        return False
    try:
        hours = store.daily_hourly_load_means(today.isoformat(), (today + timedelta(days=1)).isoformat(),
                                              ).get(today.isoformat(), {})
    except Exception:
        return False
    return any(kw >= BACK_KW for kw in hours.values())
