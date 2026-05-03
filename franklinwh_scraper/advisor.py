"""Battery mode recommendation engine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .account import Stats
from .predictor import UsageForecast
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


# ── Thresholds ────────────────────────────────────────────────────────
_SOC_CRITICAL   = 15.0   # % — Emergency Backup regardless
_SOC_LOW        = 30.0   # % — Emergency Backup if solar also poor
_SOC_HEALTHY    = 50.0   # % — above this, Self-Consumption is fine
_GHI_POOR_6H    = 80.0   # W/m² — bad solar next 6h
_GHI_POOR_24H   = 60.0   # W/m² — bad solar day ahead
_GHI_GOOD_6H    = 200.0  # W/m² — good solar coming
_GHI_GOOD_24H   = 150.0  # W/m²
# Predicted demand thresholds
_DEMAND_HIGH_KWH     = 8.0   # kWh over next 12h = high usage period
_DEMAND_DEFICIT_KWH  = -5.0  # kWh net (solar minus load) — battery will be drawn down


def recommend(
    stats: Stats,
    outlook: SolarOutlook,
    forecast: Optional[UsageForecast] = None,
) -> Recommendation:
    """Evaluate current state + weather + usage forecast → mode recommendation."""
    soc        = stats.current.battery_soc_pct
    home_kw    = stats.current.home_load_kw
    solar_kw   = stats.current.solar_production_kw
    grid_status = stats.current.grid_status

    ghi_6h   = outlook.avg_ghi(6)
    ghi_24h  = outlook.avg_ghi(24)
    cloud_6h = outlook.avg_cloud_cover(6)
    peak_ghi = outlook.peak_ghi_today()

    details: dict = {
        "soc_pct":              soc,
        "home_load_kw":         home_kw,
        "solar_kw":             solar_kw,
        "ghi_next_6h_wm2":      round(ghi_6h, 1),
        "ghi_next_24h_wm2":     round(ghi_24h, 1),
        "cloud_cover_6h_pct":   round(cloud_6h, 1),
        "peak_ghi_today_wm2":   round(peak_ghi, 1),
    }

    # Enrich details with usage forecast if available
    predicted_load_12h   = None
    predicted_deficit_12h = None
    if forecast and forecast.confidence != "none":
        predicted_load_12h    = forecast.total_load_kwh
        predicted_deficit_12h = forecast.net_kwh          # negative = net draw
        details.update({
            "predicted_load_12h_kwh":   round(predicted_load_12h, 2),
            "predicted_solar_12h_kwh":  round(forecast.total_solar_kwh, 2),
            "predicted_net_12h_kwh":    round(predicted_deficit_12h, 2),
            "predicted_peak_load_kw":   forecast.peak_load_kw,
            "usage_forecast_confidence": forecast.confidence,
            "history_days":             forecast.data_days,
        })

    # ── CRITICAL: grid down ──────────────────────────────────────────
    if grid_status != "normal":
        return Recommendation(
            mode=Mode.EMERGENCY_BACKUP,
            reason=f"Grid is {grid_status}. Conserve battery reserves.",
            urgency="critical",
            details=details,
        )

    # ── CRITICAL: battery critically low ────────────────────────────
    if soc < _SOC_CRITICAL:
        return Recommendation(
            mode=Mode.EMERGENCY_BACKUP,
            reason=(
                f"Battery critically low ({soc:.0f}% SoC). "
                "Switch to Emergency Backup to protect reserves."
            ),
            urgency="critical",
            details=details,
        )

    # ── WARNING: usage pattern predicts large deficit + low SoC ─────
    if (
        predicted_deficit_12h is not None
        and predicted_deficit_12h < _DEMAND_DEFICIT_KWH
        and soc < _SOC_HEALTHY
    ):
        battery_kwh = soc / 100 * 13.6  # approximate usable kWh (aPower = 13.6 kWh)
        shortfall = abs(predicted_deficit_12h) - battery_kwh
        return Recommendation(
            mode=Mode.EMERGENCY_BACKUP,
            reason=(
                f"Usage patterns predict a {abs(predicted_deficit_12h):.1f} kWh net draw "
                f"over the next 12h, but battery only holds ~{battery_kwh:.1f} kWh at "
                f"current SoC ({soc:.0f}%). "
                + (f"Estimated shortfall: {shortfall:.1f} kWh. " if shortfall > 0 else "")
                + "Emergency Backup will top up reserves now."
            ),
            urgency="warning",
            details=details,
        )

    # ── WARNING: predicted high load + poor solar ────────────────────
    if (
        predicted_load_12h is not None
        and predicted_load_12h > _DEMAND_HIGH_KWH
        and ghi_6h < _GHI_POOR_6H
        and soc < _SOC_HEALTHY
    ):
        return Recommendation(
            mode=Mode.EMERGENCY_BACKUP,
            reason=(
                f"High usage predicted over next 12h ({predicted_load_12h:.1f} kWh) "
                f"with poor solar (next 6h avg {ghi_6h:.0f} W/m²) "
                f"and only {soc:.0f}% SoC. "
                "Emergency Backup will build reserves before demand peaks."
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
                "Emergency Backup will help preserve reserves."
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
                "Emergency Backup will charge from grid to build reserves."
            ),
            urgency="warning",
            details=details,
        )

    # ── INFO: usage pattern + solar predict comfortable surplus ──────
    if (
        predicted_deficit_12h is not None
        and predicted_deficit_12h >= 0
        and ghi_6h > _GHI_GOOD_6H
    ):
        return Recommendation(
            mode=Mode.SELF_CONSUMPTION,
            reason=(
                f"Patterns predict {predicted_deficit_12h:.1f} kWh solar surplus over 12h "
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
                f", predicted 12h load {predicted_load_12h:.1f} kWh"
                if predicted_load_12h is not None else ""
            )
            + "). No mode change needed."
        ),
        urgency="info",
        details=details,
    )
