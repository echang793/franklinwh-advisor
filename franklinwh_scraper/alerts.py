"""Peak-hour alert engine: state persistence, calibration, and all alert checks.

Extracted from cli.py so alert logic can evolve independently of the
command-line interface. cli.py re-exports the names it uses.
"""

from __future__ import annotations

import fcntl
import json
import logging
import statistics
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .format_utils import fmt_hours, soc_bar, time_to_pct
from .history import integrate_intervals
from .notifier import notify_email, notify_imessage_text, notify_telegram, notify_webhook
from .tou import (TouPeriod, base_service_cost, cheap_charge_deadline,
                  export_rate_at, peak_export_hour, period_at, rate_at,
                  rates_are_stale)
from .weather import fetch_nws_storm_alerts, fetch_solar_outlook

logger = logging.getLogger(__name__)

_BATTERY_CAPACITY_KWH = 13.6  # fallback default — overridden by cfg.battery_capacity_kwh at runtime

def _get_system_peak_kw(state: dict) -> float | None:
    """Return calibrated system peak kW (P75 of sunny-day samples), or None if < 3 samples.

    P75 balances the two failure modes: P50 under-predicts on clear summer days,
    P85 over-predicts because cloud-edge enhancement spikes inflate the top decile.
    """
    samples = state.get("solar_cal_samples", [])
    if len(samples) < 3:
        return None
    s = sorted(samples)
    return s[int(len(s) * 0.75)]


_GHI_CLOUDY_THRESHOLD = 300  # W/m² avg over 12h — below this = dim/cloudy day


_soc_bar     = soc_bar
_time_to_pct = time_to_pct
_fmt_hours   = fmt_hours


_PR_EWMA_ALPHA = 0.35  # weight on newest sample — tracks multi-day cloud regime
                       # shifts (e.g. marine layer persisting for a week) faster
                       # than a flat median of the whole rolling window.


def _ewma(samples: list[float]) -> float:
    est = samples[0]
    for v in samples[1:]:
        est = _PR_EWMA_ALPHA * v + (1 - _PR_EWMA_ALPHA) * est
    return est


def _get_performance_ratio(state: dict, cloudy: bool = False) -> float:
    """Return empirical PR (actual / predicted daily kWh) for sunny or cloudy days.

    Separate buckets prevent the sunny-day bias (hot panels, lower efficiency)
    from distorting cloudy-day forecasts where panels run cooler.
    Falls back to sunny PR × 1.10 until 3 cloudy-day samples accumulate.
    Samples are stored oldest-first, so the EWMA weights the most recent day
    most heavily.
    """
    if cloudy:
        samples = [v for v in state.get("perf_ratio_cloudy_samples", []) if v <= 1.4]
        if len(samples) < 3:
            sunny = [v for v in state.get("perf_ratio_samples", []) if v <= 1.4]
            if len(sunny) >= 3:
                return max(_ewma(sunny) * 1.10, 0.60)
            return 0.85  # reasonable prior: cloudy panels run cooler
        return max(_ewma(samples), 0.55)
    else:
        samples = [v for v in state.get("perf_ratio_samples", []) if v <= 1.4]
        if len(samples) < 3:
            return 1.0
        return max(_ewma(samples), 0.60)


# ── Weather forecast cache (30-min TTL) ──────────────────────────────

_outlook_cache: dict = {}


def _fetch_outlook_cached(lat: float, lon: float):
    """Return a SolarOutlook, fetching fresh data at most once per 30 minutes."""
    now_ts = time.time()
    if _outlook_cache.get("outlook") is not None and now_ts - _outlook_cache.get("fetched_at", 0) < 1800:
        return _outlook_cache["outlook"]
    try:
        outlook = fetch_solar_outlook(lat, lon)
        _outlook_cache["outlook"] = outlook
        _outlook_cache["fetched_at"] = now_ts
        return outlook
    except Exception as e:
        logger.warning("Weather forecast fetch failed: %s", e)
        return _outlook_cache.get("outlook")  # serve stale cache rather than None


# ── Multi-channel alert dispatcher ───────────────────────────────────

def _send_alert(body: str, cfg: Config, urgent: bool = False) -> None:
    """Send to all configured channels."""
    if cfg.imessage_phone:
        notify_imessage_text(body, cfg.imessage_phone)
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        notify_telegram(body, cfg.telegram_bot_token, cfg.telegram_chat_id)
    if cfg.smtp_host and cfg.email_to:
        notify_email(body, cfg)
    if cfg.webhook_url:
        notify_webhook(body, urgent, cfg)


def _ping_healthcheck(cfg: Config) -> None:
    """Ping the uptime monitor (e.g. healthchecks.io) after a healthy cycle.

    Fire-and-forget — if pings stop, the monitor alerts the user that the
    advisor has gone down. Never raises.
    """
    if not cfg.healthcheck_url:
        return
    try:
        import requests as _rq
        _rq.get(cfg.healthcheck_url, timeout=5)
    except Exception as e:
        logger.debug("Healthcheck ping failed: %s", e)


# ── Peak-hour alert helpers ───────────────────────────────────────────

_PEAK_STATE_FILE = ".peak_alert_state.json"
_CMR_OUTAGE_FLAG = Path.home() / ".cmr-power-outage.flag"

# Safety alerts that cannot be disabled by the user.
_ALWAYS_ON_ALERTS = frozenset({"grid_down", "grid_restored", "area_power_outage", "fast_drain"})


def _alert_enabled(cfg: Config, name: str) -> bool:
    if name in _ALWAYS_ON_ALERTS:
        return True
    return name not in (cfg.disabled_alerts or [])




def _precharge_plan(now: datetime, soc: float, tmrw_solar_kwh: float,
                    bat_cap: float, target_soc: float = 80.0) -> str:
    """Concrete grid pre-charge recommendation, or '' if not needed.

    Fires when tomorrow's predicted solar won't refill the battery enough to
    cover the next on-peak window and current SoC is below target. Picks the
    cheapest charge deadline: today's super-off-peak (before 2 PM, via
    cheap_charge_deadline) else tonight's super-off-peak window.
    """
    if tmrw_solar_kwh >= bat_cap * 0.6 or soc >= target_soc:
        return ""
    deadline = cheap_charge_deadline(now)
    when = (deadline.strftime("%-I %p") if deadline is not None
            else "tonight (after midnight, super-off-peak)")
    sop = rate_at(now.replace(hour=1,  minute=0, second=0, microsecond=0))
    onp = rate_at(now.replace(hour=17, minute=0, second=0, microsecond=0))
    return (
        f"\n⚡ Pre-charge to ~{target_soc:.0f}% by {when} "
        f"(${sop:.2f}/kWh super-off-peak vs ${onp:.2f} on-peak). "
        f"Tomorrow's solar (~{tmrw_solar_kwh:.1f} kWh) won't fully refill the battery."
    )


def _load_peak_state(out: Path) -> dict:
    p = out / _PEAK_STATE_FILE
    try:
        state = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    # Migrate renamed key from earlier version
    if "monthly_summary_month" in state and "monthly_summary_date" not in state:
        old = state.pop("monthly_summary_month")
        if isinstance(old, str):
            state["monthly_summary_date"] = old + "-19"
    return state


def _safe_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _prune_old_state(state: dict) -> dict:
    """Drop date-keyed entries older than 30 days to prevent unbounded growth."""
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    pruned = {}
    for k, v in state.items():
        if k.endswith("_date") and isinstance(v, str) and v < cutoff:
            continue
        # Drop daily_pr_YYYY-MM-DD and peak_cov_YYYY-MM-DD entries older than 30 days
        for prefix in ("daily_pr_", "peak_cov_"):
            if k.startswith(prefix) and k[len(prefix):] < cutoff:
                break
        else:
            pruned[k] = v
    return pruned


def _save_peak_state(out: Path, state: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    target = out / _PEAK_STATE_FILE
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(_prune_old_state(state)))
    tmp.replace(target)  # atomic on POSIX — no partial-write corruption


@contextmanager
def _state_lock(out: Path):
    """Exclusive file lock preventing concurrent cron processes from double-alerting."""
    lock_path = out / ".peak_alert_state.lock"
    out.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


# ── Per-alert helper functions ────────────────────────────────────────
# Each takes (state, today, now, c, ...) and returns the alert body or None.
# State mutations happen inside each function; caller saves state once at end.

def _calibrate_solar(state: dict, solar_kw: float, outlook) -> None:
    """Record a (solar_kw / GHI) sample used to estimate system peak kW.

    A single noisy reading (sensor glitch, a cloud edge the coarse hourly GHI
    missed) shouldn't be able to swing the peak-kW estimate that scales every
    downstream prediction. Once >=10 samples exist, reject a new sample that's
    far from the recent trailing median — unless 3 consecutive rejects agree
    with each other, which means it's a real step-change (panel cleaning,
    shading removed) rather than one-off noise, and gets accepted as a block.
    """
    if not (outlook and solar_kw >= 1.0):
        return
    current_ghi = outlook.avg_ghi(1)
    if current_ghi < 600:  # raised from 400 — only calibrate during clearly sunny conditions
        return
    sample = round(solar_kw / (current_ghi / 1000.0), 2)
    if not (0.5 <= sample <= 25.0):
        return

    samples = state.get("solar_cal_samples", [])
    pending = state.get("solar_cal_pending", [])

    if len(samples) >= 10:
        recent_median = statistics.median(samples[-10:])
        if recent_median > 0 and not (0.5 * recent_median <= sample <= 1.75 * recent_median):
            pending = (pending + [sample])[-3:]
            if len(pending) == 3 and (max(pending) - min(pending)) / statistics.mean(pending) <= 0.15:
                logger.info(
                    "Accepted solar calibration step-change: %.2f -> ~%.2f",
                    recent_median, statistics.mean(pending),
                )
                samples.extend(pending)
                state["solar_cal_samples"] = samples[-50:]
                pending = []
            state["solar_cal_pending"] = pending
            return

    state["solar_cal_pending"] = []
    samples.append(sample)
    state["solar_cal_samples"] = samples[-50:]


