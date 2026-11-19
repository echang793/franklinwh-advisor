"""Outage assistant: how long the battery lasts with the grid down and whether it reaches sunrise.

Pure numbers, no network and no clock: the caller passes the current state, an hourly forecast
(`predictor.HourPrediction`, only its `dt` and `predicted_solar_kw` are used) and the next sunrise.
The load is held at the current draw (or at "essentials" for the shed scenario) because in an outage
the house load is what you choose it to be; solar comes from the forecast, and nothing is assumed
past the end of the forecast. Same battery arithmetic as the sundown walk, in kWh.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

ESSENTIALS_SHARE = 0.4        # fridge, network, lights, a few outlets: ~40% of the current load
MIN_ESSENTIALS_KW = 0.2
MIN_LEARNED_KW = 0.3          # ignore appliances too small to matter
_STATIC_SHED = "AC, EV, dryer, pool pump"


@dataclass(frozen=True)
class OutageReport:
    soc_pct: float
    load_kw: float
    essentials_kw: float
    empty_at: datetime | None            # None = lasts through the whole forecast
    runtime_h: float | None
    survives: bool
    soc_at_sunrise: float | None         # % when the sun comes up (0 when it ran out first)
    makes_sunrise: bool | None           # None = no sunrise known
    shed_empty_at: datetime | None
    shed_runtime_h: float | None
    shed_soc_at_sunrise: float | None
    makes_sunrise_if_shed: bool | None
    sunrise: datetime | None
    biggest: list[tuple[str, float]] = field(default_factory=list)


def _simulate(soc_pct: float, cap_kwh: float, load_kw: float, hours: list, now: datetime,
              until: datetime | None, floor_pct: float) -> tuple[datetime | None, float | None]:
    """(time the battery hits the floor or None, % at `until` or None). Hourly slices; the first is
    cut at `now` and the one holding `until` is split there."""
    floor = max(0.0, floor_pct) / 100.0 * cap_kwh
    kwh = min(cap_kwh, max(floor, soc_pct / 100.0 * cap_kwh))
    soc_pct_at_until = None
    one_hour = timedelta(hours=1)
    for h in sorted(hours, key=lambda x: x.dt):
        seg_start, seg_end = max(h.dt, now), h.dt + one_hour
        if seg_end <= seg_start:
            continue
        solar = max(0.0, float(getattr(h, "predicted_solar_kw", 0.0) or 0.0))
        cuts = [seg_start, seg_end]
        if until is not None and seg_start < until < seg_end:
            cuts = [seg_start, until, seg_end]
        for a, b in zip(cuts, cuts[1:], strict=False):
            dur = (b - a).total_seconds() / 3600.0
            net = solar - load_kw
            if net < 0 and kwh + net * dur < floor:
                empty_at = a + timedelta(hours=max(0.0, (kwh - floor) / -net))
                if until is None:
                    return empty_at, None
                # Ran out after `until` (already measured), or before it (so it sits at the floor).
                return empty_at, (soc_pct_at_until if soc_pct_at_until is not None else floor / cap_kwh * 100.0)
            kwh = min(cap_kwh, kwh + net * dur)
            if until is not None and b == until:
                soc_pct_at_until = kwh / cap_kwh * 100.0
    if until is not None and soc_pct_at_until is None:
        soc_pct_at_until = kwh / cap_kwh * 100.0   # `until` is past the forecast: the level at its end
    return None, soc_pct_at_until


def learned_loads(events: list[dict], top: int = 3) -> list[tuple[str, float]]:
    """The biggest appliances you have actually run, as (name, typical kW) from the answers to
    "what was that?" — heaviest first. 'other' and sub-0.3 kW loads are dropped."""
    by_name: dict[str, list[float]] = {}
    for e in events or []:
        name, kw = e.get("name"), e.get("kw")
        if not name or name == "other" or not isinstance(kw, (int, float)) or not math.isfinite(kw):
            continue
        by_name.setdefault(name, []).append(float(kw))
    typical = [(n, round(statistics.median(v), 1)) for n, v in by_name.items()]
    return sorted([t for t in typical if t[1] >= MIN_LEARNED_KW], key=lambda t: -t[1])[:top]


def outage_report(soc_pct: float, cap_kwh: float, load_kw: float, hours: list, now: datetime,
                  sunrise: datetime | None = None, loads: list[tuple[str, float]] | None = None,
                  floor_pct: float = 0.0) -> OutageReport | None:
    """The report for a grid-down night, or None when the battery size is unknown."""
    if not cap_kwh or cap_kwh <= 0 or not math.isfinite(cap_kwh) or not math.isfinite(soc_pct):
        return None
    load = max(0.0, float(load_kw)) if math.isfinite(load_kw) else 0.0
    ess = round(max(MIN_ESSENTIALS_KW, load * ESSENTIALS_SHARE), 2)
    empty_at, soc_sr = _simulate(soc_pct, cap_kwh, load, hours, now, sunrise, floor_pct)
    s_empty, s_soc_sr = _simulate(soc_pct, cap_kwh, ess, hours, now, sunrise, floor_pct)

    def runtime(e):
        return None if e is None else max(0.0, (e - now).total_seconds() / 3600.0)

    def makes(e):
        return None if sunrise is None else (e is None or e >= sunrise)

    return OutageReport(
        soc_pct=soc_pct, load_kw=load, essentials_kw=ess,
        empty_at=empty_at, runtime_h=runtime(empty_at), survives=empty_at is None,
        soc_at_sunrise=soc_sr, makes_sunrise=makes(empty_at),
        shed_empty_at=s_empty, shed_runtime_h=runtime(s_empty), shed_soc_at_sunrise=s_soc_sr,
        makes_sunrise_if_shed=makes(s_empty), sunrise=sunrise, biggest=list(loads or []))


def fmt_duration(hours: float) -> str:
    m = int(round(hours * 60))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def _clock(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p")


def format_report(r: OutageReport, now: datetime, grid_down: bool) -> str:
    """Telegram HTML for the grid-down update (grid_down=True) or the planning answer (False)."""
    head = (f"⏱ <b>Backup runtime</b> at {now.strftime('%-I:%M %p')}" if grid_down
            else "🧪 <b>If the grid dropped now</b> (it's up, this is a what-if)")
    lines = [head, f"🔋 {r.soc_pct:.0f}% · load {r.load_kw:.1f} kW"]
    if r.survives:
        lines.append("At this load it lasts through the whole 24 h forecast.")
    else:
        lines.append(f"At this load: ~{fmt_duration(r.runtime_h)} (empty ~{_clock(r.empty_at)}).")
    if r.makes_sunrise is True:
        lines.append(f"✅ Reaches sunrise ({_clock(r.sunrise)}) with ~{r.soc_at_sunrise:.0f}% left.")
    elif r.makes_sunrise is False:
        if r.makes_sunrise_if_shed:
            lines.append(f"⚠️ Won't reach sunrise ({_clock(r.sunrise)}) at this load, but would on essentials "
                         f"only (~{r.essentials_kw:.1f} kW), with ~{r.shed_soc_at_sunrise:.0f}% left.")
        else:
            end = f", ~{_clock(r.shed_empty_at)}" if r.shed_empty_at else ""
            lines.append(f"🚨 Won't reach sunrise ({_clock(r.sunrise)}) even on essentials only "
                         f"(~{r.essentials_kw:.1f} kW{end}). Cut every load you can.")
    if r.biggest:
        big = ", ".join(f"{n} ~{kw:.1f} kW" for n, kw in r.biggest)
        lines.append(f"Biggest loads you run: {big}. Also {_STATIC_SHED.replace('dryer, ', '')}.")
    else:
        lines.append(f"Conserve: turn off {_STATIC_SHED}.")
    return "\n".join(lines)


def to_dict(r: OutageReport) -> dict:
    """JSON-safe form for the API (datetimes as ISO strings, no NaN)."""
    def f(x):
        return None if x is None or not math.isfinite(x) else round(float(x), 2)

    def t(x):
        return x.isoformat() if x else None

    return {"soc_pct": f(r.soc_pct), "load_kw": f(r.load_kw), "essentials_kw": f(r.essentials_kw),
            "empty_at": t(r.empty_at), "runtime_h": f(r.runtime_h), "survives": r.survives,
            "soc_at_sunrise": f(r.soc_at_sunrise), "makes_sunrise": r.makes_sunrise,
            "shed_empty_at": t(r.shed_empty_at), "shed_runtime_h": f(r.shed_runtime_h),
            "shed_soc_at_sunrise": f(r.shed_soc_at_sunrise), "makes_sunrise_if_shed": r.makes_sunrise_if_shed,
            "sunrise": t(r.sunrise), "biggest": [[n, f(kw)] for n, kw in r.biggest]}
