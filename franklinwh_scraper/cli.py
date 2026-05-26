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
from .client import FranklinWHClient
from .config import Config, load as load_config, save as save_config
from .exporters import export_csv, export_json

from .history import HistoryStore
from .notifier import (notify_imessage, notify_imessage_text, notify_log,
                       notify_macos, notify_telegram, fetch_telegram_chat_id,
                       rec_to_text)
from .predictor import predict
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


def _get_performance_ratio(state: dict) -> float:
    """Return empirical performance ratio (actual / predicted daily kWh).

    Computed as the median of the last 30 days where we had both a stored
    prediction and a measurable actual output (>= 3 kWh predicted).
    Returns 1.0 until at least 3 complete days have been compared.
    """
    samples = state.get("perf_ratio_samples", [])
    if len(samples) < 3:
        return 1.0
    s = sorted(samples)
    # Floor 0.60: prevents one catastrophic outlier from tanking forecasts,
    # but low enough to not override the real median (currently ~0.606).
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
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _prune_old_state(state: dict) -> dict:
    """Drop date-keyed entries older than 30 days to prevent unbounded growth."""
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    return {
        k: v for k, v in state.items()
        if not (k.endswith("_date") and isinstance(v, str) and v < cutoff)
    }


def _save_peak_state(out: Path, state: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    target = out / _PEAK_STATE_FILE
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(_prune_old_state(state)))
    tmp.replace(target)  # atomic on POSIX — no partial-write corruption


