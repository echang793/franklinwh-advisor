"""Learn the afternoon car-charging habit from history.

Some weekdays the car gets a solar-following charge (a steady ~3 kW for a few
afternoon hours), other days it does not. A per-hour median forecast blurs that
into "a bit more than quiet" every day, which is never what happens. This
separates the two: the quiet-day load per hour (days without a session), the
size and length of a typical session, and how often each weekday has one.
Pure reads from HistoryStore; nothing here changes what is polled or stored.
"""

import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# A car session is made of STEADY blocks: readings held at or above
# SESSION_MIN_KW, unbroken, for at least BLOCK_MIN_MINUTES, with little
# variation inside the block. Shape is what separates a car from AC: a charger
# holds a flat draw for tens of minutes to hours (and may stop and restart);
# an AC compressor cycles (on 5-10 min, off 5-10 min) so it never forms a long
# block even when its hourly average looks the same. A day counts as a car day
# when its blocks add up to SESSION_MIN_MINUTES. Baseline is ~0.4 kW; a 120 V
# trickle charge is ~1.4 kW, a solar-following 240 V charge 3-6 kW.
SESSION_MIN_KW = 1.2
BLOCK_MIN_MINUTES = 30
SESSION_MIN_MINUTES = 45
BLOCK_MAX_CV = 0.35                      # std/mean inside a block
AFTERNOON_START_HOUR, AFTERNOON_END_HOUR = 11, 17     # solar-hours charging, not dinner
AFTERNOON_HOURS = range(AFTERNOON_START_HOUR, AFTERNOON_END_HOUR)
_MAX_GAP = timedelta(minutes=10)         # a longer hole in the readings ends a run
_READING_HOURS = 5 / 60.0                # nominal reading spacing for energy sums
_MIN_DAYS = 10                           # usable days before we claim a pattern
_MIN_QUIET_DAYS = 5
_LOOKBACK_DAYS = 45
_SHRINK_WEIGHT = 2.0                     # weekday odds count as 2 days of the overall rate
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass
class AfternoonEvProfile:
    quiet_by_hour: dict[int, float]      # median hourly load on days without a session (kW)
    session_kw: float                    # typical load ABOVE the quiet baseline during a session
    session_hours: float
    session_kwh: float
    p_today: float                       # odds of a session today (weekday-specific, shrunk)
    k_weekday: int                       # sessions on this weekday in the lookback
    n_weekday: int                       # usable days of this weekday in the lookback
    weekday_name: str
    n_days: int
    n_sessions: int


def find_afternoon_session(readings: list[tuple[str, float]]) -> dict | None:
    """Steady car-charge blocks in a day's (iso timestamp, kW) readings between
    11:00 and 17:00, or None. Returns {"start_hour", "minutes" (all blocks),
    "blocks", "avg_kw", "readings" (those in blocks)}."""
    blocks: list[list[tuple[datetime, float]]] = []
    run: list[tuple[datetime, float]] = []

    def flush():
        if len(run) >= 2:
            minutes = (run[-1][0] - run[0][0]).total_seconds() / 60.0 + _READING_HOURS * 60
            kws = [kw for _t, kw in run]
            mean = statistics.mean(kws)
            if minutes >= BLOCK_MIN_MINUTES and statistics.pstdev(kws) / mean <= BLOCK_MAX_CV:
                blocks.append(list(run))
        run.clear()

    for ts, kw in readings:
        t = datetime.fromisoformat(ts)
        if not AFTERNOON_START_HOUR <= t.hour < AFTERNOON_END_HOUR or kw < SESSION_MIN_KW:
            flush()
            continue
        if run and t - run[-1][0] > _MAX_GAP:
            flush()
        run.append((t, kw))
    flush()

    minutes = sum(round((b[-1][0] - b[0][0]).total_seconds() / 60.0 + _READING_HOURS * 60) for b in blocks)
    if minutes < SESSION_MIN_MINUTES:
        return None
    flat = [r for b in blocks for r in b]
    return {"start_hour": blocks[0][0][0].hour, "minutes": minutes, "blocks": len(blocks),
            "avg_kw": statistics.mean(kw for _t, kw in flat), "readings": flat}


def build_profile(store, today: date, lookback_days: int = _LOOKBACK_DAYS) -> AfternoonEvProfile | None:
    """Profile from the trailing `lookback_days` (not including `today`), or None
    when there is too little clean history to call it a pattern."""
    start, end = (today - timedelta(days=lookback_days)).isoformat(), today.isoformat()
    hour_means = store.daily_hourly_load_means(start, end)
    usable = {d: hm for d, hm in hour_means.items() if sum(h in hm for h in AFTERNOON_HOURS) >= 5}
    if len(usable) < _MIN_DAYS:
        return None
    readings = store.daily_afternoon_readings(start, end, AFTERNOON_START_HOUR, AFTERNOON_END_HOUR)
    sessions = {d: find_afternoon_session(readings.get(d, [])) for d in usable}

    quiet_days = [hm for d, hm in usable.items() if sessions[d] is None]
    if len(quiet_days) < _MIN_QUIET_DAYS:
        return None
    quiet_by_hour = {
        h: statistics.median(hm[h] for hm in quiet_days if h in hm)
        for h in range(24) if any(h in hm for hm in quiet_days)
    }

    car_days = [d for d, s in sessions.items() if s is not None]
    if car_days:
        kwh = [sum(max(0.0, kw - quiet_by_hour.get(t.hour, 0.4)) * _READING_HOURS
                   for t, kw in sessions[d]["readings"]) for d in car_days]
        session_hours = statistics.median(sessions[d]["minutes"] for d in car_days) / 60.0
        session_kwh = statistics.median(kwh)
    else:
        session_hours, session_kwh = 0.0, 0.0

    wd = today.weekday()
    same = [d for d in usable if date.fromisoformat(d).weekday() == wd]
    k = sum(sessions[d] is not None for d in same)
    p_all = len(car_days) / len(usable)
    p_today = (k + _SHRINK_WEIGHT * p_all) / (len(same) + _SHRINK_WEIGHT)
    return AfternoonEvProfile(
        quiet_by_hour=quiet_by_hour,
        session_kw=session_kwh / session_hours if session_hours else 0.0,
        session_hours=session_hours, session_kwh=session_kwh,
        p_today=p_today, k_weekday=k, n_weekday=len(same), weekday_name=_WEEKDAYS[wd],
        n_days=len(usable), n_sessions=len(car_days),
    )
