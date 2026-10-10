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
# A session must START in 11:00-17:00 (solar-hours charging, not dinner) but is
# measured to its end, so readings are read to 19:00.
AFTERNOON_START_HOUR, AFTERNOON_END_HOUR = 11, 19    # window of readings to load
SESSION_START_BEFORE_HOUR = 17
AFTERNOON_HOURS = range(AFTERNOON_START_HOUR, SESSION_START_BEFORE_HOUR)   # day-coverage check
# Home from work: on weekdays the car is away until about 3 PM (the user's
# routine: weekday blocks start 3:00-3:17 PM), so a weekday block that starts
# earlier is more likely a dryer, AC or a work-from-home day. Weekends are open.
WEEKDAY_ARRIVAL_MINUTE = 14 * 60 + 45
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


def before_arrival(start: datetime) -> bool:
    """True for a weekday time before the usual ~3 PM arrival home."""
    return start.weekday() < 5 and start.hour * 60 + start.minute < WEEKDAY_ARRIVAL_MINUTE


def _steady_blocks(readings: list[tuple[str, float]], min_kw: float, min_minutes: float,
                   window: tuple[int, int] | None = None) -> list[list[tuple[datetime, float]]]:
    """Runs of consecutive readings at or above `min_kw` (no gap over 10 minutes,
    optionally only inside the [start, end) clock-hour `window`) that last at
    least `min_minutes` and stay within BLOCK_MAX_CV of flat."""
    blocks: list[list[tuple[datetime, float]]] = []
    run: list[tuple[datetime, float]] = []

    def flush():
        if len(run) >= 2:
            minutes = (run[-1][0] - run[0][0]).total_seconds() / 60.0 + _READING_HOURS * 60
            kws = [kw for _t, kw in run]
            if minutes >= min_minutes and statistics.pstdev(kws) / statistics.mean(kws) <= BLOCK_MAX_CV:
                blocks.append(list(run))
        run.clear()

    for ts, kw in readings:
        t = datetime.fromisoformat(ts)
        if (window and not window[0] <= t.hour < window[1]) or kw < min_kw:
            flush()
            continue
        if run and t - run[-1][0] > _MAX_GAP:
            flush()
        run.append((t, kw))
    flush()
    return blocks


def _block_minutes(block: list[tuple[datetime, float]]) -> int:
    return round((block[-1][0] - block[0][0]).total_seconds() / 60.0 + _READING_HOURS * 60)


def find_afternoon_session(readings: list[tuple[str, float]], weekday: bool = False) -> dict | None:
    """Steady car-charge blocks in a day's (iso timestamp, kW) readings, or None.
    A block must start between 11:00 and 17:00 (and, with weekday=True, not before
    the usual ~3 PM arrival home); it is measured to its end. Returns
    {"start_hour", "minutes" (all blocks), "blocks", "avg_kw", "readings"}."""
    def starts_ok(block) -> bool:
        start = block[0][0]
        return (AFTERNOON_START_HOUR <= start.hour < SESSION_START_BEFORE_HOUR
                and not (weekday and start.hour * 60 + start.minute < WEEKDAY_ARRIVAL_MINUTE))

    blocks = [b for b in _steady_blocks(readings, SESSION_MIN_KW, BLOCK_MIN_MINUTES,
                                        (AFTERNOON_START_HOUR, AFTERNOON_END_HOUR)) if starts_ok(b)]
    minutes = sum(_block_minutes(b) for b in blocks)
    if minutes < SESSION_MIN_MINUTES:
        return None
    flat = [r for b in blocks for r in b]
    return {"start_hour": blocks[0][0][0].hour, "minutes": minutes, "blocks": len(blocks),
            "avg_kw": statistics.mean(kw for _t, kw in flat), "readings": flat}


SPIKE_MIN_KW = 3.0
SPIKE_MIN_MINUTES = 25


