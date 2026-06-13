"""FranklinWH scraper — polished CLI."""

from __future__ import annotations

import atexit
import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path


import click

logger = logging.getLogger(__name__)

from .account import AccountClient
from .advisor import recommend
from .chatbot import TelegramChatBot
from .client import FranklinWHClient
from .config import Config, load as load_config, save as save_config
from .exporters import export_csv, export_json

from .history import HistoryStore, integrate_intervals
from .notifier import (notify_imessage, notify_imessage_text, notify_log,
                       notify_macos, notify_telegram, fetch_telegram_chat_id, rec_to_text)
from .predictor import predict
from .tou import TouPeriod, base_service_cost, cheap_charge_deadline, period_at, rate_at
from .scrapers import FAQScraper, ProductsScraper, SupportScraper
from .weather import fetch_nws_storm_alerts, fetch_solar_outlook, geocode


# ── Helpers ──────────────────────────────────────────────────────────

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def _ok(msg: str)   -> None: click.echo(click.style(f"  ✓  {msg}", fg="green"))
def _warn(msg: str) -> None: click.echo(click.style(f"  ⚠  {msg}", fg="yellow"))
def _err(msg: str)  -> None: click.echo(click.style(f"  ✗  {msg}", fg="red"))
def _info(msg: str) -> None: click.echo(f"     {msg}")
def _hr()           -> None: click.echo(click.style("─" * 60, dim=True))
def _header(title: str) -> None:
    click.echo()
    click.echo(click.style(f"  {title}", bold=True))
    _hr()


_PID_FILE = Path.home() / ".franklinwh.pid"

_BATTERY_CAPACITY_KWH = 13.6  # fallback default — overridden by cfg.battery_capacity_kwh at runtime


