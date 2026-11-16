"""What the water heater would have cost in another mode, for the same hot water.

Read-only: nothing here changes the heater. EcoNet reports the heater's own hourly kWh
(collected daily into output/wh_energy.json by wh_controller.py). The heater runs heat pump
only, so the heat it delivered is that kWh x the heat pump's COP. Another mode delivers the same
heat partly from the 1:1 resistance element:

    kWh in mode = heat x (1 - share) / COP + heat x share = actual kWh x ((1 - share) + share x COP)

Each hour's extra kWh is replayed through that day's real solar and battery readings at the real
time-of-use and export rates (besttime.simulate), so the dollars are what the bill would have
moved, not kWh x a rate. A day whose readings have gaps is priced at the import rate instead.

Assumptions, all in Config: COP 3.0 (conservative next to Rheem's rated ~3.7+, so the "you saved"
figures are on the low side); Energy Saver ~10% and High Demand ~40% of heat from the element
(Rheem doesn't publish these; they vary with demand and air temperature).
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from . import besttime, tou

ENERGY_FILE = "wh_energy.json"
MODES = [("energy_saver", "Energy Saver"), ("high_demand", "High Demand"), ("electric", "Electric")]
MIN_DAYS_FOR_ALERT = 7
_STATE_KEY = "wh_mode_report_date"
_DEFAULTS = {"wh_heat_pump_cop": 3.0, "wh_energy_saver_element_share": 0.10,
             "wh_high_demand_element_share": 0.40}


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def alt_factor(cop: float, element_share: float) -> float:
    """kWh in the other mode per kWh actually used in heat pump only mode."""
    s = min(1.0, max(0.0, element_share))
    return (1.0 - s) + s * max(1.0, cop)


# ── the energy file ──────────────────────────────────────────────────────────────────────────────

def load_energy(outdir: Path) -> dict[str, dict[int, float]]:
    """{"YYYY-MM-DD": {hour: kWh}}; {} when missing or unreadable."""
    try:
        raw = json.loads((Path(outdir) / ENERGY_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict[int, float]] = {}
    for day, hours in (raw.items() if isinstance(raw, dict) else []):
        if not isinstance(hours, dict):
            continue
        clean = {}
        for h, v in hours.items():
            try:
                hh = int(h)
            except (TypeError, ValueError):
                continue
            f = _finite(v)
            if f is not None and f >= 0 and 0 <= hh <= 23:
                clean[hh] = f
        out[day] = clean
    return out


def save_day(outdir: Path, day: date, hourly: dict[int, float]) -> None:
    """Store one day's hourly kWh (NaN, negatives and bad hours dropped), atomically."""
    data = {d: {str(h): v for h, v in hrs.items()} for d, hrs in load_energy(outdir).items()}
    data[day.isoformat()] = {str(int(h)): f for h, v in hourly.items()
                             if (f := _finite(v)) is not None and f >= 0 and 0 <= int(h) <= 23}
    path = Path(outdir) / ENERGY_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True))
    os.replace(tmp, path)


# ── pricing ──────────────────────────────────────────────────────────────────────────────────────

def day_delta_cost(readings: list, day: date, soc_start_pct: float | None, cap: float, reserve_pct: float,
                   hourly: dict[int, float], factor: float, rate_at: Callable[[datetime], float],
                   export_rate_at: Callable[[datetime], float]) -> tuple[float, float, bool]:
    """(extra kWh, extra $, replayed) for one day in a mode `factor` x the heat pump's kWh.
    replayed is False when the readings had gaps and the extra was priced at the import rate."""
    delta = {h: kwh * (factor - 1.0) for h, kwh in hourly.items() if kwh > 0}
    total = sum(delta.values())
    if total <= 0:
        return 0.0, 0.0, True
    t0 = datetime.combine(day, time(0))
    until = t0 + timedelta(hours=24 + besttime.MORNING_HOUR)
    if readings and soc_start_pct is not None and besttime._complete(readings, t0, until):
        hours = besttime._hourly(readings)
        soc, floor = soc_start_pct / 100.0 * cap, reserve_pct / 100.0 * cap

        def run(extra):
            return besttime.simulate(hours, t0, soc, cap, floor, extra, rate_at, export_rate_at, until)

        base = run(lambda t: 0.0)
        r = run(lambda t: delta.get(t.hour, 0.0) if t.date() == day else 0.0)
        cost = (r.import_cost - base.import_cost) + (base.export_credit - r.export_credit)
        return total, _finite(cost) or 0.0, True
    cost = sum(v * rate_at(t0 + timedelta(hours=h)) for h, v in delta.items())
    return total, _finite(cost) or 0.0, False


@dataclass
class ModeCost:
    label: str
    element_share: float
    factor: float
    extra_kwh: float = 0.0
    extra_cost: float = 0.0


