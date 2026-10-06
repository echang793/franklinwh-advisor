"""Summaries of the poll-timing data (history.poll_timing) for `account latency`.

Pure functions over (timestamp_iso, duration_s, ok, error) rows so they test
without a database. Durations are summarized over *successful* polls only; a
failed poll's duration is how long it took to fail, which would blur "the API
is slow" with "the API is down", so failures are counted separately.
"""

from __future__ import annotations

from datetime import datetime


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile (p in 0-100) of a non-empty list."""
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1))))
    return ordered[idx]


def summarize(rows) -> dict:
    ok_d = [r[1] for r in rows if r[2]]
    out = {"n": len(rows), "failures": sum(1 for r in rows if not r[2]),
           "median_s": None, "p95_s": None, "max_s": None}
    if ok_d:
        srt = sorted(ok_d)
        mid = len(srt) // 2
        out["median_s"] = srt[mid] if len(srt) % 2 else (srt[mid - 1] + srt[mid]) / 2
        out["p95_s"] = percentile(ok_d, 95)
        out["max_s"] = max(ok_d)
    return out


def by_hour(rows) -> dict[int, dict]:
    buckets: dict[int, list] = {}
    for r in rows:
        buckets.setdefault(datetime.fromisoformat(r[0]).hour, []).append(r)
    return {h: summarize(v) for h, v in sorted(buckets.items())}


def compare_events(rows, events) -> tuple[dict, dict]:
    """(polls inside the event windows, polls in the same clock hours on days
    with no event). Comparing like hours keeps the evening-peak effect out of it."""
    windows = [(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in events]
    event_days = {w[0].date() for w in windows}
    spans = [(w[0].time(), w[1].time()) for w in windows]
    inside, outside = [], []
    for r in rows:
        t = datetime.fromisoformat(r[0])
        if any(a <= t <= b for a, b in windows):
            inside.append(r)
        elif t.date() not in event_days and any(lo <= t.time() <= hi for lo, hi in spans):
            outside.append(r)
    return summarize(inside), summarize(outside)
