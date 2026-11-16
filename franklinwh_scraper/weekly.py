"""A week of best times for a load you run by hand (dryer, dishwasher, oven), from history.

/when answers "when today?" from the live forecast, which only reaches a day or two out. This
answers "which slot, each day this week?" for loads you run on a routine: a typical day built
from the last few weeks (solar by hour, house load by weekday, the battery level you usually
wake to), walked through besttime.score_windows at that date's real time-of-use rates. Weekends
and holidays get their own cheap hours on EV-TOU-5, so the answer differs by day.

Against your usual time (learned habits plus Config.habit_loads) it shows what moving would save,
and the "Delay End" time to set on the machine so the cycle finishes inside the best slot. Samsung
washers and dryers have Delay End on the panel, so this needs no appliance connection.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from . import besttime
from .predictor import HourPrediction

DAYS = 7
SOLAR_DAYS = 21                 # a season moves slowly; three weeks smooths the clouds
LOAD_DAYS = 28                  # four of each weekday
_EARLIEST = time(besttime.MORNING_HOUR)
_LATEST = time(besttime.LATEST_START_HOUR)
_WEEKS_PER_MONTH = 52.0 / 12.0
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@dataclass
class DaySlot:
    day: date
    best: besttime.Window
    usual_start: datetime | None = None
    usual_cost: float | None = None


@dataclass
class WeekPlan:
    name: str
    kw: float
    hours: float
    days: list[DaySlot] = field(default_factory=list)

    @property
    def weekly_saving(self) -> float:
        return round(sum(max(0.0, d.usual_cost - d.best.cost)
                         for d in self.days if d.usual_cost is not None), 2)


def _finite(x: float, default: float = 0.0) -> float:
    return x if isinstance(x, (int, float)) and math.isfinite(x) else default


def typical_day(day: date, solar_by_hour: dict[int, float],
                load_by_slot: dict[tuple[int, int], float]) -> list[HourPrediction]:
    """Hourly typical solar and load from midnight on `day` to 7 AM the next morning."""
    start = datetime.combine(day, time(0))
    out = []
    for i in range(24 + besttime.MORNING_HOUR + 1):
        dt = start + timedelta(hours=i)
        solar = _finite(solar_by_hour.get(dt.hour, 0.0))
        dow_loads = [v for (d, _h), v in load_by_slot.items() if d == dt.weekday()]
        fallback = statistics.mean(dow_loads) if dow_loads else 0.0
        load = _finite(load_by_slot.get((dt.weekday(), dt.hour), fallback))
        out.append(HourPrediction(dt=dt, predicted_load_kw=load, predicted_solar_kw=solar,
                                  net_kw=solar - load, confidence="medium"))
    return out


def _usual(habits: dict | None, day: date, name: str) -> datetime | None:
    """Your usual start for `name` on this weekday, rounded to the half hour, inside 7 AM-10 PM."""
    for entry in (habits or {}).get(_WEEKDAYS[day.weekday()], []):
        try:
            if entry[0] != name:
                continue
            hh, mm = (int(x) for x in str(entry[1]).split(":"))
        except (IndexError, TypeError, ValueError):
            continue
        mins = int(round((hh * 60 + mm) / 30.0) * 30)
        t = datetime.combine(day, time(0)) + timedelta(minutes=mins)
        if _EARLIEST <= t.time() <= _LATEST and t.date() == day:
            return t
    return None


def plan_week(start: date, solar_by_hour: dict[int, float], load_by_slot: dict[tuple[int, int], float],
              soc7_pct: float, cap: float, reserve_pct: float, name: str, kw: float, hours: float,
              rate_at: Callable[[datetime], float], export_rate_at: Callable[[datetime], float],
              habits: dict | None = None, days: int = DAYS) -> WeekPlan | None:
    """The best start each day for `days` days from `start`, or None without history."""
    if not solar_by_hour or not load_by_slot or kw <= 0 or hours <= 0:
        return None
    plan = WeekPlan(name=name, kw=kw, hours=hours)
    for i in range(days):
        d = start + timedelta(days=i)
        res = besttime.score_windows(typical_day(d, solar_by_hour, load_by_slot),
                                     datetime.combine(d, _EARLIEST), soc7_pct, cap, reserve_pct,
                                     kw, hours, rate_at, export_rate_at)
        if res is None or not res.windows:
            continue
        slot = DaySlot(day=d, best=res.best)
        usual = _usual(habits, d, name)
        if usual is not None:
            match = next((w for w in res.windows if w.start == usual), None)
            if match is not None:
                slot.usual_start, slot.usual_cost = usual, match.cost
        plan.days.append(slot)
    return plan if plan.days else None


def _clock(t: datetime) -> str:
    return t.strftime("%-I:%M %p")


def describe_week(plan: WeekPlan) -> str:
    dur = timedelta(hours=plan.hours)
    lines = [f"🗓 Best time to run the {plan.name} each day this week "
             f"({plan.kw:g} kW, {besttime.fmt_duration(plan.hours)}), from a typical day of the last few weeks:"]
    for d in plan.days:
        b = d.best
        lines.append(f"{d.day:%a %-m/%-d}  {_clock(b.start)}  ${b.cost:.2f}  · Delay End {_clock(b.start + dur)}")
    usual = [d for d in plan.days if d.usual_cost is not None]
    for d in usual:
        gain = d.usual_cost - d.best.cost
        if gain >= 0.01:
            lines.append(f"Your usual {d.day:%a} {_clock(d.usual_start)} costs ${d.usual_cost:.2f}; "
                         f"{_clock(d.best.start)} saves ${gain:.2f}.")
        else:
            lines.append(f"Your usual {d.day:%a} {_clock(d.usual_start)} is already one of the cheapest "
                         f"(${d.usual_cost:.2f}); keep it.")
    if plan.weekly_saving >= 0.01:
        lines.append(f"Moving to the best slots saves about ${plan.weekly_saving:.2f}/week "
                     f"(~${plan.weekly_saving * _WEEKS_PER_MONTH:.2f}/month).")
    lines.append("Delay End finishes the cycle by that time; set it when you load the machine. "
                 "Cloudy days shift this; /when checks today against the live forecast.")
    return "\n".join(lines)


def for_store(store: Any, cfg: Any, state: dict, today: date, name: str = "dryer",
              rate_at: Callable[[datetime], float] | None = None,
              export_rate_at: Callable[[datetime], float] | None = None,
              reserve_pct: float | None = None, days: int = DAYS) -> WeekPlan | None:
    """The week's plan from the history store; None without enough history or on any store error."""
    if store is None:
        return None
    size = besttime.load_size(name, (state or {}).get("appliance_events"), cfg)
    if size is None:
        return None
    kw, hours = size
    if rate_at is None or export_rate_at is None:
        from .tou import export_rate_at as _er, rate_at as _r
        rate_at, export_rate_at = rate_at or _r, export_rate_at or _er
    try:
        raw_solar = store.recent_solar_profile(SOLAR_DAYS)
        load = store.recent_load_profile(LOAD_DAYS, 0.5)
        socs = [s for i in range(1, 8)
                if (s := store.soc_near(datetime.combine(today - timedelta(days=i), _EARLIEST).isoformat()))
                is not None]
        if reserve_pct is None:
            from .alerts import _effective_reserve_pct
            reserve_pct = _effective_reserve_pct(cfg, store, datetime.now())
    except Exception:  # noqa: BLE001  (a weekly summary must never break on a locked db)
        return None
    by_hour: dict[int, list[float]] = {}
    for (_d, h), v in (raw_solar or {}).items():
        by_hour.setdefault(int(h), []).append(_finite(v))
    solar = {h: statistics.mean(v) for h, v in by_hour.items()}
    soc7 = statistics.median(socs) if socs else float(reserve_pct)
    from .appliances import learned_habit_loads
    habits: dict[str, list] = {k: list(v) for k, v in (getattr(cfg, "habit_loads", None) or {}).items()}
    for k, v in learned_habit_loads((state or {}).get("appliance_events") or [], today).items():
        habits.setdefault(k, []).extend(v)
    cap = float(getattr(cfg, "battery_capacity_kwh", 13.6) or 13.6)
    # The week starts tomorrow: today's 7 AM has passed, and /when covers today.
    return plan_week(today + timedelta(days=1), solar, load or {}, soc7, cap, float(reserve_pct),
                     name, kw, hours, rate_at, export_rate_at, habits, days)