@dataclass
class ModeReport:
    start: date
    end: date
    days_in_period: int
    days_with_data: int
    actual_kwh: float
    cop: float
    days_grid_priced: int = 0
    modes: dict[str, ModeCost] = field(default_factory=dict)


def _cfg(cfg: Any, name: str) -> float:
    v = _finite(getattr(cfg, name, _DEFAULTS[name]))
    return _DEFAULTS[name] if v is None else v


def cycle_report(store: Any, outdir: Path, cfg: Any, start: date, end: date,
                 rate_at: Callable[[datetime], float] | None = None,
                 export_rate_at: Callable[[datetime], float] | None = None) -> ModeReport | None:
    """What each other mode would have added from `start` to `end`; None without energy data."""
    rate_at, export_rate_at = rate_at or tou.rate_at, export_rate_at or tou.export_rate_at
    energy = load_energy(outdir)
    n_days = (end - start).days + 1
    days = [start + timedelta(days=i) for i in range(max(0, n_days))
            if (start + timedelta(days=i)).isoformat() in energy]
    if not days:
        return None
    cop = max(1.0, _cfg(cfg, "wh_heat_pump_cop"))
    shares = {"energy_saver": _cfg(cfg, "wh_energy_saver_element_share"),
              "high_demand": _cfg(cfg, "wh_high_demand_element_share"), "electric": 1.0}
    cap = float(getattr(cfg, "battery_capacity_kwh", 13.6) or 13.6)
    reserve = float(getattr(cfg, "battery_reserve_pct", 10.0) or 10.0)
    rep = ModeReport(start=start, end=end, days_in_period=n_days, days_with_data=len(days),
                     actual_kwh=0.0, cop=cop,
                     modes={m: ModeCost(label, shares[m], alt_factor(cop, shares[m])) for m, label in MODES})
    for d in days:
        hourly = energy[d.isoformat()]
        rep.actual_kwh += sum(hourly.values())
        t0 = datetime.combine(d, time(0))
        try:
            readings = store.readings_between(
                t0.isoformat(), (t0 + timedelta(hours=24 + besttime.MORNING_HOUR)).isoformat()) if store else []
            soc = store.soc_near(t0.isoformat()) if store else None
        except Exception:  # noqa: BLE001  (a locked db falls back to grid-rate pricing)
            readings, soc = [], None
        grid_priced = False
        for mc in rep.modes.values():
            kwh, cost, replayed = day_delta_cost(readings, d, soc, cap, reserve, hourly, mc.factor,
                                                 rate_at, export_rate_at)
            mc.extra_kwh += kwh
            mc.extra_cost += cost
            grid_priced = grid_priced or not replayed
        rep.days_grid_priced += int(grid_priced)
    return rep


# ── the message ──────────────────────────────────────────────────────────────────────────────────

def describe_report(rep: ModeReport) -> str:
    lines = [f"🚿 Water heater: what other modes would have cost, {rep.start:%b %-d} – {rep.end:%b %-d} "
             f"({rep.days_with_data} of {rep.days_in_period} days of EcoNet data)",
             f"Heat pump only (yours): {rep.actual_kwh:.1f} kWh"]
    for mc in rep.modes.values():
        lines.append(f"{mc.label}: +{mc.extra_kwh:.1f} kWh, ${mc.extra_cost:.2f} more")
    shares = {k: round(m.element_share * 100) for k, m in rep.modes.items()}
    lines.append(f"Priced through your real solar, battery and time-of-use rates. Assumes COP {rep.cop:g} for "
                 f"the heat pump, and that Energy Saver gets ~{shares['energy_saver']}% and High Demand "
                 f"~{shares['high_demand']}% of its heat from the element (estimates; set wh_heat_pump_cop "
                 f"and wh_*_element_share in the config).")
    if rep.days_grid_priced:
        lines.append(f"{rep.days_grid_priced} day(s) had gaps in the readings and were priced at the grid rate.")
    return "\n".join(lines)


def maybe_alert(state: dict, today: str, now: datetime, store: Any, cfg: Any,
                rate_at: Callable[[datetime], float] | None = None,
                export_rate_at: Callable[[datetime], float] | None = None) -> str | None:
    """Last evening of the billing cycle, once: the cycle's mode comparison."""
    if now.hour not in (21, 22):
        return None
    start, end = tou.cycle_bounds(now.date(), getattr(cfg, "billing_cycle_start_day", 20))
    if now.date() != end or state.get(_STATE_KEY) == today:
        return None
    rep = cycle_report(store, Path(getattr(cfg, "output_dir", "output") or "output"), cfg, start, end,
                       rate_at, export_rate_at)
    if rep is None or rep.days_with_data < MIN_DAYS_FOR_ALERT:
        return None
    state[_STATE_KEY] = today
    return describe_report(rep)
