"""How often, and how much, the owner charges the battery from the grid by day
(Emergency Backup). It is the owner's decision, so it cannot be forecast, but it can
be put in front of them: "you ran EB on 6 of 30 days like this, ~2.5 kWh".

A session is a run of polls importing from the grid while the battery charges, long
enough and big enough to be a deliberate top-off rather than a blip. Overnight
charging is planned (the reserve is set first) and is not counted here: only sessions
that start in the daytime window.
"""

import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

GRID_KW = 0.5               # importing over this ...
BATT_KW = 0.5               # ... while the battery charges over this
MIN_POLLS = 3               # about 15 minutes at the 5-minute cadence
MIN_KWH = 1.0               # a real top-off, not a trickle
MAX_GAP = timedelta(minutes=15)
DAY_START = time(6, 0)
DAY_END = time(17, 0)       # after this, EB no longer changes the sundown number
MIN_DAY_READINGS = 150      # a day with most polls missing proves nothing
MIN_DAYS = 14


def find_eb_sessions(rows: list[tuple[str, float, float]]) -> list[dict]:
    """Sessions in one day's readings, each {"start": datetime, "grid_kwh": float}.
    `rows` are (timestamp, grid_use_kw, battery_use_kw), oldest first."""
    sessions: list[dict] = []
    cur: dict | None = None
    prev_t: datetime | None = None
    for ts, grid, batt in rows:
        t = datetime.fromisoformat(ts)
        on = grid > GRID_KW and batt < -BATT_KW
        if cur is not None and prev_t is not None:
            if on and t - cur["last"] <= MAX_GAP:
                cur["kwh"] += cur["pending_kw"] * min((t - cur["last"]).total_seconds(), 600) / 3600.0
            else:
                cur["kwh"] += cur["pending_kw"] * 300 / 3600.0   # the last poll stood for a poll interval
                sessions.append(cur)
                cur = None
        if on:
            kw = min(grid, -batt)
            if cur is None:
                cur = {"start": t, "last": t, "polls": 0, "kwh": 0.0, "pending_kw": kw}
            cur["last"], cur["pending_kw"] = t, kw
            cur["polls"] += 1
        prev_t = t
    if cur is not None:
        cur["kwh"] += cur["pending_kw"] * 300 / 3600.0
        sessions.append(cur)
    return [{"start": s["start"], "grid_kwh": s["kwh"]} for s in sessions
            if s["polls"] >= MIN_POLLS and s["kwh"] >= MIN_KWH and DAY_START <= s["start"].time() < DAY_END]


@dataclass
class EbProfile:
    """Odds and size of an EB top-off between now and mid-afternoon."""
    n_days: int
    n_with: int
    p: float
    k_weekday: int
    n_weekday: int
    weekday_name: str
    kwh: float                       # typical grid energy on days it happened
    kwh_lo: float | None             # 25th / 75th percentile
    kwh_hi: float | None


def build_eb_profile(store, now: datetime, lookback: int = 70) -> EbProfile | None:
    """Over the last `lookback` days: of the days with no EB yet by this clock time, how
    many had one start between now and mid-afternoon, and how much grid energy it was.
    None with too little history (or no store)."""
    if store is None:
        return None
    n_days = n_with = n_wd = k_wd = 0
    sizes: list[float] = []
    for i in range(1, lookback + 1):
        day: date = now.date() - timedelta(days=i)
        try:
            raw = store.soc_flow_readings(f"{day.isoformat()}T00:00:00", f"{(day + timedelta(days=1)).isoformat()}T00:00:00")
        except Exception:
            continue
        if len(raw) < MIN_DAY_READINGS:
            continue
        sessions = find_eb_sessions([(ts, grid, batt) for ts, _soc, batt, grid in raw])
        if any(s["start"].time() < now.time() for s in sessions):
            continue                                           # it already happened that day by this time
        later = [s for s in sessions if s["start"].time() >= now.time()]
        n_days += 1
        same_weekday = day.weekday() == now.weekday()
        n_wd += same_weekday
        if later:
            n_with += 1
            k_wd += same_weekday
            sizes.append(sum(s["grid_kwh"] for s in later))
    if n_days < MIN_DAYS:
        return None
    lo = hi = None
    if len(sizes) >= 5:
        q = statistics.quantiles(sizes, n=4)
        lo, hi = q[0], q[2]
    return EbProfile(n_days=n_days, n_with=n_with, p=n_with / n_days, k_weekday=k_wd, n_weekday=n_wd,
                     weekday_name=now.strftime("%A"), kwh=statistics.median(sizes) if sizes else 0.0,
                     kwh_lo=lo, kwh_hi=hi)
