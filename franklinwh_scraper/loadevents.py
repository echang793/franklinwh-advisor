"""The big loads you run, learned from the load history by weekday (dryer, oven, and so on).

Averaging a weekday's load smears a dryer that runs on half the Sundays into a steady 2 kW all
afternoon. This finds the individual blocks instead, groups the ones that start at about the same
time on the same weekday, and keeps how often each shows up: "a 4 kW hour at 3 PM on 4 of 8
Sundays". It does not name the appliance, only the load, which is what the battery cares about.

A block is a run of 5-minute readings at BLOCK_KW or more (dips of up to 30 minutes bridged, as an
oven cycles), 15 to 150 minutes long and under MAX_KW. Longer or bigger is a car session, which
evpattern.py already models.
"""

import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta

WINDOW = (12, 23)               # the afternoon and evening hours that matter for the battery
LOOKBACK_DAYS = 84
BLOCK_KW = 1.8                  # a reading at or above this belongs to a block
BRIDGE_MIN = 10                 # readings closer than this are one run
MERGE_GAP_MIN = 30              # runs closer than this are one block (an oven cycling)
MIN_MINUTES = 15
MAX_MINUTES = 150
MAX_KW = 6.5
MIN_EXTRA_KW = 1.3              # what it adds over the quiet house
MIN_COVERAGE = 100              # readings in the window for a day to count (132 when complete)
CLUSTER_HOURS = 1.0             # blocks starting this close are the same load
MIN_DAYS = 3                    # occurrences before it is a habit
MIN_SHARE = 0.3                 # ... on at least this share of that weekday's days
USUAL_SHARE = 0.6               # what /ebplan treats as "your usual" with nothing else to go on
RECENT_DAYS = 28                # the last month counts double, so a changed routine shows within weeks
RECENT_WEIGHT = 2.0
QUESTION_SHARE = 0.5            # a routine this regular is worth asking about, once
QUESTION_DAYS = 4


@dataclass
class LoadEvent:
    weekday: int                # Monday = 0
    start: float                # clock hour, median start
    hours: float
    kw: float                   # added over the quiet house
    share: float                # of this weekday's recorded days (the last month counts double)
    days: int
    of: int

    def as_load(self, name: str = "usual load") -> dict:
        return {"name": name, "start": self.start, "hours": self.hours, "kw": self.kw}


def pattern_key(ev: LoadEvent) -> str:
    """"6:15": weekday and start hour, how an answer to "what is it?" is filed."""
    return f"{ev.weekday}:{int(round(ev.start)) % 24}"


def name_for(names: dict | None, ev: LoadEvent) -> str:
    """What you called this routine ("dryer"), else "usual load"."""
    return (names or {}).get(pattern_key(ev)) or "usual load"


def day_blocks(readings: list[tuple[str, float]]) -> list[dict]:
    """One day's big blocks as {start (clock hour), minutes, kw (added over the quiet house)}."""
    parsed = []
    for ts, kw in readings:
        try:
            parsed.append((datetime.fromisoformat(ts), float(kw)))
        except (TypeError, ValueError):
            continue
    if not parsed:
        return []
    quiet = [kw for _t, kw in parsed if kw < 1.0]
    baseline = statistics.median(quiet) if quiet else 0.4
    runs: list[dict] = []
    for t, kw in parsed:
        if kw < BLOCK_KW:
            continue
        if runs and (t - runs[-1]["end"]).total_seconds() / 60.0 <= BRIDGE_MIN:
            runs[-1]["end"] = t
            runs[-1]["kws"].append(kw)
        else:
            runs.append({"start": t, "end": t, "kws": [kw]})
    merged: list[dict] = []
    for r in runs:
        if merged and (r["start"] - merged[-1]["end"]).total_seconds() / 60.0 <= MERGE_GAP_MIN:
            merged[-1]["end"] = r["end"]
            merged[-1]["kws"].extend(r["kws"])
        else:
            merged.append(r)
    out = []
    for b in merged:
        minutes = (b["end"] - b["start"]).total_seconds() / 60.0 + 5.0
        kw = statistics.mean(b["kws"])
        if not MIN_MINUTES <= minutes <= MAX_MINUTES or kw > MAX_KW or kw - baseline < MIN_EXTRA_KW:
            continue
        out.append({"start": b["start"].hour + b["start"].minute / 60.0, "minutes": round(minutes),
                    "kw": kw - baseline})
    return out


def learn(store, today: date, car_days: dict | None = None,
          lookback: int = LOOKBACK_DAYS) -> dict[int, list[LoadEvent]]:
    """{weekday: [LoadEvent, ...]} from the last `lookback` days before `today`, in start order.
    Days labelled as car days (car_days {"YYYY-MM-DD": True}) and days with holes in the window are
    left out entirely. {} with no store or on any error: this only adds detail, it never blocks."""
    if store is None:
        return {}
    try:
        raw = store.daily_afternoon_readings((today - timedelta(days=lookback)).isoformat(), today.isoformat(),
                                             WINDOW[0], WINDOW[1])
    except Exception:
        return {}
    car_days = car_days or {}
    of: dict[int, int] = {}
    of_weight: dict[int, float] = {}
    weight_of: dict[str, float] = {}
    blocks: dict[int, list[tuple[str, dict]]] = {}
    for day, readings in raw.items():
        if car_days.get(day) is True or len(readings) < MIN_COVERAGE:
            continue
        try:
            wd = date.fromisoformat(day).weekday()
        except ValueError:
            continue
        w = RECENT_WEIGHT if (today - date.fromisoformat(day)).days <= RECENT_DAYS else 1.0
        weight_of[day] = w
        of[wd] = of.get(wd, 0) + 1
        of_weight[wd] = of_weight.get(wd, 0.0) + w
        for b in day_blocks(readings):
            blocks.setdefault(wd, []).append((day, b))
    out: dict[int, list[LoadEvent]] = {}
    for wd, items in blocks.items():
        items.sort(key=lambda it: it[1]["start"])
        clusters: list[list[tuple[str, dict]]] = []
        for it in items:
            if clusters and it[1]["start"] - clusters[-1][-1][1]["start"] <= CLUSTER_HOURS:
                clusters[-1].append(it)
            else:
                clusters.append([it])
        for cl in clusters:
            days = {d for d, _b in cl}
            share = sum(weight_of[d] for d in days) / of_weight[wd]
            if len(days) < MIN_DAYS or share < MIN_SHARE:
                continue
            out.setdefault(wd, []).append(LoadEvent(
                weekday=wd, start=statistics.median(b["start"] for _d, b in cl),
                hours=statistics.median(b["minutes"] for _d, b in cl) / 60.0,
                kw=statistics.median(b["kw"] for _d, b in cl), share=share, days=len(days), of=of[wd]))
    return out