def _calibrate_solar_hourly(state: dict, solar_kw: float, outlook, now: datetime) -> None:
    """Track per-hour (actual / GHI-predicted) ratio for adaptive bias correction.

    Accumulates rolling 30-sample median per clock-hour so the predictor learns
    systematic patterns — morning shade, afternoon heat, inverter clipping — and
    corrects for them automatically over time.
    """
    if not (outlook and solar_kw >= 0.2):
        return
    system_peak = _get_system_peak_kw(state)
    if system_peak is None:
        return
    current_ghi = outlook.avg_ghi(1)
    if current_ghi < 100:
        return
    predicted_kw = (current_ghi / 1000.0) * system_peak
    if predicted_kw < 0.1:
        return
    ratio = round(solar_kw / predicted_kw, 3)
    if 0.3 <= ratio <= 2.0:
        key = f"solar_bias_h{now.hour}"
        samples = state.get(key, [])
        samples.append(ratio)
        state[key] = samples[-30:]


def _get_hourly_bias(state: dict) -> dict[int, float]:
    """Return per-hour learned solar correction factors (median of samples, min 5)."""
    bias: dict[int, float] = {}
    for h in range(24):
        samples = state.get(f"solar_bias_h{h}", [])
        if len(samples) >= 5:
            bias[h] = statistics.median(samples)
    return bias


def _alert_morning_preview(
    state: dict, today: str, now: datetime, c,
    outlook, usage_forecast, store, cfg: Config | None = None,
) -> str | None:
    in_window = (now.hour == 7 and now.minute >= 30) or now.hour == 8
    if not in_window or state.get("morning_preview_date") == today:
        return None

    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    pred_key  = f"predicted_kwh_{yesterday}"
    yest_pred   = state.get(pred_key, 0.0)
    yest_actual = 0.0

    # Update PR calibration and fetch actual for accuracy display in one pass
    if store is not None and yest_pred > 0:
        yest_actual = store.daily_solar_kwh_api(yesterday)
        if yest_actual <= 0.0:
            yest_actual = store.daily_solar_kwh(yesterday)
        yesterday_ghi = state.get(f"predicted_avg_ghi_{yesterday}", 400.0)
        cloudy_day    = yesterday_ghi < _GHI_CLOUDY_THRESHOLD
        min_predicted = 0.5 if cloudy_day else 3.0
        # Skip days where actual was less than 65% of prediction — indicates unexpected
        # cloud cover that the GHI forecast missed entirely (not a model calibration signal).
        _PR_MIN = 0.65
        if yest_pred >= min_predicted and yest_actual >= 0.3:
            ratio  = round(yest_actual / yest_pred, 3)
            if ratio >= _PR_MIN:
                bucket = "perf_ratio_cloudy_samples" if cloudy_day else "perf_ratio_samples"
                pr_samples = state.get(bucket, [])
                pr_samples.append(ratio)
                state[bucket] = pr_samples[-30:]  # rolling 30-day window balances recency with stability
            state[f"daily_pr_{yesterday}"] = ratio  # always record for accuracy display
            logger.info(
                "PR update (%s): actual=%.1f predicted=%.1f ratio=%.3f ghi=%.0f",
                "cloudy" if cloudy_day else "sunny",
                yest_actual, yest_pred, ratio, yesterday_ghi,
            )

    soc      = c.battery_soc_pct
    solar_kw = c.solar_production_kw

    if outlook:
        cal_samples = state.get("solar_cal_samples", [])
        system_peak_kw = _get_system_peak_kw(state)  # P85 — consistent with EOD digest
        if system_peak_kw is not None:
            cal_note = f"{len(cal_samples)} readings"
        else:
            if usage_forecast and usage_forecast.hours:
                system_peak_kw = max(
                    (p.predicted_solar_kw for p in usage_forecast.hours),
                    default=solar_kw,
                ) * 1.2
            else:
                system_peak_kw = max(solar_kw, 1.0) * 1.2
            cal_note = f"calibrating, {len(cal_samples)}/3 readings"
        system_peak_kw = max(system_peak_kw, 1.0)

        avg_ghi    = outlook.avg_ghi(12)
        cloudy_day = avg_ghi < _GHI_CLOUDY_THRESHOLD
        perf_ratio = _get_performance_ratio(state, cloudy=cloudy_day)
        hourly_bias = _get_hourly_bias(state)
        gen_kwh    = round(outlook.today_generation_kwh(system_peak_kw, perf_ratio, hourly_bias), 1)
        state[f"predicted_kwh_{today}"]     = gen_kwh
        state[f"predicted_avg_ghi_{today}"] = round(avg_ghi, 1)

        sky = "Sunny" if avg_ghi >= 400 else ("Partly cloudy" if avg_ghi >= _GHI_CLOUDY_THRESHOLD else "Cloudy")
        cloudy_samples = state.get("perf_ratio_cloudy_samples", [])
        sunny_samples  = state.get("perf_ratio_samples", [])
        if cloudy_day and len(cloudy_samples) >= 3:
            pr_note = f"cloudy eff={perf_ratio:.2f}"
        elif not cloudy_day and len(sunny_samples) >= 3:
            pr_note = f"eff={perf_ratio:.2f}"
        else:
            pr_note = cal_note
        solar_est = f"~{gen_kwh:.1f} kWh predicted ({sky}, {pr_note})"

        # Tomorrow forecast
        tmrw_ghi = outlook.tomorrow_avg_ghi()
        tmrw_sky = "Sunny" if tmrw_ghi >= 400 else ("Partly cloudy" if tmrw_ghi >= _GHI_CLOUDY_THRESHOLD else "Cloudy")
        tmrw_kwh = outlook.tomorrow_generation_kwh(system_peak_kw, perf_ratio, hourly_bias)
        solar_est += f"\nTomorrow: {tmrw_sky} — ~{tmrw_kwh:.1f} kWh"
        bat_cap = cfg.battery_capacity_kwh if cfg else _BATTERY_CAPACITY_KWH
        solar_est += _precharge_plan(now, soc, tmrw_kwh, bat_cap)

        # Peak solar window — relative threshold (50% of day's peak, min 200 W/m²)
        today_hrs = [h for h in outlook.hours if h.time.date() == now.date()]
        day_peak_ghi = max((h.ghi_wm2 for h in today_hrs), default=0.0)
        ghi_thresh = max(200.0, day_peak_ghi * 0.50)
        peak_hrs = [h for h in today_hrs if h.ghi_wm2 >= ghi_thresh]
        if peak_hrs:
            start    = peak_hrs[0].time.strftime("%-I%p").lower()
            end      = (peak_hrs[-1].time + timedelta(hours=1)).strftime("%-I%p").lower()
            peak_hr  = max(peak_hrs, key=lambda h: h.ghi_wm2)
            peak_at  = peak_hr.time.strftime("%-I%p").lower()
            peak_window_str = f"\n🕐 Best solar: {start}–{end} (peak ~{peak_at})"
        else:
            peak_window_str = ""
    else:
        solar_est       = "Solar forecast unavailable"
        peak_window_str = ""

    state["morning_preview_date"] = today
    logger.info("Morning preview alert sent for %s", today)
    return (
        f"☀️ <b>FranklinWH: Good morning!</b>\n"
        f"🔋 {_soc_bar(soc)}  ·  Solar: <b>{solar_kw:.2f} kW</b>\n"
        f"{solar_est}{peak_window_str}"
    )


def _alert_grid_import(state: dict, today: str, now: datetime, c) -> str | None:
    if not (16 <= now.hour < 21) or c.grid_use_kw <= 0.3:
        return None
    if state.get("grid_import_alerted_date") == today:
        return None
    state["grid_import_alerted_date"] = today
    logger.info("Peak grid-import alert sent for %s", today)
    return (
        f"⚠️ <b>FranklinWH: Grid import during peak (4–9 pm)</b>\n"
        f"🔋 {_soc_bar(c.battery_soc_pct)}  ·  Grid <b>+{c.grid_use_kw:.2f} kW</b>  ·  "
        f"Solar {c.solar_production_kw:.2f} kW  ·  Load {c.home_load_kw:.2f} kW\n"
        f"Time: {now.strftime('%-I:%M %p')}"
    )


def _alert_low_soc_1pm(state: dict, today: str, now: datetime, c) -> str | None:
    in_window = now.hour == 13
    if not in_window or c.battery_soc_pct >= 40.0:
        return None
    if state.get("low_soc_1pm_alerted_date") == today:
        return None
    state["low_soc_1pm_alerted_date"] = today
    logger.info("Low 1 pm SoC alert sent for %s (%.0f%%)", today, c.battery_soc_pct)
    tte = _time_to_pct(c.battery_soc_pct, 0.0, _BATTERY_CAPACITY_KWH, c.battery_use_kw)
    tte_str = f"⏱ ~{_fmt_hours(tte)} to empty · " if tte is not None else ""
    return (
        f"🟡 <b>FranklinWH: Battery low at {now.strftime('%-I:%M %p')}</b>\n"
        f"🔋 {_soc_bar(c.battery_soc_pct)} — grid import risk during 4–9 pm peak\n"
        f"Solar {c.solar_production_kw:.2f} kW  ·  Load {c.home_load_kw:.2f} kW\n"
        + tte_str
        + "Consider switching to Emergency Backup to charge before peak."
    )


