"""The loads you tell the advisor you will run today (/plan dryer 4pm).

The forecast cannot see a dryer or an oven that has not started yet, so a plan you give it is
kept for the day (state["planned_loads_<date>"]) and counted by /sundown, /evening, /ebplan and the
12:30 "tonight may run short" alert. Each load is {name, start (clock hour, may be fractional),
hours, kw}, the same shape as Config.habit_loads gives /ebplan.
"""

EVENING_START_HOUR = 16        # evening.py's window: 4 PM to midnight
_KEY = "planned_loads_"


def _valid(ld) -> bool:
    try:
        return bool(ld["name"]) and float(ld["start"]) >= 0 and float(ld["hours"]) > 0 and float(ld["kw"]) > 0
    except (KeyError, TypeError, ValueError):
        return False


def get_plan(state: dict, today: str) -> list[dict]:
    """Today's planned loads; [] when none, or when the stored value is not usable."""
    raw = state.get(_KEY + today)
    if not isinstance(raw, list):
        return []
    return [{"name": str(ld["name"]), "start": float(ld["start"]), "hours": float(ld["hours"]), "kw": float(ld["kw"])}
            for ld in raw if _valid(ld)]


def set_plan(state: dict, today: str, loads: list[dict]) -> list[dict]:
    """Add `loads` to today's plan. A load for an appliance already planned replaces it. Returns the plan."""
    merged = {ld["name"]: ld for ld in get_plan(state, today)}
    for ld in loads:
        if _valid(ld):
            merged[str(ld["name"])] = {"name": str(ld["name"]), "start": float(ld["start"]),
                                       "hours": float(ld["hours"]), "kw": float(ld["kw"])}
    state[_KEY + today] = sorted(merged.values(), key=lambda ld: ld["start"])
    return state[_KEY + today]


def clear_plan(state: dict, today: str) -> None:
    state.pop(_KEY + today, None)


def set_none(state: dict, today: str) -> None:
    """You said there are no big loads today: an empty plan that still counts as an answer."""
    state[_KEY + today] = []


def declared(state: dict, today: str) -> bool:
    """True once you have answered for today, with loads (set_plan) or with none (set_none)."""
    return isinstance(state.get(_KEY + today), list)


def evening_kwh(loads: list[dict], after_hour: float = EVENING_START_HOUR) -> float:
    """kWh the planned loads draw from `after_hour` to midnight."""
    total = 0.0
    for ld in loads:
        start, end = ld["start"], min(24.0, ld["start"] + ld["hours"])
        total += ld["kw"] * max(0.0, end - max(start, after_hour))
    return total


def _clock(hour: float) -> tuple[str, str]:
    total = int(round(hour * 60)) % (24 * 60)
    h, m = divmod(total, 60)
    label = f"{(h % 12) or 12}" + (f":{m:02d}" if m else "")
    return label, "AM" if h < 12 else "PM"


def clock(hour: float) -> str:
    """A clock hour as "3 PM" or "3:30 PM"."""
    label, mer = _clock(hour)
    return f"{label} {mer}"


def span(load: dict) -> str:
    """When a load runs: "4–5 PM", or "11 AM–1 PM" across noon."""
    (a, a_mer), (b, b_mer) = _clock(load["start"]), _clock(load["start"] + load["hours"])
    return f"{a if a_mer == b_mer else f'{a} {a_mer}'}–{b} {b_mer}"


def describe(loads: list[dict]) -> str:
    """"dryer 4–5 PM (4 kW), oven 5–7 PM (2.5 kW)"."""
    return ", ".join(f"{ld['name']} {span(ld)} ({ld['kw']:g} kW)" for ld in loads)
