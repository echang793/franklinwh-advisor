"""Learning which appliances you run, and when, from the answers to "what was that?".

A big steady draw is remembered (its power and length) when the advisor asks about it. When you
say it was the dryer or the oven, that becomes an event. A load you run on most of a weekday's
recent occurrences becomes a habit, in the same shape as Config.habit_loads, so /ebplan can count
it without you typing the schedule in.
"""

import statistics
from datetime import date, timedelta

NAMES = {"d": "dryer", "o": "oven", "w": "dishwasher", "x": "other"}
_LABELS = {"d": "👕 Dryer", "o": "🍳 Oven", "w": "🍽 Dishwasher", "x": "❔ Something else"}
LEARNABLE = ("dryer", "oven", "dishwasher")

LOOKBACK_DAYS = 56
MIN_COUNT = 3                # uses on one weekday before it counts as a habit
MIN_SHARE = 0.4              # ... and at least this share of that weekday's occurrences
MAX_FACTS = 40
MAX_EVENTS = 300


def choices() -> list[tuple[str, str]]:
    """(code, button text) for each answer to "which appliance?"."""
    return list(_LABELS.items())


def buttons(key: str) -> list[dict]:
    """Answers to "which appliance?" for the spike starting at `key` (callback apl:<key>:<code>)."""
    return [{"text": text, "callback_data": f"apl:{key}:{code}"} for code, text in _LABELS.items()]


def remember_spike(state: dict, key: str, kw: float, minutes: float) -> None:
    """Keep a spike's power and length until it is labelled (the callback only carries its start)."""
    facts = state.get("spike_facts", {})
    facts[key] = {"kw": round(float(kw), 2), "minutes": int(round(minutes))}
    state["spike_facts"] = dict(list(facts.items())[-MAX_FACTS:])


def record_event(state: dict, key: str, code: str) -> str | None:
    """The answer to "which appliance?": the appliance's name, or None when the code is unknown
    or the spike is no longer remembered. A later answer for the same spike replaces the first."""
    name = NAMES.get(code)
    fact = state.get("spike_facts", {}).get(key)
    if name is None or fact is None:
        return None
    events = [e for e in state.get("appliance_events", []) if e.get("key") != key]
    events.append({"key": key, "name": name, "kw": fact["kw"], "minutes": fact["minutes"]})
    state["appliance_events"] = events[-MAX_EVENTS:]
    return name


def _merge_day(parts: list[dict]) -> dict:
    """One run of an appliance on one day: the earliest start, the minutes added up, the power
    averaged by minutes (an oven's preheat and bake are two spikes)."""
    parts = sorted(parts, key=lambda e: e["key"])
    minutes = sum(p["minutes"] for p in parts) or 1
    return {"start": int(parts[0]["key"][11:13]) * 60 + int(parts[0]["key"][14:16]), "minutes": minutes,
            "kw": sum(p["kw"] * p["minutes"] for p in parts) / minutes}


def learned_habit_loads(events: list[dict], today: date, lookback_days: int = LOOKBACK_DAYS,
                        min_count: int = MIN_COUNT, min_share: float = MIN_SHARE) -> dict[str, list]:
    """The loads you run on a given weekday, as {"sunday": [[name, "HH:MM", hours, kW], ...]}.

    An appliance is a habit on a weekday when you ran it on at least `min_count` of them and on
    `min_share` of that weekday's occurrences since the first answer (or the lookback, if shorter).
    Times, lengths and power are the middle of what you did."""
    window_start = today - timedelta(days=lookback_days - 1)
    days: dict[tuple[date, str], list[dict]] = {}
    for e in events:
        try:
            d = date.fromisoformat(e["key"][:10])
        except (KeyError, ValueError):
            continue
        if window_start <= d <= today and e.get("name") in LEARNABLE:
            days.setdefault((d, e["name"]), []).append(e)
    if not days:
        return {}
    first = max(window_start, min(d for d, _n in days))
    occurrences = [first + timedelta(days=i) for i in range((today - first).days + 1)]
    out: dict[str, list] = {}
    for weekday in range(7):
        n_days = sum(1 for d in occurrences if d.weekday() == weekday) or 1
        for name in LEARNABLE:
            runs = [_merge_day(p) for (d, n), p in days.items() if n == name and d.weekday() == weekday]
            if len(runs) < min_count or len(runs) / n_days < min_share:
                continue
            start = round(statistics.median(r["start"] for r in runs))
            out.setdefault(date(2000, 1, 3 + weekday).strftime("%A").lower(), []).append(
                [name, f"{start // 60:02d}:{start % 60:02d}",
                 round(statistics.median(r["minutes"] for r in runs) / 60.0, 2),
                 round(statistics.median(r["kw"] for r in runs), 1)])
    return out
