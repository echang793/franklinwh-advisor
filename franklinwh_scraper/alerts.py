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
                  cycle_bounds, export_rate_at, on_peak_window,
                  peak_export_hour, period_at, rate_at, rates_are_stale)
from .predictor import predict
from .savings import compute as savings_compute
from .weather import (_outlook_cache, fetch_nws_storm_alerts)

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
    # Clamp outliers to 1.4 rather than dropping them — a big under-prediction
    # day (ratio > 1.4) is exactly the signal that should pull the EWMA up.
    if cloudy:
        samples = [min(v, 1.4) for v in state.get("perf_ratio_cloudy_samples", [])]
        if len(samples) < 3:
            sunny = [min(v, 1.4) for v in state.get("perf_ratio_samples", [])]
            if len(sunny) >= 3:
                return max(_ewma(sunny) * 1.10, 0.60)
            return 0.85  # reasonable prior: cloudy panels run cooler
        return max(_ewma(samples), 0.55)
    else:
        samples = [min(v, 1.4) for v in state.get("perf_ratio_samples", [])]
        if len(samples) < 3:
            return 1.0
        return max(_ewma(samples), 0.60)


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
    _log_alert(body, cfg, urgent)


def _log_alert(body: str, cfg: Config, urgent: bool) -> None:
    """Append the alert to output/alerts_log.jsonl for the web dashboard feed."""
    try:
        path = Path(cfg.output_dir) / "alerts_log.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "urgent": urgent,
                "body": body,
            }) + "\n")
    except OSError as e:
        logger.debug("Alert log write failed: %s", e)


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

# Alerts that set the `urgent` flag on the webhook payload and in the alert
# log. Deliberately NOT the same set as _ALWAYS_ON_ALERTS: grid_restored is
# always-on because you need to know the outage ended, but it's an all-clear —
# it must never be escalated the way the outage itself is.
#
# Until this existed, _check_peak_alerts never passed the urgent argument at
# all, so notify_webhook's urgent flag was dead and every entry in
# alerts_log.jsonl was urgent:false — including grid_down.
_URGENT_ALERTS = frozenset({"grid_down", "area_power_outage", "fast_drain"})


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
    # Orphan from the battery-full-cycle alert removed in 49c6632 (noisy
    # duplicate). It isn't date-suffixed and matches no _prune_old_state rule,
    # so it would otherwise sit in the state file forever.
    state.pop("full_charge_state", None)
    return state


def _safe_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_DATE_KEYED_PREFIXES = (
    "daily_pr_", "peak_cov_",
    # These three are written daily but don't end in a literal "_date" suffix
    # and previously matched none of the prune rules — the state file grew
    # by one key per day per prefix, forever, for the life of the install.
    "predicted_kwh_", "predicted_avg_ghi_", "daily_import_cost_",
    "outages_", "sundown_pred_", "savings_daily_",
)


def _prune_old_state(state: dict) -> dict:
    """Drop date-keyed entries older than 30 days (or week-keyed dedup
    markers older than ~4 weeks) to prevent unbounded growth."""
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    cutoff_week = (datetime.now() - timedelta(days=30)).strftime("%G-W%V")
    pruned = {}
    for k, v in state.items():
        if k.endswith("_date") and isinstance(v, str) and v < cutoff:
            continue
        # Weekly dedup markers (solar_degradation_alerted_week, etc.) store an
        # ISO "YYYY-Www" string, which sorts lexically the same way dates do.
        if k.endswith("_week") and isinstance(v, str) and v < cutoff_week:
            continue
        for prefix in _DATE_KEYED_PREFIXES:
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

def _calibrate_solar(state: dict, solar_kw: float, outlook, now: datetime | None = None) -> None:
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
    # Only sample near solar noon: GHI stays >600 well into the afternoon while
    # panel output falls off with sun azimuth, so late-day samples systematically
    # drag the peak-kW estimate below true midday capability.
    if not (10 <= (now or datetime.now()).hour < 14):
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
                state["solar_cal_samples"] = samples[-240:]
                pending = []
            state["solar_cal_pending"] = pending
            return

    state["solar_cal_pending"] = []
    samples.append(sample)
    # ~48 midday samples/day at 5-min polls — 240 spans ~5 days instead of one.
    state["solar_cal_samples"] = samples[-240:]


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
        # 12 samples/hour/day at 5-min polls — 360 spans ~30 days so the median
        # reflects a month of weather, not the last 2-3 days.
        state[key] = samples[-360:]


def _get_hourly_bias(state: dict) -> dict[int, float]:
    """Return per-hour learned solar correction factors (EWMA of samples, min 5).

    Was a flat 30-day median. The array's per-hour shading profile keeps
    shifting as day length changes through the seasons — e.g. the 2026-08
    investigation into a month of ~4-6% low-biased predictions found hour 7
    trending 0.97->1.75 and hour 17 trending 0.60->0.38 within the same
    window, a real physical drift a flat median of the whole month can't
    track. `perf_ratio` already uses this same EWMA for exactly that
    reason (see _ewma) — using the same weighting here means both
    correction layers chase a moving target at the same speed instead of
    the daily one converging while this one lags weeks behind it. No
    extra clamping needed: EWMA is a convex combination, so it can't leave
    the [0.3, 2.0] range samples are already restricted to on append.
    """
    bias: dict[int, float] = {}
    for h in range(24):
        samples = state.get(f"solar_bias_h{h}", [])
        if len(samples) >= 5:
            bias[h] = _ewma(samples)
    return bias


_SOLAR_CAL_LOG_FILE = "solar_calibration_log.jsonl"


def _log_solar_calibration_inputs(
    cfg: Config | None, today: str, now: datetime, *,
    system_peak_kw: float, perf_ratio: float, hourly_bias: dict[int, float],
    avg_ghi: float, cloudy_day: bool, predicted_kwh: float, cal_samples_n: int,
) -> None:
    """Append the exact inputs behind today's solar prediction to a JSONL log.

    `state` only ever holds *current* calibration values — every morning
    overwrites perf_ratio/hourly_bias/system_peak_kw in place, so a
    surprising-in-hindsight prediction can't be diagnosed after the fact.
    Two real cases (2026-07-14 and 2026-07-26, kWh error +46% and +34% with
    GHI forecast error in the normal range both days — the miss was in the
    calibration, not the weather) could only be half-explained from the
    surrounding cloud-cover trend, because there was no record of what
    perf_ratio/hourly_bias actually were that morning. This is the fix: one
    line per day, append-only, survives every subsequent recalibration.
    """
    if cfg is None:
        return
    try:
        path = Path(cfg.output_dir) / _SOLAR_CAL_LOG_FILE
        entry = {
            "date": today,
            "timestamp": now.isoformat(),
            "system_peak_kw": round(system_peak_kw, 3),
            "perf_ratio": round(perf_ratio, 3),
            "cloudy_day": cloudy_day,
            "avg_ghi": round(avg_ghi, 1),
            "predicted_kwh": predicted_kwh,
            "cal_samples_n": cal_samples_n,
            "hourly_bias": {str(h): round(v, 3) for h, v in hourly_bias.items()},
        }
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        logger.debug("Solar calibration log write failed", exc_info=True)


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
        system_peak_kw = _get_system_peak_kw(state)  # P75 — consistent with EOD digest
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
        _log_solar_calibration_inputs(
            cfg, today, now, system_peak_kw=system_peak_kw, perf_ratio=perf_ratio,
            hourly_bias=hourly_bias, avg_ghi=avg_ghi, cloudy_day=cloudy_day,
            predicted_kwh=gen_kwh, cal_samples_n=len(cal_samples),
        )

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


