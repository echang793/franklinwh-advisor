"""Best time to run a load today: when a dryer, oven, AC or car costs the least.

For each start time (now, then every half hour to 10 PM) the rest of the day is simulated twice,
with and without the load, through a self-consumption battery: solar surplus fills the battery and
the rest is exported; a deficit drains it to the reserve and the rest is bought. The load's cost is
the import it adds plus the export credit it gives up, at the real time-of-use and export rates, so
a 6 PM dryer is charged for the on-peak kWh it makes the house buy later, not for the moment it
runs. The walk ends at 7 AM after the last start, so overnight import counts too.

Round-trip battery losses (~10%) are ignored: they shift every option alike and would only blur a
ranking that the rate gaps decide. Everything here is pure; the chat bot and the dashboard build
the forecast and pass it in.
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

STEP = timedelta(minutes=30)
LATEST_START_HOUR = 22
MORNING_HOUR = 7
HARDLY_MATTERS = 0.10              # $ spread under which "any time" is the honest answer

LOADS: dict[str, tuple[float, float]] = {        # name -> (kW, hours); same sizes as the plan presets
    "dryer": (4.0, 1.0),
    "oven": (2.5, 2.0),
    "kitchen": (2.5, 2.0),
    "ac": (3.0, 3.0),
    "dishwasher": (1.5, 1.5),
}
_ALIASES = {"a/c": "ac", "aircon": "ac", "air conditioning": "ac", "dish washer": "dishwasher",
            "dishes": "dishwasher", "ev": "car", "tesla": "car", "cooking": "kitchen", "stove": "kitchen"}
_LEARN_MIN = 3


@dataclass
class SimResult:
    import_kwh: float = 0.0
    export_kwh: float = 0.0
    import_cost: float = 0.0
    export_credit: float = 0.0
    soc_at_sunset_kwh: float | None = None


@dataclass
class Window:
    start: datetime
    cost: float                       # $ the load adds over the walk
    grid_kwh: float                   # extra import it causes
    export_lost_kwh: float            # export it displaces
    sundown_delta: float | None       # battery % points at sunset (<= 0), None without a sunset ahead


@dataclass
class BestTime:
    windows: list[Window] = field(default_factory=list)

    @property
    def now(self) -> Window:
        return self.windows[0]

    @property
    def best(self) -> Window:
        return min(self.windows, key=lambda w: (w.cost, w.start))

    @property
    def worst(self) -> Window:
        return max(self.windows, key=lambda w: (w.cost, -w.start.timestamp()))


def _finite(x: float) -> float:
    return x if math.isfinite(x) else 0.0


def simulate(hours: list, now: datetime, soc_kwh: float, cap: float, floor_kwh: float,
             extra: Callable[[datetime], float], rate_at: Callable[[datetime], float],
             export_rate_at: Callable[[datetime], float], until: datetime,
             sunset: datetime | None = None, breaks: list[datetime] | None = None,
             trace: list | None = None) -> SimResult:
    """Walk the forecast from `now` to `until` in half-hour steps (also split at `breaks`, so a
    load window's edges fall on step edges). `extra(t)` is the added kW during the step at t.
    Stops early where the forecast runs out."""
    by_hour = {h.dt.replace(minute=0, second=0, microsecond=0): h for h in hours}
    cuts = sorted({b for b in (breaks or []) if now < b < until} | ({sunset} if sunset and now < sunset < until else set()))
    r = SimResult()
    soc = max(0.0, min(cap, soc_kwh))
    floor = max(0.0, min(cap, floor_kwh))
    t = now
    while t < until:
        h = by_hour.get(t.replace(minute=0, second=0, microsecond=0))
        if h is None:
            break
        half = t.replace(minute=30 if t.minute < 30 else 0, second=0, microsecond=0)
        if half <= t:
            half = (t + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        nxt = min([half, until] + [c for c in cuts if c > t])
        dt_h = (nxt - t).total_seconds() / 3600.0
        if sunset is not None and t == sunset:
            r.soc_at_sunset_kwh = soc
        if trace is not None:
            trace.append((t, soc))
        net = (_finite(h.predicted_solar_kw) - _finite(h.predicted_load_kw) - extra(t)) * dt_h
        if net >= 0:
            charge = min(net, cap - soc)
            soc += charge
            exp = net - charge
            r.export_kwh += exp
            r.export_credit += exp * export_rate_at(t)
        else:
            need = -net
            dis = min(need, max(0.0, soc - floor))
            soc -= dis
            imp = need - dis
            r.import_kwh += imp
            r.import_cost += imp * rate_at(t)
        t = nxt
    if sunset is not None and now < sunset and r.soc_at_sunset_kwh is None and t >= sunset:
        r.soc_at_sunset_kwh = soc
    return r


def _starts(now: datetime) -> list[datetime]:
    out = [now]
    last = now.replace(hour=LATEST_START_HOUR, minute=0, second=0, microsecond=0)
    t = now.replace(minute=30 if now.minute < 30 else 0, second=0, microsecond=0)
    if t <= now:
        t = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    while t <= last:
        out.append(t)
        t += STEP
    return out


def score_windows(hours: list, now: datetime, soc_pct: float, cap: float, reserve_pct: float,
                  kw: float, duration_h: float, rate_at: Callable[[datetime], float],
                  export_rate_at: Callable[[datetime], float], sunset: datetime | None = None) -> BestTime | None:
    """Every start time with what the load would cost there, or None without a forecast."""
    if not hours or kw <= 0 or duration_h <= 0:
        return None
    last_start = max(now, now.replace(hour=LATEST_START_HOUR, minute=0, second=0, microsecond=0))
    until = (last_start + timedelta(days=1)).replace(hour=MORNING_HOUR, minute=0, second=0, microsecond=0)
    soc_kwh = soc_pct / 100.0 * cap
    floor_kwh = reserve_pct / 100.0 * cap
    sun = sunset if sunset is not None and sunset > now else None

    def run(extra, breaks):
        return simulate(hours, now, soc_kwh, cap, floor_kwh, extra, rate_at, export_rate_at, until,
                        sunset=sun, breaks=breaks)

    base = run(lambda t: 0.0, [])
    dur = timedelta(hours=duration_h)
    out = BestTime()
    for s in _starts(now):
        e = s + dur
        r = run(lambda t, s=s, e=e: kw if s <= t < e else 0.0, [s, e])
        cost = (r.import_cost - base.import_cost) + (base.export_credit - r.export_credit)
        delta = None
        if sun is not None and r.soc_at_sunset_kwh is not None and base.soc_at_sunset_kwh is not None:
            delta = min(0.0, (r.soc_at_sunset_kwh - base.soc_at_sunset_kwh) / cap * 100.0) if cap else 0.0
        out.windows.append(Window(start=s, cost=round(_finite(cost), 2),
                                  grid_kwh=round(_finite(r.import_kwh - base.import_kwh), 2),
                                  export_lost_kwh=round(_finite(base.export_kwh - r.export_kwh), 2),
                                  sundown_delta=None if delta is None else round(delta, 1)))
    return out


# ── load sizes and requests ──────────────────────────────────────────────────────────────────────

def _canonical(name: str) -> str:
    n = name.lower().strip()
    return _ALIASES.get(n, n)


def load_size(name: str, events: list[dict] | None = None, cfg: Any = None) -> tuple[float, float] | None:
    """(kW, hours) for a named load: the car from the config, otherwise the median of your own
    answered "what was that?" events once there are three, otherwise the preset size."""
    n = _canonical(name)
    if n == "car":
        kw = float(getattr(cfg, "ev_charging_kw", 0) or 7.6)
        kwh = float(getattr(cfg, "ev_kwh_per_session", 0) or 0)
        return kw, (round(kwh / kw, 2) if kwh > 0 else 2.0)
    mine = [e for e in (events or []) if e.get("name") == n and e.get("kw") and e.get("minutes")]
    if len(mine) >= _LEARN_MIN:
        return round(statistics.median(e["kw"] for e in mine), 1), round(statistics.median(e["minutes"] for e in mine) / 60.0, 2)
    return LOADS.get(n)


_KW_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kw\b")
_H_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b")
_MIN_RE = re.compile(r"(\d+)\s*(?:m|min|mins|minute|minutes)\b")
_NAMES = sorted(set(LOADS) | {"car"} | set(_ALIASES), key=len, reverse=True)


def parse_request(text: str) -> tuple[str | None, float | None, float | None] | None:
    """(name, kW, hours) from "dryer", "3.5 kW 2h", "oven for 90 min"; None when neither a known
    load nor a kW figure is there."""
    t = (text or "").lower()
    kw_m = _KW_RE.search(t)
    kw = float(kw_m.group(1)) if kw_m else None
    hours = None
    if (m := _H_RE.search(t)) is not None:
        hours = float(m.group(1))
    elif (m := _MIN_RE.search(t)) is not None:
        hours = round(int(m.group(1)) / 60.0, 2)
    name = None
    for n in _NAMES:
        if re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", t):
            name = _canonical(n)
            break
    if name is None and kw is None:
        return None
    return name, kw, hours


# ── the reply ────────────────────────────────────────────────────────────────────────────────────

def _clock(t: datetime) -> str:
    return t.strftime("%-I:%M %p")


def _why(w: Window) -> str:
    if w.grid_kwh >= 0.05:
        return f"makes the house buy {w.grid_kwh:.1f} kWh more"
    if w.export_lost_kwh >= 0.05:
        return "runs on sun you'd otherwise export"
    return "the battery covers it"


def _line(tag: str, w: Window) -> str:
    sd = f", battery at sunset {w.sundown_delta:+.0f} pts" if w.sundown_delta not in (None, 0.0) and abs(w.sundown_delta) >= 0.5 else ""
    return f"{tag}: {_clock(w.start)}, about ${w.cost:.2f} ({_why(w)}{sd})"


def fmt_duration(hours: float) -> str:
    total = int(round(hours * 60))
    h, m = divmod(total, 60)
    return " ".join(p for p in (f"{h} h" if h else "", f"{m} min" if m else "") if p) or "0 min"


def describe(res: BestTime, label: str, kw: float, hours: float) -> str:
    """The chat reply for /when."""
    head = f"🕐 <b>When to run the {label}</b> ({kw:g} kW for {fmt_duration(hours)})"
    best, worst, now = res.best, res.worst, res.now
    if worst.cost - best.cost < HARDLY_MATTERS:
        return f"{head}\nIt hardly matters today: any time costs about ${best.cost:.2f}."
    lines = [head, _line("✅ Best", best)]
    if now is not best:
        lines.append(_line("Now", now))
    lines.append(_line("❌ Worst", worst))
    if now is best:
        lines.append("Now is the best time.")
    return "\n".join(lines)


# ── live inputs (shared by the chat bot and the dashboard) ───────────────────────────────────────

def _now() -> datetime:
    return datetime.now()


def _lay(hours: list, loads: list[dict], day) -> list:
    """Copy of the forecast hours with each planned load's kW added over the hours it covers."""
    import dataclasses
    out = list(hours)
    for ld in loads:
        try:
            start, dur, kw = float(ld["start"]), float(ld["hours"]), float(ld["kw"])
        except (KeyError, TypeError, ValueError):
            continue
        for i, h in enumerate(out):
            if h.dt.date() != day:
                continue
            hs = h.dt.hour + h.dt.minute / 60.0
            overlap = max(0.0, min(hs + 1.0, start + dur) - max(hs, start))
            if overlap > 0:
                out[i] = dataclasses.replace(h, predicted_load_kw=h.predicted_load_kw + kw * overlap)
    return out


def live_hours(c: Any, state: dict, outlook: Any, forecast: Any, store: Any, now: datetime) -> list | None:
    """The forecast hours /sundown walks: live-anchored, the quiet afternoon, today's planned loads."""
    import dataclasses

    from . import planned
    from .alerts import _afternoon_profile, _live_anchored_forecast
    try:
        live = _live_anchored_forecast(state, c, outlook, forecast, store)
    except Exception:  # noqa: BLE001  (fall back to the shared forecast)
        live = forecast
    if live is None or not getattr(live, "hours", None):
        return None
    hours = list(live.hours)
    profile = _afternoon_profile(store, now.date(), state.get("car_day_labels"))
    if profile is not None:
        for i, h in enumerate(hours):
            if h.dt.date() == now.date() and h.dt > now:
                quiet = profile.quiet_by_hour.get(h.dt.hour)
                if quiet is not None:
                    hours[i] = dataclasses.replace(h, predicted_load_kw=quiet)
    return _lay(hours, planned.get_plan(state, now.strftime("%Y-%m-%d")), now.date())


def for_snapshot(c: Any, state: dict, cfg: Any, outlook: Any, forecast: Any, store: Any, now: datetime,
                 kw: float, duration_h: float) -> BestTime | None:
    """Score start times from the advisor's live inputs: the same live-anchored forecast and quiet
    afternoon as /sundown and /whatif, today's planned loads laid in, the reserve the battery is
    actually holding, and the real sunset."""
    from .alerts import _effective_reserve_pct, _sunset_today
    from .tou import export_rate_at, rate_at
    hours = live_hours(c, state, outlook, forecast, store, now)
    if hours is None:
        return None
    cap = float(getattr(cfg, "battery_capacity_kwh", 13.6) or 13.6)
    return score_windows(hours, now, float(c.battery_soc_pct), cap, _effective_reserve_pct(cfg, store, now),
                         kw, duration_h, rate_at, export_rate_at, sunset=_sunset_today(outlook, now))


def resolve(req: tuple, state: dict, cfg: Any) -> tuple[str, str, float, float] | None:
    """(plan name, label, kW, hours) for a parsed request, or None when it can't be sized."""
    name, kw, hours = req
    size = load_size(name, state.get("appliance_events"), cfg) if name else None
    kw = kw if kw is not None else (size[0] if size else None)
    if kw is None or kw <= 0:
        return None
    hours = hours if hours is not None else (size[1] if size else 1.0)
    return (name or "load"), (name or f"{kw:g} kW load"), float(kw), float(hours)


# ── missed savings: replay last week's big loads against what the day really did ─────────────────

MIN_SAVING = 0.25                 # per load, $: smaller shifts aren't worth a line
WEEK_MIN_TOTAL = 0.50             # the weekly block stays out under this
_EARLIEST, _LATEST_END = 9, 23    # a shifted load starts after 9 AM and ends by 11 PM
_MIN_PER_HOUR = 6                 # readings an hour needs (of 12) for the day to count


@dataclass
class Missed:
    start: datetime
    minutes: int
    kw: float
    actual_cost: float
    better_start: datetime
    better_cost: float
    name: str = "big load"

    @property
    def saving(self) -> float:
        return round(self.actual_cost - self.better_cost, 2)


def _hourly(readings: list) -> list:
    """Mean load and solar per clock hour as forecast-shaped entries."""
    from .predictor import HourPrediction
    buckets: dict[datetime, list] = {}
    for ts, _grid, load, solar in readings:
        try:
            t = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            continue
        buckets.setdefault(t.replace(minute=0, second=0, microsecond=0), []).append((float(load), float(solar)))
    out = []
    for h in sorted(buckets):
        vals = buckets[h]
        load = sum(v[0] for v in vals) / len(vals)
        solar = sum(v[1] for v in vals) / len(vals)
        out.append(HourPrediction(dt=h, predicted_load_kw=load, predicted_solar_kw=solar, net_kw=solar - load,
                                  confidence="actual"))
    return out


def _complete(readings: list, t0: datetime, until: datetime) -> bool:
    counts: dict[datetime, int] = {}
    for ts, *_ in readings:
        try:
            h = datetime.fromisoformat(ts).replace(minute=0, second=0, microsecond=0)
        except (TypeError, ValueError):
            continue
        counts[h] = counts.get(h, 0) + 1
    t = t0
    while t < until:
        if counts.get(t, 0) < _MIN_PER_HOUR:
            return False
        t += timedelta(hours=1)
    return True


def _local_extra(readings: list, start: datetime, end: datetime) -> float | None:
    """The block's power over what the house drew in the hour either side of it: on a busy evening
    the quiet-house median would count the evening itself as part of the load."""
    inside, around = [], []
    for ts, _g, load, _s in readings:
        t = datetime.fromisoformat(ts)
        if start <= t < end:
            inside.append(load)
        elif start - timedelta(hours=1) <= t < start or end <= t < end + timedelta(hours=1):
            around.append(load)
    if not inside or not around:
        return None
    return statistics.mean(inside) - statistics.median(around)


def replay_day(readings: list, day, soc_start_pct: float, cap: float, reserve_pct: float,
               rate_at: Callable[[datetime], float], export_rate_at: Callable[[datetime], float],
               min_saving: float = MIN_SAVING) -> list[Missed]:
    """The day's big loads that would have cost noticeably less at another time that day.
    `readings` are (timestamp, grid, load, solar) from midnight to 7 AM the next morning."""
    from .loadevents import day_blocks
    t0 = datetime(day.year, day.month, day.day)
    until = t0 + timedelta(days=1, hours=MORNING_HOUR)
    if not _complete(readings, t0, until):
        return []
    blocks = day_blocks([(ts, load) for ts, _g, load, _s in readings
                         if ts[:10] == day.isoformat() and int(ts[11:13]) >= 6])
    hours = _hourly(readings)
    soc_kwh = soc_start_pct / 100.0 * cap
    floor_kwh = reserve_pct / 100.0 * cap
    out: list[Missed] = []
    for b in blocks:
        start = t0 + timedelta(hours=b["start"])
        dur = timedelta(minutes=b["minutes"])
        kw = _local_extra(readings, start, start + dur)
        if kw is None or kw <= 0:
            continue
        without = _lay(hours, [{"start": b["start"], "hours": b["minutes"] / 60.0, "kw": -kw}], day)

        def run(extra, breaks, without=without):
            return simulate(without, t0, soc_kwh, cap, floor_kwh, extra, rate_at, export_rate_at, until, breaks=breaks)

        base = run(lambda t: 0.0, [])

        def cost_at(s, kw=kw, dur=dur, base=base, run=run):
            e = s + dur
            r = run(lambda t: kw if s <= t < e else 0.0, [s, e])
            return (r.import_cost - base.import_cost) + (base.export_credit - r.export_credit)

        actual = cost_at(start)
        cands, s = [], t0 + timedelta(hours=_EARLIEST)
        while s + dur <= t0 + timedelta(hours=_LATEST_END):
            cands.append(s)
            s += STEP
        if not cands:
            continue
        scored = [(round(cost_at(c), 2), c) for c in cands]
        better_cost, better_start = min(scored)
        m = Missed(start=start, minutes=int(b["minutes"]), kw=round(kw, 1), actual_cost=round(_finite(actual), 2),
                   better_start=better_start, better_cost=round(_finite(better_cost), 2))
        if m.saving >= min_saving:
            out.append(m)
    return out


def missed_week(store: Any, today, state: dict, cfg: Any, rate_at: Callable[[datetime], float] | None = None,
                export_rate_at: Callable[[datetime], float] | None = None, days: int = 7) -> list[Missed]:
    """Replay the `days` days before `today` (car days left out), naming loads you've answered."""
    from .tou import export_rate_at as _exp
    from .tou import rate_at as _rate
    rate_at, export_rate_at = rate_at or _rate, export_rate_at or _exp
    cap = float(getattr(cfg, "battery_capacity_kwh", 13.6) or 13.6)
    reserve = float(getattr(cfg, "battery_reserve_pct", 10.0) or 10.0)
    cars = state.get("car_day_labels") or {}
    events = state.get("appliance_events") or []
    out: list[Missed] = []
    for i in range(days, 0, -1):
        day = today - timedelta(days=i)
        if cars.get(day.isoformat()) is True:
            continue
        t0 = datetime(day.year, day.month, day.day)
        readings = store.readings_between(t0.isoformat(), (t0 + timedelta(days=1, hours=MORNING_HOUR)).isoformat())
        soc = store.soc_near(t0.isoformat())
        if not readings or soc is None:
            continue
        for m in replay_day(readings, day, float(soc), cap, reserve, rate_at, export_rate_at):
            for e in events:
                try:
                    et = datetime.fromisoformat(e["key"])
                except (KeyError, TypeError, ValueError):
                    continue
                if abs((et - m.start).total_seconds()) <= 1800 and e.get("name") not in (None, "other"):
                    m.name = e["name"]
                    break
            out.append(m)
    return out


def describe_missed(missed: list[Missed], top: int = 3) -> str | None:
    """The weekly block / the /missed reply, or None when the total is too small to bother."""
    total = round(sum(m.saving for m in missed), 2)
    if total < WEEK_MIN_TOTAL:
        return None
    lines = [f"💸 <b>Could have saved</b> about ${total:.2f} last week by moving {len(missed)} "
             f"load{'s' if len(missed) != 1 else ''}:"]
    for m in sorted(missed, key=lambda m: -m.saving)[:top]:
        lines.append(f"• {m.start:%a} {_clock(m.start)} {m.name} ({m.kw:g} kW, {fmt_duration(m.minutes / 60)}): "
                     f"${m.actual_cost:.2f}, at {_clock(m.better_start)} about ${m.better_cost:.2f}")
    return "\n".join(lines)


def pin_to(series: list[tuple[datetime, float]], now: datetime, at: datetime, target: float) -> list[tuple[datetime, float]]:
    """Bend a projected battery % curve so it passes through `target` at `at` (the headline sundown
    number, which carries corrections this plain walk doesn't): no change at `now`, the full lift at
    `at` and after, linear in between. Clamped to 0-100."""
    if not series or at <= now or target is None or not math.isfinite(target):
        return series
    before = [(t, v) for t, v in series if t <= at]
    after = [(t, v) for t, v in series if t >= at]
    if not before or not after:
        return series
    (t0, v0), (t1, v1) = before[-1], after[0]
    raw_at = v0 if t1 == t0 else v0 + (v1 - v0) * (at - t0).total_seconds() / (t1 - t0).total_seconds()
    lift = target - raw_at
    span = (at - now).total_seconds()
    out = []
    for t, v in series:
        frac = min(1.0, max(0.0, (t - now).total_seconds() / span))
        out.append((t, min(100.0, max(0.0, v + lift * frac))))
    return out