def find_load_spikes(readings: list[tuple[str, float]], min_minutes: float = SPIKE_MIN_MINUTES) -> list[dict]:
    """Big steady draws anywhere in a day's readings (a car top-off, an oven, a
    water heater): >= SPIKE_MIN_KW for >= min_minutes, unbroken and flat.
    Each is {"start", "end", "minutes", "avg_kw"}, in time order."""
    out = []
    for b in _steady_blocks(readings, SPIKE_MIN_KW, min_minutes):
        out.append({"start": b[0][0], "end": b[-1][0] + timedelta(minutes=_READING_HOURS * 60),
                    "minutes": _block_minutes(b), "avg_kw": statistics.mean(kw for _t, kw in b)})
    return out


def build_profile(store, today: date, lookback_days: int = _LOOKBACK_DAYS,
                  labels: dict[str, bool] | None = None) -> AfternoonEvProfile | None:
    """Profile from the trailing `lookback_days` (not including `today`), or None
    when there is too little clean history to call it a pattern.

    `labels` ({"YYYY-MM-DD": was_the_car}) are the user's own answers and win
    over detection: a steady block can be a dryer or a long heat-pump run, and
    a quiet-looking day can still have been the car. Session size/length come
    only from days where a block was actually detected."""
    start, end = (today - timedelta(days=lookback_days)).isoformat(), today.isoformat()
    hour_means = store.daily_hourly_load_means(start, end)
    usable = {d: hm for d, hm in hour_means.items() if sum(h in hm for h in AFTERNOON_HOURS) >= 5}
    if len(usable) < _MIN_DAYS:
        return None
    readings = store.daily_afternoon_readings(start, end, AFTERNOON_START_HOUR, AFTERNOON_END_HOUR)
    labels = labels or {}
    # Unlabeled days count only blocks that fit the routine (weekdays: from ~3 PM).
    # A "yes" label on a day outside the routine (work from home) still counts, and
    # then its size/length come from whatever block is there.
    sessions = {}
    for d in usable:
        wk = date.fromisoformat(d).weekday() < 5
        sessions[d] = find_afternoon_session(readings.get(d, []), weekday=wk)
        if sessions[d] is None and labels.get(d):
            sessions[d] = find_afternoon_session(readings.get(d, []), weekday=False)
    is_car = {d: labels.get(d, sessions[d] is not None) for d in usable}

    quiet_days = [hm for d, hm in usable.items() if not is_car[d]]
    if len(quiet_days) < _MIN_QUIET_DAYS:
        return None
    quiet_by_hour = {
        h: statistics.median(hm[h] for hm in quiet_days if h in hm)
        for h in range(24) if any(h in hm for hm in quiet_days)
    }

    car_days = [d for d in usable if is_car[d]]
    measured = [d for d in car_days if sessions[d] is not None]
    if measured:
        kwh = [sum(max(0.0, kw - quiet_by_hour.get(t.hour, 0.4)) * _READING_HOURS
                   for t, kw in sessions[d]["readings"]) for d in measured]
        session_hours = statistics.median(sessions[d]["minutes"] for d in measured) / 60.0
        session_kwh = statistics.median(kwh)
    else:
        session_hours, session_kwh = 0.0, 0.0

    wd = today.weekday()
    same = [d for d in usable if date.fromisoformat(d).weekday() == wd]
    k = sum(is_car[d] for d in same)
    p_all = len(car_days) / len(usable)
    p_today = (k + _SHRINK_WEIGHT * p_all) / (len(same) + _SHRINK_WEIGHT)
    return AfternoonEvProfile(
        quiet_by_hour=quiet_by_hour,
        session_kw=session_kwh / session_hours if session_hours else 0.0,
        session_hours=session_hours, session_kwh=session_kwh,
        p_today=p_today, k_weekday=k, n_weekday=len(same), weekday_name=_WEEKDAYS[wd],
        n_days=len(usable), n_sessions=len(car_days),
    )
