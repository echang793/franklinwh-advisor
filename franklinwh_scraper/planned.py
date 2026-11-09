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


def describe(loads: list[dict]) -> str:
    """"dryer 4–5 PM (4 kW), oven 5–7 PM (2.5 kW)"."""
    parts = []
    for ld in loads:
        (a, a_mer), (b, b_mer) = _clock(ld["start"]), _clock(ld["start"] + ld["hours"])
        start = a if a_mer == b_mer else f"{a} {a_mer}"
        parts.append(f"{ld['name']} {start}–{b} {b_mer} ({ld['kw']:g} kW)")
    return ", ".join(parts)