def _alert_eb_ready(state: dict, today: str, now: datetime, c) -> str | None:
    in_window = now.hour in (13, 14)
    if not in_window or c.battery_soc_pct < 80.0:
        return None
    if state.get("eb_80pct_alerted_date") == today:
        return None
    state["eb_80pct_alerted_date"] = today
    logger.info("EB 80%% SoC alert sent for %s (%.0f%%)", today, c.battery_soc_pct)
    return (
        f"🟢 <b>FranklinWH: Battery at {c.battery_soc_pct:.0f}% — Emergency Backup target reached</b>\n"
        f"Time: {now.strftime('%-I:%M %p')} — battery ready before 4 pm peak\n"
        f"Solar {c.solar_production_kw:.2f} kW  ·  Load {c.home_load_kw:.2f} kW\n"
        f"You can now switch modes if needed."
    )


def _alert_low_morning_solar(state: dict, today: str, now: datetime, c) -> str | None:
    in_window = now.hour in (9, 10)
    if not in_window or c.solar_production_kw >= 0.5:
        return None
    if state.get("low_solar_morning_date") == today:
        return None
    state["low_solar_morning_date"] = today
    logger.info("Low morning solar alert sent for %s (%.2f kW)", today, c.solar_production_kw)
    return (
        f"☁️ <b>FranklinWH: Low solar at {now.strftime('%-I:%M %p')} — cloudy day ahead</b>\n"
        f"Solar {c.solar_production_kw:.2f} kW  ·  SoC {c.battery_soc_pct:.0f}%  ·  Load {c.home_load_kw:.2f} kW\n"
        f"Consider conserving battery early — less solar charging expected today."
    )


def _alert_solar_stopped(state: dict, today: str, now: datetime, c) -> str | None:
    """Always updates last_midday_solar_kw when in the midday window."""
    if not (11 <= now.hour < 15):
        return None
    last_solar = state.get("last_midday_solar_kw", 0.0)
    state["last_midday_solar_kw"] = c.solar_production_kw
    if last_solar < 0.5 or c.solar_production_kw >= 0.3 or state.get("solar_stopped_date") == today:
        return None
    state["solar_stopped_date"] = today
    logger.info("Solar stopped alert sent for %s (%.2f→%.2f kW)", today, last_solar, c.solar_production_kw)
    return (
        f"🔴 <b>FranklinWH: Solar dropped mid-day — possible issue</b>\n"
        f"Was {last_solar:.2f} kW → now {c.solar_production_kw:.2f} kW "
        f"at {now.strftime('%-I:%M %p')}\n"
        f"SoC {c.battery_soc_pct:.0f}%  ·  Check inverter or cloud cover."
    )


def _alert_low_noon_soc(state: dict, today: str, now: datetime, c) -> str | None:
    in_window = now.hour in (11, 12)
    if not in_window or c.battery_soc_pct >= 30.0 or c.solar_production_kw <= 0.5:
        return None
    if state.get("low_noon_soc_date") == today:
        return None
    state["low_noon_soc_date"] = today
    logger.info("Low noon SoC alert sent for %s (%.0f%%)", today, c.battery_soc_pct)
    return (
        f"🟡 <b>FranklinWH: Battery still low at noon — only {c.battery_soc_pct:.0f}% SoC</b>\n"
        f"Solar {c.solar_production_kw:.2f} kW available but battery hasn't recovered\n"
        f"Time: {now.strftime('%-I:%M %p')}  ·  Load {c.home_load_kw:.2f} kW\n"
        f"Check battery mode — may need manual intervention."
    )


def _alert_export_arbitrage(
    state: dict, today: str, now: datetime, c, cfg: Config, usage_forecast,
) -> str | None:
    """Aug/Sep only: when the battery is full and won't be needed for self-supply,
    advise exporting surplus to grid at the single highest-rate hour of the day.

    Advisory only — does not command the inverter.
    """
    peak = peak_export_hour(now.month)
    if peak is None:
        return None  # hard month gate — inert outside Aug/Sep
    peak_hour, peak_rate = peak

    # Fire late-morning/early-afternoon so the user has lead time before the
    # export hour, once solar has had a chance to fill the battery.
    if now.hour not in (11, 12, 13) or state.get("export_arb_date") == today:
        return None

    soc = c.battery_soc_pct
    if soc < 85.0:
        return None  # not enough surplus to bother

    bat_cap     = cfg.battery_capacity_kwh
    reserve_pct = 20.0  # keep a floor for overnight / backup
    exportable_kwh = max(0.0, (soc - reserve_pct) / 100 * bat_cap)

    # Subtract predicted self-supply need at the export hour (net_kw = solar − load).
    # Only apply when confidence is not "none" — a zero-load forecast (no history)
    # would never reduce exportable_kwh, giving a falsely optimistic value.
    if usage_forecast and usage_forecast.hours and usage_forecast.confidence != "none":
        for p in usage_forecast.hours:
            if p.dt.date() == now.date() and p.dt.hour == peak_hour:
                exportable_kwh = max(0.0, exportable_kwh - max(0.0, -p.net_kw))
                break

    if exportable_kwh < 0.5:
        return None

    credit     = exportable_kwh * peak_rate
    hour_label = datetime(now.year, now.month, now.day, peak_hour).strftime("%-I %p")
    state["export_arb_date"] = today
    logger.info("Export arbitrage alert: %.1f kWh @ $%.3f = $%.2f at %s",
                exportable_kwh, peak_rate, credit, hour_label)
    return (
        f"💰 <b>FranklinWH: Peak export opportunity today</b>\n"
        f"Battery {soc:.0f}% — hold and export ~{exportable_kwh:.1f} kWh to grid at "
        f"{hour_label} (${peak_rate:.3f}/kWh) ≈ ${credit:.2f} credit\n"
        f"That's the day's highest export rate this month. Recharge afterward from solar."
    )