def _acquire_pid_lock() -> bool:
    """Return True if no other watcher is running; register cleanup on exit."""
    if _PID_FILE.exists():
        try:
            pid = int(_PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return False  # process alive
        except (ValueError, ProcessLookupError, PermissionError):
            _PID_FILE.unlink(missing_ok=True)  # stale — remove before atomic create
    # Exclusive create is atomic — fails with FileExistsError if another process wins the race
    try:
        with open(_PID_FILE, "x") as f:
            f.write(str(os.getpid()))
        atexit.register(_release_pid_lock)
        return True
    except FileExistsError:
        return False


def _release_pid_lock() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _load_last_mode(out: Path) -> str | None:
    """Read the last recommended mode from disk (persists across cron runs)."""
    p = out / ".last_recommendation.json"
    try:
        return json.loads(p.read_text()).get("mode")
    except (OSError, json.JSONDecodeError):
        return None


def _save_last_mode(out: Path, mode: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / ".last_recommendation.json").write_text(json.dumps({"mode": mode}))


def _get_system_peak_kw(state: dict) -> float | None:
    """Return calibrated system peak kW (median of sunny-day samples), or None if < 3 samples."""
    samples = state.get("solar_cal_samples", [])
    if len(samples) < 3:
        return None
    s = sorted(samples)
    return s[len(s) // 2]  # median — less biased than P75


_GHI_CLOUDY_THRESHOLD = 300  # W/m² avg over 12h — below this = dim/cloudy day


def _get_performance_ratio(state: dict, cloudy: bool = False) -> float:
    """Return empirical PR (actual / predicted daily kWh) for sunny or cloudy days.

    Separate buckets prevent the sunny-day bias (hot panels, lower efficiency)
    from distorting cloudy-day forecasts where panels run cooler.
    Falls back to sunny PR × 1.10 until 3 cloudy-day samples accumulate.
    """
    if cloudy:
        samples = [v for v in state.get("perf_ratio_cloudy_samples", []) if v <= 1.4]
        if len(samples) < 3:
            sunny = [v for v in state.get("perf_ratio_samples", []) if v <= 1.4]
            if len(sunny) >= 3:
                s = sorted(sunny)
                return max(s[len(s) // 2] * 1.10, 0.60)
            return 0.85  # reasonable prior: cloudy panels run cooler
        s = sorted(samples)
        return max(s[len(s) // 2], 0.55)
    else:
        samples = [v for v in state.get("perf_ratio_samples", []) if v <= 1.4]
        if len(samples) < 3:
            return 1.0
        s = sorted(samples)
        return max(s[len(s) // 2], 0.60)


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

# NEM evening export compensation ($/kWh) by month → {hour: rate}.
# Only August and September have boosted evening export worth arbitraging;
# every other month the export rate stays far below the on-peak import rate,
# so the arbitrage advisor is inert outside these two months.
# (Source: user's SDGE export schedule.)
_EXPORT_RATES: dict[int, dict[int, float]] = {
    8: {17: 0.907, 18: 1.022, 19: 0.920, 20: 0.996, 21: 0.895, 22: 0.885},
    9: {17: 0.253, 18: 0.595, 19: 0.673, 20: 0.380, 21: 0.154, 22: 0.154},
}


def _peak_export_hour(month: int) -> tuple[int, float] | None:
    """Highest-value export (hour, $/kWh) for the month, or None if not a
    boosted export month. Aug → (18, 1.022), Sep → (19, 0.673)."""
    rates = _EXPORT_RATES.get(month)
    if not rates:
        return None
    hour = max(rates, key=rates.__getitem__)
    return hour, rates[hour]


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
        state["monthly_summary_date"] = state.pop("monthly_summary_month") + "-19"
    return state


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
    if not (outlook and solar_kw >= 1.0):
        return
    current_ghi = outlook.avg_ghi(1)
    if current_ghi < 400:
        return
    sample = round(solar_kw / (current_ghi / 1000.0), 2)
    if 0.5 <= sample <= 25.0:
        samples = state.get("solar_cal_samples", [])
        samples.append(sample)
        state["solar_cal_samples"] = samples[-50:]


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
        if yest_pred >= min_predicted and yest_actual >= 0.3:
            ratio  = round(yest_actual / yest_pred, 3)
            bucket = "perf_ratio_cloudy_samples" if cloudy_day else "perf_ratio_samples"
            pr_samples = state.get(bucket, [])
            pr_samples.append(ratio)
            state[bucket] = pr_samples[-30:]
            state[f"daily_pr_{yesterday}"] = ratio
            logger.info(
                "PR update (%s): actual=%.1f predicted=%.1f ratio=%.3f ghi=%.0f",
                "cloudy" if cloudy_day else "sunny",
                yest_actual, yest_pred, ratio, yesterday_ghi,
            )

    soc      = c.battery_soc_pct
    solar_kw = c.solar_production_kw

    if outlook:
        cal_samples = state.get("solar_cal_samples", [])
        if len(cal_samples) >= 3:
            sorted_s = sorted(cal_samples)
            system_peak_kw = sorted_s[len(sorted_s) // 2]
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
        gen_kwh    = round(outlook.today_generation_kwh(system_peak_kw) * perf_ratio, 1)
        state[f"predicted_kwh_{today}"]     = gen_kwh
        state[f"predicted_avg_ghi_{today}"] = round(avg_ghi, 1)

        sky = "Sunny" if avg_ghi >= 400 else ("Partly cloudy" if avg_ghi >= _GHI_CLOUDY_THRESHOLD else "Cloudy")
        cloudy_samples = state.get("perf_ratio_cloudy_samples", [])
        sunny_samples  = state.get("perf_ratio_samples", [])
        if cloudy_day and len(cloudy_samples) >= 3:
            pr_note = f"cloudy PR={perf_ratio:.2f}"
        elif not cloudy_day and len(sunny_samples) >= 3:
            pr_note = f"PR={perf_ratio:.2f}"
        else:
            pr_note = cal_note
        solar_est = f"~{gen_kwh:.1f} kWh predicted ({sky}, {pr_note})"

        # Tomorrow forecast
        tmrw_ghi = outlook.tomorrow_avg_ghi()
        tmrw_sky = "Sunny" if tmrw_ghi >= 400 else ("Partly cloudy" if tmrw_ghi >= _GHI_CLOUDY_THRESHOLD else "Cloudy")
        tmrw_kwh = outlook.tomorrow_generation_kwh(system_peak_kw, perf_ratio)
        solar_est += f"\nTomorrow: {tmrw_sky} — ~{tmrw_kwh:.1f} kWh"
        bat_cap = cfg.battery_capacity_kwh if cfg else _BATTERY_CAPACITY_KWH
        solar_est += _precharge_plan(now, soc, tmrw_kwh, bat_cap)

        # Peak solar window — relative threshold (30% of day's peak, min 150 W/m²)
        today_hrs = [h for h in outlook.hours if h.time.date() == now.date()]
        day_peak_ghi = max((h.ghi_wm2 for h in today_hrs), default=0.0)
        ghi_thresh = max(150.0, day_peak_ghi * 0.30)
        peak_hrs = [h for h in today_hrs if h.ghi_wm2 >= ghi_thresh]
        if peak_hrs:
            start    = peak_hrs[0].time.strftime("%-I%p").lower()
            end      = (peak_hrs[-1].time + timedelta(hours=1)).strftime("%-I%p").lower()
            peak_hr  = max(peak_hrs, key=lambda h: h.ghi_wm2)
            peak_at  = peak_hr.time.strftime("%-I%p").lower()
            peak_window_str = f"\nBest solar: {start}–{end} (peak ~{peak_at})"
        else:
            peak_window_str = ""
    else:
        solar_est       = "Solar forecast unavailable"
        peak_window_str = ""

    state["morning_preview_date"] = today
    logger.info("Morning preview alert sent for %s", today)
    return (
        f"☀️ FranklinWH: Good morning! Daily preview\n"
        f"Battery: {soc:.0f}% SoC  |  Solar now: {solar_kw:.2f} kW\n"
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
        f"⚠️ FranklinWH: Pulling from grid during peak hours (4–9 pm)\n"
        f"SoC {c.battery_soc_pct:.0f}%  |  Grid +{c.grid_use_kw:.2f} kW  |  "
        f"Solar {c.solar_production_kw:.2f} kW  |  Load {c.home_load_kw:.2f} kW\n"
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
    return (
        f"🟡 FranklinWH: Battery under 40% at {now.strftime('%-I:%M %p')}\n"
        f"SoC {c.battery_soc_pct:.0f}% — grid import risk during 4–9 pm peak\n"
        f"Solar {c.solar_production_kw:.2f} kW  |  Load {c.home_load_kw:.2f} kW\n"
        f"Consider switching to Emergency Backup to charge before peak."
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
        f"🟢 FranklinWH: Battery at {c.battery_soc_pct:.0f}% — Emergency Backup target reached\n"
        f"Time: {now.strftime('%-I:%M %p')} — battery ready before 4 pm peak\n"
        f"Solar {c.solar_production_kw:.2f} kW  |  Load {c.home_load_kw:.2f} kW\n"
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
        f"☁️ FranklinWH: Low solar at {now.strftime('%-I:%M %p')} — cloudy day ahead\n"
        f"Solar {c.solar_production_kw:.2f} kW  |  SoC {c.battery_soc_pct:.0f}%  |  Load {c.home_load_kw:.2f} kW\n"
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
        f"🔴 FranklinWH: Solar dropped mid-day — possible issue\n"
        f"Was {last_solar:.2f} kW → now {c.solar_production_kw:.2f} kW "
        f"at {now.strftime('%-I:%M %p')}\n"
        f"SoC {c.battery_soc_pct:.0f}%  |  Check inverter or cloud cover."
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
        f"🟡 FranklinWH: Battery still low at noon — only {c.battery_soc_pct:.0f}% SoC\n"
        f"Solar {c.solar_production_kw:.2f} kW available but battery hasn't recovered\n"
        f"Time: {now.strftime('%-I:%M %p')}  |  Load {c.home_load_kw:.2f} kW\n"
        f"Check battery mode — may need manual intervention."
    )


def _alert_export_arbitrage(
    state: dict, today: str, now: datetime, c, cfg: Config, usage_forecast,
) -> str | None:
    """Aug/Sep only: when the battery is full and won't be needed for self-supply,
    advise exporting surplus to grid at the single highest-rate hour of the day.

    Advisory only — does not command the inverter.
    """
    peak = _peak_export_hour(now.month)
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

    # Subtract predicted self-supply need at the export hour (net_kw = solar − load)
    if usage_forecast and usage_forecast.hours:
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
        f"💰 FranklinWH: Peak export opportunity today\n"
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
        backup_str = f"\nEst. backup now: ~{backup_h:.1f} hr at current load"

    solar_delta_str = ""
    predicted_kwh   = state.get(f"predicted_kwh_{today}")
    if predicted_kwh and predicted_kwh > 0:
        delta_kwh = t.solar_kwh - predicted_kwh
        sign      = "+" if delta_kwh >= 0 else ""
        solar_delta_str = (
            f"\n\nSolar forecast vs actual:\n"
            f"  Predicted:  {predicted_kwh:.1f} kWh\n"
            f"  Actual:     {t.solar_kwh:.1f} kWh\n"
            f"  Delta:      {sign}{delta_kwh:.1f} kWh ({sign}{delta_kwh / predicted_kwh * 100:.0f}%)"
        )

    soc_6am_str = ""
    if usage_forecast and usage_forecast.hours:
        tomorrow_6am  = (now + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        night_net_kwh = sum(p.net_kw for p in usage_forecast.hours if now <= p.dt < tomorrow_6am)
        pred_soc_6am  = max(0.0, min(100.0, soc + night_net_kwh / bat_cap * 100))
        soc_6am_str   = f"\nPredicted SoC @ 6 am: ~{pred_soc_6am:.0f}%"

    precharge_str = ""
    if outlook:
        sp = _get_system_peak_kw(state)
        if sp:
            cloudy   = outlook.tomorrow_avg_ghi() < _GHI_CLOUDY_THRESHOLD
            pr       = _get_performance_ratio(state, cloudy=cloudy)
            tmrw_kwh = outlook.tomorrow_generation_kwh(sp, pr)
            precharge_str = _precharge_plan(now, soc, tmrw_kwh, bat_cap)

    self_suff_str = ""
    if t.home_use_kwh > 0:
        self_suff     = max(0.0, min(100.0, (t.home_use_kwh - t.grid_load_kwh) / t.home_use_kwh * 100))
        self_suff_str = f"\nSelf-sufficiency:  {self_suff:.0f}%"

    # TOU daily cost estimate + peak coverage (requires history store)
    tou_str      = ""
    peak_cov_str = ""
    if store is not None:
        readings = store.weekly_readings(today, today)
        if readings:
            import_cost = export_credit = 0.0
            for dt, hours, grid_kw, _home_kw, _solar_kw in integrate_intervals(readings):
                r = rate_at(dt)
                if grid_kw > 0:
                    import_cost   += grid_kw * hours * r
                elif grid_kw < 0:
                    export_credit += -grid_kw * hours * r
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
            base_fee = base_service_cost(1)
            net = import_cost - export_credit + base_fee
            tou_str = (
                f"\nEst. grid cost today:  ${import_cost:.2f} import  "
                f"${export_credit:.2f} export  +${base_fee:.2f} base  (net ${net:.2f})"
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
        f"📊 FranklinWH Daily Summary — {now.strftime('%a %b %-d')}\n"
        f"Solar generated:  {t.solar_kwh:.1f} kWh\n"
        f"Grid consumed:    {t.grid_load_kwh:.1f} kWh\n"
        f"Grid exported:    {t.grid_export_kwh:.1f} kWh\n"
        f"Home used:        {t.home_use_kwh:.1f} kWh{self_suff_str}{peak_cov_str}{tou_str}\n"
        f"Battery SoC now:  {soc:.0f}%{backup_str}{soc_6am_str}{solar_delta_str}{precharge_str}"
    )


def _alert_weekly_summary(state: dict, today: str, now: datetime, store) -> str | None:
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
        r      = rate_at(dt)
        period = period_at(dt)
        if grid_kw > 0:
            import_cost   += grid_kw * hours * r
        elif grid_kw < 0:
            export_credit += -grid_kw * hours * r
        # Energy served by battery+solar (not drawn from grid) × avoided rate
        batt_solar_kwh = max(0.0, home_kw - max(0.0, grid_kw)) * hours
        if period == TouPeriod.ON_PEAK:
            peak_saved += batt_solar_kwh * r
        elif period == TouPeriod.SUPER_OFF_PEAK:
            sop_saved  += batt_solar_kwh * r

    base_fee    = base_service_cost(7)
    net_cost    = import_cost - export_credit + base_fee
    total_saved = peak_saved + sop_saved
    week_label  = f"{week_start.strftime('%b %-d')}–{week_end.strftime('%b %-d')}"

    # Avg daily cost from stored EOD data
    daily_costs = [
        state[f"daily_import_cost_{(now.date() - timedelta(days=i)).strftime('%Y-%m-%d')}"]
        for i in range(7)
        if f"daily_import_cost_{(now.date() - timedelta(days=i)).strftime('%Y-%m-%d')}" in state
    ]
    avg_cost_str = f"  Avg daily import: ${sum(daily_costs) / len(daily_costs):.2f}\n" if daily_costs else ""

    # Solar prediction accuracy (±% avg error vs actual)
    cutoff = (now.date() - timedelta(days=7)).strftime("%Y-%m-%d")
    week_prs = [
        float(v) for k, v in state.items()
        if k.startswith("daily_pr_") and k[len("daily_pr_"):] >= cutoff
    ]
    accuracy_str = ""
    if len(week_prs) >= 3:
        avg_err = sum(abs(1.0 - pr) * 100 for pr in week_prs) / len(week_prs)
        accuracy_str = f"\nSolar forecast accuracy: ±{avg_err:.1f}% avg ({len(week_prs)} days)"

    # Battery cycle count
    cycles_week  = state.pop("batt_cycles_this_week", 0)
    total_cycles = state.get("batt_cycle_count", 0)
    cycle_str    = ""
    if total_cycles > 0 or cycles_week > 0:
        pct_used = total_cycles / 6000 * 100
        cycle_str = f"\nBattery cycles: {cycles_week:.1f} this week | {total_cycles:.1f} total ({pct_used:.1f}% of 6000 rated)"

    state["weekly_summary_sent"] = today
    logger.info("Weekly TOU summary sent for week ending %s", today)
    return (
        f"📊 FranklinWH Weekly Summary — {week_label}\n\n"
        f"Grid cost (EV-TOU-5 rates):\n"
        f"  Imported:     ${import_cost:.2f}\n"
        f"  Exported:     ${export_credit:.2f} (est.)\n"
        f"  Base service: ${base_fee:.2f}\n"
        f"  Net cost:     ${net_cost:.2f}\n"
        f"{avg_cost_str}"
        f"\nEst. savings from battery + solar:\n"
        f"  Peak (4–9 pm):    ${peak_saved:.2f}\n"
        f"  Super off-peak:   ${sop_saved:.2f}\n"
        f"  Total saved:      ${total_saved:.2f}"
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
        r = rate_at(dt)
        if grid_kw > 0:
            import_cost   += grid_kw * hours * r
        elif grid_kw < 0:
            export_credit += -grid_kw * hours * r
    days       = max(1, (cycle_end - cycle_start).days + 1)
    base_fee   = base_service_cost(days)
    net_cycle  = import_cost - export_credit + base_fee
    annual     = net_cycle / days * 365
    direction  = "net consumer (you owe)" if annual >= 0 else "net exporter (building credit)"
    trueup_str = (
        f"\n\nNEM true-up (est.):\n"
        f"  Import:       ${import_cost:.2f}\n"
        f"  Export:       ${export_credit:.2f}\n"
        f"  Base service: ${base_fee:.2f}\n"
        f"  Net:          ${net_cycle:+.2f} this cycle\n"
        f"  ~${annual:+.0f}/yr at this rate — {direction}"
    )

    state["monthly_summary_date"] = today
    logger.info("Billing-cycle summary sent for %s → %s", cycle_start, cycle_end)
    return (
        f"📅 FranklinWH Billing Cycle — {cur_label}\n"
        f"vs prior cycle ({prev_label})\n\n"
        f"Solar generated:\n"
        f"  This:  {cur.solar_kwh:.1f} kWh{_mdelta(cur.solar_kwh, prev.solar_kwh)}\n"
        f"  Prior: {prev.solar_kwh:.1f} kWh\n\n"
        f"Grid imported:\n"
        f"  This:  {cur.grid_import_kwh:.1f} kWh{_mdelta(cur.grid_import_kwh, prev.grid_import_kwh)}\n"
        f"  Prior: {prev.grid_import_kwh:.1f} kWh\n\n"
        f"Grid exported:\n"
        f"  This:  {cur.grid_export_kwh:.1f} kWh{_mdelta(cur.grid_export_kwh, prev.grid_export_kwh)}\n"
        f"  Prior: {prev.grid_export_kwh:.1f} kWh\n\n"
        f"Home used:\n"
        f"  This:  {cur.home_load_kwh:.1f} kWh{_mdelta(cur.home_load_kwh, prev.home_load_kwh)}\n"
        f"  Prior: {prev.home_load_kwh:.1f} kWh{sparse_note}"
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
        f"\nBackup: ~{cur_h:.1f} hr at current {load_kw:.1f} kW — "
        f"cut to essentials (~{ess_load:.1f} kW) for ~{ess_h:.1f} hr.\n"
        f"Turn off AC, EV charging, dryer, and pool pump to extend runtime."
    )


def _alert_grid_down(state: dict, today: str, now: datetime, c, cfg: Config) -> str | None:
    if c.grid_status != "down" or state.get("grid_down_alerted_date") == today:
        return None
    state["grid_down_alerted_date"] = today
    state["grid_down_start"]        = now.isoformat()
    state["grid_down_soc"]          = c.battery_soc_pct
    logger.info("Grid-down alert sent for %s", today)
    conservation = _conservation_advice(c.battery_soc_pct, c.home_load_kw, cfg.battery_capacity_kwh)
    return (
        f"🔴 FranklinWH: GRID DOWN at {now.strftime('%-I:%M %p')}\n"
        f"Running on battery — SoC {c.battery_soc_pct:.0f}%  |  Load {c.home_load_kw:.2f} kW\n"
        f"Solar {c.solar_production_kw:.2f} kW{conservation}"
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
    kwh_str      = f"  |  ~{kwh_used:.1f} kWh used from battery" if kwh_used > 0.1 else ""
    logger.info("Grid-restored alert: outage lasted %s", dur_str)
    return (
        f"🟢 FranklinWH: GRID RESTORED at {now.strftime('%-I:%M %p')}\n"
        f"Outage lasted {dur_str}{kwh_str}\n"
        f"Battery SoC now: {c.battery_soc_pct:.0f}%  |  Solar {c.solar_production_kw:.2f} kW"
    )


def _alert_battery_full_cycle(state: dict, today: str, now: datetime, c) -> str | None:
    full_state = state.get("full_charge_state", "watching_for_full")
    if full_state == "watching_for_full" and c.battery_soc_pct >= 99.0:
        state["full_charge_state"] = "watching_for_discharge"
        logger.info("Full charge alert sent (%.0f%%)", c.battery_soc_pct)
        return (
            f"🔋 FranklinWH: Battery fully charged — {c.battery_soc_pct:.0f}% SoC\n"
            f"Time: {now.strftime('%-I:%M %p')}\n"
            f"Solar {c.solar_production_kw:.2f} kW  |  Load {c.home_load_kw:.2f} kW"
        )
    if full_state == "watching_for_discharge" and c.battery_soc_pct < 90.0:
        state["full_charge_state"] = "watching_for_full"
        if 15 <= now.hour < 19 and state.get("no_longer_full_date") != today:
            state["no_longer_full_date"] = today
            logger.info("Battery discharged below 90%% alert sent (%.0f%%)", c.battery_soc_pct)
            return (
                f"🔋 FranklinWH: Battery no longer full — {c.battery_soc_pct:.0f}% SoC\n"
                f"Time: {now.strftime('%-I:%M %p')}\n"
                f"Solar {c.solar_production_kw:.2f} kW  |  Load {c.home_load_kw:.2f} kW"
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
                    body = (
                        f"⚡ FranklinWH: Battery draining fast — {drain_rate:.0f}%/hr\n"
                        f"SoC {c.battery_soc_pct:.0f}%  |  Load {c.home_load_kw:.2f} kW  |  "
                        f"Solar {c.solar_production_kw:.2f} kW\n"
                        f"Time: {now.strftime('%-I:%M %p')}"
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
        f"⚠️ FranklinWH: Battery not charging despite strong solar\n"
        f"Solar {c.solar_production_kw:.2f} kW  |  Load {c.home_load_kw:.2f} kW  |  "
        f"Battery {c.battery_use_kw:+.2f} kW  |  SoC {c.battery_soc_pct:.0f}%\n"
        f"Time: {now.strftime('%-I:%M %p')} — check battery mode or inverter."
    )


def _alert_solar_degradation(state: dict, today: str, now: datetime) -> str | None:
    """Morning check: 7-day rolling PR median drops >5% vs 30-day baseline → possible degradation."""
    week_key = now.strftime("%G-W%V")  # ISO year-week, e.g. 2026-W23
    if now.hour not in (8, 9) or state.get("solar_degradation_alerted_week") == week_key:
        return None

    cutoff_30 = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    cutoff_7  = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

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
        f"⚠️ FranklinWH: Solar output trending down\n"
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

    import statistics
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
        f"🔋 FranklinWH: Possible battery capacity fade\n"
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
    check_date = datetime.now().date() - timedelta(days=1)
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
        f"⚠️ FranklinWH: Battery running short at peak for 3 days in a row\n"
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
    cycle_start = (datetime.now().date().replace(day=1) - timedelta(days=1)).replace(day=20)
    days_so_far = (datetime.now().date() - cycle_start).days
    if days_so_far < 5:
        return None

    readings = store.weekly_readings(cycle_start.strftime("%Y-%m-%d"), today)
    if not readings:
        return None

    import_cost   = 0.0
    export_credit = 0.0
    for dt, hours, grid_kw, _home_kw, _solar_kw in integrate_intervals(readings):
        r = rate_at(dt)
        if grid_kw > 0:
            import_cost   += grid_kw * hours * r
        elif grid_kw < 0:
            export_credit += -grid_kw * hours * r

    base_actual    = base_service_cost(days_so_far)
    net_actual     = import_cost - export_credit + base_actual
    daily_net      = net_actual / days_so_far
    projected_net  = daily_net * 30
    projected_imp  = import_cost / days_so_far * 30
    projected_exp  = export_credit / days_so_far * 30
    projected_base = base_service_cost(30)
    cycle_label    = f"{cycle_start.strftime('%b %-d')} – {datetime.now().date().strftime('%b %-d')}"

    state["bill_projection_date"] = today
    logger.info("Bill projection alert: %d days, net $%.2f/day → $%.2f projected",
                days_so_far, daily_net, projected_net)
    return (
        f"💡 FranklinWH: Billing cycle projection\n"
        f"Cycle so far ({cycle_label}, {days_so_far} days):\n"
        f"  Grid import:  ${import_cost:.2f}\n"
        f"  Grid export:  ${export_credit:.2f}\n"
        f"  Base service: ${base_actual:.2f}\n"
        f"  Net cost:     ${net_actual:.2f}\n\n"
        f"Projected full cycle (~30 days):\n"
        f"  Import:  ${projected_imp:.2f}\n"
        f"  Export:  ${projected_exp:.2f}\n"
        f"  Base:    ${projected_base:.2f}\n"
        f"  Net:     ${projected_net:.2f}  (${daily_net:.2f}/day avg)"
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
        f"🌡️ FranklinWH: Heat wave tomorrow — {max_temp_f:.0f}°F forecast\n"
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
        f"🔌 FranklinWH: Best EV charging window tonight\n"
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
        f"⛈️ FranklinWH: Weather alert — {events[0]}\n"
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
        f"⚡ Area power outage detected nearby (via {source})\n"
        f"Detected: {ts}\n"
        f"{status_line}{conservation}"
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
            ("weekly_summary",    lambda: _alert_weekly_summary(state, today, now, store)),
            ("monthly_summary",   lambda: _alert_monthly_summary(state, today, now, store)),
            ("grid_down",         lambda: _alert_grid_down(state, today, now, c, cfg)),
            ("grid_restored",     lambda: _alert_grid_restored(state, now, c, cfg)),
            ("battery_full_cycle",lambda: _alert_battery_full_cycle(state, today, now, c)),
            ("fast_drain",        lambda: _alert_fast_drain(state, today, now, c)),
            ("not_charging",      lambda: _alert_not_charging(state, today, now, c)),
            ("solar_degradation", lambda: _alert_solar_degradation(state, today, now)),
            ("capacity_fade",     lambda: _alert_capacity_fade(state, today, now, store)),
            ("peak_streak",       lambda: _alert_peak_streak(state, today, now)),
            ("bill_projection",   lambda: _alert_bill_projection(state, today, now, store)),
            ("heat_wave_prep",    lambda: _alert_heat_wave_prep(state, today, now, c, outlook)),
            ("storm_prep",        lambda: _alert_storm_prep(state, today, now, c, cfg)),
            ("ev_charge_window",  lambda: _alert_ev_charge_window(state, today, now, c, cfg)),
            ("area_power_outage", lambda: _alert_area_power_outage(state, today, now, c, cfg)),
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


def _dispatch_notifications(rec, cfg: Config, notify_flag: bool, last_mode: str | None, outdir: Path | None = None) -> None:
    """Send macOS + iMessage notifications when the recommendation changes or is critical."""
    changed  = rec.mode.value != last_mode
    critical = rec.urgency == "critical"

    if not (changed or critical):
        return

    # Never notify for NO_CHANGE — "Battery OK" messages are noise.
    if not rec.needs_action and not critical:
        return

    # Suppress info-level alerts during quiet hours (midnight–7am).
    if rec.urgency == "info" and not critical and datetime.now().hour < 7:
        logger.debug("Suppressing info alert during quiet hours: %s", rec.mode.value)
        return

    # Mode-change alerts fire at most once per day per mode to stop oscillation noise.
    if outdir is not None:
        with _state_lock(outdir):
            state = _load_peak_state(outdir)
            today = datetime.now().strftime("%Y-%m-%d")
            key   = f"alerted_{rec.mode.value}_date"
            if state.get(key) == today:
                return
            state[key] = today
            _save_peak_state(outdir, state)

    if notify_flag:
        notify_macos(rec)

    if cfg.imessage_phone:
        notify_imessage(rec, cfg.imessage_phone)

    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        notify_telegram(rec_to_text(rec), cfg.telegram_bot_token, cfg.telegram_chat_id)


def _resolve_gateway(client: AccountClient, gateway: str) -> str:
    if gateway:
        return gateway
    gateways = client.get_gateways()
    if not gateways:
        raise click.ClickException("No gateways found on this account.")
    gw_obj = gateways[0]
    gid = gw_obj.get("gatewayId") or gw_obj.get("id", "")
    _info(f"Gateway: {gid}")
    return gid


def _require_config(cfg: Config) -> None:
    if not cfg.is_complete():
        raise click.ClickException(
            "Setup not complete. Run:  python3.13 scrape.py setup"
        )


# ── Root group ───────────────────────────────────────────────────────

@click.group()
@click.option("--verbose", "-v", is_flag=True)
@click.option("--delay", default=1.5, hidden=True)
@click.pass_context
def cli(ctx: click.Context, verbose: bool, delay: float) -> None:
    """FranklinWH energy scraper & battery advisor."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["delay"]  = delay
    ctx.obj["config"] = load_config()


# ── Setup wizard ─────────────────────────────────────────────────────

@cli.command()
def setup() -> None:
    """Interactive setup — saves your credentials and location once."""
    cfg = load_config()

    click.echo()
    click.echo(click.style("  FranklinWH Setup Wizard", bold=True, fg="cyan"))
    _hr()
    click.echo("  Credentials are saved to ~/.franklinwh.json (chmod 600).")
    click.echo("  Press Enter to keep the current value shown in [brackets].")
    click.echo()

    # ── Credentials ──────────────────────────────────────────────────
    click.echo(click.style("  Account", bold=True))
    cfg.email    = click.prompt("  Email",    default=cfg.email or "")
    cfg.password = click.prompt("  Password", default=cfg.password or "",
                                hide_input=True, confirmation_prompt=not cfg.password)

    # Test login
    click.echo()
    click.echo("  Testing login…", nl=False)
    try:
        with AccountClient(cfg.email, cfg.password) as client:
            client.login()
            gateways = client.get_gateways()
        click.echo(click.style(" OK", fg="green"))
        if gateways:
            gw_obj  = gateways[0]
            cfg.gateway = gw_obj.get("gatewayId") or gw_obj.get("id", "")
            _ok(f"Gateway detected: {cfg.gateway}")
        else:
            _warn("No gateways found — you can add one later.")
    except ValueError as e:
        click.echo(click.style(" FAILED", fg="red"))
        _err(str(e))
        if not click.confirm("  Continue saving anyway?", default=False):
            raise SystemExit(1)

    # ── Location ─────────────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Location  (for solar forecast)", bold=True))

    if cfg.location_name:
        click.echo(f"  Current: {cfg.location_name} ({cfg.lat:.4f}, {cfg.lon:.4f})")

    while True:
        city = click.prompt(
            "  City or town",
            default=cfg.location_name or "",
        )
        click.echo(f'  Looking up "{city}"…', nl=False)
        loc = geocode(city)
        if loc:
            click.echo(click.style(f" Found: {loc.name}, {loc.country} "
                                   f"({loc.lat:.4f}, {loc.lon:.4f})", fg="green"))
            cfg.lat           = loc.lat
            cfg.lon           = loc.lon
            cfg.location_name = f"{loc.name}, {loc.country}"
            break
        else:
            click.echo(click.style(" Not found.", fg="red"))
            click.echo("  Try a larger nearby city, or enter coordinates manually.")
            if click.confirm("  Enter coordinates manually?", default=False):
                cfg.lat = click.prompt("  Latitude",  type=float, default=cfg.lat)
                cfg.lon = click.prompt("  Longitude", type=float, default=cfg.lon)
                cfg.location_name = f"{cfg.lat:.4f}, {cfg.lon:.4f}"
                break

    # ── Notifications ────────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Notifications", bold=True))
    click.echo("  Choose how you want to receive alerts. You can enable multiple channels.")
    click.echo()

    # ── Telegram ─────────────────────────────────────────────────────
    click.echo(click.style("  Telegram", bold=True) + "  — free, cross-platform, recommended")
    click.echo("    1. Message @BotFather on Telegram → /newbot → copy the token")
    click.echo("    2. Send any message to your new bot")
    click.echo("    3. Paste the token below (chat ID is auto-detected)")
    click.echo()
    tg_token = click.prompt(
        "  Bot token (leave blank to skip)",
        default=cfg.telegram_bot_token or "",
        hide_input=True,
    ).strip()
    if tg_token:
        cfg.telegram_bot_token = tg_token
        click.echo("  Send any message to your bot in Telegram now, then wait…")
        click.echo("  Detecting your Telegram chat ID (up to ~9 seconds)…", nl=False)
        chat_id = fetch_telegram_chat_id(tg_token)
        if chat_id:
            cfg.telegram_chat_id = chat_id
            click.echo(click.style(f" Found (chat ID: {chat_id})", fg="green"))
            _ok("Telegram alerts configured")
        else:
            click.echo(click.style(" Not found", fg="yellow"))
            _warn("Could not detect chat ID automatically.")
            cfg.telegram_chat_id = click.prompt(
                "  Enter chat ID manually (visit t.me/userinfobot to find yours)",
                default=cfg.telegram_chat_id or "",
            ).strip()
    else:
        cfg.telegram_bot_token = ""
        cfg.telegram_chat_id   = ""

    # ── Email ─────────────────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Email", bold=True) + "  — SMTP (Gmail, Outlook, any provider)")
    click.echo("    Gmail tip: use an App Password (myaccount.google.com → Security → App Passwords)")
    click.echo()
    email_to = click.prompt(
        "  Recipient email (leave blank to skip)",
        default=cfg.email_to or "",
    ).strip()
    if email_to:
        cfg.email_to   = email_to
        cfg.smtp_host  = click.prompt("  SMTP host",     default=cfg.smtp_host  or "smtp.gmail.com").strip()
        cfg.smtp_port  = click.prompt("  SMTP port",     default=cfg.smtp_port  or 587, type=int)
        cfg.smtp_user  = click.prompt("  SMTP username", default=cfg.smtp_user  or email_to).strip()
        cfg.smtp_password = click.prompt(
            "  SMTP password / app password", default=cfg.smtp_password or "",
            hide_input=True,
        ).strip()
        cfg.email_from = click.prompt(
            "  From address", default=cfg.email_from or email_to,
        ).strip()
        # Test
        click.echo("  Sending test email…", nl=False)
        try:
            notify_email("FranklinWH advisor connected ✓\nThis is your test message.", cfg)
            click.echo(click.style(" Sent!", fg="green"))
            _ok("Email alerts configured")
        except Exception as e:
            click.echo(click.style(f" Failed: {e}", fg="red"))
            _warn("Email saved but test failed — double-check your credentials.")
    else:
        cfg.email_to = cfg.email_from = cfg.smtp_host = cfg.smtp_user = cfg.smtp_password = ""

    # ── Webhook ───────────────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Webhook", bold=True) + "  — POST JSON to Slack, Discord, or any custom URL")
    click.echo("    Payload: {\"alert\": \"...\", \"urgent\": bool, \"timestamp\": \"ISO8601\"}")
    click.echo()
    wh = click.prompt(
        "  Webhook URL (leave blank to skip)",
        default=cfg.webhook_url or "",
    ).strip()
    if wh:
        cfg.webhook_url = wh
        click.echo("  Sending test webhook…", nl=False)
        try:
            notify_webhook("FranklinWH advisor connected ✓  This is your test message.", False, cfg)
            click.echo(click.style(" Sent!", fg="green"))
            _ok("Webhook configured")
        except Exception as e:
            click.echo(click.style(f" Failed: {e}", fg="red"))
            _warn("Webhook saved but test failed — check the URL.")
    else:
        cfg.webhook_url = ""

    # ── iMessage ─────────────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  iMessage", bold=True) + "  — macOS only")
    click.echo()
    phone = click.prompt(
        "  Phone number (e.g. +19255884276, leave blank to skip)",
        default=cfg.imessage_phone or "",
    ).strip()
    cfg.imessage_phone = phone if phone else ""
    if cfg.imessage_phone:
        import sys as _sys
        if _sys.platform != "darwin":
            _warn("iMessage only works on macOS — saved but won't send on this OS.")
        else:
            _ok(f"iMessage alerts will be sent to {cfg.imessage_phone}")

    if not any([cfg.telegram_chat_id, cfg.email_to, cfg.webhook_url, cfg.imessage_phone]):
        _warn("No notification channels configured — you won't receive any alerts.")

    # ── AI Chatbot ────────────────────────────────────────────────────
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        click.echo()
        click.echo(click.style("  AI Chatbot (optional)", bold=True))
        click.echo('  Answer questions like "How much did I save this week?" in Telegram.')
        click.echo()
        backend = click.prompt(
            "  Chat backend",
            type=click.Choice(["anthropic", "ollama", "none"]),
            default=cfg.chat_backend if cfg.chat_backend in ("anthropic", "ollama") else "none",
        )
        if backend == "anthropic":
            click.echo("  Get an API key at console.anthropic.com → API Keys (free credits available)")
            ak = click.prompt(
                "  Anthropic API key",
                default=cfg.anthropic_api_key or "",
                hide_input=True,
            ).strip()
            cfg.anthropic_api_key = ak
            cfg.chat_backend = "anthropic"
            _ok("Anthropic chatbot enabled")
        elif backend == "ollama":
            cfg.chat_backend = "ollama"
            cfg.ollama_model = click.prompt("  Ollama model", default=cfg.ollama_model or "llama3.1:8b").strip()
            cfg.ollama_url   = click.prompt("  Ollama URL",   default=cfg.ollama_url or "http://localhost:11434").strip()
            _ok(f"Ollama chatbot enabled (model: {cfg.ollama_model})")
            _info("Make sure Ollama is running: ollama serve")
        else:
            cfg.chat_backend = "none"
            _info("Chatbot disabled")

    # ── Alert preferences ─────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Alert preferences", bold=True))
    click.echo("  Safety alerts (grid outage, fast battery drain) are always on.")
    click.echo("  Toggle the optional groups below — you can change these later in ~/.franklinwh.json.")
    click.echo()

    _ALERT_GROUPS: list[tuple[str, str, list[str]]] = [
        (
            "Morning briefing",
            "Daily preview with today's solar forecast, pre-charge advice, and peak solar window",
            ["morning_preview"],
        ),
        (
            "Peak-hour monitoring",
            "Alerts during 4–9 pm: grid import, low SoC, battery not charging, export opportunity, EV charging window",
            ["grid_import", "eb_ready", "low_soc_1pm", "low_noon_soc",
             "low_morning_solar", "solar_stopped", "not_charging", "export_arbitrage",
             "ev_charge_window"],
        ),
        (
            "Daily / weekly reports",
            "End-of-day digest, weekly TOU cost summary, monthly billing cycle, bill projection",
            ["eod_digest", "weekly_summary", "monthly_summary", "bill_projection"],
        ),
        (
            "Battery health",
            "Full-charge events, capacity fade, solar degradation, heat wave & storm prep",
            ["battery_full_cycle", "solar_degradation", "capacity_fade",
             "peak_streak", "heat_wave_prep", "storm_prep"],
        ),
    ]

    cfg.disabled_alerts = list(cfg.disabled_alerts or [])
    for group_name, group_desc, members in _ALERT_GROUPS:
        currently_on = not any(m in cfg.disabled_alerts for m in members)
        click.echo(f"  {click.style(group_name, bold=True)}")
        click.echo(f"    {group_desc}")
        enabled = click.confirm("    Enable?", default=currently_on)
        if enabled:
            for m in members:
                if m in cfg.disabled_alerts:
                    cfg.disabled_alerts.remove(m)
            if click.confirm("    Customize individual alerts in this group?", default=False):
                for m in members:
                    on = click.confirm(f"      {m}?", default=m not in cfg.disabled_alerts)
                    if on and m in cfg.disabled_alerts:
                        cfg.disabled_alerts.remove(m)
                    elif not on and m not in cfg.disabled_alerts:
                        cfg.disabled_alerts.append(m)
        else:
            for m in members:
                if m not in cfg.disabled_alerts:
                    cfg.disabled_alerts.append(m)
        click.echo()

    if cfg.disabled_alerts:
        _info(f"Disabled alerts: {', '.join(sorted(cfg.disabled_alerts))}")
    else:
        _info("All optional alerts enabled")

    # ── Battery & system ──────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Battery & system", bold=True))
    _BATTERY_MODELS = [
        ("aPower 10",       10.0),
        ("aPower 15",       15.0),
        ("2× aPower 10",    20.0),
        ("2× aPower 15",    30.0),
        ("Enter manually",  None),
    ]
    click.echo()
    click.echo("  Battery model:")
    for i, (name, kwh) in enumerate(_BATTERY_MODELS, 1):
        kwh_str = f"  ({kwh} kWh)" if kwh else ""
        click.echo(f"    {i}. {name}{kwh_str}")
    model_choice = click.prompt(
        "  Select model",
        type=click.IntRange(1, len(_BATTERY_MODELS)),
        default=next(
            (i for i, (_, k) in enumerate(_BATTERY_MODELS, 1) if k == cfg.battery_capacity_kwh),
            len(_BATTERY_MODELS),
        ),
    )
    chosen_kwh = _BATTERY_MODELS[model_choice - 1][1]
    if chosen_kwh is None:
        cfg.battery_capacity_kwh = click.prompt(
            "  Battery usable capacity (kWh)", type=float,
            default=cfg.battery_capacity_kwh,
        )
    else:
        cfg.battery_capacity_kwh = chosen_kwh
        _ok(f"Battery set to {cfg.battery_capacity_kwh} kWh")

    cfg.output_dir = click.prompt("  Output directory", default=cfg.output_dir)

    # ── EV charging ───────────────────────────────────────────────────
    click.echo()
    cfg.ev_charging = click.confirm(
        "  Do you charge an EV at home? (enables off-peak charging advice)",
        default=cfg.ev_charging,
    )
    if cfg.ev_charging:
        cfg.ev_kwh_per_session = click.prompt(
            "  Typical kWh per charge (0 if unsure)", type=float,
            default=cfg.ev_kwh_per_session or 0.0,
        )

    # ── Uptime monitoring ─────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Uptime monitoring (optional)", bold=True))
    click.echo("  Get a free ping URL at healthchecks.io — you'll be notified if the advisor stops running.")
    cfg.healthcheck_url = click.prompt(
        "  Healthcheck ping URL (leave blank to skip)",
        default=cfg.healthcheck_url or "",
    ).strip()

    # ── Save ─────────────────────────────────────────────────────────
    save_config(cfg)
    click.echo()
    _ok("Configuration saved to ~/.franklinwh.json")
    click.echo()

    import sys as _sys2
    if _sys2.platform == "darwin":
        click.echo("  Start the advisor:")
        click.echo(click.style("      franklinwh install-service", fg="cyan", bold=True))
        click.echo("  (installs a LaunchAgent that runs automatically on login)")
    else:
        click.echo("  Add a cron job to run the advisor every 5 minutes (7am–11pm):")
        click.echo(click.style(
            "      (crontab -l; echo '*/5 7-23 * * * franklinwh account advise >> ~/franklinwh.log 2>&1') | crontab -",
            fg="cyan", bold=True,
        ))
    click.echo()


# ── Doctor ───────────────────────────────────────────────────────────

@cli.command()
def doctor() -> None:
    """Check your configuration and connectivity."""
    from franklinwh_scraper.config import CONFIG_PATH
    import pathlib

    click.echo()
    click.echo(click.style("  FranklinWH Doctor", bold=True, fg="cyan"))
    _hr()

    def _check(label: str, ok: bool, detail: str = "") -> None:
        mark  = click.style("✓", fg="green") if ok else click.style("✗", fg="red")
        extra = f"  {detail}" if detail else ""
        click.echo(f"  {mark}  {label}{extra}")

    cfg = load_config()

    _check("Config file exists",   CONFIG_PATH.exists(),       str(CONFIG_PATH))
    _check("Email configured",     bool(cfg.email))
    _check("Password set",         bool(cfg.password))
    _check("Location set",         bool(cfg.lat and cfg.lon),  f"{cfg.lat:.4f}, {cfg.lon:.4f}" if cfg.lat else "")

    # At least one notification channel
    has_channel = bool(
        cfg.imessage_phone or (cfg.telegram_bot_token and cfg.telegram_chat_id)
        or (cfg.smtp_host and cfg.email_to) or cfg.webhook_url
    )
    _check("Notification channel", has_channel)
    _check("Uptime monitoring",    bool(cfg.healthcheck_url),
           "configured" if cfg.healthcheck_url else "optional — set up at healthchecks.io")

    # Output dir writable
    out = pathlib.Path(cfg.output_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / ".doctor_tmp").touch()
        (out / ".doctor_tmp").unlink()
        _check("Output directory writable", True, str(out.resolve()))
    except Exception as e:
        _check("Output directory writable", False, str(e))

    # History DB
    db_path = out / "history.db"
    _check("History database exists", db_path.exists(), str(db_path) if db_path.exists() else "(will be created on first run)")

    # API login
    if cfg.email and cfg.password:
        click.echo("  Checking FranklinWH API login…", nl=False)
        try:
            with AccountClient(cfg.email, cfg.password) as client:
                client.login()
                gateways = client.get_gateways()
            click.echo(click.style(" OK", fg="green"))
            _check("API login", True, f"{len(gateways)} gateway(s) found")
        except Exception as e:
            click.echo(click.style(f" FAILED: {e}", fg="red"))
            _check("API login", False)
    else:
        _check("API login", False, "credentials not set — run: franklinwh setup")

    click.echo()


# ── Start (one-command entry point) ──────────────────────────────────

@cli.command()
@click.pass_context
def start(ctx: click.Context) -> None:
    """Start the battery advisor using your saved configuration.

    Equivalent to:  account advise --watch
    """
    cfg = ctx.obj["config"]
    _require_config(cfg)

    click.echo()
    click.echo(click.style("  FranklinWH Battery Advisor", bold=True, fg="cyan"))
    _hr()
    _info(f"Account:  {cfg.email}")
    _info(f"Location: {cfg.location_name} ({cfg.lat:.4f}, {cfg.lon:.4f})")
    _info(f"Checking every {cfg.watch_interval} minutes")
    _info(f"Output:   {cfg.output_dir}/")
    click.echo()

    ctx.invoke(
        cmd_advise,
        email=cfg.email,
        password=cfg.password,
        gateway=cfg.gateway or None,
        lat=cfg.lat,
        lon=cfg.lon,
        notify=True,
        out=cfg.output_dir,
        watch=True,
        interval=cfg.watch_interval,
    )


# ── Install macOS LaunchAgent ─────────────────────────────────────────

@cli.command("install-service")
@click.pass_context
def cmd_install_service(ctx: click.Context) -> None:
    """Install a macOS LaunchAgent so the advisor starts automatically on login."""
    import sys

    if sys.platform != "darwin":
        _err("install-service is only supported on macOS. On Linux, set up a cron job or systemd timer manually.")
        sys.exit(1)

    cfg       = ctx.obj["config"]
    python    = sys.executable
    script    = (Path(__file__).parent.parent / "scrape.py").resolve()
    log_dir   = Path(cfg.output_dir).resolve()
    label     = "com.franklinwh.advisor"
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"{label}.plist"

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script}</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>{log_dir}/advisor.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/advisor.log</string>
</dict>
</plist>"""

    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist)

    _header("LaunchAgent Installed")
    _ok(f"Written: {plist_path}")
    click.echo()
    _info(f"Load now:    launchctl load {plist_path}")
    _info(f"Start now:   launchctl start {label}")
    _info(f"Uninstall:   launchctl unload {plist_path} && rm {plist_path}")
    click.echo()


# ── Public website scrapers ───────────────────────────────────────────

@cli.group("scrape")
def grp_scrape() -> None:
    """Scrape public FranklinWH product / support data (no login needed)."""


@grp_scrape.command("products")
@click.option("--out", "-o", default="output", show_default=True)
@click.option("--format", "fmt", type=click.Choice(["json", "csv", "both"]), default="both")
@click.pass_context
def cmd_products(ctx: click.Context, out: str, fmt: str) -> None:
    """Scrape product pages — specs, features, images."""
    _header("Scraping Products")
    with FranklinWHClient(delay=ctx.obj["delay"]) as client:
        data = ProductsScraper(client).scrape_all()
    _ok(f"Scraped {len(data)} products")
    _write(data, Path(out) / "products", fmt)


@grp_scrape.command("faq")
@click.option("--out", "-o", default="output", show_default=True)
@click.option("--format", "fmt", type=click.Choice(["json", "csv", "both"]), default="both")
@click.option("--max-pages", default=5, show_default=True)
@click.pass_context
def cmd_faq(ctx: click.Context, out: str, fmt: str, max_pages: int) -> None:
    """Scrape FAQ articles for homeowners and installers."""
    _header("Scraping FAQs")
    with FranklinWHClient(delay=ctx.obj["delay"]) as client:
        data = FAQScraper(client, max_pages=max_pages).scrape_all()
    _ok(f"Scraped {len(data)} FAQ items")
    _write(data, Path(out) / "faq", fmt)


@grp_scrape.command("support")
@click.option("--out", "-o", default="output", show_default=True)
@click.option("--format", "fmt", type=click.Choice(["json", "csv", "both"]), default="both")
@click.pass_context
def cmd_support(ctx: click.Context, out: str, fmt: str) -> None:
    """Scrape knowledge base and support articles."""
    _header("Scraping Support Articles")
    with FranklinWHClient(delay=ctx.obj["delay"]) as client:
        data = SupportScraper(client).scrape_all()
    _ok(f"Scraped {len(data)} articles")
    _write(data, Path(out) / "support", fmt)


@grp_scrape.command("all")
@click.option("--out", "-o", default="output", show_default=True)
@click.option("--format", "fmt", type=click.Choice(["json", "csv", "both"]), default="both")
@click.option("--max-pages", default=5, show_default=True, hidden=True)
@click.pass_context
def cmd_scrape_all(ctx: click.Context, out: str, fmt: str, max_pages: int) -> None:
    """Scrape everything: products, FAQs, and support articles."""
    outdir = Path(out)
    delay  = ctx.obj["delay"]
    _header("Scraping All Public Data")

    with FranklinWHClient(delay=delay) as client:
        products = ProductsScraper(client).scrape_all()
        _ok(f"Products: {len(products)}")
        _write(products, outdir / "products", fmt)

        faqs = FAQScraper(client, max_pages=max_pages).scrape_all()
        _ok(f"FAQs: {len(faqs)}")
        _write(faqs, outdir / "faq", fmt)

        support = SupportScraper(client).scrape_all()
        _ok(f"Support articles: {len(support)}")
        _write(support, outdir / "support", fmt)

    export_json(
        {"products": len(products), "faqs": len(faqs), "support": len(support)},
        outdir / "summary.json",
    )
    click.echo()
    _ok(f"Done — output saved to {outdir.resolve()}/")


# ── Account commands ──────────────────────────────────────────────────

@cli.group("account")
def grp_account() -> None:
    """Live account data — requires your FranklinWH login."""


@grp_account.command("gateways")
@click.option("--email",    envvar="FRANKLINWH_EMAIL",    default=None)
@click.option("--password", envvar="FRANKLINWH_PASSWORD", default=None, hide_input=True)
@click.pass_context
def cmd_gateways(ctx: click.Context, email: str | None, password: str | None) -> None:
    """List all aGates on your account."""
    cfg = ctx.obj["config"]
    email    = email    or cfg.email
    password = password or cfg.password
    if not email or not password:
        raise click.ClickException("Run 'setup' first, or set FRANKLINWH_EMAIL / FRANKLINWH_PASSWORD.")

    _header("Your Gateways")
    with AccountClient(email, password) as client:
        gateways = client.get_gateways()

    if not gateways:
        _warn("No gateways found on this account.")
        return

    for gw in gateways:
        gid  = gw.get("gatewayId") or gw.get("id", "?")
        loc  = gw.get("address") or gw.get("location", "")
        _ok(f"{gid}  {click.style(loc, dim=True)}")


@grp_account.command("stats")
@click.option("--email",    envvar="FRANKLINWH_EMAIL",    default=None)
@click.option("--password", envvar="FRANKLINWH_PASSWORD", default=None, hide_input=True)
@click.option("--gateway",  envvar="FRANKLINWH_GATEWAY",  default=None)
@click.option("--out", "-o", default="output", show_default=True)
@click.option("--format", "fmt", type=click.Choice(["json", "csv", "both"]), default="both")
@click.pass_context
def cmd_stats(ctx: click.Context, email: str | None, password: str | None,
              gateway: str | None, out: str, fmt: str) -> None:
    """Fetch a live energy snapshot from your system."""
    cfg = ctx.obj["config"]
    email    = email    or cfg.email
    password = password or cfg.password
    gateway  = gateway  or cfg.gateway or None
    if not email or not password:
        raise click.ClickException("Run 'setup' first, or set FRANKLINWH_EMAIL / FRANKLINWH_PASSWORD.")

    _header("Live Energy Snapshot")
    with AccountClient(email, password) as client:
        gateway = _resolve_gateway(client, gateway)
        stats   = client.get_stats(gateway)

    c = stats.current
    t = stats.totals

    click.echo(f"  {'Solar':12} {c.solar_production_kw:>6.2f} kW")
    click.echo(f"  {'Battery':12} {c.battery_use_kw:>+6.2f} kW  "
               f"{click.style(f'SoC {c.battery_soc_pct:.0f}%', bold=True)}")
    click.echo(f"  {'Grid':12} {c.grid_use_kw:>+6.2f} kW  "
               f"[{c.grid_status}]")
    click.echo(f"  {'Home load':12} {c.home_load_kw:>6.2f} kW")
    _hr()
    click.echo(click.style("  Today's totals", bold=True))
    click.echo(f"  {'Solar':20} {t.solar_kwh:>7.2f} kWh")
    click.echo(f"  {'Grid consumed':20} {t.grid_load_kwh:>7.2f} kWh")
    click.echo(f"  {'Grid exported':20} {t.grid_export_kwh:>7.2f} kWh")
    click.echo(f"  {'Grid meter import':20} {t.grid_import_kwh:>7.2f} kWh  (incl. battery charging)")
    click.echo(f"  {'Home use':20} {t.home_use_kwh:>7.2f} kWh")
    click.echo(f"  {'Battery charged':20} {t.battery_charge_kwh:>7.2f} kWh")
    click.echo(f"  {'Battery discharged':20} {t.battery_discharge_kwh:>7.2f} kWh")

    _write([stats.to_flat_dict()], Path(out) / "stats", fmt)


@grp_account.command("poll")
@click.option("--email",    envvar="FRANKLINWH_EMAIL",    default=None)
@click.option("--password", envvar="FRANKLINWH_PASSWORD", default=None, hide_input=True)
@click.option("--gateway",  envvar="FRANKLINWH_GATEWAY",  default=None)
@click.option("--interval", "-i", default=30,  show_default=True, help="Seconds between readings")
@click.option("--count",    "-n", default=0,   show_default=True, help="Readings to take (0 = infinite)")
@click.option("--out", "-o", default="output", show_default=True)
@click.pass_context
def cmd_poll(ctx: click.Context, email: str | None, password: str | None,
             gateway: str | None, interval: int, count: int, out: str) -> None:
    """Continuously log live stats to a CSV file."""
    cfg = ctx.obj["config"]
    email    = email    or cfg.email
    password = password or cfg.password
    gateway  = gateway  or cfg.gateway or None
    if not email or not password:
        raise click.ClickException("Run 'setup' first.")

    outdir   = Path(out)
    log_path = outdir / "poll_log.csv"

    _header("Live Polling")
    with AccountClient(email, password) as client:
        gateway = _resolve_gateway(client, gateway)
        _info(f"Logging to {log_path}  (Ctrl+C to stop)")
        click.echo()

        iteration = 0
        try:
            while count == 0 or iteration < count:
                try:
                    stats = client.get_stats(gateway)
                    export_csv([stats.to_flat_dict()], log_path, append=True)
                    c = stats.current
                    click.echo(
                        f"  {stats.timestamp}  "
                        f"Solar {c.solar_production_kw:.2f}kW  "
                        f"Grid {c.grid_use_kw:+.2f}kW  "
                        f"Batt {c.battery_use_kw:+.2f}kW @ "
                        f"{click.style(f'{c.battery_soc_pct:.0f}%', bold=True)}  "
                        f"Home {c.home_load_kw:.2f}kW"
                    )
                    iteration += 1
                    if count == 0 or iteration < count:
                        time.sleep(interval)
                except (TimeoutError, ConnectionError) as e:
                    _warn(f"{e} — retrying in {interval}s")
                    time.sleep(interval)
        except KeyboardInterrupt:
            click.echo()
            _ok(f"Stopped after {iteration} reading(s). Log saved to {log_path}")


@grp_account.command("advise")
@click.option("--email",    envvar="FRANKLINWH_EMAIL",    default=None)
@click.option("--password", envvar="FRANKLINWH_PASSWORD", default=None, hide_input=True)
@click.option("--gateway",  envvar="FRANKLINWH_GATEWAY",  default=None)
@click.option("--lat",  envvar="FRANKLINWH_LAT",  default=None, type=float)
@click.option("--lon",  envvar="FRANKLINWH_LON",  default=None, type=float)
@click.option("--notify/--no-notify", default=True, show_default=True)
@click.option("--out", "-o", default=None)
@click.option("--watch", is_flag=True, default=False,
              help="Keep running and re-check on --interval")
@click.option("--interval", default=None, type=int,
              help="Minutes between checks (default: from setup)")
@click.pass_context
def cmd_advise(
    ctx: click.Context,
    email: str | None, password: str | None, gateway: str | None,
    lat: float | None, lon: float | None,
    notify: bool, out: str | None,
    watch: bool, interval: int | None,
) -> None:
    """Recommend a battery mode based on live stats, weather, and usage patterns.

    Tip: run 'setup' once so you never need to pass credentials here.
    """
    cfg = ctx.obj["config"]
    email    = email    or cfg.email
    password = password or cfg.password
    gateway  = gateway  or cfg.gateway or None
    lat      = lat      or (cfg.lat  if cfg.lat  else None)
    lon      = lon      or (cfg.lon  if cfg.lon  else None)
    out      = out      or cfg.output_dir
    interval = interval or cfg.watch_interval

    if not email or not password:
        raise click.ClickException("Run 'setup' first.")
    if not lat or not lon:
        raise click.ClickException("Location not set. Run 'setup' to configure it.")

    outdir    = Path(out)
    log_path  = outdir / "advisor_log.jsonl"
    db_path   = outdir / "history.db"
    last_mode = _load_last_mode(outdir)   # persists across cron runs

    if watch and not _acquire_pid_lock():
        raise click.ClickException(
            "Another instance is already running. "
            f"Stop it first, or delete {_PID_FILE} if it's stale."
        )

    with AccountClient(email, password) as client, HistoryStore(db_path) as history:
        gateway = _resolve_gateway(client, gateway)

        days     = history.distinct_days()
        readings = history.reading_count()

        _header("Battery Advisor")
        _info(f"Location:  {cfg.location_name or f'{lat:.4f}, {lon:.4f}'}")
        if days == 0:
            _info("Usage history: none yet — collecting now, predictions activate after 3 days")
        else:
            status = "predictions active" if days >= 3 else f"predictions in {3-days} more day(s)"
            _info(f"Usage history: {readings} readings across {days} day(s) — {status}")
        click.echo()

        # Start Telegram AI chatbot thread if configured
        _chatbot: TelegramChatBot | None = None
        _backend = getattr(cfg, "chat_backend", "anthropic")
        _bot_ready = (
            cfg.telegram_bot_token and _backend != "none" and (
                (_backend == "anthropic" and getattr(cfg, "anthropic_api_key", "")) or
                (_backend == "ollama")
            )
        )
        if _bot_ready and _backend == "anthropic":
            try:
                import anthropic as _anthropic_check  # noqa: F401
            except ImportError:
                _warn("anthropic package not installed — chatbot disabled. Run: pip install anthropic")
                _bot_ready = False
        if _bot_ready:
            import threading as _threading
            _chatbot = TelegramChatBot(cfg, getattr(cfg, "anthropic_api_key", ""))
            _bot_thread = _threading.Thread(target=_chatbot.run, daemon=True, name="tg-chatbot")
            _bot_thread.start()
            _info("Telegram AI chatbot started — message the bot to ask energy questions")
            click.echo()

        while True:
            try:
                stats = client.get_stats(gateway)
                history.record(stats)

                outlook        = _fetch_outlook_cached(lat, lon)
                _peak_state    = _load_peak_state(outdir)
                system_peak_kw = _get_system_peak_kw(_peak_state)
                cloudy_now     = (
                    outlook.avg_ghi(12) < _GHI_CLOUDY_THRESHOLD
                    if outlook else False
                )
                perf_ratio     = _get_performance_ratio(_peak_state, cloudy=cloudy_now)
                avg_temp_c     = outlook.avg_temp_c(24) if outlook else 22.0
                usage_forecast = (
                    predict(history, 24, outlook=outlook, system_peak_kw=system_peak_kw,
                            perf_ratio=perf_ratio, avg_temp_c=avg_temp_c)
                    if history.has_enough_data() else None
                )
                rec = recommend(
                    stats, outlook, usage_forecast,
                    battery_capacity_kwh=getattr(cfg, "battery_capacity_kwh", _BATTERY_CAPACITY_KWH),
                )

                if _chatbot is not None:
                    _chatbot.update_state(stats, history, outlook, system_peak_kw, perf_ratio)

                # Home Assistant webhook state push
                if getattr(cfg, "ha_webhook_url", ""):
                    from .notifier import notify_ha_webhook as _ha_push
                    from .tou import period_at as _pat, rate_at as _rat
                    _now = datetime.now()
                    _ha_push(cfg.ha_webhook_url, {
                        "soc_pct":          stats.current.battery_soc_pct,
                        "solar_kw":         stats.current.solar_production_kw,
                        "home_load_kw":     stats.current.home_load_kw,
                        "grid_kw":          stats.current.grid_use_kw,
                        "battery_kw":       stats.current.battery_use_kw,
                        "grid_status":      stats.current.grid_status,
                        "solar_today_kwh":  stats.totals.solar_kwh,
                        "tou_period":       _pat(_now).value,
                        "tou_rate":         _rat(_now),
                        "timestamp":        _now.isoformat(),
                    })

                # First-run welcome message
                if history.reading_count() == 1 and cfg.telegram_bot_token and cfg.telegram_chat_id:
                    notify_telegram(
                        "✅ FranklinWH Advisor is running!\n\n"
                        f"Monitoring your battery at {cfg.location_name or 'your location'}.\n"
                        "Collecting usage data — full predictions and alerts activate after 3 days.\n\n"
                        "You'll get a morning preview each day at 7:30 am and alerts whenever action is needed."
                        + ("\n\nTip: message this bot to ask energy questions." if _chatbot is not None else ""),
                        cfg.telegram_bot_token, cfg.telegram_chat_id,
                    )

                _print_recommendation(rec, stats, usage_forecast, cfg.location_name)
                notify_log(rec, log_path)
                _dispatch_notifications(rec, cfg, notify, last_mode, outdir)
                _check_peak_alerts(stats, cfg, outdir, outlook=outlook, usage_forecast=usage_forecast, store=history)

                last_mode = rec.mode.value
                _save_last_mode(outdir, last_mode)

                _ping_healthcheck(cfg)  # signal a healthy completed cycle

            except Exception as e:
                logger.exception("Watch loop error")
                _err(str(e))

            if not watch:
                break

            click.echo(click.style(
                f"  Next check in {interval} min — Ctrl+C to stop",
                dim=True,
            ))
            try:
                time.sleep(interval * 60)
                click.echo()
            except KeyboardInterrupt:
                click.echo()
                _ok("Advisor stopped.")
                break


@grp_account.command("history")
@click.option("--out", "-o", default=None)
@click.pass_context
def cmd_history(ctx: click.Context, out: str | None) -> None:
    """Show your recorded usage history and hourly load profile."""
    cfg     = ctx.obj["config"]
    out     = out or cfg.output_dir
    db_path = Path(out) / "history.db"

    if not db_path.exists():
        raise click.ClickException(
            "No history yet. Run 'start' or 'account advise --watch' to begin collecting."
        )

    _header("Usage History")
    with HistoryStore(db_path) as history:
        days     = history.distinct_days()
        readings = history.reading_count()

        _info(f"{readings} readings across {days} day(s)")
        status = "active" if days >= 3 else f"need {3-days} more day(s)"
        _info(f"Predictions: {status}")

        recent = history.recent_avg_load(2)
        if recent is not None:
            _info(f"Recent avg load (last 2h): {recent:.2f} kW")

        profile = history.load_profile()
        if not profile:
            return

        click.echo()
        click.echo(click.style("  Avg home load by hour", bold=True))
        _hr()

        by_hour: dict[int, list[float]] = {}
        for (_, hr), kw in profile.items():
            by_hour.setdefault(hr, []).append(kw)

        peak = max(
            sum(v) / len(v) for v in by_hour.values() if v
        ) if by_hour else 1.0

        for hr in range(24):
            vals = by_hour.get(hr, [])
            avg  = sum(vals) / len(vals) if vals else 0.0
            bar_len = int((avg / peak) * 30) if peak else 0
            bar  = click.style("█" * bar_len, fg="cyan") + click.style("░" * (30 - bar_len), dim=True)
            label = f"{hr:02d}:00"
            click.echo(f"  {label}  {bar}  {avg:.2f} kW")


# ── Shared helpers ────────────────────────────────────────────────────

def _print_recommendation(rec, stats, usage_forecast=None, location="") -> None:
    urgency_color = {"info": "green", "warning": "yellow", "critical": "red"}
    urgency_label = {"info": "INFO", "warning": "WARN", "critical": "CRIT"}
    emoji         = {"info": "🟢",  "warning": "🟡",   "critical": "🔴"}

    color  = urgency_color.get(rec.urgency, "white")
    action = (
        f"→ Switch to {rec.mode.value.replace('_', ' ').upper()}"
        if rec.needs_action else "No mode change needed"
    )

    click.echo(
        f"  {emoji.get(rec.urgency, '⚪')} "
        + click.style(f"[{urgency_label.get(rec.urgency)}]  {action}", fg=color, bold=True)
    )
    click.echo(f"     {rec.reason}")
    click.echo()

    c = stats.current
    click.echo(
        f"  {'Now':10}  "
        f"Solar {c.solar_production_kw:.1f}kW  "
        f"Grid {c.grid_use_kw:+.1f}kW  "
        f"Battery {c.battery_use_kw:+.1f}kW @ "
        + click.style(f"{c.battery_soc_pct:.0f}%", bold=True)
        + f"  Home {c.home_load_kw:.1f}kW"
    )
    d = rec.details
    click.echo(
        f"  {'Weather':10}  "
        f"next 6h {d['ghi_next_6h_wm2']:.0f} W/m²  "
        f"next 24h {d['ghi_next_24h_wm2']:.0f} W/m²  "
        f"cloud {d['cloud_cover_6h_pct']:.0f}%"
        + (f"  [{location}]" if location else "")
    )
    if usage_forecast and usage_forecast.confidence != "none":
        net_color = "green" if usage_forecast.net_kwh >= 0 else "yellow"
        click.echo(
            f"  {'Patterns':10}  "
            f"12h load {usage_forecast.total_load_kwh:.1f} kWh  "
            f"solar {usage_forecast.total_solar_kwh:.1f} kWh  "
            f"net "
            + click.style(f"{usage_forecast.net_kwh:+.1f} kWh", fg=net_color)
            + f"  [{usage_forecast.confidence} confidence, {usage_forecast.data_days}d data]"
        )
    _hr()


def _write(data: list, base: Path, fmt: str) -> None:
    if fmt in ("json", "both"):
        export_json(data, base.with_suffix(".json"))
    if fmt in ("csv", "both"):
        export_csv(data, base.with_suffix(".csv"))


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
