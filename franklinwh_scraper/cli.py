"""FranklinWH scraper — polished CLI."""

from __future__ import annotations

import atexit
import json
import logging
import os
import time
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

from .history import HistoryStore
from .notifier import (notify_imessage, notify_imessage_text, notify_log,
                       notify_macos, notify_telegram, fetch_telegram_chat_id,
                       rec_to_text)
from .predictor import predict
from .tou import TouPeriod, period_at, rate_at
from .scrapers import FAQScraper, ProductsScraper, SupportScraper
from .weather import fetch_solar_outlook, geocode


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
        samples = state.get("perf_ratio_cloudy_samples", [])
        if len(samples) < 3:
            sunny = state.get("perf_ratio_samples", [])
            if len(sunny) >= 3:
                s = sorted(sunny)
                return max(s[len(s) // 2] * 1.10, 0.60)
            return 0.85  # reasonable prior: cloudy panels run cooler
        s = sorted(samples)
        return max(s[len(s) // 2], 0.55)
    else:
        samples = state.get("perf_ratio_samples", [])
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

def _send_alert(body: str, cfg: Config) -> None:
    """Send to every configured channel (iMessage and/or Telegram)."""
    if cfg.imessage_phone:
        notify_imessage_text(body, cfg.imessage_phone)
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        notify_telegram(body, cfg.telegram_bot_token, cfg.telegram_chat_id)


# ── Peak-hour alert helpers ───────────────────────────────────────────

_PEAK_STATE_FILE = ".peak_alert_state.json"


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
    outlook, usage_forecast, store,
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
            pr_note = f"cloudy PR={perf_ratio:.2f} ({len(cloudy_samples)} days)"
        elif not cloudy_day and len(sunny_samples) >= 3:
            pr_note = f"PR={perf_ratio:.2f} ({len(sunny_samples)} days)"
        else:
            pr_note = cal_note
        solar_est = f"~{gen_kwh:.1f} kWh predicted ({sky}, {pr_note})"

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
    in_window = now.hour == 13 or (now.hour == 14 and now.minute == 0)
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
    in_window = (now.hour == 13 and now.minute >= 40) or (now.hour == 14 and now.minute <= 10)
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
    in_window = (now.hour == 9 and now.minute >= 30) or (now.hour == 10 and now.minute <= 30)
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
    in_window = (now.hour == 11 and now.minute >= 30) or (now.hour == 12 and now.minute <= 30)
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
        tmrw_ghi = outlook.tomorrow_avg_ghi()
        if tmrw_ghi < 100.0 and soc < 60.0:
            precharge_str = (
                f"\n\n⚡ Tomorrow looks dim ({tmrw_ghi:.0f} W/m² avg). "
                f"Consider switching to Emergency Backup tonight to top up battery."
            )

    self_suff_str = ""
    if t.home_use_kwh > 0:
        self_suff     = max(0.0, min(100.0, (t.home_use_kwh - t.grid_load_kwh) / t.home_use_kwh * 100))
        self_suff_str = f"\nSelf-sufficiency:  {self_suff:.0f}%"

    # TOU daily cost estimate + peak coverage (requires history store)
    tou_str      = ""
    peak_cov_str = ""
    if store is not None:
        interval = 0.25
        readings = store.weekly_readings(today, today)
        if readings:
            import_cost = export_credit = 0.0
            peak_total = peak_no_grid = 0
            for ts, grid_kw, _home_kw, _solar_kw in readings:
                try:
                    dt = datetime.fromisoformat(ts)
                except Exception:
                    continue
                r = rate_at(dt)
                if grid_kw > 0:
                    import_cost   += grid_kw * interval * r
                elif grid_kw < 0:
                    export_credit += abs(grid_kw) * interval * r
                if 16 <= dt.hour < 21:
                    peak_total += 1
                    if grid_kw <= 0:
                        peak_no_grid += 1
            net = import_cost - export_credit
            tou_str = (
                f"\nEst. grid cost today:  ${import_cost:.2f} import  "
                f"${export_credit:.2f} export  (net ${net:.2f})"
            )
            if peak_total > 0:
                pct = peak_no_grid / peak_total * 100
                peak_cov_str = f"\nPeak coverage (4–9 pm): {pct:.0f}% battery/solar"
                state[f"peak_cov_{today}"] = pct
            else:
                state[f"peak_cov_{today}"] = 0.0

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

    interval      = 0.25   # 15-min poll → kWh per reading
    import_cost   = 0.0
    export_credit = 0.0
    peak_saved    = 0.0    # on-peak hours (4–9 pm)
    sop_saved     = 0.0    # super off-peak hours

    for ts, grid_kw, home_kw, _solar_kw in readings:
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            continue
        r      = rate_at(dt)
        period = period_at(dt)
        if grid_kw > 0:
            import_cost   += grid_kw * interval * r
        elif grid_kw < 0:
            export_credit += abs(grid_kw) * interval * r
        # Energy served by battery+solar (not drawn from grid) × avoided rate
        batt_solar_kwh = max(0.0, home_kw - max(0.0, grid_kw)) * interval
        if period == TouPeriod.ON_PEAK:
            peak_saved += batt_solar_kwh * r
        elif period == TouPeriod.SUPER_OFF_PEAK:
            sop_saved  += batt_solar_kwh * r

    net_cost    = import_cost - export_credit
    total_saved = peak_saved + sop_saved
    week_label  = f"{week_start.strftime('%b %-d')}–{week_end.strftime('%b %-d')}"

    state["weekly_summary_sent"] = today
    logger.info("Weekly TOU summary sent for week ending %s", today)
    return (
        f"📊 FranklinWH Weekly Summary — {week_label}\n\n"
        f"Grid cost (EV-TOU-5 rates):\n"
        f"  Imported:  ${import_cost:.2f}\n"
        f"  Exported:  ${export_credit:.2f} (est.)\n"
        f"  Net cost:  ${net_cost:.2f}\n\n"
        f"Est. savings from battery + solar:\n"
        f"  Peak (4–9 pm):    ${peak_saved:.2f}\n"
        f"  Super off-peak:   ${sop_saved:.2f}\n"
        f"  Total saved:      ${total_saved:.2f}"
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
    )


def _alert_grid_down(state: dict, today: str, now: datetime, c, cfg: Config) -> str | None:
    if c.grid_status != "down" or state.get("grid_down_alerted_date") == today:
        return None
    backup_str = ""
    if c.home_load_kw > 0.1:
        backup_h   = c.battery_soc_pct / 100 * cfg.battery_capacity_kwh / c.home_load_kw
        backup_str = f"  |  Est. backup: ~{backup_h:.1f} hr"
    state["grid_down_alerted_date"] = today
    state["grid_down_start"]        = now.isoformat()
    state["grid_down_soc"]          = c.battery_soc_pct
    logger.info("Grid-down alert sent for %s", today)
    return (
        f"🔴 FranklinWH: GRID DOWN at {now.strftime('%-I:%M %p')}\n"
        f"Running on battery — SoC {c.battery_soc_pct:.0f}%  |  Load {c.home_load_kw:.2f} kW{backup_str}\n"
        f"Solar {c.solar_production_kw:.2f} kW"
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
    state["not_charging_date"] = today
    logger.info("Not-charging alert sent for %s (solar=%.2f kW, batt=%.2f kW)", today, c.solar_production_kw, c.battery_use_kw)
    return (
        f"⚠️ FranklinWH: Battery not charging despite strong solar\n"
        f"Solar {c.solar_production_kw:.2f} kW  |  Battery {c.battery_use_kw:+.2f} kW  |  SoC {c.battery_soc_pct:.0f}%\n"
        f"Time: {now.strftime('%-I:%M %p')} — check battery mode or inverter."
    )


def _alert_solar_degradation(state: dict, today: str, now: datetime) -> str | None:
    """Morning check: 7-day rolling PR median drops >5% vs 30-day baseline → possible degradation."""
    if now.hour not in (8, 9) or state.get("solar_degradation_alerted_week") == today[:7]:
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
    state["solar_degradation_alerted_week"] = today[:7]
    logger.info("Solar degradation alert: baseline PR=%.3f recent PR=%.3f drop=%.1f%%",
                baseline, recent, drop_pct)
    return (
        f"⚠️ FranklinWH: Solar output trending down\n"
        f"7-day performance ratio: {recent:.2f} vs 30-day baseline {baseline:.2f} "
        f"({drop_pct:.0f}% drop)\n"
        f"This may indicate panel soiling, shading, or inverter efficiency loss.\n"
        f"Consider cleaning panels or checking inverter logs."
    )


def _alert_peak_streak(state: dict, today: str, now: datetime) -> str | None:
    """Evening check: last 3 consecutive days all <50% peak coverage → battery under-sized or depleting early."""
    if now.hour not in (21, 22) or state.get("peak_streak_alerted_week") == today[:7]:
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

    state["peak_streak_alerted_week"] = today[:7]
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

    interval      = 0.25
    import_cost   = 0.0
    export_credit = 0.0
    for ts, grid_kw, _home_kw, _solar_kw in readings:
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            continue
        r = rate_at(dt)
        if grid_kw > 0:
            import_cost   += grid_kw * interval * r
        elif grid_kw < 0:
            export_credit += abs(grid_kw) * interval * r

    net_actual     = import_cost - export_credit
    daily_net      = net_actual / days_so_far
    projected_net  = daily_net * 30
    projected_imp  = import_cost / days_so_far * 30
    projected_exp  = export_credit / days_so_far * 30
    cycle_label    = f"{cycle_start.strftime('%b %-d')} – {datetime.now().date().strftime('%b %-d')}"

    state["bill_projection_date"] = today
    logger.info("Bill projection alert: %d days, net $%.2f/day → $%.2f projected",
                days_so_far, daily_net, projected_net)
    return (
        f"💡 FranklinWH: Billing cycle projection\n"
        f"Cycle so far ({cycle_label}, {days_so_far} days):\n"
        f"  Grid import:  ${import_cost:.2f}\n"
        f"  Grid export:  ${export_credit:.2f}\n"
        f"  Net cost:     ${net_actual:.2f}\n\n"
        f"Projected full cycle (~30 days):\n"
        f"  Import:  ${projected_imp:.2f}\n"
        f"  Export:  ${projected_exp:.2f}\n"
        f"  Net:     ${projected_net:.2f}  (${daily_net:.2f}/day avg)"
    )


def _check_peak_alerts(stats, cfg: Config, out: Path, outlook=None, usage_forecast=None, store=None) -> None:
    if not cfg.imessage_phone and not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        return

    now   = datetime.now()
    today = now.strftime("%Y-%m-%d")
    state = _load_peak_state(out)
    c     = stats.current

    _calibrate_solar(state, c.solar_production_kw, outlook)

    for body in [
        _alert_morning_preview(state, today, now, c, outlook, usage_forecast, store),
        _alert_grid_import(state, today, now, c),
        _alert_low_soc_1pm(state, today, now, c),
        _alert_eb_ready(state, today, now, c),
        _alert_low_morning_solar(state, today, now, c),
        _alert_solar_stopped(state, today, now, c),
        _alert_low_noon_soc(state, today, now, c),
        _alert_eod_digest(state, today, now, stats, cfg, outlook, usage_forecast, store),
        _alert_weekly_summary(state, today, now, store),
        _alert_monthly_summary(state, today, now, store),
        _alert_grid_down(state, today, now, c, cfg),
        _alert_grid_restored(state, now, c, cfg),
        _alert_battery_full_cycle(state, today, now, c),
        _alert_fast_drain(state, today, now, c),
        _alert_not_charging(state, today, now, c),
        _alert_solar_degradation(state, today, now),
        _alert_peak_streak(state, today, now),
        _alert_bill_projection(state, today, now, store),
    ]:
        if body:
            _send_alert(body, cfg)

    _save_peak_state(out, state)


def _dispatch_notifications(rec, cfg: Config, notify_flag: bool, last_mode: str | None, outdir: Path | None = None) -> None:
    """Send macOS + iMessage notifications when the recommendation changes or is critical."""
    changed  = rec.mode.value != last_mode
    critical = rec.urgency == "critical"

    if not (changed or critical):
        return

    # Never notify for NO_CHANGE — "Battery OK" messages are noise.
    if not rec.needs_action and not critical:
        return

    # Mode-change alerts fire at most once per day per mode to stop oscillation noise.
    if outdir is not None:
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
    click.echo("  You can enable iMessage (macOS only) and/or Telegram (free, any OS).")
    click.echo()

    # ── iMessage ─────────────────────────────────────────────────────
    phone = click.prompt(
        "  iMessage phone number (e.g. +19255884276, or leave blank to skip)",
        default=cfg.imessage_phone or "",
    ).strip()
    cfg.imessage_phone = phone if phone else ""
    if cfg.imessage_phone:
        _ok(f"iMessage alerts will be sent to {cfg.imessage_phone}")

    # ── Telegram ──────────────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Telegram (optional, works on any device)", bold=True))
    click.echo("  1. Message @BotFather on Telegram → /newbot → copy the token")
    click.echo("  2. Send any message to your new bot")
    click.echo("  3. Paste the token below — chat ID is auto-detected")
    click.echo()
    tg_token = click.prompt(
        "  Telegram bot token (leave blank to skip)",
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

    # ── AI Chatbot ────────────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  AI Chatbot (optional — answers questions in Telegram)", bold=True))
    click.echo("  Backends: 'anthropic' (cloud, free API key) or 'ollama' (local, private)")
    click.echo()
    backend = click.prompt(
        "  Chat backend",
        type=click.Choice(["anthropic", "ollama", "none"]),
        default=cfg.chat_backend if cfg.chat_backend in ("anthropic", "ollama") else "none",
    )
    if backend == "anthropic":
        click.echo("  Get a free API key at console.anthropic.com → API Keys")
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
        cfg.ollama_model = click.prompt(
            "  Ollama model",
            default=cfg.ollama_model or "llama3.1:8b",
        ).strip()
        cfg.ollama_url = click.prompt(
            "  Ollama URL",
            default=cfg.ollama_url or "http://localhost:11434",
        ).strip()
        _ok(f"Ollama chatbot enabled (model: {cfg.ollama_model})")
        _info("Make sure Ollama is running: ollama serve")
        _info(f"Pull model if needed: ollama pull {cfg.ollama_model}")
    else:
        cfg.chat_backend = "none"
        cfg.anthropic_api_key = ""
        _info("Chatbot disabled")

    # ── Preferences ──────────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  Preferences", bold=True))
    cfg.watch_interval = click.prompt(
        "  Advisor check interval (minutes)", type=int,
        default=cfg.watch_interval,
    )
    cfg.output_dir = click.prompt(
        "  Output directory", default=cfg.output_dir,
    )
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

    # ── Save ─────────────────────────────────────────────────────────
    save_config(cfg)
    click.echo()
    _ok("Configuration saved to ~/.franklinwh.json")
    click.echo()
    click.echo("  You're all set! To start monitoring, run:")
    click.echo(click.style("      python3.13 scrape.py start", fg="cyan", bold=True))
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
                system_peak_kw = _get_system_peak_kw(_load_peak_state(outdir))
                usage_forecast = (
                    predict(history, 24, outlook=outlook, system_peak_kw=system_peak_kw)
                    if history.has_enough_data() else None
                )
                rec = recommend(
                    stats, outlook, usage_forecast,
                    battery_capacity_kwh=getattr(cfg, "battery_capacity_kwh", _BATTERY_CAPACITY_KWH),
                )

                if _chatbot is not None:
                    _chatbot.update_state(stats, history, outlook)

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