def _check_peak_alerts(stats, cfg: Config, out: Path, outlook=None, usage_forecast=None, store=None) -> None:
    """
    Ten daily alerts for solar, battery, and grid awareness.

    0.  Morning preview 8 am      — SoC + predicted solar kWh for the day
    1.  Grid import 4–9 pm        — grid_kw > 0.3 during peak window
    2.  Low SoC 1–2 pm            — SoC < 40% before peak; switch to EB
    3.  EB 80% ready ~1:50 pm     — SoC >= 80% in 1:40–2:10 pm window
    4.  Low solar by 10 am        — solar < 0.5 kW at 9:30–10:30 am
    5.  Solar stopped mid-day     — solar drops to < 0.3 kW between 11 am–3 pm
    6.  Battery not charging noon — SoC < 30% at 11:30 am–12:30 pm despite solar
    7.  End-of-day digest 9 pm    — single summary text each evening
    8.  Grid down                 — grid_status == "down", fires immediately (once/day)
    9.  Battery fully charged     — SoC >= 99%, fires once per charge cycle
    10. Battery draining fast     — > 8%/hr and SoC < 35%, fires once/day
    11. Battery not charging      — solar > 1.5 kW, SoC < 80%, battery idle 10am–2pm
    """
    if not cfg.imessage_phone and not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        return

    now       = datetime.now()
    today     = now.strftime("%Y-%m-%d")
    hour      = now.hour
    minute    = now.minute
    state     = _load_peak_state(out)
    changed   = False

    c           = stats.current
    soc         = c.battery_soc_pct
    grid_kw     = c.grid_use_kw
    solar_kw    = c.solar_production_kw
    load_kw     = c.home_load_kw
    battery_kw  = c.battery_use_kw   # negative = charging, positive = discharging (p_fhp convention)
    grid_status = c.grid_status

    # ── Solar calibration (runs every poll) ──────────────────────────
    # When solar is actively generating and GHI is known, record the
    # system's effective peak kW at standard test conditions (1000 W/m²).
    # This converges to an accurate system_peak_kw over time.
    if outlook and solar_kw >= 1.0:
        current_ghi = outlook.avg_ghi(1)  # GHI over the next hour ≈ current
        if current_ghi >= 400:
            sample = round(solar_kw / (current_ghi / 1000.0), 2)
            # Sanity check: residential PV systems are 0.5–25 kW peak; reject corrupted readings
            if 0.5 <= sample <= 25.0:
                samples = state.get("solar_cal_samples", [])
                samples.append(sample)
                state["solar_cal_samples"] = samples[-50:]  # keep last 50
                changed = True

    # ── Alert 0: morning preview 8:00–12:59 pm ───────────────────────
    # Wide window so Mac sleep/wake still delivers the alert. Dedup via
    # morning_preview_date ensures it fires at most once per day.
    in_morning_preview = (8 <= hour < 13)
    if in_morning_preview and state.get("morning_preview_date") != today:
        # ── Update performance ratio from yesterday's actual vs predicted ──
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        pred_key  = f"predicted_kwh_{yesterday}"
        if store is not None and pred_key in state:
            # Use API's own daily total (MAX of running counter) — immune to poll gaps.
            # Fall back to sampled sum only if API total unavailable (old rows).
            actual_kwh = store.daily_solar_kwh_api(yesterday)
            if actual_kwh <= 0.0:
                actual_kwh = store.daily_solar_kwh(yesterday)
            predicted_kwh = state[pred_key]
            if predicted_kwh >= 3.0 and actual_kwh >= 0.5:
                ratio = round(actual_kwh / predicted_kwh, 3)
                pr_samples = state.get("perf_ratio_samples", [])
                pr_samples.append(ratio)
                state["perf_ratio_samples"] = pr_samples[-30:]
                logger.info(
                    "Performance ratio update: actual=%.1f predicted=%.1f ratio=%.3f (median=%.3f)",
                    actual_kwh, predicted_kwh, ratio, _get_performance_ratio(state),
                )
                changed = True

        if outlook:
            cal_samples = state.get("solar_cal_samples", [])
            if len(cal_samples) >= 3:
                sorted_samples = sorted(cal_samples)
                system_peak_kw = sorted_samples[len(sorted_samples) // 2]  # median
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

            perf_ratio = _get_performance_ratio(state)
            gen_kwh    = round(outlook.today_generation_kwh(system_peak_kw) * perf_ratio, 1)

            # Store today's prediction for tomorrow's ratio update
            state[f"predicted_kwh_{today}"] = gen_kwh
            changed = True

            avg_ghi = outlook.avg_ghi(12)
            if avg_ghi >= 400:
                sky = "Sunny"
            elif avg_ghi >= 180:
                sky = "Partly cloudy"
            else:
                sky = "Cloudy"
            pr_note = f"PR={perf_ratio:.2f}" if len(state.get("perf_ratio_samples", [])) >= 3 else cal_note
            solar_est = f"~{gen_kwh:.1f} kWh predicted ({sky}, {pr_note})"
        else:
            solar_est = "Solar forecast unavailable"
        body = (
            f"☀️ FranklinWH: Good morning! Daily preview\n"
            f"Battery: {soc:.0f}% SoC  |  Solar now: {solar_kw:.2f} kW\n"
            f"{solar_est}"
        )
        state["morning_preview_date"] = today
        changed = True
        _send_alert(body, cfg)
        logger.info("Morning preview alert sent for %s", today)

    # ── Alert 1: pulling from grid during 4–9 pm ─────────────────────
    in_peak_window = 16 <= hour < 21
    if in_peak_window and grid_kw > 0.3:
        if state.get("grid_import_alerted_date") != today:
            body = (
                f"⚠️ FranklinWH: Pulling from grid during peak hours (4–9 pm)\n"
                f"SoC {soc:.0f}%  |  Grid +{grid_kw:.2f} kW  |  "
                f"Solar {solar_kw:.2f} kW  |  Load {load_kw:.2f} kW\n"
                f"Time: {now.strftime('%-I:%M %p')}"
            )
            _send_alert(body, cfg)
            logger.info("Peak grid-import alert sent for %s", today)
            state["grid_import_alerted_date"] = today
            changed = True

    # ── Alert 2: SoC < 40% at 1–2 pm ────────────────────────────────
    in_1pm_window = hour == 13 or (hour == 14 and minute == 0)
    if in_1pm_window and soc < 40.0:
        if state.get("low_soc_1pm_alerted_date") != today:
            body = (
                f"🟡 FranklinWH: Battery under 40% at {now.strftime('%-I:%M %p')}\n"
                f"SoC {soc:.0f}% — grid import risk during 4–9 pm peak\n"
                f"Solar {solar_kw:.2f} kW  |  Load {load_kw:.2f} kW\n"
                f"Consider switching to Emergency Backup to charge before peak."
            )
            _send_alert(body, cfg)
            logger.info("Low 1 pm SoC alert sent for %s (%.0f%%)", today, soc)
            state["low_soc_1pm_alerted_date"] = today
            changed = True

    # ── Alert 3: SoC ≥ 80% at ~1:50 pm (Emergency Backup ready) ─────
    in_eb_window = (hour == 13 and minute >= 40) or (hour == 14 and minute <= 10)
    if in_eb_window and soc >= 80.0:
        if state.get("eb_80pct_alerted_date") != today:
            body = (
                f"🟢 FranklinWH: Battery at {soc:.0f}% — Emergency Backup target reached\n"
                f"Time: {now.strftime('%-I:%M %p')} — battery ready before 4 pm peak\n"
                f"Solar {solar_kw:.2f} kW  |  Load {load_kw:.2f} kW\n"
                f"You can now switch modes if needed."
            )
            _send_alert(body, cfg)
            logger.info("EB 80%% SoC alert sent for %s (%.0f%%)", today, soc)
            state["eb_80pct_alerted_date"] = today
            changed = True

    # ── Alert 4: low solar production by 10 am ───────────────────────
    # Window: 9:30–10:30 am. Threshold: < 0.5 kW means heavy cloud cover.
    in_morning_window = (hour == 9 and minute >= 30) or (hour == 10 and minute <= 30)
    if in_morning_window and solar_kw < 0.5:
        if state.get("low_solar_morning_date") != today:
            body = (
                f"☁️ FranklinWH: Low solar at {now.strftime('%-I:%M %p')} — cloudy day ahead\n"
                f"Solar {solar_kw:.2f} kW  |  SoC {soc:.0f}%  |  Load {load_kw:.2f} kW\n"
                f"Consider conserving battery early — less solar charging expected today."
            )
            _send_alert(body, cfg)
            logger.info("Low morning solar alert sent for %s (%.2f kW)", today, solar_kw)
            state["low_solar_morning_date"] = today
            changed = True

    # ── Alert 5: solar stopped unexpectedly mid-day ───────────────────
    # Window: 11 am–3 pm. Fires if solar was previously > 0.5 kW and
    # drops to < 0.3 kW. Track last-seen solar to detect the drop.
    in_midday = 11 <= hour < 15
    if in_midday:
        last_solar = state.get("last_midday_solar_kw", 0.0)
        if last_solar >= 0.5 and solar_kw < 0.3:
            if state.get("solar_stopped_date") != today:
                body = (
                    f"🔴 FranklinWH: Solar dropped mid-day — possible issue\n"
                    f"Was {last_solar:.2f} kW → now {solar_kw:.2f} kW "
                    f"at {now.strftime('%-I:%M %p')}\n"
                    f"SoC {soc:.0f}%  |  Check inverter or cloud cover."
                )
                _send_alert(body, cfg)
                logger.info("Solar stopped alert sent for %s (%.2f→%.2f kW)", today, last_solar, solar_kw)
                state["solar_stopped_date"] = today
                changed = True
        state["last_midday_solar_kw"] = solar_kw
        changed = True

    # ── Alert 6: battery not charging by noon despite solar ───────────
    # Window: 11:30 am–12:30 pm. SoC < 30% with solar available (> 0.5 kW).
    in_noon_window = (hour == 11 and minute >= 30) or (hour == 12 and minute <= 30)
    if in_noon_window and soc < 30.0 and solar_kw > 0.5:
        if state.get("low_noon_soc_date") != today:
            body = (
                f"🟡 FranklinWH: Battery still low at noon — only {soc:.0f}% SoC\n"
                f"Solar {solar_kw:.2f} kW available but battery hasn't recovered\n"
                f"Time: {now.strftime('%-I:%M %p')}  |  Load {load_kw:.2f} kW\n"
                f"Check battery mode — may need manual intervention."
            )
            _send_alert(body, cfg)
            logger.info("Low noon SoC alert sent for %s (%.0f%%)", today, soc)
            state["low_noon_soc_date"] = today
            changed = True

    # ── Alert 7: end-of-day digest at 9 pm ───────────────────────────
    # Window: full 9 pm hour. Wide enough to guarantee a 15-min poll hits it.
    in_eod_window = hour in (21, 22)
    if in_eod_window:
        if state.get("eod_digest_date") != today:
            t = stats.totals
            if load_kw > 0.1:
                backup_h = soc / 100 * getattr(cfg, "battery_capacity_kwh", _BATTERY_CAPACITY_KWH) / load_kw
                backup_str = f"\nEst. backup now: ~{backup_h:.1f} hr at current load"
            else:
                backup_str = ""

            # Solar prediction vs actual delta
            predicted_kwh = state.get(f"predicted_kwh_{today}")
            actual_kwh    = t.solar_kwh
            if predicted_kwh and predicted_kwh > 0:
                delta_kwh = actual_kwh - predicted_kwh
                delta_pct = (delta_kwh / predicted_kwh) * 100
                sign      = "+" if delta_kwh >= 0 else ""
                solar_delta_str = (
                    f"\n\nSolar forecast vs actual:\n"
                    f"  Predicted:  {predicted_kwh:.1f} kWh\n"
                    f"  Actual:     {actual_kwh:.1f} kWh\n"
                    f"  Delta:      {sign}{delta_kwh:.1f} kWh ({sign}{delta_pct:.0f}%)"
                )
            else:
                solar_delta_str = ""

            # Predict SoC at 6 am tomorrow from overnight load profile
            bat_cap = getattr(cfg, "battery_capacity_kwh", _BATTERY_CAPACITY_KWH)
            soc_6am_str = ""
            if usage_forecast and usage_forecast.hours:
                tomorrow_6am = (now + timedelta(days=1)).replace(
                    hour=6, minute=0, second=0, microsecond=0
                )
                # Sum net kWh (solar−load) for each predicted hour between now and 6 am.
                # Solar is ~0 overnight so net_kw ≈ −load_kw for each slot.
                night_net_kwh = sum(
                    p.net_kw
                    for p in usage_forecast.hours
                    if now <= p.dt < tomorrow_6am
                )
                predicted_soc_6am = max(0.0, min(100.0, soc + night_net_kwh / bat_cap * 100))
                soc_6am_str = f"\nPredicted SoC @ 6 am: ~{predicted_soc_6am:.0f}%"

            body = (
                f"📊 FranklinWH Daily Summary — {now.strftime('%a %b %-d')}\n"
                f"Solar generated:  {t.solar_kwh:.1f} kWh\n"
                f"Grid consumed:    {t.grid_load_kwh:.1f} kWh\n"
                f"Grid exported:    {t.grid_export_kwh:.1f} kWh\n"
                f"Home used:        {t.home_use_kwh:.1f} kWh\n"
                f"Battery SoC now:  {soc:.0f}%{backup_str}{soc_6am_str}{solar_delta_str}"
            )
            _send_alert(body, cfg)
            logger.info("End-of-day digest sent for %s", today)
            state["eod_digest_date"] = today
            changed = True

    # ── Monthly summary: last day of month at EOD ─────────────────────
    # Fires once on the last calendar day, comparing this month to last month.
    if in_eod_window and store is not None:
        import calendar as _cal
        last_day_of_month = _cal.monthrange(now.year, now.month)[1]
        if now.day == last_day_of_month and state.get("monthly_summary_month") != today[:7]:
            this_ym  = now.strftime("%Y-%m")
            first_of_this = now.replace(day=1)
            last_ym  = (first_of_this - timedelta(days=1)).strftime("%Y-%m")

            cur  = store.monthly_totals(this_ym)
            prev = store.monthly_totals(last_ym)

            def _delta(a: float, b: float) -> str:
                if b == 0:
                    return ""
                d = a - b
                pct = d / b * 100
                sign = "+" if d >= 0 else ""
                return f"  ({sign}{d:.1f} kWh, {sign}{pct:.0f}%)"

            prev_label = (first_of_this - timedelta(days=1)).strftime("%b")
            cur_label  = now.strftime("%b")
            sparse_note = (
                f"\n⚠️ Prior month only {prev.days_with_data}d data"
                if prev.days_with_data < 20 else ""
            )

            body = (
                f"📅 FranklinWH Monthly Summary — {cur_label} vs {prev_label}\n\n"
                f"Solar generated:\n"
                f"  {cur_label}: {cur.solar_kwh:.1f} kWh{_delta(cur.solar_kwh, prev.solar_kwh)}\n"
                f"  {prev_label}: {prev.solar_kwh:.1f} kWh\n\n"
                f"Grid imported:\n"
                f"  {cur_label}: {cur.grid_import_kwh:.1f} kWh{_delta(cur.grid_import_kwh, prev.grid_import_kwh)}\n"
                f"  {prev_label}: {prev.grid_import_kwh:.1f} kWh\n\n"
                f"Grid exported:\n"
                f"  {cur_label}: {cur.grid_export_kwh:.1f} kWh{_delta(cur.grid_export_kwh, prev.grid_export_kwh)}\n"
                f"  {prev_label}: {prev.grid_export_kwh:.1f} kWh\n\n"
                f"Home used:\n"
                f"  {cur_label}: {cur.home_load_kwh:.1f} kWh{_delta(cur.home_load_kwh, prev.home_load_kwh)}\n"
                f"  {prev_label}: {prev.home_load_kwh:.1f} kWh{sparse_note}"
            )
            _send_alert(body, cfg)
            logger.info("Monthly summary sent for %s", this_ym)
            state["monthly_summary_month"] = today[:7]
            changed = True

    # ── Alert 9: battery fully charged / no longer full ──────────────
    # State machine: fires when SoC reaches 99%, then fires again when
    # SoC drops below 90% (battery is being used), then resets.
    full_state = state.get("full_charge_state", "watching_for_full")
    if full_state == "watching_for_full" and soc >= 99.0:
        body = (
            f"🔋 FranklinWH: Battery fully charged — {soc:.0f}% SoC\n"
            f"Time: {now.strftime('%-I:%M %p')}\n"
            f"Solar {solar_kw:.2f} kW  |  Load {load_kw:.2f} kW"
        )
        _send_alert(body, cfg)
        logger.info("Full charge alert sent (%.0f%%)", soc)
        state["full_charge_state"] = "watching_for_discharge"
        changed = True
    elif full_state == "watching_for_discharge" and soc < 90.0:
        # Only alert once per day in the 3–7 pm window (solar winding down).
        # Outside that window, reset state silently so the cycle works tomorrow.
        if 15 <= hour < 19 and state.get("no_longer_full_date") != today:
            body = (
                f"🔋 FranklinWH: Battery no longer full — {soc:.0f}% SoC\n"
                f"Time: {now.strftime('%-I:%M %p')}\n"
                f"Solar {solar_kw:.2f} kW  |  Load {load_kw:.2f} kW"
            )
            _send_alert(body, cfg)
            logger.info("Battery discharged below 90%% alert sent (%.0f%%)", soc)
            state["no_longer_full_date"] = today
        else:
            logger.info("Battery discharged below 90%% — outside 3–7 pm window, suppressed")
        state["full_charge_state"] = "watching_for_full"
        changed = True

    # ── Alert 8: grid down ────────────────────────────────────────────
    # Fires immediately (once per day) when grid_status is "down".
    if grid_status == "down":
        if state.get("grid_down_alerted_date") != today:
            if load_kw > 0.1:
                backup_h = soc / 100 * getattr(cfg, "battery_capacity_kwh", _BATTERY_CAPACITY_KWH) / load_kw
                backup_str = f"  |  Est. backup: ~{backup_h:.1f} hr"
            else:
                backup_str = ""
            body = (
                f"🔴 FranklinWH: GRID DOWN at {now.strftime('%-I:%M %p')}\n"
                f"Running on battery — SoC {soc:.0f}%  |  Load {load_kw:.2f} kW{backup_str}\n"
                f"Solar {solar_kw:.2f} kW"
            )
            _send_alert(body, cfg)
            logger.info("Grid-down alert sent for %s", today)
            state["grid_down_alerted_date"] = today
            changed = True

    # ── Alert 10: battery draining fast ─────────────────────────────────
    # Fires when SoC drops > 8%/hr and battery is below 35% — unexpected
    # high load or inverter issue. Track SoC every poll to compute the rate.
    prev_soc      = state.get("last_soc")
    prev_soc_time = state.get("last_soc_time")
    if prev_soc is not None and prev_soc_time is not None:
        try:
            elapsed_h = (now - datetime.fromisoformat(prev_soc_time)).total_seconds() / 3600
            if elapsed_h > 0:
                drain_rate = (prev_soc - soc) / elapsed_h
                if drain_rate >= 8.0 and soc < 35.0:
                    if state.get("fast_drain_alerted_date") != today:
                        body = (
                            f"⚡ FranklinWH: Battery draining fast — {drain_rate:.0f}%/hr\n"
                            f"SoC {soc:.0f}%  |  Load {load_kw:.2f} kW  |  "
                            f"Solar {solar_kw:.2f} kW\n"
                            f"Time: {now.strftime('%-I:%M %p')}"
                        )
                        state["fast_drain_alerted_date"] = today
                        changed = True
                        _send_alert(body, cfg)
                        logger.info("Fast drain alert sent for %s (%.0f%%/hr, %.0f%%)", today, drain_rate, soc)
        except (ValueError, TypeError):
            pass
    state["last_soc"] = soc
    state["last_soc_time"] = now.isoformat()
    changed = True

    # ── Alert 11: battery not charging despite strong solar ──────────────
    # Window: 10 am–2 pm. Fires if solar is strong but battery isn't absorbing it.
    # p_fhp sign: negative = charging, positive = discharging. "Not charging" = battery_kw > -0.2.
    in_solar_peak = 10 <= hour < 14
    if in_solar_peak and solar_kw > 1.5 and soc < 80.0 and battery_kw > -0.2:
        if state.get("not_charging_date") != today:
            body = (
                f"⚠️ FranklinWH: Battery not charging despite strong solar\n"
                f"Solar {solar_kw:.2f} kW  |  Battery {battery_kw:+.2f} kW  |  SoC {soc:.0f}%\n"
                f"Time: {now.strftime('%-I:%M %p')} — check battery mode or inverter."
            )
            _send_alert(body, cfg)
            logger.info("Not-charging alert sent for %s (solar=%.2f kW, batt=%.2f kW)", today, solar_kw, battery_kw)
            state["not_charging_date"] = today
            changed = True

    if changed:
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
        click.echo("  Detecting your Telegram chat ID…", nl=False)
        chat_id = fetch_telegram_chat_id(tg_token)
        if chat_id:
            cfg.telegram_chat_id = chat_id
            click.echo(click.style(f" Found (chat ID: {chat_id})", fg="green"))
            _ok("Telegram alerts configured")
        else:
            click.echo(click.style(" Not found", fg="yellow"))
            _warn("Make sure you sent a message to your bot first, then re-run setup.")
            cfg.telegram_chat_id = click.prompt(
                "  Or enter chat ID manually (or leave blank to skip)",
                default="",
            ).strip()
    else:
        cfg.telegram_bot_token = ""
        cfg.telegram_chat_id   = ""

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
    click.echo("  Battery capacity: check your FranklinWH app or manual (aPower 10=10, aPower 15=15)")
    cfg.battery_capacity_kwh = click.prompt(
        "  Battery usable capacity (kWh)", type=float,
        default=cfg.battery_capacity_kwh,
    )

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

        while True:
            try:
                stats = client.get_stats(gateway)
                history.record(stats)

                outlook        = _fetch_outlook_cached(lat, lon)
                system_peak_kw = _get_system_peak_kw(_load_peak_state(outdir))
                usage_forecast = (
                    predict(history, 12, outlook=outlook, system_peak_kw=system_peak_kw)
                    if history.has_enough_data() else None
                )
                rec = recommend(stats, outlook, usage_forecast)

                _print_recommendation(rec, stats, usage_forecast, cfg.location_name)
                notify_log(rec, log_path)
                _dispatch_notifications(rec, cfg, notify, last_mode, outdir)
                _check_peak_alerts(stats, cfg, outdir, outlook=outlook, usage_forecast=usage_forecast, store=history)

                last_mode = rec.mode.value
                _save_last_mode(outdir, last_mode)

            except Exception as e:
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
