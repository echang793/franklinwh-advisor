"""Battery mode recommendation engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .account import Stats
from .predictor import UsageForecast
from .tou import cheap_charge_deadline, on_peak_window, period_at, rate_at
from .weather import SolarOutlook


class Mode(str, Enum):
    SELF_CONSUMPTION = "self_consumption"
    EMERGENCY_BACKUP = "emergency_backup"
    TIME_OF_USE = "time_of_use"
    NO_CHANGE = "no_change"


@dataclass
class Recommendation:
    mode: Mode
    reason: str
    urgency: str  # "info" | "warning" | "critical"
    details: dict

    @property
    def needs_action(self) -> bool:
        return self.mode != Mode.NO_CHANGE


# ── Static thresholds (fallback when no usage history) ────────────────
_SOC_CRITICAL        = 15.0   # % — Emergency Backup regardless
_SOC_LOW             = 30.0   # % — Emergency Backup if solar also poor
_SOC_HEALTHY         = 50.0   # % — above this, Self-Consumption is fine
_GHI_POOR_6H         = 80.0   # W/m² — bad solar next 6h
_GHI_POOR_24H        = 60.0   # W/m² — bad solar day ahead
_GHI_GOOD_6H         = 200.0  # W/m² — good solar coming
_GHI_GOOD_24H        = 150.0  # W/m²
_DEMAND_HIGH_KWH     = 8.0    # kWh in forecast window = high usage (fallback only)
_DEMAND_DEFICIT_KWH  = -5.0   # kWh net deficit threshold (fallback only)

# ── TOU-aware charging constants ─────────────────────────────────────
_EB_CHARGE_KW  = 5.0   # conservative FranklinWH AC charge rate in EB mode
_EB_SOC_BUFFER = 0.10  # extra 10% capacity buffer beyond projected peak draw


def _tou_eb_plan(
    now: datetime,
    soc: float,
    capacity_kwh: float,
    forecast: UsageForecast | None,
    charge_kw: float = _EB_CHARGE_KW,
) -> dict:
    """
    Project battery SoC through today's 4–9 pm on-peak window using the
    usage+solar forecast, then compute whether EB charging is needed and
    for how long (targeting the cheapest Super Off-Peak window before 2 pm).
    """
    peak_start, peak_end = on_peak_window(now)
    current_kwh    = soc / 100.0 * capacity_kwh
    current_rate   = rate_at(now)
    current_period = period_at(now).value.replace("_", " ")

    # ── Simulate battery from now → 4 pm ────────────────────────────
    solar_until_peak = 0.0
    load_until_peak  = 0.0
    if forecast and forecast.confidence != "none":
        for h in forecast.hours:
            if now <= h.dt < peak_start:
                solar_until_peak += max(0.0, h.predicted_solar_kw)
                load_until_peak  += max(0.0, h.predicted_load_kw)

    kwh_at_peak = max(0.0, min(capacity_kwh,
                               current_kwh + solar_until_peak - load_until_peak))
    soc_at_peak = kwh_at_peak / capacity_kwh * 100.0

    # ── Forecast energy needed during 4–9 pm ────────────────────────
    if forecast and forecast.confidence != "none":
        peak_load  = sum(max(0.0, h.predicted_load_kw)
                         for h in forecast.hours if peak_start <= h.dt < peak_end)
        peak_solar = sum(max(0.0, h.predicted_solar_kw)
                         for h in forecast.hours if peak_start <= h.dt < peak_end)
    else:
        peak_load  = 4.0   # rough default: ~0.8 kW avg × 5 h
        peak_solar = 0.0

    net_peak_draw = max(0.0, peak_load - peak_solar)
    target_kwh    = min(capacity_kwh, net_peak_draw + _EB_SOC_BUFFER * capacity_kwh)
    shortfall_kwh = max(0.0, target_kwh - kwh_at_peak)
    eb_needed     = shortfall_kwh > 0.0

    # ── Compute charge window ────────────────────────────────────────
    run_hours              = shortfall_kwh / charge_kw if charge_kw > 0 else 0.0
    deadline               = cheap_charge_deadline(now)
    run_until: datetime | None = None

    if eb_needed:
        if deadline is not None:
            run_until = min(deadline, now + timedelta(hours=run_hours))
        elif now < peak_start:
            run_until = min(peak_start, now + timedelta(hours=run_hours))
        else:
            run_until = now + timedelta(hours=run_hours)

    # ── Build hint string ────────────────────────────────────────────
    if now >= peak_start:
        hint = (
            f"Grid import is peak-priced (${current_rate:.3f}/kWh) — "
            "run EB only if critical."
        )
    elif not eb_needed:
        hint = (
            f"Battery projects to cover peak without EB "
            f"(est. {soc_at_peak:.0f}% at 4 pm, need ~{net_peak_draw:.1f} kWh)."
        )
    elif deadline is not None and run_until is not None:
        if run_until >= deadline:
            # Can't fully charge before 2 pm — run until cutoff
            mins_left = (deadline - now).total_seconds() / 60
            soc_at_cutoff = min(100.0,
                (kwh_at_peak + charge_kw * (deadline - now).total_seconds() / 3600)
                / capacity_kwh * 100.0)
            hint = (
                f"Run EB until 2 pm ({mins_left:.0f} min) — "
                f"est. SoC at 4 pm: ~{soc_at_cutoff:.0f}%. "
                "Switch back at 2 pm to avoid peak costs."
            )
        else:
            until_str = run_until.strftime("%-I:%M %p")
            hint = (
                f"Est. ~{run_hours:.1f}h to charge (until ~{until_str}). "
                "Switch back before 2 pm to avoid peak costs."
            )
    elif now < peak_start and run_until is not None:
        until_str = run_until.strftime("%-I:%M %p")
        hint = (
            f"Run EB until ~{until_str} (~{run_hours:.1f}h). "
            f"Off-peak rate: ${current_rate:.3f}/kWh — switch back before 4 pm on-peak."
        )
    else:
        hint = f"Run EB for ~{run_hours:.1f}h. Current rate: ${current_rate:.3f}/kWh."

    return {
        "eb_needed":               eb_needed,
        "soc_at_4pm":              round(soc_at_peak, 1),
        "net_peak_draw":           round(net_peak_draw, 2),
        "shortfall_kwh":           round(shortfall_kwh, 2),
        "run_hours":               round(run_hours, 1),
        "run_until":               run_until,
        "current_period":          current_period,
        "current_rate":            round(current_rate, 5),
        "hint_str":                hint,
    }


def recommend(
    stats: Stats,
    outlook: SolarOutlook | None,
    forecast: UsageForecast | None = None,
    battery_capacity_kwh: float = 13.6,
) -> Recommendation:
    """Evaluate current state + weather + usage forecast → mode recommendation."""
    soc         = stats.current.battery_soc_pct
    home_kw     = stats.current.home_load_kw
    solar_kw    = stats.current.solar_production_kw
    grid_status = stats.current.grid_status

    ghi_6h   = outlook.avg_ghi(6)    if outlook else 0.0
    ghi_24h  = outlook.avg_ghi(24)   if outlook else 0.0
    cloud_6h = outlook.avg_cloud_cover(6) if outlook else 100.0
    peak_ghi = outlook.peak_ghi_today()   if outlook else 0.0

    details: dict = {
        "soc_pct":            soc,
        "home_load_kw":       home_kw,
        "solar_kw":           solar_kw,
        "ghi_next_6h_wm2":    round(ghi_6h, 1),
        "ghi_next_24h_wm2":   round(ghi_24h, 1),
        "cloud_cover_6h_pct": round(cloud_6h, 1),
        "peak_ghi_today_wm2": round(peak_ghi, 1),
    }

    predicted_load_kwh    = None
    predicted_deficit_kwh = None
    if forecast and forecast.confidence != "none":
        predicted_load_kwh    = forecast.total_load_kwh
        predicted_deficit_kwh = forecast.net_kwh
        details.update({
            "predicted_load_kwh":        round(predicted_load_kwh, 2),
            "predicted_solar_kwh":       round(forecast.total_solar_kwh, 2),
            "predicted_net_kwh":         round(predicted_deficit_kwh, 2),
            "predicted_peak_load_kw":    forecast.peak_load_kw,
            "forecast_confidence":       forecast.confidence,
            "history_days":              forecast.data_days,
        })

    # ── CRITICAL: grid down ──────────────────────────────────────────
    if grid_status != "normal":
        return Recommendation(
            mode=Mode.EMERGENCY_BACKUP,
            reason=f"Grid is {grid_status}. Conserve battery reserves.",
            urgency="critical",
            details=details,
        )

    # Compute TOU plan once — appended to all EB recommendations below
    now  = datetime.now()
    plan = _tou_eb_plan(now, soc, battery_capacity_kwh, forecast)
    details.update({
        "tou_period":              plan["current_period"],
        "tou_rate_per_kwh":        plan["current_rate"],
        "projected_soc_4pm_pct":   plan["soc_at_4pm"],
        "projected_peak_draw_kwh": plan["net_peak_draw"],
    })

    # ── CRITICAL: battery critically low ────────────────────────────
    if soc < _SOC_CRITICAL:
        return Recommendation(
            mode=Mode.EMERGENCY_BACKUP,
            reason=(
                f"Battery critically low ({soc:.0f}% SoC). "
                "Switch to Emergency Backup to protect reserves.\n"
                + plan["hint_str"]
            ),
            urgency="critical",
            details=details,
        )

    # ── WARNING: TOU-aware EB decision (when usage history available) ─
    if forecast and forecast.confidence != "none":
        if plan["eb_needed"] and soc < _SOC_HEALTHY:
            return Recommendation(
                mode=Mode.EMERGENCY_BACKUP,
                reason=(
                    f"Battery projects to reach 4 pm on-peak at {plan['soc_at_4pm']:.0f}% SoC — "
                    f"~{plan['shortfall_kwh']:.1f} kWh short of covering tonight's "
                    f"~{plan['net_peak_draw']:.1f} kWh peak draw.\n"
                    + plan["hint_str"]
                ),
                urgency="warning",
                details=details,
            )
        # Forecast shows battery sufficient — fall through to self-consumption / no-change

    else:
        # No reliable history — fall back to static threshold logic

        # ── WARNING: usage pattern predicts large deficit + low SoC ─────
        if (
            predicted_deficit_kwh is not None
            and predicted_deficit_kwh < _DEMAND_DEFICIT_KWH
            and soc < _SOC_HEALTHY
        ):
            battery_kwh = soc / 100 * battery_capacity_kwh
            shortfall   = abs(predicted_deficit_kwh) - battery_kwh
            return Recommendation(
                mode=Mode.EMERGENCY_BACKUP,
                reason=(
                    f"Usage patterns predict a {abs(predicted_deficit_kwh):.1f} kWh net draw "
                    f"but battery only holds ~{battery_kwh:.1f} kWh at {soc:.0f}% SoC. "
                    + (f"Estimated shortfall: {shortfall:.1f} kWh. " if shortfall > 0 else "")
                    + "Emergency Backup will top up reserves now.\n"
                    + plan["hint_str"]
                ),
                urgency="warning",
                details=details,
            )

        # ── WARNING: predicted high load + poor solar ────────────────────
        if (
            predicted_load_kwh is not None
            and predicted_load_kwh > _DEMAND_HIGH_KWH
            and ghi_6h < _GHI_POOR_6H
            and soc < _SOC_HEALTHY
        ):
            return Recommendation(
                mode=Mode.EMERGENCY_BACKUP,
                reason=(
                    f"High usage predicted ({predicted_load_kwh:.1f} kWh) "
                    f"with poor solar (next 6h avg {ghi_6h:.0f} W/m²) "
                    f"and only {soc:.0f}% SoC. "
                    "Emergency Backup will build reserves before demand peaks.\n"
                    + plan["hint_str"]
                ),
                urgency="warning",
                details=details,
            )

        # ── WARNING: low SoC + poor solar forecast ───────────────────────
        if soc < _SOC_LOW and ghi_6h < _GHI_POOR_6H and ghi_24h < _GHI_POOR_24H:
            return Recommendation(
                mode=Mode.EMERGENCY_BACKUP,
                reason=(
                    f"Low SoC ({soc:.0f}%) with poor solar forecast "
                    f"(next 6h avg {ghi_6h:.0f} W/m², next 24h avg {ghi_24h:.0f} W/m²). "
                    "Emergency Backup will help preserve reserves.\n"
                    + plan["hint_str"]
                ),
                urgency="warning",
                details=details,
            )

        # ── WARNING: extended bad weather + SoC not topped up ───────────
        if ghi_24h < _GHI_POOR_24H and soc < _SOC_HEALTHY:
            return Recommendation(
                mode=Mode.EMERGENCY_BACKUP,
                reason=(
                    f"Poor solar forecast for next 24h (avg {ghi_24h:.0f} W/m²) "
                    f"and SoC only {soc:.0f}%. "
                    "Emergency Backup will charge from grid to build reserves.\n"
                    + plan["hint_str"]
                ),
                urgency="warning",
                details=details,
            )

    # ── INFO: usage pattern + solar predict comfortable surplus ──────
    if (
        predicted_deficit_kwh is not None
        and predicted_deficit_kwh >= 0
        and ghi_6h > _GHI_GOOD_6H
    ):
        return Recommendation(
            mode=Mode.SELF_CONSUMPTION,
            reason=(
                f"Patterns predict {predicted_deficit_kwh:.1f} kWh solar surplus "
                f"(next 6h irradiance {ghi_6h:.0f} W/m²). "
                "Self-Consumption will maximise self-use and minimise grid import."
            ),
            urgency="info",
            details=details,
        )

    # ── INFO: good solar coming, switch to Self-Consumption ──────────
    if ghi_6h > _GHI_GOOD_6H and ghi_24h > _GHI_GOOD_24H and soc >= 20.0:
        return Recommendation(
            mode=Mode.SELF_CONSUMPTION,
            reason=(
                f"Good solar ahead (next 6h avg {ghi_6h:.0f} W/m², "
                f"next 24h avg {ghi_24h:.0f} W/m²). "
                "Self-Consumption will maximise solar use and minimise grid import."
            ),
            urgency="info",
            details=details,
        )

    # ── No change needed ─────────────────────────────────────────────
    return Recommendation(
        mode=Mode.NO_CHANGE,
        reason=(
            f"Conditions look stable "
            f"(SoC {soc:.0f}%, next 6h solar {ghi_6h:.0f} W/m²"
            + (
                f", forecast load {predicted_load_kwh:.1f} kWh"
                if predicted_load_kwh is not None else ""
            )
            + "). No mode change needed."
        ),
        urgency="info",
        details=details,
    )
