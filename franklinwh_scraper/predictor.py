"""Predicts future home load and net energy balance from historical patterns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .history import HistoryStore
from .tou import _is_holiday

_SEASON_MIN_DAYS = 21  # need at least this many days in season for seasonal profile

_RECENT_WINDOW_DAYS      = 21   # trailing window for the recency-weighted blend
_RECENT_BLEND_WEIGHT     = 0.65  # weight on the recent window once it has enough samples
_RECENT_MIN_SLOT_SAMPLES = 3     # min recent-window readings for a slot before blending it in


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


_LOAD_NOWCAST_HALFLIFE_H = 2.0  # current draw's influence on the forecast
                                # halves every 2h — by hour 8 (e.g. the
                                # overnight span behind the 9pm digest's
                                # "predicted SoC @ 7am") it's under 1% of its
                                # starting weight, so an unusual load right
                                # now nudges the next couple hours without
                                # distorting the pure-overnight tail.


def predict(
    store: HistoryStore,
    horizon_hours: int = 12,
    outlook=None,
    system_peak_kw: float | None = None,
    perf_ratio: float = 1.0,
    avg_temp_c: float = 22.0,
    hourly_bias: dict[int, float] | None = None,
    current_load_kw: float | None = None,
    load_percentile: float = 0.5,
) -> UsageForecast:
    """
    Predict home load and solar production for the next `horizon_hours` hours.

    Uses (day_of_week, hour_of_day) buckets from historical data.
    If outlook + system_peak_kw are provided, solar is weather-adjusted using
    GHI forecast instead of historical averages.
    hourly_bias: per-hour learned correction factors (actual/predicted) that
    improve accuracy over time as real readings accumulate.
    current_load_kw: live home_load_kw reading at call time, if available.
    Anchors the near-term forecast to what's actually happening right now
    (e.g. a guest over, laundry running) instead of relying purely on the
    historical average for this hour-of-week slot. The correction decays
    over _LOAD_NOWCAST_HALFLIFE_H so it fades out well before the
    overnight tail — it nudges the next couple hours, it doesn't assume
    tonight's unusual load holds until morning.
    load_percentile: which percentile of the home_load_kw distribution to
    use per (day_of_week, hour_of_day) slot — default 0.5 (median). Callers
    building a "without EV" prediction pass something lower (e.g. 0.25):
    median alone can still side with EV-charging nights when they're a
    slim majority of a small recent sample (see
    HistoryStore._percentile_load_by_slot). Leave at the default for the
    general forecast (Emergency-Backup decisions, /sundown, general
    dashboard) where realistic mixed expectations are the point.
    Confidence degrades with fewer data points per slot.
    """
    now        = datetime.now()
    season     = _current_season(now.month)
    data_days  = store.distinct_days()

    # Use seasonal profiles when we have enough seasonal data (better accuracy);
    # fall back to all-time profiles to avoid sparse-bucket gaps.
    using_seasonal = store.days_in_season(season) >= _SEASON_MIN_DAYS
    if using_seasonal:
        load_profile  = store.seasonal_load_profile(season, load_percentile)
        solar_profile = store.seasonal_solar_profile(season)
    else:
        load_profile  = store.load_profile(load_percentile)
        solar_profile = store.solar_profile()

    # Blend in a recency-weighted profile per slot so a sustained recent change
    # (new EV, HVAC swap) shows up in days rather than being diluted by months
    # of older seasonal/all-time data — same idea as the solar-forecast EWMA fix.
    recent_counts = store.recent_slot_counts(_RECENT_WINDOW_DAYS)
    recent_load   = store.recent_load_profile(_RECENT_WINDOW_DAYS, load_percentile)
    recent_solar  = store.recent_solar_profile(_RECENT_WINDOW_DAYS)
    for slot, n in recent_counts.items():
        if n < _RECENT_MIN_SLOT_SAMPLES:
            continue
        if slot in recent_load:
            baseline = load_profile.get(slot)
            load_profile[slot] = (
                _RECENT_BLEND_WEIGHT * recent_load[slot] + (1 - _RECENT_BLEND_WEIGHT) * baseline
                if baseline is not None else recent_load[slot]
            )
        if slot in recent_solar:
            baseline = solar_profile.get(slot)
            solar_profile[slot] = (
                _RECENT_BLEND_WEIGHT * recent_solar[slot] + (1 - _RECENT_BLEND_WEIGHT) * baseline
                if baseline is not None else recent_solar[slot]
            )

    # Confidence must be gated on the sample size actually backing the
    # profile value in use — when using_seasonal, that's the in-season count,
    # not the unrelated all-time count for the same weekday/hour (which can
    # look "high confidence" purely from other seasons' data).
    slot_counts   = store.seasonal_slot_counts(season) if using_seasonal else store.slot_counts()
    conf_data_days = store.days_in_season(season) if using_seasonal else data_days

    # Temperature-load scaling:
    # +2.5% per °C above 27°C (AC draw, waking hours only)
    # +2.0% per °C below 18°C (heat pump / resistive, all hours)
    ac_temp_scale   = 1.0 + 0.025 * max(0.0, avg_temp_c - 27.0)
    heat_temp_scale = 1.0 + 0.020 * max(0.0, 18.0 - avg_temp_c)

    predictions: list[HourPrediction] = []
    live_residual_kw = 0.0  # set from h=0's (current - baseline) gap, then decayed

    for h in range(horizon_hours):
        future = now + timedelta(hours=h)
        # Holidays behave like Sundays (SDG&E TOU already treats them this way,
        # see tou.py) — bucket under Sunday's weekday index instead of the
        # actual weekday so a holiday's load doesn't dilute weekday profiles.
        weekday = 6 if _is_holiday(future) else future.weekday()
        slot   = (weekday, future.hour)
        slot_n = slot_counts.get(slot, 0)

        load_kw  = load_profile.get(slot)
        direct_slot_hit = load_kw is not None

        # Weather-adjusted solar: GHI/1000 × system_peak_kw × perf_ratio corrects
        # for systematic GHI model bias learned from actual vs. predicted history.
        # hourly_bias applies per-hour learned correction on top of perf_ratio.
        if outlook is not None and system_peak_kw is not None:
            solar_kw = max(0.0, outlook.ghi_at(future) / 1000.0 * system_peak_kw * perf_ratio)
            if hourly_bias and future.hour in hourly_bias:
                solar_kw *= hourly_bias[future.hour]
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
        elif not direct_slot_hit:
            # Fell back to same-hour cross-day average — treat as low regardless of data_days
            confidence = "low"
        elif slot_n >= 8 and conf_data_days >= 7:
            confidence = "high"
        elif slot_n >= 3 or conf_data_days >= 3:
            confidence = "medium"
        else:
            confidence = "low"

        # AC during waking hours; heating applies all hours
        if 7 <= future.hour < 23:
            temp_scale = ac_temp_scale * heat_temp_scale
        else:
            temp_scale = heat_temp_scale
        load_kw = load_kw * temp_scale

        if current_load_kw is not None:
            if h == 0:
                live_residual_kw = current_load_kw - load_kw
            weight = 0.5 ** (h / _LOAD_NOWCAST_HALFLIFE_H)
            load_kw = max(0.0, load_kw + live_residual_kw * weight)

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