def _alert_eod_digest(
    state: dict, today: str, now: datetime, stats, cfg: Config,
    outlook, usage_forecast, store=None,
) -> str | None:
    if now.hour not in (21, 22) or state.get("eod_digest_date") == today:
        return None

    t       = stats.totals
    c       = stats.current
    soc     = c.battery_soc_pct
    bat_cap = cfg.battery_capacity_kwh

    backup_str = ""
    if c.home_load_kw > 0.1:
        backup_h   = soc / 100 * bat_cap / c.home_load_kw
        backup_str = f"\n⏱ Backup: ~{backup_h:.1f} hr at current load"

    # ── DB-sourced energy totals ─────────────────────────────────────
    # API running counters reset on gateway restart / internet outage;
    # DB accumulates across all polls and is the reliable daily source.
    db_solar_kwh    = 0.0
    db_batt_chg_kwh = 0.0
    db_batt_dis_kwh = 0.0
    if store is not None:
        db_solar_kwh = store.daily_solar_kwh_api(today)
        if db_solar_kwh <= 0.0:
            db_solar_kwh = store.daily_solar_kwh(today)
        db_batt_chg_kwh, db_batt_dis_kwh = store.daily_battery_kwh(today)

    solar_kwh    = db_solar_kwh    if db_solar_kwh    > 0 else t.solar_kwh
    batt_chg_kwh = db_batt_chg_kwh if db_batt_chg_kwh > 0 else t.battery_charge_kwh
    batt_dis_kwh = db_batt_dis_kwh if db_batt_dis_kwh > 0 else t.battery_discharge_kwh
    grid_in_kwh  = 0.0  # filled below from readings
    grid_out_kwh = 0.0
    home_kwh     = 0.0

    solar_delta_str = ""
    predicted_kwh   = state.get(f"predicted_kwh_{today}")
    if predicted_kwh and predicted_kwh > 0:
        actual_kwh = solar_kwh
        delta_kwh  = actual_kwh - predicted_kwh
        sign       = "+" if delta_kwh >= 0 else ""
        solar_delta_str = (
            f"\n🎯 Solar forecast vs actual:\n"
            f"<code>  Predicted: {predicted_kwh:.1f} kWh\n"
            f"  Actual:    {actual_kwh:.1f} kWh\n"
            f"  Delta:     {sign}{delta_kwh:.1f} kWh ({sign}{delta_kwh / predicted_kwh * 100:.0f}%)</code>"
        )

    soc_7am_str = ""
    if usage_forecast and usage_forecast.hours and usage_forecast.confidence != "none":
        tomorrow_7am  = (now + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
        night_net_kwh = sum(p.net_kw for p in usage_forecast.hours if now <= p.dt < tomorrow_7am)
        pred_soc_7am  = max(0.0, min(100.0, soc + night_net_kwh / bat_cap * 100))
        soc_7am_str   = f"\n🌅 Predicted SoC @ 7 am: ~{pred_soc_7am:.0f}%"

    precharge_str  = ""
    tmrw_solar_str = ""
    if outlook:
        sp = _get_system_peak_kw(state)
        if sp:
            cloudy   = outlook.tomorrow_avg_ghi() < _GHI_CLOUDY_THRESHOLD
            pr       = _get_performance_ratio(state, cloudy=cloudy)
            tmrw_kwh = outlook.tomorrow_generation_kwh(sp, pr, _get_hourly_bias(state))
            precharge_str  = _precharge_plan(now, soc, tmrw_kwh, bat_cap)
            tmrw_solar_str = f"\n☀️ Tomorrow's solar: ~{tmrw_kwh:.1f} kWh ({'cloudy' if cloudy else 'sunny'})"

    self_suff_str = ""

    # TOU daily cost estimate + peak coverage (requires history store)
    tou_str      = ""
    peak_cov_str = ""
    if store is not None:
        readings = store.weekly_readings(today, today)
        if not readings:
            # No DB readings — fall back to API snapshot for display
            grid_in_kwh  = t.grid_load_kwh
            grid_out_kwh = t.grid_export_kwh
            home_kwh     = t.home_use_kwh
            if home_kwh > 0:
                self_suff     = max(0.0, min(100.0, (home_kwh - grid_in_kwh) / home_kwh * 100))
                self_suff_str = f"\nSelf-sufficiency:  {self_suff:.0f}%"
        if readings:
            import_cost = export_credit = 0.0
            for dt, hours, grid_kw, home_kw, _solar_kw in integrate_intervals(readings):
                if grid_kw > 0:
                    import_cost   += grid_kw * hours * rate_at(dt)
                    grid_in_kwh   += grid_kw * hours
                elif grid_kw < 0:
                    export_credit += -grid_kw * hours * export_rate_at(dt)
                    grid_out_kwh  += -grid_kw * hours
                home_kwh += home_kw * hours
            # Peak coverage is a per-reading count (fraction of 4-9 pm polls with
            # no real grid draw), independent of energy integration.
            peak_total = peak_no_grid = 0
            for ts, grid_kw, _home_kw, _solar_kw in readings:
                try:
                    dt = datetime.fromisoformat(ts)
                except Exception:
                    continue
                if 16 <= dt.hour < 21:
                    peak_total += 1
                    if grid_kw < 0.05:  # <50 W treated as noise, not real grid draw
                        peak_no_grid += 1
            # Self-sufficiency uses DB-integrated home + grid values
            if home_kwh > 0:
                self_suff     = max(0.0, min(100.0, (home_kwh - grid_in_kwh) / home_kwh * 100))
                self_suff_str = f"\nSelf-sufficiency:  {self_suff:.0f}%"
            base_fee = base_service_cost(1)
            net = import_cost - export_credit + base_fee
            tou_str = (
                f"\n💰 Grid cost: ${import_cost:.2f} in  ·  ${export_credit:.2f} out  ·  "
                f"+${base_fee:.2f} base  →  net ${net:.2f}"
            )
            if peak_total > 0:
                pct = peak_no_grid / peak_total * 100
                if pct < 95:
                    peak_cov_str = f"\nPeak coverage (4–9 pm): {pct:.0f}% battery/solar"
                state[f"peak_cov_{today}"] = pct
            else:
                state[f"peak_cov_{today}"] = 0.0
            state[f"daily_import_cost_{today}"] = round(import_cost, 2)

    state["eod_digest_date"] = today
    logger.info("End-of-day digest sent for %s", today)
    return (
        f"📊 <b>FranklinWH Daily Summary — {now.strftime('%a %b %-d')}</b>\n"
        f"<code>Solar:    {solar_kwh:.1f} kWh\n"
        f"Grid in:  {grid_in_kwh:.1f} kWh\n"
        f"Grid out: {grid_out_kwh:.1f} kWh\n"
        f"Batt chg: {batt_chg_kwh:.1f} kWh\n"
        f"Batt dis: {batt_dis_kwh:.1f} kWh\n"
        f"Home:     {home_kwh:.1f} kWh</code>{self_suff_str}{peak_cov_str}{tou_str}\n"
        f"<code>─────────────────────</code>\n"
        f"🔋 {_soc_bar(soc)}{backup_str}{soc_7am_str}{solar_delta_str}{tmrw_solar_str}{precharge_str}"
    )


def _alert_weekly_summary(state: dict, today: str, now: datetime, store, cfg: Config) -> str | None:
    """Sunday evening: TOU-weighted import/export cost + peak savings for the week."""
    if store is None or now.weekday() != 6 or now.hour not in (21, 22):
        return None
    if state.get("weekly_summary_sent") == today:
        return None

    week_end   = now.date()
    week_start = week_end - timedelta(days=6)
    readings   = store.weekly_readings(
        week_start.strftime("%Y-%m-%d"),
        week_end.strftime("%Y-%m-%d"),
    )
    if not readings:
        return None

    import_cost   = 0.0
    export_credit = 0.0
    peak_saved    = 0.0    # on-peak hours (4–9 pm)
    sop_saved     = 0.0    # super off-peak hours

    for dt, hours, grid_kw, home_kw, _solar_kw in integrate_intervals(readings):
        period = period_at(dt)
        if grid_kw > 0:
            import_cost   += grid_kw * hours * rate_at(dt)
        elif grid_kw < 0:
            export_credit += -grid_kw * hours * export_rate_at(dt)
        # Energy served by battery+solar (not drawn from grid) × avoided rate
        batt_solar_kwh = max(0.0, home_kw - max(0.0, grid_kw)) * hours
        if period == TouPeriod.ON_PEAK:
            peak_saved += batt_solar_kwh * rate_at(dt)
        elif period == TouPeriod.SUPER_OFF_PEAK:
            sop_saved  += batt_solar_kwh * rate_at(dt)

    base_fee    = base_service_cost(7)
    net_cost    = import_cost - export_credit + base_fee
    total_saved = peak_saved + sop_saved
    week_label  = f"{week_start.strftime('%b %-d')}–{week_end.strftime('%b %-d')}"

    # Avg daily cost from stored EOD data
    daily_costs = [
        cost
        for i in range(7)
        if (cost := _safe_float(state.get(
            f"daily_import_cost_{(now.date() - timedelta(days=i)).strftime('%Y-%m-%d')}"
        ))) is not None
    ]
    avg_cost_str = f"  Avg daily import: ${sum(daily_costs) / len(daily_costs):.2f}\n" if daily_costs else ""

    # Solar prediction accuracy (±% avg error vs actual)
    cutoff = (now.date() - timedelta(days=7)).strftime("%Y-%m-%d")
    week_prs = [
        pr for k, v in state.items()
        if k.startswith("daily_pr_") and k[len("daily_pr_"):] >= cutoff
        and (pr := _safe_float(v)) is not None
    ]
    accuracy_str = ""
    if len(week_prs) >= 3:
        avg_err = sum(abs(1.0 - pr) * 100 for pr in week_prs) / len(week_prs)
        accuracy_str = f"\n🎯 Solar forecast accuracy: ±{avg_err:.1f}% avg ({len(week_prs)} days)"

    # Battery cycle count — computed from discharge throughput in the history DB,
    # more accurate than the SOC-trough method which only fires when SOC drops below 20%.
    # Extrapolate backward if the DB doesn't cover the full system lifetime.
    cycle_str = ""
    if store is not None:
        try:
            discharge_kwh = store.total_discharge_kwh()
            bat_cap       = cfg.battery_capacity_kwh
            if bat_cap > 0 and discharge_kwh > 0:
                db_cycles    = discharge_kwh / bat_cap
                first_date   = store.first_reading_date()
                # Estimate cycles for any period before the DB started
                extra_cycles = 0.0
                extra_note   = ""
                if first_date:
                    db_start    = datetime.strptime(first_date, "%Y-%m-%d")
                    install_est = db_start  # fallback — no pre-DB data
                    # Check if state has a known install date; otherwise assume Nov 2024
                    install_str = state.get("install_date")
                    if install_str:
                        try:
                            install_est = datetime.strptime(install_str, "%Y-%m-%d")
                        except ValueError:
                            pass
                    else:
                        # Default to Nov 1 2024 unless DB starts earlier
                        default_install = datetime(2024, 11, 1)
                        install_est     = min(db_start, default_install)
                    missing_days = (db_start - install_est).days
                    if missing_days > 7:
                        db_days       = max(1, (now.date() - db_start.date()).days)
                        rate_per_day  = db_cycles / db_days
                        extra_cycles  = rate_per_day * missing_days
                        extra_note    = f" (~{extra_cycles:.0f} extrapolated pre-{db_start.strftime('%b %Y')})"
                total_cycles = db_cycles + extra_cycles
                pct_used     = total_cycles / 6000 * 100
                cycles_week  = state.get("batt_cycles_this_week", 0)
                cycle_str    = (
                    f"\nBattery cycles: {cycles_week:.1f} this week  ·  "
                    f"{total_cycles:.0f} total{extra_note} ({pct_used:.1f}% of 6000 rated)"
                )
        except Exception as e:
            logger.debug("Battery cycle throughput calc failed: %s", e)
            # Fallback to legacy SOC-trough counter
            total_cycles = state.get("batt_cycle_count", 0)
            if total_cycles > 0:
                pct_used  = total_cycles / 6000 * 100
                cycle_str = f"\n🔋 Battery cycles: {total_cycles:.1f} total ({pct_used:.1f}% of 6000 rated)"
    state["batt_cycles_this_week"] = 0  # reset after sent marker

    stale_note = ""
    if rates_are_stale():
        stale_note = "\n⚠️ TOU rates may be outdated (>180 days) — check tou.py."

    state["weekly_summary_sent"] = today
    logger.info("Weekly TOU summary sent for week ending %s", today)
    return (
        f"📊 FranklinWH Weekly Summary — {week_label}\n\n"
        f"Grid cost (EV-TOU-5 rates):\n"
        f"<code>  Imported:     ${import_cost:.2f}\n"
        f"  Exported:     ${export_credit:.2f} (est.)\n"
        f"  Base service: ${base_fee:.2f}\n"
        f"  Net cost:     ${net_cost:.2f}</code>\n"
        f"{avg_cost_str}"
        f"\nEst. savings from battery + solar:\n"
        f"<code>  Peak (4–9 pm):    ${peak_saved:.2f}\n"
        f"  Super off-peak:   ${sop_saved:.2f}\n"
        f"  Total saved:      ${total_saved:.2f}</code>{stale_note}"
        f"{accuracy_str}{cycle_str}"
    )


def _alert_monthly_summary(state: dict, today: str, now: datetime, store) -> str | None:
    """19th of each month: billing-cycle summary (20th last month → 19th this month)."""
    if now.hour not in (21, 22) or store is None:
        return None
    if now.day != 19 or state.get("monthly_summary_date") == today:
        return None

    # Billing cycle: 20th of prior month → 19th of this month
    cycle_end   = now.date()
    cycle_start = (cycle_end.replace(day=1) - timedelta(days=1)).replace(day=20)
    prev_end    = cycle_start - timedelta(days=1)
    prev_start  = (prev_end.replace(day=1) - timedelta(days=1)).replace(day=20)

    cur  = store.period_totals(cycle_start.strftime("%Y-%m-%d"), cycle_end.strftime("%Y-%m-%d"))
    prev = store.period_totals(prev_start.strftime("%Y-%m-%d"), prev_end.strftime("%Y-%m-%d"))

    cur_label  = f"{cycle_start.strftime('%b %-d')} – {cycle_end.strftime('%b %-d')}"
    prev_label = f"{prev_start.strftime('%b %-d')} – {prev_end.strftime('%b %-d')}"

    def _mdelta(a: float, b: float) -> str:
        if b == 0:
            return ""
        d    = a - b
        sign = "+" if d >= 0 else ""
        return f"  ({sign}{d:.1f} kWh, {sign}{d / b * 100:.0f}%)"

    sparse_note = f"\n⚠️ Prior cycle only {prev.days_with_data}d data" if prev.days_with_data < 20 else ""

    # NEM true-up tracker: cycle import cost vs export credit + annual projection.
    # Export valued at the TOU rate (est.) for consistency with the weekly summary.
    import_cost = export_credit = 0.0
    for dt, hours, grid_kw, _home_kw, _solar_kw in integrate_intervals(
        store.weekly_readings(cycle_start.strftime("%Y-%m-%d"), cycle_end.strftime("%Y-%m-%d"))
    ):
        if grid_kw > 0:
            import_cost   += grid_kw * hours * rate_at(dt)
        elif grid_kw < 0:
            export_credit += -grid_kw * hours * export_rate_at(dt)
    days       = max(1, (cycle_end - cycle_start).days + 1)
    base_fee   = base_service_cost(days)
    net_cycle  = import_cost - export_credit + base_fee
    annual     = net_cycle / days * 365
    direction  = "net consumer (you owe)" if annual >= 0 else "net exporter (building credit)"
    trueup_str = (
        f"\n\nNEM true-up (est.):\n"
        f"<code>  Import:       ${import_cost:.2f}\n"
        f"  Export:       ${export_credit:.2f}\n"
        f"  Base service: ${base_fee:.2f}\n"
        f"  Net:          ${net_cycle:+.2f} this cycle\n"
        f"  ~${annual:+.0f}/yr at this rate — {direction}</code>"
    )

    state["monthly_summary_date"] = today
    logger.info("Billing-cycle summary sent for %s → %s", cycle_start, cycle_end)
    return (
        f"📅 FranklinWH Billing Cycle — {cur_label}\n"
        f"vs prior cycle ({prev_label})\n\n"
        f"Solar generated:\n"
        f"<code>  This:  {cur.solar_kwh:.1f} kWh{_mdelta(cur.solar_kwh, prev.solar_kwh)}\n"
        f"  Prior: {prev.solar_kwh:.1f} kWh</code>\n\n"
        f"Grid imported:\n"
        f"<code>  This:  {cur.grid_import_kwh:.1f} kWh{_mdelta(cur.grid_import_kwh, prev.grid_import_kwh)}\n"
        f"  Prior: {prev.grid_import_kwh:.1f} kWh</code>\n\n"
        f"Grid exported:\n"
        f"<code>  This:  {cur.grid_export_kwh:.1f} kWh{_mdelta(cur.grid_export_kwh, prev.grid_export_kwh)}\n"
        f"  Prior: {prev.grid_export_kwh:.1f} kWh</code>\n\n"
        f"Home used:\n"
        f"<code>  This:  {cur.home_load_kwh:.1f} kWh{_mdelta(cur.home_load_kwh, prev.home_load_kwh)}\n"
        f"  Prior: {prev.home_load_kwh:.1f} kWh</code>{sparse_note}"
        f"{trueup_str}"
    )


def _conservation_advice(soc: float, load_kw: float, bat_cap: float) -> str:
    """Backup runtime at current vs essentials-only load, with a conservation nudge.

    Essentials estimated at ~40% of current load (fridge, network, lights, a few
    outlets); returns '' if load is negligible.
    """
    if load_kw <= 0.1:
        return ""
    avail_kwh = max(0.0, soc) / 100 * bat_cap
    cur_h     = avail_kwh / load_kw
    ess_load  = max(0.2, load_kw * 0.4)
    ess_h     = avail_kwh / ess_load
    return (
        f"\n⏱ Backup: ~{cur_h:.1f} hr now  ·  ~{ess_h:.1f} hr at essentials (~{ess_load:.1f} kW)\n"
        f"Conserve: turn off AC, EV, dryer, pool pump."
    )


def _alert_grid_down(state: dict, today: str, now: datetime, c, cfg: Config) -> str | None:
    if c.grid_status != "down" or state.get("grid_down_alerted_date") == today:
        return None
    state["grid_down_alerted_date"] = today
    state["grid_down_start"]        = now.isoformat()
    state["grid_down_soc"]          = c.battery_soc_pct
    logger.info("Grid-down alert sent for %s", today)
    conservation = _conservation_advice(c.battery_soc_pct, c.home_load_kw, cfg.battery_capacity_kwh)
    gen_str = (f"\n🔌 Generator: {c.generator_production_kw:.2f} kW running"
               if c.generator_enabled and c.generator_production_kw > 0.1 else "")
    tte = _time_to_pct(c.battery_soc_pct, 0.0, cfg.battery_capacity_kwh, c.battery_use_kw)
    tte_str = f"\n⏱ ~{_fmt_hours(tte)} to empty at current load" if tte is not None else ""
    return (
        f"🔴 <b>FranklinWH: GRID DOWN at {now.strftime('%-I:%M %p')}</b>\n"
        f"🔋 {_soc_bar(c.battery_soc_pct)}  ·  Load <b>{c.home_load_kw:.2f} kW</b>\n"
        f"Solar {c.solar_production_kw:.2f} kW{gen_str}{tte_str}{conservation}"
    )


def _alert_grid_restored(state: dict, now: datetime, c, cfg: Config) -> str | None:
    if c.grid_status != "normal" or "grid_down_start" not in state:
        return None
    try:
        outage_start = datetime.fromisoformat(state["grid_down_start"])
        duration_min = (now - outage_start).total_seconds() / 60
    except (ValueError, TypeError):
        state.pop("grid_down_start", None)
        state.pop("grid_down_soc", None)
        return None

    soc_start    = state.pop("grid_down_soc", c.battery_soc_pct)
    state.pop("grid_down_start")
    soc_used     = max(0.0, soc_start - c.battery_soc_pct)
    kwh_used     = round(soc_used / 100 * cfg.battery_capacity_kwh, 1)
    dur_str      = (f"{duration_min / 60:.1f}h" if duration_min >= 60
                    else f"{duration_min:.0f} min")
    kwh_str      = f"  ·  ~{kwh_used:.1f} kWh used from battery" if kwh_used > 0.1 else ""
    logger.info("Grid-restored alert: outage lasted %s", dur_str)
    return (
        f"🟢 <b>FranklinWH: GRID RESTORED at {now.strftime('%-I:%M %p')}</b>\n"
        f"Outage lasted <b>{dur_str}</b>{kwh_str}\n"
        f"🔋 {_soc_bar(c.battery_soc_pct)}  ·  Solar {c.solar_production_kw:.2f} kW"
    )


def _alert_battery_full_cycle(state: dict, today: str, now: datetime, c) -> str | None:
    full_state = state.get("full_charge_state", "watching_for_full")
    if full_state == "watching_for_full" and c.battery_soc_pct >= 99.0:
        state["full_charge_state"] = "watching_for_discharge"
        logger.info("Full charge alert sent (%.0f%%)", c.battery_soc_pct)
        return (
            f"🔋 FranklinWH: Battery fully charged — {c.battery_soc_pct:.0f}% SoC\n"
            f"Time: {now.strftime('%-I:%M %p')}\n"
            f"Solar {c.solar_production_kw:.2f} kW  ·  Load {c.home_load_kw:.2f} kW"
        )
    if full_state == "watching_for_discharge" and c.battery_soc_pct < 90.0:
        state["full_charge_state"] = "watching_for_full"
        if 15 <= now.hour < 19 and state.get("no_longer_full_date") != today:
            state["no_longer_full_date"] = today
            logger.info("Battery discharged below 90%% alert sent (%.0f%%)", c.battery_soc_pct)
            return (
                f"🔋 FranklinWH: Battery no longer full — {c.battery_soc_pct:.0f}% SoC\n"
                f"Time: {now.strftime('%-I:%M %p')}\n"
                f"Solar {c.solar_production_kw:.2f} kW  ·  Load {c.home_load_kw:.2f} kW"
            )
        logger.info("Battery discharged below 90%% — outside 3–7 pm window, suppressed")
    return None


def _track_battery_cycles(state: dict, c) -> None:
    """Count equivalent full cycles for battery health estimation.

    A cycle = full 100%→0% depth of discharge. A shallow swing (e.g. 80%→20%)
    counts as its actual depth (0.6 cycle), not a whole cycle, so cumulative
    throughput matches the manufacturer's 6000-cycle rating.
    """
    soc = c.battery_soc_pct
    if not state.get("batt_cycle_active", False):
        if soc >= 80.0:
            state["batt_cycle_active"]   = True
            state["batt_cycle_start_soc"] = soc  # peak SoC at start of discharge
    else:
        # Track the highest SoC seen in case it kept charging past 80%
        state["batt_cycle_start_soc"] = max(state.get("batt_cycle_start_soc", soc), soc)
        if soc <= 20.0:
            depth = (state.pop("batt_cycle_start_soc", 80.0) - soc) / 100.0  # fraction of full cycle
            state["batt_cycle_active"]     = False
            state["batt_cycle_count"]      = state.get("batt_cycle_count", 0) + depth
            state["batt_cycles_this_week"] = state.get("batt_cycles_this_week", 0) + depth
            logger.debug("Battery cycle completed (%.2f depth), total=%.2f", depth, state["batt_cycle_count"])


def _alert_fast_drain(state: dict, today: str, now: datetime, c) -> str | None:
    """Always updates last_soc/last_soc_time for rate tracking."""
    prev_soc      = state.get("last_soc")
    prev_soc_time = state.get("last_soc_time")
    body = None
    if prev_soc is not None and prev_soc_time is not None:
        try:
            elapsed_h = (now - datetime.fromisoformat(prev_soc_time)).total_seconds() / 3600
            if elapsed_h > 0:
                drain_rate = (prev_soc - c.battery_soc_pct) / elapsed_h
                if drain_rate >= 8.0 and c.battery_soc_pct < 35.0 and state.get("fast_drain_alerted_date") != today:
                    state["fast_drain_alerted_date"] = today
                    logger.info("Fast drain alert sent for %s (%.0f%%/hr, %.0f%%)", today, drain_rate, c.battery_soc_pct)
                    _tte_fd = _time_to_pct(c.battery_soc_pct, 0.0, _BATTERY_CAPACITY_KWH, c.battery_use_kw)
                    _tte_fd_s = f"\n⏱ ~{_fmt_hours(_tte_fd)} to empty" if _tte_fd is not None else ""
                    body = (
                        f"⚡ <b>FranklinWH: Battery draining fast — {drain_rate:.0f}%/hr</b>\n"
                        f"🔋 {_soc_bar(c.battery_soc_pct)}  ·  Load <b>{c.home_load_kw:.2f} kW</b>  ·  "
                        f"Solar {c.solar_production_kw:.2f} kW\n"
                        f"Time: {now.strftime('%-I:%M %p')}{_tte_fd_s}"
                    )
        except (ValueError, TypeError):
            pass
    state["last_soc"]      = c.battery_soc_pct
    state["last_soc_time"] = now.isoformat()
    return body


def _alert_not_charging(state: dict, today: str, now: datetime, c) -> str | None:
    if not (10 <= now.hour < 14):
        return None
    if c.solar_production_kw <= 1.5 or c.battery_soc_pct >= 80.0 or c.battery_use_kw <= -0.2:
        return None
    if state.get("not_charging_date") == today:
        return None
    # Suppress when home load absorbs most of solar — EV charging or heavy AC
    # explains why battery isn't getting much; this isn't a fault worth alerting.
    solar_surplus_kw = c.solar_production_kw - c.home_load_kw
    if solar_surplus_kw < 0.8:
        return None
    state["not_charging_date"] = today
    logger.info("Not-charging alert sent for %s (solar=%.2f kW, load=%.2f kW, batt=%.2f kW)", today, c.solar_production_kw, c.home_load_kw, c.battery_use_kw)
    return (
        f"⚠️ <b>FranklinWH: Battery not charging despite strong solar</b>\n"
        f"Solar {c.solar_production_kw:.2f} kW  ·  Load {c.home_load_kw:.2f} kW  ·  "
        f"Battery {c.battery_use_kw:+.2f} kW  ·  SoC {c.battery_soc_pct:.0f}%\n"
        f"Time: {now.strftime('%-I:%M %p')} — check battery mode or inverter."
    )


def _alert_solar_degradation(state: dict, today: str, now: datetime) -> str | None:
    """Morning check: 7-day rolling PR median drops >5% vs 30-day baseline → possible degradation."""
    week_key = now.strftime("%G-W%V")  # ISO year-week, e.g. 2026-W23
    if now.hour not in (8, 9) or state.get("solar_degradation_alerted_week") == week_key:
        return None

    cutoff_30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    cutoff_7  = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    all_pr: list[float]    = []
    recent_pr: list[float] = []
    for k, v in state.items():
        if not k.startswith("daily_pr_"):
            continue
        date_str = k[len("daily_pr_"):]
        if date_str < cutoff_30:
            continue
        try:
            ratio = float(v)
        except (TypeError, ValueError):
            continue
        all_pr.append(ratio)
        if date_str >= cutoff_7:
            recent_pr.append(ratio)

    if len(all_pr) < 10 or len(recent_pr) < 4:
        return None  # not enough data

    def _median(lst: list[float]) -> float:
        s = sorted(lst)
        return s[len(s) // 2]

    baseline = _median(all_pr)
    recent   = _median(recent_pr)
    if baseline <= 0 or recent >= baseline * 0.95:
        return None

    drop_pct = (baseline - recent) / baseline * 100
    state["solar_degradation_alerted_week"] = week_key
    logger.info("Solar degradation alert: baseline PR=%.3f recent PR=%.3f drop=%.1f%%",
                baseline, recent, drop_pct)
    return (
        f"⚠️ <b>FranklinWH: Solar output trending down</b>\n"
        f"7-day performance ratio: {recent:.2f} vs 30-day baseline {baseline:.2f} "
        f"({drop_pct:.0f}% drop)\n"
        f"This may indicate panel soiling, shading, or inverter efficiency loss.\n"
        f"Consider cleaning panels or checking inverter logs."
    )


def _alert_capacity_fade(state: dict, today: str, now: datetime, store) -> str | None:
    """Morning check: effective usable battery capacity (kWh per 100% SoC) trending
    down vs a baseline window suggests cell degradation. Weekly throttle.

    Needs several weeks of stored battery_use_kw data before it can fire.
    """
    if now.hour not in (8, 9) or store is None:
        return None
    week_key = now.strftime("%G-W%V")
    if state.get("capacity_fade_alerted_week") == week_key:
        return None

    today_str    = now.date().strftime("%Y-%m-%d")
    recent_start = (now.date() - timedelta(days=14)).strftime("%Y-%m-%d")
    base_start   = (now.date() - timedelta(days=75)).strftime("%Y-%m-%d")
    base_end     = (now.date() - timedelta(days=21)).strftime("%Y-%m-%d")

    recent = store.capacity_samples(recent_start, today_str)
    base   = store.capacity_samples(base_start, base_end)
    if len(recent) < 3 or len(base) < 3:
        return None  # not enough clean discharge runs yet

    recent_cap = statistics.median(recent)
    base_cap   = statistics.median(base)
    if base_cap <= 0:
        return None
    fade_pct = (1 - recent_cap / base_cap) * 100
    if fade_pct < 8.0:
        return None

    state["capacity_fade_alerted_week"] = week_key
    logger.info("Capacity-fade alert: recent %.1f kWh vs baseline %.1f kWh (%.0f%%)",
                recent_cap, base_cap, fade_pct)
    return (
        f"🔋 <b>FranklinWH: Possible battery capacity fade</b>\n"
        f"Effective usable capacity ~{recent_cap:.1f} kWh recently vs ~{base_cap:.1f} kWh baseline "
        f"({fade_pct:.0f}% lower)\n"
        f"From {len(recent)} recent / {len(base)} baseline discharge runs. "
        f"Some seasonal variation is normal — watch the trend; if it persists, check warranty."
    )


def _alert_peak_streak(state: dict, today: str, now: datetime) -> str | None:
    """Evening check: last 3 consecutive days all <50% peak coverage → battery under-sized or depleting early."""
    week_key = now.strftime("%G-W%V")  # ISO year-week, e.g. 2026-W23
    if now.hour not in (21, 22) or state.get("peak_streak_alerted_week") == week_key:
        return None

    low_days = []
    check_date = now.date() - timedelta(days=1)
    for _ in range(3):
        date_str = check_date.strftime("%Y-%m-%d")
        pct = state.get(f"peak_cov_{date_str}")
        if pct is None:
            return None  # missing data — can't confirm streak
        if pct >= 50.0:
            return None  # streak broken
        low_days.append((date_str, pct))
        check_date -= timedelta(days=1)

    state["peak_streak_alerted_week"] = week_key
    logger.info("Peak-coverage streak alert: 3 consecutive days under 50%%")
    lines = "\n".join(f"  {d}: {p:.0f}%" for d, p in reversed(low_days))
    return (
        f"⚠️ <b>FranklinWH: Battery running short at peak for 3 days in a row</b>\n"
        f"{lines}\n"
        f"Battery may not be reaching 4 pm with enough charge. "
        f"Consider charging earlier or checking whether EB mode is being triggered in time."
    )


def _alert_bill_projection(
    state: dict, today: str, now: datetime, store,
) -> str | None:
    """5th of each month: project full-cycle bill from partial billing cycle data."""
    if store is None or now.day != 5 or now.hour not in (8, 9):
        return None
    if state.get("bill_projection_date") == today:
        return None

    # Billing cycle started on 20th of prior month
    cycle_start = (now.date().replace(day=1) - timedelta(days=1)).replace(day=20)
    days_so_far = (now.date() - cycle_start).days
    if days_so_far < 5:
        return None

    readings = store.weekly_readings(cycle_start.strftime("%Y-%m-%d"), today)
    if not readings:
        return None

    import_cost   = 0.0
    export_credit = 0.0
    for dt, hours, grid_kw, _home_kw, _solar_kw in integrate_intervals(readings):
        if grid_kw > 0:
            import_cost   += grid_kw * hours * rate_at(dt)
        elif grid_kw < 0:
            export_credit += -grid_kw * hours * export_rate_at(dt)

    base_actual    = base_service_cost(days_so_far)
    net_actual     = import_cost - export_credit + base_actual
    daily_net      = net_actual / days_so_far
    projected_net  = daily_net * 30
    projected_imp  = import_cost / days_so_far * 30
    projected_exp  = export_credit / days_so_far * 30
    projected_base = base_service_cost(30)
    cycle_label    = f"{cycle_start.strftime('%b %-d')} – {now.date().strftime('%b %-d')}"

    state["bill_projection_date"] = today
    logger.info("Bill projection alert: %d days, net $%.2f/day → $%.2f projected",
                days_so_far, daily_net, projected_net)
    return (
        f"💡 FranklinWH: Billing cycle projection\n"
        f"Cycle so far ({cycle_label}, {days_so_far} days):\n"
        f"<code>  Grid import:  ${import_cost:.2f}\n"
        f"  Grid export:  ${export_credit:.2f}\n"
        f"  Base service: ${base_actual:.2f}\n"
        f"  Net cost:     ${net_actual:.2f}</code>\n\n"
        f"Projected full cycle (~30 days):\n"
        f"<code>  Import:  ${projected_imp:.2f}\n"
        f"  Export:  ${projected_exp:.2f}\n"
        f"  Base:    ${projected_base:.2f}\n"
        f"  Net:     ${projected_net:.2f}  (${daily_net:.2f}/day avg)</code>"
    )


def _alert_heat_wave_prep(state: dict, today: str, now: datetime, c, outlook) -> str | None:
    """Evening alert when tomorrow's forecast exceeds 95°F — AC load spike risk."""
    if now.hour not in (21, 22) or outlook is None:
        return None
    if state.get("heat_wave_prep_date") == today:
        return None
    tomorrow = (now + timedelta(days=1)).date()
    tmrw_hours = [h for h in outlook.hours if h.time.date() == tomorrow]
    if not tmrw_hours:
        return None
    max_temp_c = max(h.temp_c for h in tmrw_hours)
    if max_temp_c < 35.0:  # 95°F
        return None
    max_temp_f = max_temp_c * 9 / 5 + 32
    state["heat_wave_prep_date"] = today
    logger.info("Heat wave prep alert: tomorrow max %.1f°C (%.0f°F)", max_temp_c, max_temp_f)
    soc = c.battery_soc_pct
    action = (
        f"Battery at {soc:.0f}% — consider switching to Emergency Backup tonight to top up before the AC load spike."
        if soc < 80 else
        f"Battery at {soc:.0f}% — well positioned. Monitor peak-hour load tomorrow."
    )
    return (
        f"🌡️ <b>FranklinWH: Heat wave tomorrow — {max_temp_f:.0f}°F forecast</b>\n"
        f"Expect higher AC load and grid risk during 4–9 pm on-peak.\n"
        f"{action}"
    )


def _alert_ev_charge_window(state: dict, today: str, now: datetime, c, cfg: Config) -> str | None:
    """Evening: recommend the cheapest window to charge an EV (super-off-peak).

    Only fires when cfg.ev_charging is set. Advisory only.
    """
    if not getattr(cfg, "ev_charging", False):
        return None
    if now.hour not in (20, 21) or state.get("ev_charge_window_date") == today:
        return None
    state["ev_charge_window_date"] = today
    # Super-off-peak overnight window: midnight–6 am (weekday rate)
    sop = rate_at(now.replace(hour=1, minute=0, second=0, microsecond=0))
    onp = rate_at(now.replace(hour=17, minute=0, second=0, microsecond=0))
    cost_line = ""
    kwh = getattr(cfg, "ev_kwh_per_session", 0.0) or 0.0
    if kwh > 0:
        save = kwh * (onp - sop)
        cost_line = (f"\n~{kwh:.0f} kWh: ${kwh * sop:.2f} at super-off-peak "
                     f"vs ${kwh * onp:.2f} on-peak (save ${save:.2f}).")
    logger.info("EV charge window alert sent for %s", today)
    return (
        f"🔌 <b>FranklinWH: Best EV charging window tonight</b>\n"
        f"Charge midnight–6 AM (super-off-peak, ${sop:.2f}/kWh) — cheapest of the day. "
        f"Avoid 4–9 PM on-peak (${onp:.2f}/kWh).{cost_line}"
    )


def _alert_storm_prep(state: dict, today: str, now: datetime, c, cfg: Config) -> str | None:
    """Evening: if an NWS storm/wind/flood alert is active and SoC < 90%, advise
    charging to 100% tonight so the battery is ready for a possible outage.
    """
    if now.hour not in (21, 22) or state.get("storm_prep_date") == today:
        return None
    if c.battery_soc_pct >= 90.0:
        return None
    try:
        events = fetch_nws_storm_alerts(cfg.lat, cfg.lon)
    except Exception:
        events = []
    if not events:
        return None
    state["storm_prep_date"] = today
    logger.info("Storm prep alert: %s", ", ".join(events))
    return (
        f"⛈️ <b>FranklinWH: Weather alert — {events[0]}</b>\n"
        f"Battery at {c.battery_soc_pct:.0f}%. Consider charging to 100% tonight "
        f"(Emergency Backup) so you have full backup if the grid goes down."
    )


def _alert_area_power_outage(state: dict, today: str, now: datetime, c, cfg: Config) -> str | None:
    """Check if CMR News wrote an outage flag for the local area."""
    if not _CMR_OUTAGE_FLAG.exists():
        state.pop("cmr_outage_alerted_date", None)
        return None
    try:
        data        = json.loads(_CMR_OUTAGE_FLAG.read_text())
        detected_at = data.get("detected_at", "")
        source      = data.get("source", "CMR News")
    except Exception:
        return None
    if state.get("cmr_outage_alerted_date") == today:
        return None
    state["cmr_outage_alerted_date"] = today
    logger.info("CMR area power outage alert bridged from %s", source)
    ts = detected_at[:16].replace("T", " ")
    # If we're actually on battery, add conservation runtime guidance.
    conservation = ""
    if c.grid_status == "down":
        conservation = _conservation_advice(c.battery_soc_pct, c.home_load_kw, cfg.battery_capacity_kwh)
        status_line = f"Your grid is DOWN — running on battery (SoC {c.battery_soc_pct:.0f}%)."
    else:
        status_line = "Your grid still reads normal — battery ready if it drops."
    return (
        f"⚡ <b>Area power outage detected nearby (via {source})</b>\n"
        f"Detected: {ts}\n"
        f"{status_line}{conservation}"
    )


def _alert_multiday_cloudy_precharge(
    state: dict, today: str, now: datetime, c, outlook, cfg: Config
) -> str | None:
    """Fire when the next 2 days of solar are both poor and battery is below 65%.

    Gives advance notice for multi-day grey stretches so the user can top up
    during today's super-off-peak window instead of scrambling tomorrow.
    """
    if now.hour not in (7, 8, 9):
        return None
    week_key = now.strftime("%G-W%V")
    if state.get("multiday_cloudy_week") == week_key:
        return None
    if outlook is None:
        return None

    soc     = c.battery_soc_pct
    bat_cap = cfg.battery_capacity_kwh

    sp = _get_system_peak_kw(state)
    if sp is None:
        return None
    cloudy = outlook.avg_ghi(48) < _GHI_CLOUDY_THRESHOLD
    if not cloudy:
        return None
    pr = _get_performance_ratio(state, cloudy=True)
    tmrw_kwh     = outlook.tomorrow_generation_kwh(sp, pr, _get_hourly_bias(state))
    day2_date    = (now + timedelta(days=2)).date()
    day2_hours   = [h for h in outlook.hours if h.time.date() == day2_date]
    from franklinwh_scraper.weather import _MIN_EFFICIENCY, _TEMP_COEFF
    day2_kwh = 0.0
    if day2_hours:
        for h in day2_hours:
            eff = max(_MIN_EFFICIENCY, 1.0 + _TEMP_COEFF * (h.panel_temp_c - 25.0))
            day2_kwh += h.ghi_wm2 / 1000.0 * sp * eff
        day2_kwh = round(day2_kwh * pr, 1)

    two_day_solar = tmrw_kwh + day2_kwh
    if two_day_solar >= bat_cap * 0.9 or soc >= 65.0:
        return None

    sop = rate_at(now.replace(hour=1, minute=0, second=0, microsecond=0))
    state["multiday_cloudy_week"] = week_key
    logger.info("Multi-day cloudy pre-charge alert: 2-day solar=%.1f kWh, SoC=%.0f%%",
                two_day_solar, soc)
    return (
        f"☁️ <b>FranklinWH: 2-day cloudy stretch ahead</b>\n"
        f"Tomorrow: ~{tmrw_kwh:.1f} kWh  ·  Day after: ~{day2_kwh:.1f} kWh  "
        f"(total ~{two_day_solar:.1f} kWh)\n"
        f"🔋 {_soc_bar(soc)} — battery won't fully recover from solar alone.\n"
        f"Charge to 80–90% now at super-off-peak (${sop:.2f}/kWh) before rates rise."
    )


def _alert_solar_surplus_overflow(
    state: dict, today: str, now: datetime, c
) -> str | None:
    """Battery ~full before 2 pm super-off-peak ends → advise Time-of-Use export mode.

    Triggers when solar is filling a near-full battery during cheap hours so the
    user can switch to TOU mode and push surplus to the grid instead of clipping.
    """
    if not (10 <= now.hour < 14):
        return None
    if state.get("surplus_overflow_date") == today:
        return None
    soc = c.battery_soc_pct
    if soc < 93.0:
        return None
    # Battery charging or holding — solar exceeds load
    if c.battery_use_kw > 0.1 or c.solar_production_kw < c.home_load_kw:
        return None
    state["surplus_overflow_date"] = today
    logger.info("Solar surplus overflow alert: SoC=%.0f%%, solar=%.2f kW", soc, c.solar_production_kw)
    return (
        f"☀️ <b>FranklinWH: Battery full — solar surplus available</b>\n"
        f"🔋 {_soc_bar(soc)}  ·  Solar {c.solar_production_kw:.2f} kW  ·  "
        f"Load {c.home_load_kw:.2f} kW\n"
        f"Time: {now.strftime('%-I:%M %p')} — super-off-peak still active (until 2 pm).\n"
        f"Consider switching to Time-of-Use mode to export surplus to grid."
    )


def _alert_solar_back_to_baseline(
    state: dict, today: str, now: datetime
) -> str | None:
    """After a solar_degradation alert, confirm when PR recovers back within 3% of baseline."""
    if now.hour not in (8, 9):
        return None
    # Only fire if a degradation alert has previously been sent
    if not state.get("solar_degradation_alerted_week"):
        return None
    if state.get("solar_recovery_alerted_week") == now.strftime("%G-W%V"):
        return None

    cutoff_30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    cutoff_7  = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    all_pr:    list[float] = []
    recent_pr: list[float] = []
    for k, v in state.items():
        if not k.startswith("daily_pr_"):
            continue
        date_str = k[len("daily_pr_"):]
        if date_str < cutoff_30:
            continue
        try:
            ratio = float(v)
        except (TypeError, ValueError):
            continue
        all_pr.append(ratio)
        if date_str >= cutoff_7:
            recent_pr.append(ratio)

    if len(all_pr) < 10 or len(recent_pr) < 4:
        return None

    def _med(lst: list[float]) -> float:
        s = sorted(lst); return s[len(s) // 2]

    baseline = _med(all_pr)
    recent   = _med(recent_pr)
    if baseline <= 0 or recent < baseline * 0.97:
        return None  # still degraded or not enough recovery

    week_key = now.strftime("%G-W%V")
    state["solar_recovery_alerted_week"] = week_key
    logger.info("Solar back-to-baseline: recent PR=%.3f baseline=%.3f", recent, baseline)
    return (
        f"✅ <b>FranklinWH: Solar output back to normal</b>\n"
        f"7-day performance ratio {recent:.2f} is within 3% of 30-day baseline {baseline:.2f}.\n"
        f"Previous degradation alert has resolved — panels appear clean and healthy."
    )


def _alert_tou_rates_stale(state: dict, today: str, now: datetime) -> str | None:
    """One-time critical alert the first time TOU rates cross the 180-day staleness
    threshold — previously this only surfaced as a buried note in the weekly summary,
    easy to miss for months while cost estimates silently drift."""
    if not rates_are_stale(now.date()):
        return None
    if state.get("tou_stale_alerted"):
        return None
    state["tou_stale_alerted"] = today
    logger.info("TOU rates stale alert sent for %s", today)
    return (
        "⚠️ <b>FranklinWH: TOU rates may be outdated</b>\n"
        "It's been over 180 days since the rate schedule in tou.py was last updated. "
        "SDG&E typically revises rates ~twice a year — check their current EV-TOU-5 "
        "schedule and update _RATES / _RATES_EFFECTIVE_DATE in tou.py if it changed.\n"
        "Cost estimates and export-arbitrage timing may be off until this is refreshed."
    )


_WEATHER_STALE_HOURS = 3  # age of served cache before we warn the forecast is stuck


def _alert_weather_stale(state: dict, today: str, now: datetime) -> str | None:
    """One-time alert when the cached weather forecast has gone stale because
    fetch_solar_outlook has been failing repeatedly — otherwise this fails
    silently forever per _fetch_outlook_cached's stale-cache fallback, and
    solar predictions quietly run on old data."""
    fetched_at = _outlook_cache.get("fetched_at")
    if fetched_at is None:
        return None
    age_hours = (time.time() - fetched_at) / 3600
    if age_hours < _WEATHER_STALE_HOURS:
        state["weather_stale_alerted"] = False  # fresh fetch succeeded — allow re-firing later
        return None
    if state.get("weather_stale_alerted"):
        return None
    state["weather_stale_alerted"] = True
    logger.info("Weather staleness alert sent for %s (age %.1fh)", today, age_hours)
    return (
        f"⚠️ <b>FranklinWH: Weather forecast stale</b>\n"
        f"Open-Meteo hasn't returned fresh data in over {age_hours:.0f}h — solar "
        f"predictions and forecasts are running on old data until this recovers."
    )


def _check_peak_alerts(stats, cfg: Config, out: Path, outlook=None, usage_forecast=None, store=None) -> None:
    if not cfg.imessage_phone and not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        return

    now   = datetime.now()
    today = now.strftime("%Y-%m-%d")
    c     = stats.current

    with _state_lock(out):
        state = _load_peak_state(out)
        _calibrate_solar(state, c.solar_production_kw, outlook)
        _calibrate_solar_hourly(state, c.solar_production_kw, outlook, now)
        _track_battery_cycles(state, c)
        _candidates = [
            ("morning_preview",   lambda: _alert_morning_preview(state, today, now, c, outlook, usage_forecast, store, cfg)),
            ("grid_import",       lambda: _alert_grid_import(state, today, now, c)),
            ("eb_ready",          lambda: _alert_eb_ready(state, today, now, c)),
            ("low_soc_1pm",       lambda: _alert_low_soc_1pm(state, today, now, c)),
            ("low_morning_solar", lambda: _alert_low_morning_solar(state, today, now, c)),
            ("solar_stopped",     lambda: _alert_solar_stopped(state, today, now, c)),
            ("low_noon_soc",      lambda: _alert_low_noon_soc(state, today, now, c)),
            ("export_arbitrage",  lambda: _alert_export_arbitrage(state, today, now, c, cfg, usage_forecast)),
            ("eod_digest",        lambda: _alert_eod_digest(state, today, now, stats, cfg, outlook, usage_forecast, store)),
            ("weekly_summary",    lambda: _alert_weekly_summary(state, today, now, store, cfg)),
            ("monthly_summary",   lambda: _alert_monthly_summary(state, today, now, store)),
            ("grid_down",         lambda: _alert_grid_down(state, today, now, c, cfg)),
            ("grid_restored",     lambda: _alert_grid_restored(state, now, c, cfg)),
            ("fast_drain",        lambda: _alert_fast_drain(state, today, now, c)),
            ("not_charging",      lambda: _alert_not_charging(state, today, now, c)),
            ("solar_degradation",    lambda: _alert_solar_degradation(state, today, now)),
            ("solar_back_to_baseline", lambda: _alert_solar_back_to_baseline(state, today, now)),
            ("capacity_fade",        lambda: _alert_capacity_fade(state, today, now, store)),
            ("peak_streak",          lambda: _alert_peak_streak(state, today, now)),
            ("bill_projection",      lambda: _alert_bill_projection(state, today, now, store)),
            ("heat_wave_prep",       lambda: _alert_heat_wave_prep(state, today, now, c, outlook)),
            ("multiday_cloudy_precharge", lambda: _alert_multiday_cloudy_precharge(state, today, now, c, outlook, cfg)),
            ("solar_surplus_overflow",    lambda: _alert_solar_surplus_overflow(state, today, now, c)),
            ("storm_prep",           lambda: _alert_storm_prep(state, today, now, c, cfg)),
            ("ev_charge_window",     lambda: _alert_ev_charge_window(state, today, now, c, cfg)),
            ("area_power_outage",    lambda: _alert_area_power_outage(state, today, now, c, cfg)),
            ("tou_rates_stale",      lambda: _alert_tou_rates_stale(state, today, now)),
            ("weather_stale",        lambda: _alert_weather_stale(state, today, now)),
        ]
        to_send: list[str] = []
        for _name, _fn in _candidates:
            if _alert_enabled(cfg, _name):
                _body = _fn()
                if _body:
                    to_send.append(_body)
        _save_peak_state(out, state)

    for body in to_send:
        _send_alert(body, cfg)

