"""Predicts future home load and net energy balance from historical patterns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .history import HistoryStore

_SEASON_MIN_DAYS = 21  # need at least this many days in season for seasonal profile


def _current_season(month: int) -> str:
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    if month in (9, 10, 11):
        return "fall"
    return "winter"


@dataclass
class HourPrediction:
    dt: datetime
    predicted_load_kw: float
    predicted_solar_kw: float   # from historical pattern (weather adjusts this separately)
    net_kw: float               # solar - load (negative = net draw from battery/grid)
    confidence: str             # "high" | "medium" | "low" | "none"


@dataclass
class UsageForecast:
    hours: list[HourPrediction]
    total_load_kwh: float       # sum of predicted load over window
    total_solar_kwh: float      # sum of predicted solar over window
    net_kwh: float              # solar - load (negative = battery/grid needed)
    peak_load_kw: float
    confidence: str
    data_days: int              # how many days of history were used


def predict(
    store: HistoryStore,
    horizon_hours: int = 12,
    outlook=None,
    system_peak_kw: float | None = None,
    perf_ratio: float = 1.0,
    avg_temp_c: float = 22.0,
) -> UsageForecast:
    """
    Predict home load and solar production for the next `horizon_hours` hours.

    Uses (day_of_week, hour_of_day) buckets from historical data.
    If outlook + system_peak_kw are provided, solar is weather-adjusted using
    GHI forecast instead of historical averages.
    Confidence degrades with fewer data points.
    """
    now        = datetime.now()
    season     = _current_season(now.month)
    data_days  = store.distinct_days()

    # Use seasonal profiles when we have enough seasonal data (better accuracy);
    # fall back to all-time profiles to avoid sparse-bucket gaps.
    if store.days_in_season(season) >= _SEASON_MIN_DAYS:
        load_profile  = store.seasonal_load_profile(season)
        solar_profile = store.seasonal_solar_profile(season)
    else:
        load_profile  = store.load_profile()
        solar_profile = store.solar_profile()

    # Temperature-load scaling: +2.5% load per °C above 27°C (80°F) models AC draw.
    temp_scale = 1.0 + 0.025 * max(0.0, avg_temp_c - 27.0)

    predictions: list[HourPrediction] = []

    for h in range(horizon_hours):
        future = now + timedelta(hours=h)
        slot = (future.weekday(), future.hour)

        load_kw = load_profile.get(slot)

        # Weather-adjusted solar: GHI/1000 × system_peak_kw × perf_ratio corrects
        # for systematic GHI model bias learned from actual vs. predicted history.
        if outlook is not None and system_peak_kw is not None:
            solar_kw = max(0.0, outlook.ghi_at(future) / 1000.0 * system_peak_kw * perf_ratio)
        else:
            solar_kw = solar_profile.get(slot, 0.0)

        if load_kw is None:
            # No data for this exact slot — fall back to same-hour any-day average
            same_hour = [v for (d, hr), v in load_profile.items() if hr == future.hour]
            load_kw = sum(same_hour) / len(same_hour) if same_hour else None

        if load_kw is None:
            # No data at all for this hour — use overall average
            load_kw = (
                sum(load_profile.values()) / len(load_profile)
                if load_profile else 0.0
            )
            confidence = "none"
        elif data_days >= 7:
            confidence = "high"
        elif data_days >= 3:
            confidence = "medium"
        else:
            confidence = "low"

        load_kw = load_kw * temp_scale
        predictions.append(HourPrediction(
            dt=future,
            predicted_load_kw=round(load_kw, 2),
            predicted_solar_kw=round(solar_kw, 2),
            net_kw=round(solar_kw - load_kw, 2),
            confidence=confidence,
        ))

    total_load = sum(p.predicted_load_kw for p in predictions)
    total_solar = sum(p.predicted_solar_kw for p in predictions)
    overall_confidence = _worst_confidence([p.confidence for p in predictions])

    return UsageForecast(
        hours=predictions,
        total_load_kwh=round(total_load, 2),
        total_solar_kwh=round(total_solar, 2),
        net_kwh=round(total_solar - total_load, 2),
        peak_load_kw=round(max(p.predicted_load_kw for p in predictions), 2),
        confidence=overall_confidence,
        data_days=data_days,
    )


def _worst_confidence(values: list[str]) -> str:
    order = ["high", "medium", "low", "none"]
    for level in reversed(order):
        if level in values:
            return level
    return "none"