def _alert_low_soc_1pm(state: dict, today: str, now: datetime, c, cfg: Config) -> str | None:
    in_window = now.hour == 13
    if not in_window or c.battery_soc_pct >= 40.0:
        return None
    if state.get("low_soc_1pm_alerted_date") == today:
        return None
    state["low_soc_1pm_alerted_date"] = today
    logger.info("Low 1 pm SoC alert sent for %s (%.0f%%)", today, c.battery_soc_pct)
    # cfg, not the module constant — the constant is only a fallback default,
    # so a user with a 30 kWh system used to get a time-to-empty computed
    # against 13.6 kWh.
    cap = cfg.battery_capacity_kwh or _BATTERY_CAPACITY_KWH
    tte = _time_to_pct(c.battery_soc_pct, 0.0, cap, c.battery_use_kw)
    tte_str = f"⏱ ~{_fmt_hours(tte)} to empty · " if tte is not None else ""
    return (
        f"🟡 <b>FranklinWH: Battery low at {now.strftime('%-I:%M %p')}</b>\n"
        f"🔋 {_soc_bar(c.battery_soc_pct)} — grid import risk during 4–9 pm peak\n"
        f"Solar {c.solar_production_kw:.2f} kW  ·  Load {c.home_load_kw:.2f} kW\n"
        + tte_str
        + "Consider switching to Emergency Backup to charge before peak."
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
    """When the battery is full and won't be needed for self-supply, advise
    exporting surplus to grid at the day's highest-value export hour —
    Aug/Sep use SDG&E's published boosted per-hour rates, other months use
    the flat NBT avoided-cost floor (see peak_export_hour). Runs year-round
    now instead of the old hard Aug/Sep-only gate.

    Advisory only — does not command the inverter.
    """
    peak_hour, peak_rate = peak_export_hour(now.month)

    # Fire late-morning through mid-afternoon so the user has lead time before
    # the export hour, once solar has had a chance to fill the battery. Window
    # extends to 3 pm to catch SoC crossing 85% late (e.g. cloudy morning that
    # clears by early afternoon) — still once-per-day gated.
    if now.hour not in (11, 12, 13, 14, 15) or state.get("export_arb_date") == today:
        return None

    soc = c.battery_soc_pct
    if soc < 85.0:
        return None  # not enough surplus to bother

    bat_cap     = cfg.battery_capacity_kwh
    reserve_pct = 20.0  # keep a floor for overnight / backup
    exportable_kwh = max(0.0, (soc - reserve_pct) / 100 * bat_cap)

    # Subtract predicted self-supply need at the export hour (net_kw = solar − load).
    # Only apply when that specific hour's own confidence is not "none" — gating
    # on the aggregate forecast.confidence would let one unrelated sparse hour
    # elsewhere in the day suppress this correction, even though only the single
    # matched hour is actually used below.
    if usage_forecast and usage_forecast.hours:
        for p in usage_forecast.hours:
            if p.dt.date() == now.date() and p.dt.hour == peak_hour:
                if p.confidence != "none":
                    exportable_kwh = max(0.0, exportable_kwh - max(0.0, -p.net_kw))
                break

    if exportable_kwh < 0.5:
        return None

    credit = exportable_kwh * peak_rate
    if credit < 1.0:
        # Outside Aug/Sep the rate is the flat $0.05 floor, not a boosted
        # hourly rate — without this, a routine day would "opportunity"-
        # alert for a $0.10-$0.30 credit that isn't worth the notification.
        return None
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


def _predict_overnight_soc(
    usage_forecast, now: datetime, soc: float, bat_cap: float,
) -> tuple[float, str] | None:
    """Predicted SoC at tomorrow's solar start, or None below min confidence.

    Returns (predicted_pct, hour_label). Shared by the normal 7am prediction
    and the manual EV-charging counterfactual so both use identical window
    selection and confidence gating.
    """
    if not (usage_forecast and usage_forecast.hours):
        return None
    # Find the first forecasted hour tomorrow where solar meaningfully starts,
    # rather than a fixed clock time — sunrise (and thus the useful "how low
    # did the battery get overnight" checkpoint) shifts several hours across
    # the year, so a fixed 6 am either checks too early in winter or misses
    # the tail of the draw-down in summer.
    sunrise_dt = next(
        (p.dt for p in usage_forecast.hours if p.dt > now and p.predicted_solar_kw > 0.1),
        None,
    )
    # Round to the nearest whole hour for the label — forecast hours are
    # offset from "now"'s minute, not aligned to :00.
    if sunrise_dt is not None:
        sunrise_hour = sunrise_dt.replace(minute=0, second=0, microsecond=0)
        if sunrise_dt.minute >= 30:
            sunrise_hour += timedelta(hours=1)
    else:
        sunrise_hour = (now + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)

    night_hours = [p for p in usage_forecast.hours if now <= p.dt < sunrise_hour]
    # Gate on the night window's own confidence, not the worst hour across
    # the whole 24h forecast — a sparse hour later in the day (e.g. 3 pm
    # tomorrow) shouldn't suppress a prediction that only uses tonight's data.
    night_confs = {p.confidence for p in night_hours}
    night_conf  = next(
        (c for c in ("none", "low", "medium", "high") if c in night_confs),
        "none",
    )
    if not night_hours or night_conf == "none":
        return None

    night_net_kwh = sum(p.net_kw for p in night_hours)
    pred_pct      = max(0.0, min(100.0, soc + night_net_kwh / bat_cap * 100))
    return pred_pct, sunrise_hour.strftime("%-I %p")


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
        # Right-align the numbers (not just the labels) so the "kWh" column
        # lines up even when predicted/actual/delta have different digit widths.
        pred_str  = f"{predicted_kwh:>5.1f}"
        act_str   = f"{actual_kwh:>5.1f}"
        delta_str = f"{sign}{delta_kwh:.1f}"
        delta_str = f"{delta_str:>5}"
        solar_delta_str = (
            f"\n🎯 Solar forecast vs actual:\n"
            f"<code>  Predicted: {pred_str} kWh\n"
            f"  Actual:    {act_str} kWh\n"
            f"  Delta:     {delta_str} kWh ({sign}{delta_kwh / predicted_kwh * 100:.0f}%)</code>"
        )

    sundown_acc_str = ""
    sundown_pred = state.get(f"sundown_pred_{today}")
    if sundown_pred:
        pred_pct = sundown_pred["pct"]
        actual_pct = None
        if store is not None:
            actual_pct = store.soc_near(sundown_pred["dt"])
        pred_dt = datetime.fromisoformat(sundown_pred["dt"])
        if actual_pct is None:
            # No reading landed within soc_near's +/-30min window around the
            # predicted time — falling back to the current (digest-time,
            # ~9-10pm) SoC used to get silently presented as if it were the
            # sundown-time reading. The battery keeps discharging through
            # the evening between sundown and the digest, so that made an
            # accurate prediction look like a large miss. Label it instead.
            actual_pct = soc
            delta = actual_pct - pred_pct
            sundown_acc_str = (
                f"\n🌇 /sundown accuracy: predicted {pred_pct:.0f}% at ~{pred_dt.strftime('%-I:%M %p')} — "
                f"no reading near that time, using now's {actual_pct:.0f}% instead ({delta:+.0f} pt, not directly comparable)."
            )
        else:
            delta = actual_pct - pred_pct
            sundown_acc_str = (
                f"\n🌇 /sundown accuracy (~{pred_dt.strftime('%-I:%M %p')}): "
                f"predicted {pred_pct:.0f}%, actual {actual_pct:.0f}% ({delta:+.0f} pt)"
            )

    soc_6am_str = ""
    # The digest builds its own live-anchored forecast rather than trusting
    # the shared `usage_forecast` param to already be anchored — that param
    # also drives advisor.recommend()'s Emergency-Backup decision, and a
    # single noisy poll (a kettle running for 5 min) shouldn't ripple into
    # "switch to Emergency Backup" just because it also happens to touch the
    # digest's 7am estimate. Falls back to the passed-in forecast when no
    # store is available (matches pre-nowcast behavior for those callers).
    digest_forecast = usage_forecast
    cloudy_now = outlook.avg_ghi(12) < _GHI_CLOUDY_THRESHOLD if outlook else False
    if store is not None:
        try:
            digest_forecast = predict(
                store, 24, outlook=outlook,
                system_peak_kw=_get_system_peak_kw(state),
                perf_ratio=_get_performance_ratio(state, cloudy=cloudy_now),
                avg_temp_c=outlook.avg_temp_c(24) if outlook else 22.0,
                hourly_bias=_get_hourly_bias(state),
                current_load_kw=c.home_load_kw,
            )
        except Exception:
            # Never let the digest's live-anchor recompute take the whole
            # alert down — fall back to whatever forecast was passed in.
            logger.exception("EOD digest: live-anchored forecast failed")
            digest_forecast = usage_forecast
    overnight = _predict_overnight_soc(digest_forecast, now, soc, bat_cap)
    if overnight is not None:
        # "Without EV" is the live-anchored baseline above, which on a
        # typical night doesn't include EV charging. "With EV" adds
        # cfg.ev_charging_kw (typical overnight draw) on top of it, so both
        # numbers are available up front to decide whether to charge — no
        # separate toggle needed.
        pred_soc_6am, hour_label = overnight
        has_ev = getattr(cfg, "ev_charging", False)
        label  = "Without EV charging" if has_ev else "Predicted SoC"
        soc_6am_str = f"\n🌅 {label} @ {hour_label}: ~{pred_soc_6am:.0f}%"

        if has_ev and store is not None:
            with_ev_load = c.home_load_kw + cfg.ev_charging_kw
            with_ev_overnight = None
            try:
                with_ev_forecast = predict(
                    store, 24, outlook=outlook,
                    system_peak_kw=_get_system_peak_kw(state),
                    perf_ratio=_get_performance_ratio(state, cloudy=cloudy_now),
                    avg_temp_c=outlook.avg_temp_c(24) if outlook else 22.0,
                    hourly_bias=_get_hourly_bias(state),
                    current_load_kw=with_ev_load,
                )
                with_ev_overnight = _predict_overnight_soc(with_ev_forecast, now, soc, bat_cap)
            except Exception:
                logger.exception("EOD digest: with-EV forecast failed")
            if with_ev_overnight is not None:
                with_ev_pct, _ = with_ev_overnight
                soc_6am_str += (
                    f"\n🔌 With EV charging (~{cfg.ev_charging_kw:.1f} kW): ~{with_ev_pct:.0f}%"
                )

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
    attribution_str = ""

    # Multi-outage-per-day summary — outages_{date} is logged in full by
    # _alert_grid_restored on every restore, not just the most recent one.
    outage_str = ""
    today_outages = state.get(f"outages_{today}", [])
    if today_outages:
        total_min = sum(o["duration_min"] for o in today_outages)
        total_kwh = sum(o["kwh_used"] for o in today_outages)
        dur_str = f"{total_min/60:.1f}h" if total_min >= 60 else f"{total_min:.0f} min"
        n = len(today_outages)
        outage_str = (
            f"\n🔴 {n} grid outage{'s' if n != 1 else ''} today — {dur_str} total"
            + (f", ~{total_kwh:.1f} kWh from battery" if total_kwh > 0.1 else "")
        )

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
            # Self-sufficiency: prefer the gateway's own measured path split
            # over the derived (home - grid_in)/home. The derived version
            # counts grid→battery charging as household consumption, so it
            # under-reports on any day the battery was charged from the grid.
            attr = store.daily_attribution(today) if store is not None else None
            if attr and sum(attr) > 0:
                batt_load, solar_load, grid_load = attr
                served = batt_load + solar_load + grid_load
                self_suff     = max(0.0, min(100.0, (batt_load + solar_load) / served * 100))
                self_suff_str = f"\nSelf-sufficiency:  {self_suff:.0f}%"
                # Worded as PATHS, not sources: solar_load is *direct*
                # solar→home only. Solar that charged the battery and served
                # load at 6pm lands under battery→home, so labelling this
                # "Solar: 30%" would badly understate solar's real share.
                attribution_str = (
                    f"\nServed by:  Battery {batt_load:.1f} ({batt_load / served * 100:.0f}%)"
                    f"  ·  Solar direct {solar_load:.1f} ({solar_load / served * 100:.0f}%)"
                    f"  ·  Grid {grid_load:.1f} ({grid_load / served * 100:.0f}%)"
                )
            elif home_kwh > 0:
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

            # Accumulate savings one day at a time. Deliberately incremental
            # rather than a full-history rescan: rollup_old_readings
            # downsamples data past 180 days, so a lifetime total recomputed
            # from scratch would silently drift downward as history ages out.
            _day_sv = savings_compute(integrate_intervals(readings))
            state[f"savings_daily_{today}"] = {
                "vs_grid": _day_sv.saved_vs_grid_only,
                "vs_solar": _day_sv.saved_vs_solar_only,
            }
            _cum = state.get("savings_cumulative")
            if not isinstance(_cum, dict):
                _cum = {"through": "", "vs_grid": 0.0, "vs_solar": 0.0, "days": 0}
            # Guard against double-counting if the digest ever re-runs.
            if today > _cum.get("through", ""):
                _cum = {
                    "through": today,
                    "vs_grid": round(_cum.get("vs_grid", 0.0) + _day_sv.saved_vs_grid_only, 2),
                    "vs_solar": round(_cum.get("vs_solar", 0.0) + _day_sv.saved_vs_solar_only, 2),
                    "days": int(_cum.get("days", 0)) + 1,
                }
                state["savings_cumulative"] = _cum

    state["eod_digest_date"] = today
    logger.info("End-of-day digest sent for %s", today)
    return (
        f"📊 <b>FranklinWH Daily Summary — {now.strftime('%a %b %-d')}</b>\n"
        f"<code>Solar:    {solar_kwh:.1f} kWh\n"
        f"Grid in:  {grid_in_kwh:.1f} kWh\n"
        f"Grid out: {grid_out_kwh:.1f} kWh\n"
        f"Batt chg: {batt_chg_kwh:.1f} kWh\n"
        f"Batt dis: {batt_dis_kwh:.1f} kWh\n"
        f"Home:     {home_kwh:.1f} kWh</code>{attribution_str}{self_suff_str}{peak_cov_str}{tou_str}{outage_str}\n"
        f"<code>─────────────────────</code>\n"
        f"🔋 {_soc_bar(soc)}{soc_6am_str}{solar_delta_str}{sundown_acc_str}{tmrw_solar_str}{precharge_str}"
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

    # One shared definition of "saved" (savings.compute) instead of this
    # function's own. The old inline version bucketed only ON_PEAK and
    # SUPER_OFF_PEAK and never added export credit, so it dropped every
    # off-peak hour — measured against real data that under-reported the
    # week's savings by about half.
    _sv = savings_compute(integrate_intervals(readings))
    import_cost   = _sv.actual_import_cost
    export_credit = _sv.actual_export_credit
    peak_saved    = _sv.saved_on_peak
    sop_saved     = _sv.saved_super_off_peak
    offpeak_saved = _sv.saved_off_peak

    base_fee    = base_service_cost(7)
    net_cost    = import_cost - export_credit + base_fee
    total_saved = _sv.saved_vs_grid_only
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
                # Extrapolate back to the install date only if we actually
                # know it. This used to fall back to a hardcoded Nov 2024,
                # inventing however many cycles that implied — and since
                # install_date was read but never written by any code, the
                # invented default was what everyone got.
                extra_cycles = 0.0
                extra_note   = ""
                since_note   = ""
                if first_date:
                    db_start    = datetime.strptime(first_date, "%Y-%m-%d")
                    install_str = getattr(cfg, "install_date", "") or state.get("install_date") or ""
                    install_est = None
                    if install_str:
                        try:
                            install_est = datetime.strptime(install_str, "%Y-%m-%d")
                        except ValueError:
                            install_est = None
                    if install_est is not None:
                        missing_days = (db_start - install_est).days
                        if missing_days > 7:
                            db_days       = max(1, (now.date() - db_start.date()).days)
                            rate_per_day  = db_cycles / db_days
                            extra_cycles  = rate_per_day * missing_days
                            extra_note    = f" (~{extra_cycles:.0f} extrapolated pre-{db_start.strftime('%b %Y')})"
                    else:
                        # No install date — say what the number actually is
                        # rather than implying it's lifetime.
                        since_note = f" since {db_start.strftime('%b %Y')} (tracking start)"
                total_cycles = db_cycles + extra_cycles
                pct_used     = total_cycles / 6000 * 100
                cycles_week  = state.get("batt_cycles_this_week", 0)
                cycle_str    = (
                    f"\nBattery cycles: {cycles_week:.1f} this week  ·  "
                    f"{total_cycles:.0f}{since_note or ' total'}{extra_note} "
                    f"({pct_used:.1f}% of 6000 rated)"
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
        f"  Off-peak:         ${offpeak_saved:.2f}\n"
        f"  Super off-peak:   ${sop_saved:.2f}\n"
        f"  Export credit:    ${export_credit:.2f}\n"
        f"  Total saved:      ${total_saved:.2f}</code>{stale_note}"
        f"{accuracy_str}{cycle_str}"
    )


def _alert_monthly_summary(state: dict, today: str, now: datetime, store, cfg: Config) -> str | None:
    """Last day of the billing cycle: cycle summary vs the prior cycle."""
    if now.hour not in (21, 22) or store is None:
        return None
    # Fires on the cycle's final day, derived from the configured start day
    # rather than a hardcoded 19th — see tou.cycle_bounds.
    start_day = getattr(cfg, "billing_cycle_start_day", 20)
    cycle_start, cycle_end = cycle_bounds(now.date(), start_day)
    if now.date() != cycle_end or state.get("monthly_summary_date") == today:
        return None

    prev_start, prev_end = cycle_bounds(cycle_start - timedelta(days=1), start_day)

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
    is_consumer = annual >= 0
    direction  = "net consumer (you owe)" if is_consumer else "net exporter (building credit)"

    # Proactive sign-change flag: crossing from net-exporter to net-consumer
    # (or back) mid-year is the kind of surprise that shows up as an
    # unexpected true-up bill if nobody's watching the trend month to month.
    prev_side  = state.get("nem_direction")
    this_side  = "consumer" if is_consumer else "exporter"
    flip_str   = ""
    if prev_side is not None and prev_side != this_side:
        flip_str = (
            f"\n\n🔀 <b>You've crossed over to {direction} this cycle</b> "
            f"(was {'net consumer' if prev_side == 'consumer' else 'net exporter'} last cycle) — "
            f"expect a different true-up direction than you're used to."
        )
    state["nem_direction"] = this_side

    trueup_str = (
        f"\n\nNEM true-up (est.):\n"
        f"<code>  Import:       ${import_cost:.2f}\n"
        f"  Export:       ${export_credit:.2f}\n"
        f"  Base service: ${base_fee:.2f}\n"
        f"  Net:          ${net_cycle:+.2f} this cycle\n"
        f"  ~${annual:+.0f}/yr at this rate — {direction}</code>"
    )

    # Running total, accumulated a day at a time by the EOD digest. Shown
    # here because this is the message the user compares against the real
    # bill. Priced at current rates — see savings.py.
    lifetime_str = ""
    _cum = state.get("savings_cumulative")
    if isinstance(_cum, dict) and _cum.get("days"):
        lifetime_str = (
            f"\n\n💰 Saved vs. no battery/solar:\n"
            f"<code>  ${_cum.get('vs_grid', 0.0):.2f} over {_cum['days']} days"
            f"  (~${_cum.get('vs_grid', 0.0) / max(1, _cum['days']):.2f}/day)</code>\n"
            f"<i>At current rates, excluding the base service charge.</i>"
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
        f"{trueup_str}{flip_str}{lifetime_str}"
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
    # Clear the per-day dedup flag on restore, not just on outage-start — grid
    # can drop more than once in a day, and grid_down/grid_restored are
    # always-on safety alerts that must not go silent for a second outage.
    state.pop("grid_down_alerted_date", None)
    soc_used     = max(0.0, soc_start - c.battery_soc_pct)
    kwh_used     = round(soc_used / 100 * cfg.battery_capacity_kwh, 1)
    dur_str      = (f"{duration_min / 60:.1f}h" if duration_min >= 60
                    else f"{duration_min:.0f} min")
    kwh_str      = f"  ·  ~{kwh_used:.1f} kWh used from battery" if kwh_used > 0.1 else ""
    logger.info("Grid-restored alert: outage lasted %s", dur_str)

    # Log every outage for today, not just the dedup flag — lets the EOD
    # digest report "N outages today" instead of only ever knowing about
    # the single most recent one.
    today_key = outage_start.strftime("%Y-%m-%d")
    log = state.get(f"outages_{today_key}", [])
    log.append({"start": outage_start.isoformat(),
               "duration_min": round(duration_min, 1), "kwh_used": kwh_used})
    state[f"outages_{today_key}"] = log
    return (
        f"🟢 <b>FranklinWH: GRID RESTORED at {now.strftime('%-I:%M %p')}</b>\n"
        f"Outage lasted <b>{dur_str}</b>{kwh_str}\n"
        f"🔋 {_soc_bar(c.battery_soc_pct)}  ·  Solar {c.solar_production_kw:.2f} kW"
    )


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


def _alert_fast_drain(state: dict, today: str, now: datetime, c, cfg: Config) -> str | None:
    """Always updates last_soc/last_soc_time for rate tracking.

    Two tiers: a critical alert below 35% SoC (unchanged), and a lower-urgency
    "unusual drain" alert at any SoC — catches an EV plugged in or AC left on
    while there's still plenty of lead time to act, instead of only firing
    once the battery is already low.
    """
    prev_soc      = state.get("last_soc")
    prev_soc_time = state.get("last_soc_time")
    body = None
    if prev_soc is not None and prev_soc_time is not None:
        try:
            elapsed_h = (now - datetime.fromisoformat(prev_soc_time)).total_seconds() / 3600
            # A floor, not just >0: two polls landing unusually close together
            # (retry after a transient failure, manual/backfill run) can turn
            # a 1% SoC blip into a triple-digit %/hr rate. This is an
            # always-on alert, so a false positive here can't be muted.
            if elapsed_h >= (2.0 / 60.0):
                drain_rate = (prev_soc - c.battery_soc_pct) / elapsed_h
                if drain_rate >= 8.0 and c.battery_soc_pct < 35.0 and state.get("fast_drain_alerted_date") != today:
                    state["fast_drain_alerted_date"] = today
                    state.pop("unusual_drain_streak", None)
                    logger.info("Fast drain alert sent for %s (%.0f%%/hr, %.0f%%)", today, drain_rate, c.battery_soc_pct)
                    _cap_fd = cfg.battery_capacity_kwh or _BATTERY_CAPACITY_KWH
                    _tte_fd = _time_to_pct(c.battery_soc_pct, 0.0, _cap_fd, c.battery_use_kw)
                    _tte_fd_s = f"\n⏱ ~{_fmt_hours(_tte_fd)} to empty" if _tte_fd is not None else ""
                    body = (
                        f"⚡ <b>FranklinWH: Battery draining fast — {drain_rate:.0f}%/hr</b>\n"
                        f"🔋 {_soc_bar(c.battery_soc_pct)}  ·  Load <b>{c.home_load_kw:.2f} kW</b>  ·  "
                        f"Solar {c.solar_production_kw:.2f} kW\n"
                        f"Time: {now.strftime('%-I:%M %p')}{_tte_fd_s}"
                    )
                elif drain_rate >= 6.0 and c.battery_soc_pct >= 35.0:
                    # Require 2 consecutive polls over threshold to avoid noise
                    # from a single momentary load spike.
                    streak = state.get("unusual_drain_streak", 0) + 1
                    state["unusual_drain_streak"] = streak
                    if streak >= 2 and state.get("unusual_drain_alerted_date") != today:
                        state["unusual_drain_alerted_date"] = today
                        logger.info("Unusual drain alert sent for %s (%.0f%%/hr, %.0f%%)", today, drain_rate, c.battery_soc_pct)
                        body = (
                            f"🟡 <b>FranklinWH: Unusual drain rate — {drain_rate:.0f}%/hr</b>\n"
                            f"🔋 {_soc_bar(c.battery_soc_pct)}  ·  Load <b>{c.home_load_kw:.2f} kW</b>  ·  "
                            f"Solar {c.solar_production_kw:.2f} kW\n"
                            f"Time: {now.strftime('%-I:%M %p')} — check for EV charging or AC left on."
                        )
                else:
                    state["unusual_drain_streak"] = 0
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


def _alert_prediction_drift(state: dict, today: str, now: datetime) -> str | None:
    """Weekly watchdog: sustained absolute bias in solar predictions.

    The PR EWMA, hourly bias, and peak-kW calibration should keep predictions
    centred on actuals. If the 14-day mean actual/predicted ratio still sits
    outside ±10%, some structural drift the loops can't fix has crept in
    (EWMA pinned at its 1.4 clamp, panel changes, stale peak estimate) and a
    human should look. Complements _alert_solar_degradation, which only
    catches a *relative drop* vs the system's own baseline, not steady bias.
    """
    if now.hour not in (9, 10):
        return None
    last = state.get("prediction_drift_alert_date")
    if last and (now - datetime.strptime(last, "%Y-%m-%d")).days < 7:
        return None

    cutoff = (now - timedelta(days=14)).strftime("%Y-%m-%d")
    ratios = []
    for k, v in state.items():
        if k.startswith("daily_pr_") and k[len("daily_pr_"):] >= cutoff:
            r = _safe_float(v)
            if r is not None:
                ratios.append(r)
    if len(ratios) < 10:
        return None

    mean = sum(ratios) / len(ratios)
    if 0.90 <= mean <= 1.10:
        return None

    state["prediction_drift_alert_date"] = today
    pct = (mean - 1.0) * 100
    direction = "low" if mean > 1.0 else "high"
    logger.info("Prediction drift alert: 14-day mean PR=%.3f (%d days)", mean, len(ratios))
    return (
        f"📐 <b>FranklinWH: Solar predictions running {direction}</b>\n"
        f"Last {len(ratios)} days: actual averaged {abs(pct):.0f}% "
        f"{'above' if mean > 1.0 else 'below'} predicted "
        f"(mean ratio {mean:.2f}).\n"
        f"Self-calibration hasn't closed the gap — worth checking "
        f"perf-ratio samples, system-peak estimate, and hourly bias in "
        f"<code>.peak_alert_state.json</code>."
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

    # Translate the % drop into an estimated $/month cost — a raw percentage
    # doesn't tell a homeowner whether to bother investigating; a dollar
    # figure does. Uses the same predicted_kwh_{date} daily estimates the
    # PR calibration already reads, so no new data source is needed.
    cost_note = ""
    predicted_daily = [
        v for k, v in state.items()
        if k.startswith("predicted_kwh_") and k[len("predicted_kwh_"):] >= cutoff_30
        and isinstance(v, (int, float)) and v > 0
    ]
    if predicted_daily:
        avg_daily_kwh   = statistics.mean(predicted_daily)
        lost_kwh_month  = avg_daily_kwh * (drop_pct / 100) * 30
        on_peak_start, _ = on_peak_window(now)
        est_rate        = rate_at(on_peak_start)  # conservative: lost solar displaced by peak-rate import
        est_monthly_cost = lost_kwh_month * est_rate
        if est_monthly_cost >= 1.0:
            cost_note = f"\nEst. ~${est_monthly_cost:.0f}/month in extra grid import at current usage."

    # Track consecutive alerted weeks — a single week can be seasonal soiling
    # that clears on its own, but a persistent multi-week trend is more likely
    # a real fault (panel failure, connector corrosion) worth escalating.
    prior_week_key = (now - timedelta(days=7)).strftime("%G-W%V")
    if state.get("solar_degradation_streak_week") == prior_week_key:
        streak = state.get("solar_degradation_streak", 0) + 1
    else:
        streak = 1
    state["solar_degradation_streak"]      = streak
    state["solar_degradation_streak_week"] = week_key

    logger.info("Solar degradation alert: baseline PR=%.3f recent PR=%.3f drop=%.1f%% streak=%d",
                baseline, recent, drop_pct, streak)

    persistent = streak >= 3
    header = (
        "🔴 <b>FranklinWH: Solar output persistently down</b>"
        if persistent else
        "⚠️ <b>FranklinWH: Solar output trending down</b>"
    )
    action = (
        f"Persistent for {streak} consecutive weeks — likely a real fault "
        "(panel failure, connector corrosion), not seasonal soiling. "
        "Consider a professional inspection."
        if persistent else
        "This may indicate panel soiling, shading, or inverter efficiency loss.\n"
        "Consider cleaning panels or checking inverter logs."
    )
    return (
        f"{header}\n"
        f"7-day performance ratio: {recent:.2f} vs 30-day baseline {baseline:.2f} "
        f"({drop_pct:.0f}% drop){cost_note}\n"
        f"{action}"
    )


_BASELINE_DRIFT_REL = 1.25   # recent must be >= 25% above baseline
_BASELINE_DRIFT_ABS = 0.10   # ...AND at least this many kW above it
_BASELINE_MIN_SAMPLES_PER_NIGHT = 8
_BASELINE_PCTL = 0.20        # 20th percentile of a night's readings


def _pctl(values: list[float], q: float) -> float:
    """Nearest-rank percentile. Small samples here, so no interpolation."""
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[idx]


def _alert_baseline_load_drift(state: dict, today: str, now: datetime, store, cfg: Config) -> str | None:
    """Morning check: the household's always-on draw creeping upward.

    There are watchdogs for generation (solar degradation) and for storage
    (capacity fade) but none for consumption, so a fridge that started
    short-cycling or a space heater left running in a spare room shows up
    only as a vaguely higher bill.

    Uses the 20th percentile of each night's midnight-5am readings rather
    than the minimum: one spurious low sample shouldn't define the night.
    Windows deliberately match _alert_capacity_fade (recent 14d vs 21-75d
    back) — both sit inside the un-rolled-up region, since
    rollup_old_readings averages to hourly past 180 days, which would flatten
    the overnight trough this depends on.
    """
    if now.hour not in (8, 9) or store is None:
        return None
    week_key = now.strftime("%G-W%V")
    if state.get("baseline_load_drift_alerted_week") == week_key:
        return None

    today_str    = now.date().strftime("%Y-%m-%d")
    recent_start = (now.date() - timedelta(days=14)).strftime("%Y-%m-%d")
    base_start   = (now.date() - timedelta(days=75)).strftime("%Y-%m-%d")
    base_end     = (now.date() - timedelta(days=21)).strftime("%Y-%m-%d")

    def _nightly(by_date: dict[str, list[float]]) -> list[float]:
        return [
            _pctl(vals, _BASELINE_PCTL)
            for vals in by_date.values()
            if len(vals) >= _BASELINE_MIN_SAMPLES_PER_NIGHT
        ]

    recent = _nightly(store.quiet_hour_loads(recent_start, today_str))
    base   = _nightly(store.quiet_hour_loads(base_start, base_end))
    if len(recent) < 7 or len(base) < 14:
        return None

    recent_med = _pctl(recent, 0.5)
    base_med   = _pctl(base, 0.5)
    if base_med <= 0:
        return None

    delta = recent_med - base_med
    # Both gates: at a ~0.2 kW baseline a 25% relative move is only 0.05 kW,
    # which is noise. The absolute floor is what stops this crying wolf.
    if recent_med < base_med * _BASELINE_DRIFT_REL or delta < _BASELINE_DRIFT_ABS:
        return None

    # Deliberately a floor, not a blend. Pricing 24h of drift across the real
    # TOU curve would require assuming when the extra draw happens; the
    # super-off-peak rate is the cheapest hour it could possibly run at, so
    # "at least $X" is defensible without inventing a usage profile.
    # 2am is super-off-peak on both the weekday and the weekend/holiday
    # schedule, so this reads the current season's cheapest rate through the
    # public API instead of reaching into tou's rate table.
    sop_rate = rate_at(now.replace(hour=2, minute=0, second=0, microsecond=0))
    monthly_floor = delta * 24 * 30 * sop_rate

    state["baseline_load_drift_alerted_week"] = week_key
    logger.info("Baseline load drift alert: %.3f -> %.3f kW (+%.0f%%)",
                base_med, recent_med, (recent_med / base_med - 1) * 100)
    return (
        f"⚠️ <b>FranklinWH: Always-on load has crept up</b>\n"
        f"Overnight baseline ~<b>{recent_med:.2f} kW</b> vs ~{base_med:.2f} kW earlier "
        f"({(recent_med / base_med - 1) * 100:.0f}% higher)\n"
        f"That's at least ~<b>${monthly_floor:.0f}/month</b> more, likely higher if it "
        f"runs around the clock.\n"
        f"From {len(recent)} recent / {len(base)} baseline nights. Check for a new "
        f"always-on device, a fridge or pump cycling constantly, or something left on."
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

    # Quantify: the lost kWh of usable capacity, assumed to cycle roughly
    # daily, priced at the on-peak rate (the shortfall most likely shows up
    # as extra 4-9pm grid import once the battery can no longer fully cover
    # that window) — same estimation approach as the solar-degradation alert.
    lost_kwh_cycle   = max(0.0, base_cap - recent_cap)
    on_peak_start, _ = on_peak_window(now)
    est_rate         = rate_at(on_peak_start)
    est_monthly_cost = lost_kwh_cycle * 30 * est_rate
    cost_note = (f"\nEst. ~${est_monthly_cost:.0f}/month in extra peak-rate grid import if this "
                f"capacity gap goes uncovered." if est_monthly_cost >= 1.0 else "")

    return (
        f"🔋 <b>FranklinWH: Possible battery capacity fade</b>\n"
        f"Effective usable capacity ~{recent_cap:.1f} kWh recently vs ~{base_cap:.1f} kWh baseline "
        f"({fade_pct:.0f}% lower){cost_note}\n"
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


def _alert_peak_streak_good(state: dict, today: str, now: datetime) -> str | None:
    """Evening check: last 7 consecutive days all >=95% peak coverage —
    positive-reinforcement mirror of _alert_peak_streak, using the same
    peak_cov_{date} state that's already tracked daily by _alert_eod_digest.
    """
    week_key = now.strftime("%G-W%V")
    if now.hour not in (21, 22) or state.get("peak_streak_good_alerted_week") == week_key:
        return None

    good_days = []
    check_date = now.date() - timedelta(days=1)
    for _ in range(7):
        date_str = check_date.strftime("%Y-%m-%d")
        pct = state.get(f"peak_cov_{date_str}")
        if pct is None or pct < 95.0:
            return None  # missing data or streak broken
        good_days.append(date_str)
        check_date -= timedelta(days=1)

    state["peak_streak_good_alerted_week"] = week_key
    logger.info("Peak-coverage good streak alert: 7 consecutive days >=95%%")
    return (
        "🟢 <b>FranklinWH: 7 days straight covering peak</b>\n"
        "Battery + solar have handled the 4–9 pm window without grid import "
        "every day this week. System is well-sized for your current usage."
    )


def _alert_bill_projection(
    state: dict, today: str, now: datetime, store, cfg: Config,
) -> str | None:
    """5th of each month: project full-cycle bill from partial billing cycle data."""
    if store is None or now.day != 5 or now.hour not in (8, 9):
        return None
    if state.get("bill_projection_date") == today:
        return None

    cycle_start, cycle_end = cycle_bounds(
        now.date(), getattr(cfg, "billing_cycle_start_day", 20)
    )
    days_so_far = (now.date() - cycle_start).days
    cycle_days  = (cycle_end - cycle_start).days + 1
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
    # Real cycle length, not a flat 30 — cycles run 28-31 days.
    projected_net  = daily_net * cycle_days
    projected_imp  = import_cost / days_so_far * cycle_days
    projected_exp  = export_credit / days_so_far * cycle_days
    projected_base = base_service_cost(cycle_days)
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
        f"Projected full cycle ({cycle_days} days):\n"
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


_EV_SOLAR_SURPLUS_KWH = 5.0  # tomorrow's forecast surplus above this = "free" EV charging window


def _alert_ev_charge_window(
    state: dict, today: str, now: datetime, c, cfg: Config, outlook=None,
) -> str | None:
    """Evening: recommend the cheapest window to charge an EV.

    Normally that's the fixed super-off-peak overnight window — but on a day
    with a big predicted solar surplus tomorrow (the same signal
    _alert_solar_surplus_overflow uses to flag solar going to waste), midday
    charging from free solar beats even the cheapest grid rate.

    Only fires when cfg.ev_charging is set. Advisory only.
    """
    if not getattr(cfg, "ev_charging", False):
        return None
    if now.hour not in (20, 21) or state.get("ev_charge_window_date") == today:
        return None
    state["ev_charge_window_date"] = today
    kwh = getattr(cfg, "ev_kwh_per_session", 0.0) or 0.0

    # Solar-surplus path: tomorrow's forecast exceeds a typical session's
    # worth of charging — free beats cheap.
    if outlook is not None:
        sp = _get_system_peak_kw(state)
        if sp is not None:
            cloudy   = outlook.tomorrow_avg_ghi() < _GHI_CLOUDY_THRESHOLD
            pr       = _get_performance_ratio(state, cloudy=cloudy)
            tmrw_kwh = outlook.tomorrow_generation_kwh(sp, pr, _get_hourly_bias(state))
            if not cloudy and tmrw_kwh >= _EV_SOLAR_SURPLUS_KWH:
                logger.info("EV charge window alert (solar path) sent for %s", today)
                return (
                    f"☀️ <b>FranklinWH: Charge your EV from solar tomorrow</b>\n"
                    f"Tomorrow's forecast: ~{tmrw_kwh:.1f} kWh solar — plenty of surplus expected. "
                    f"Plug in mid-morning through early afternoon instead of overnight grid "
                    f"charging; it's free instead of even the super-off-peak rate."
                )

    # Default path: cheapest grid window (super-off-peak overnight).
    sop = rate_at(now.replace(hour=1, minute=0, second=0, microsecond=0))
    onp = rate_at(now.replace(hour=17, minute=0, second=0, microsecond=0))
    cost_line = ""
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
    hourly_bias  = _get_hourly_bias(state)
    tmrw_kwh     = outlook.tomorrow_generation_kwh(sp, pr, hourly_bias)
    day2_date    = (now + timedelta(days=2)).date()
    day2_hours   = [h for h in outlook.hours if h.time.date() == day2_date]
    from franklinwh_scraper.weather import _MIN_EFFICIENCY, _TEMP_COEFF
    day2_kwh = 0.0
    if day2_hours:
        for h in day2_hours:
            eff = max(_MIN_EFFICIENCY, 1.0 + _TEMP_COEFF * (h.panel_temp_c - 25.0))
            # Apply the same per-hour learned bias correction day-1 gets via
            # tomorrow_generation_kwh() — without it, the two halves of this
            # "2-day total" are computed by inconsistent models and silently
            # diverge from what the system's own calibration would say.
            if hourly_bias:
                eff *= hourly_bias.get(h.time.hour, 1.0)
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


_FULL_RESET_SOC = 95.0  # drop below this re-arms the alert — see note below

def _alert_solar_surplus_overflow(
    state: dict, today: str, now: datetime, c
) -> str | None:
    """Battery full with solar exceeding load → informational only.

    Triggers when solar is filling an already-full battery. On Self-Consumption
    (this system's assumed mode at all times — see advisor.Mode.SELF_CONSUMPTION
    and cli._dispatch_notifications) the surplus already exports to grid
    automatically, so this no longer suggests switching to Time-of-Use mode —
    that switch is never something this system's user makes.

    Window: 10 am–6 pm. Was 10 am–2 pm (tied to super-off-peak ending), but
    on a day with a late morning load spike the battery can miss 100% until
    mid-afternoon — see the 2026-08-08 case where it didn't fill until 4 pm.

    Re-notify policy: `battery_full_notified` is a standing flag, not a
    per-day key, so a multi-day sunny streak doesn't renotify every single
    day the battery is still sitting at 100% — only the transition into
    full is newsworthy. The flag re-arms once SoC meaningfully drops
    (below _FULL_RESET_SOC), so the next time it fills is reported again.
    This check runs unconditionally (before the window/hour gate) so a
    drop is caught even outside 10 am–6 pm.
    """
    soc = c.battery_soc_pct
    if soc < _FULL_RESET_SOC:
        state["battery_full_notified"] = False

    if not (10 <= now.hour < 18):
        return None
    if state.get("battery_full_notified"):
        return None
    if soc < 100.0:
        return None
    # Battery charging or holding — solar exceeds load
    if c.battery_use_kw > 0.1 or c.solar_production_kw < c.home_load_kw:
        return None
    state["battery_full_notified"] = True
    logger.info("Solar surplus overflow alert: SoC=%.0f%%, solar=%.2f kW", soc, c.solar_production_kw)
    period_label = {
        TouPeriod.SUPER_OFF_PEAK: "super-off-peak",
        TouPeriod.OFF_PEAK: "off-peak",
        TouPeriod.ON_PEAK: "on-peak",
    }[period_at(now)]
    return (
        f"☀️ <b>FranklinWH: Battery full — solar surplus available</b>\n"
        f"🔋 {_soc_bar(soc)}  ·  Solar {c.solar_production_kw:.2f} kW  ·  "
        f"Load {c.home_load_kw:.2f} kW\n"
        f"Time: {now.strftime('%-I:%M %p')} — currently {period_label}.\n"
        f"Self-Consumption mode is optimal — excess going to grid."
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
        _calibrate_solar(state, c.solar_production_kw, outlook, now)
        _calibrate_solar_hourly(state, c.solar_production_kw, outlook, now)
        _track_battery_cycles(state, c)
        _candidates = [
            ("morning_preview",   lambda: _alert_morning_preview(state, today, now, c, outlook, usage_forecast, store, cfg)),
            ("grid_import",       lambda: _alert_grid_import(state, today, now, c)),
            ("low_soc_1pm",       lambda: _alert_low_soc_1pm(state, today, now, c, cfg)),
            ("low_morning_solar", lambda: _alert_low_morning_solar(state, today, now, c)),
            ("solar_stopped",     lambda: _alert_solar_stopped(state, today, now, c)),
            ("low_noon_soc",      lambda: _alert_low_noon_soc(state, today, now, c)),
            ("export_arbitrage",  lambda: _alert_export_arbitrage(state, today, now, c, cfg, usage_forecast)),
            ("eod_digest",        lambda: _alert_eod_digest(state, today, now, stats, cfg, outlook, usage_forecast, store)),
            ("weekly_summary",    lambda: _alert_weekly_summary(state, today, now, store, cfg)),
            ("monthly_summary",   lambda: _alert_monthly_summary(state, today, now, store, cfg)),
            ("grid_down",         lambda: _alert_grid_down(state, today, now, c, cfg)),
            ("grid_restored",     lambda: _alert_grid_restored(state, now, c, cfg)),
            ("fast_drain",        lambda: _alert_fast_drain(state, today, now, c, cfg)),
            ("not_charging",      lambda: _alert_not_charging(state, today, now, c)),
            ("solar_degradation",    lambda: _alert_solar_degradation(state, today, now)),
            ("prediction_drift",     lambda: _alert_prediction_drift(state, today, now)),
            ("solar_back_to_baseline", lambda: _alert_solar_back_to_baseline(state, today, now)),
            ("capacity_fade",        lambda: _alert_capacity_fade(state, today, now, store)),
            ("baseline_load_drift",  lambda: _alert_baseline_load_drift(state, today, now, store, cfg)),
            ("peak_streak",          lambda: _alert_peak_streak(state, today, now)),
            ("peak_streak_good",     lambda: _alert_peak_streak_good(state, today, now)),
            ("bill_projection",      lambda: _alert_bill_projection(state, today, now, store, cfg)),
            ("heat_wave_prep",       lambda: _alert_heat_wave_prep(state, today, now, c, outlook)),
            ("multiday_cloudy_precharge", lambda: _alert_multiday_cloudy_precharge(state, today, now, c, outlook, cfg)),
            ("solar_surplus_overflow",    lambda: _alert_solar_surplus_overflow(state, today, now, c)),
            ("storm_prep",           lambda: _alert_storm_prep(state, today, now, c, cfg)),
            ("ev_charge_window",     lambda: _alert_ev_charge_window(state, today, now, c, cfg, outlook)),
            ("area_power_outage",    lambda: _alert_area_power_outage(state, today, now, c, cfg)),
            ("tou_rates_stale",      lambda: _alert_tou_rates_stale(state, today, now)),
            ("weather_stale",        lambda: _alert_weather_stale(state, today, now)),
        ]
        # (body, urgent) — fast_drain returns two tiers from one candidate
        # (critical below 35% SoC, plus a lower-urgency "unusual drain"
        # advisory) and both inherit urgent=True. Splitting it into two
        # candidates would fragment its last_soc/last_soc_time bookkeeping,
        # which is worse; it's an always-on safety alert either way.
        to_send: list[tuple[str, bool]] = []
        for _name, _fn in _candidates:
            if _alert_enabled(cfg, _name):
                _body = _fn()
                if _body:
                    to_send.append((_body, _name in _URGENT_ALERTS))
        _save_peak_state(out, state)

    for body, urgent in to_send:
        _send_alert(body, cfg, urgent=urgent)

