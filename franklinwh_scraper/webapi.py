"""Read-only HTTP API + static dashboard server for the advisor.

Serves the live dashboard (static/) and JSON endpoints that read the same
files/DB the advisor writes — it never talks to the FranklinWH cloud itself,
so it can run alongside the watch loop with zero extra API load.

Local-only by design: the shipped LaunchAgent binds --host 127.0.0.1, and
that's the recommended binding — the /api/* routes have no auth beyond the
optional dashboard_token below, so don't expose this port past localhost
without setting one.

Run:  python3.13 -m uvicorn franklinwh_scraper.webapi:app --host 127.0.0.1 --port 8093
"""

from __future__ import annotations

import hmac
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles

from .alerts import (
    _get_hourly_bias,
    _get_performance_ratio,
    _get_system_peak_kw,
    _GHI_CLOUDY_THRESHOLD,
    _load_peak_state,
    _next_sunrise_after,
    _predict_overnight_soc_flat,
)
from .advisor import _tou_eb_plan
from .config import load as load_config
from .format_utils import time_to_pct
from .history import HistoryStore, integrate_intervals
from .predictor import predict
from . import savings
from .tou import (BASE_SERVICE_DAILY, TouPeriod, cycle_bounds, export_rate_at,
                  on_peak_window, period_at, rate_at)
from .weather import fetch_solar_outlook_cached as _fetch_outlook_cached

app = FastAPI(title="FranklinWH Advisor API", docs_url=None, redoc_url=None)

_cfg     = load_config()
_ROOT    = Path(__file__).parent.parent
# static/ now lives inside the package so an installed wheel can actually
# serve it (setuptools package-data can't reach a top-level directory).
# The _ROOT fallback keeps a pre-move dev checkout working. _ROOT itself
# stays as-is — _OUT's relative-path resolution below depends on it.
_STATIC  = Path(__file__).parent / "static"
if not (_STATIC / "index.html").exists():
    _STATIC = _ROOT / "static"
_OUT     = Path(_cfg.output_dir)
if not _OUT.is_absolute():
    _OUT = _ROOT / _OUT   # server may be launched from any CWD
_BAT_CAP = _cfg.battery_capacity_kwh or 13.6
_POLL_S  = (_cfg.watch_interval or 5) * 60

# Billing-cycle boundary now comes from cfg.billing_cycle_start_day via
# tou.cycle_bounds — see _cycle_bounds below.


def _require_token(request: Request) -> None:
    """Opt-in auth: no-op unless cfg.dashboard_token is set, so this doesn't
    break the default unauthenticated-but-127.0.0.1-only setup. Set the
    token in ~/.franklinwh.json if the dashboard is ever reachable beyond
    localhost (port-forwarded, shared network, etc.)."""
    if not _cfg.dashboard_token:
        return
    supplied = request.headers.get("X-Dashboard-Token", "")
    if not hmac.compare_digest(supplied, _cfg.dashboard_token):
        raise HTTPException(status_code=401, detail="Missing or invalid dashboard token")


_authed = [Depends(_require_token)]


def _db(path: Path) -> sqlite3.Connection:
    # busy_timeout lets a read collide with the CLI's writer commit and just
    # wait it out (paired with WAL mode set by the writer in history.py)
    # instead of raising "database is locked" immediately.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _latest_reading() -> dict | None:
    conn = None
    try:
        conn = _db(_OUT / "history.db")
        r = conn.execute(
            "SELECT timestamp, home_load_kw, solar_kw, battery_soc, grid_use_kw,"
            "       grid_status, battery_use_kw, solar_total_kwh"
            " FROM readings ORDER BY timestamp DESC LIMIT 1").fetchone()
        return dict(r) if r else None
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            conn.close()


def _readings_since(since: datetime, until: datetime | None = None) -> list[sqlite3.Row]:
    conn = None
    try:
        conn = _db(_OUT / "history.db")
        if until is not None:
            return conn.execute(
                "SELECT timestamp, grid_use_kw, home_load_kw, solar_kw, battery_soc,"
                "       battery_use_kw"
                " FROM readings WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
                (since.isoformat(), until.isoformat())).fetchall()
        return conn.execute(
            "SELECT timestamp, grid_use_kw, home_load_kw, solar_kw, battery_soc,"
            "       battery_use_kw"
            " FROM readings WHERE timestamp >= ? ORDER BY timestamp",
            (since.isoformat(),)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def _saved_today(now: datetime) -> float:
    """Battery+solar savings today. Delegates to savings.compute so the
    dashboard ticker, the billing-cycle card, and the weekly digest can't
    drift apart again — this was the only one of the three that was right."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = _readings_since(start)
    return savings.compute(_intervals(rows)).saved_vs_grid_only


def _intervals(rows):
    return integrate_intervals(
        [(r["timestamp"], r["grid_use_kw"], r["home_load_kw"], r["solar_kw"]) for r in rows]
    )


@app.get("/api/current", dependencies=_authed)
def api_current():
    r = _latest_reading()
    now = datetime.now()
    if not r:
        return {"ok": False, "error": "no readings yet"}
    last = datetime.fromisoformat(r["timestamp"])
    return {
        "ok": True,
        "ts": r["timestamp"],
        "soc_pct": r["battery_soc"],
        "solar_kw": r["solar_kw"],
        "home_load_kw": r["home_load_kw"],
        "grid_kw": r["grid_use_kw"],            # + import / − export
        "battery_kw": r["battery_use_kw"],      # − charging / + discharging
        "grid_status": r["grid_status"],
        "solar_today_kwh": r["solar_total_kwh"],
        "battery_capacity_kwh": _BAT_CAP,
        "last_poll": r["timestamp"],
        "next_poll": (last + timedelta(seconds=_POLL_S)).isoformat(),
        "poll_seconds": _POLL_S,
        "saved_today": _saved_today(now),
        "on_peak": period_at(now) == TouPeriod.ON_PEAK,
    }


@app.get("/api/recommendation", dependencies=_authed)
def api_recommendation():
    rec = {}
    try:
        with open(_OUT / "advisor_log.jsonl", "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            lines = f.read().decode(errors="replace").strip().splitlines()
            rec = json.loads(lines[-1]) if lines else {}
    except (OSError, json.JSONDecodeError, IndexError):
        pass
    # Live EB plan from the same inputs the advisor uses
    plan = None
    try:
        now = datetime.now()
        state = _load_peak_state(_OUT)
        r = _latest_reading()
        with HistoryStore(_OUT / "history.db") as history:
            outlook = _fetch_outlook_cached(_cfg.lat, _cfg.lon)
            sp = _get_system_peak_kw(state)
            cloudy = bool(outlook and outlook.avg_ghi(12) < _GHI_CLOUDY_THRESHOLD)
            fc = (predict(history, 24, outlook=outlook, system_peak_kw=sp,
                          perf_ratio=_get_performance_ratio(state, cloudy=cloudy),
                          hourly_bias=_get_hourly_bias(state))
                  if history.has_enough_data() else None)
            p = _tou_eb_plan(now, r["battery_soc"] if r else 50.0, _BAT_CAP, fc)
            plan = {k: (v.isoformat() if isinstance(v, datetime) else v)
                    for k, v in p.items()}
    except Exception:
        plan = None
    return {"recommendation": rec, "eb_plan": plan}


@app.get("/api/alerts", dependencies=_authed)
def api_alerts(limit: int = Query(20, ge=1, le=100)):
    out = []
    try:
        lines = (_OUT / "alerts_log.jsonl").read_text().strip().splitlines()
        for ln in lines[-limit:]:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return {"alerts": list(reversed(out))}


@app.get("/api/history", dependencies=_authed)
def api_history(hours: int = Query(48, ge=1, le=168)):
    rows = _readings_since(datetime.now() - timedelta(hours=hours))
    return {"samples": [
        {"ts": r["timestamp"], "soc": r["battery_soc"],
         "solar_kw": r["solar_kw"], "load_kw": r["home_load_kw"]}
        for r in rows
    ]}


@app.get("/api/forecast", dependencies=_authed)
def api_forecast():
    now = datetime.now()
    state = _load_peak_state(_OUT)
    r = _latest_reading()
    soc = r["battery_soc"] if r else 50.0
    hours_out = []
    cloudy = False
    perf_ratio = 1.0
    sp = None
    fc = None
    error = False
    try:
        with HistoryStore(_OUT / "history.db") as history:
            outlook = _fetch_outlook_cached(_cfg.lat, _cfg.lon)
            sp = _get_system_peak_kw(state)
            cloudy = bool(outlook and outlook.avg_ghi(12) < _GHI_CLOUDY_THRESHOLD)
            perf_ratio = _get_performance_ratio(state, cloudy=cloudy)
            hourly_bias = _get_hourly_bias(state)
            fc = (predict(history, 12, outlook=outlook, system_peak_kw=sp,
                          perf_ratio=perf_ratio, hourly_bias=hourly_bias)
                  if history.has_enough_data() else None)
    except Exception:
        # Degrade to an empty-but-valid forecast instead of a 500 — one bad
        # cycle (e.g. a transient weather-API hiccup) shouldn't blank the
        # panel; the frontend's existing "not enough data" empty state
        # already handles hours=[] gracefully.
        fc = None
        error = True
    kwh = soc / 100 * _BAT_CAP
    if fc:
        for h in fc.hours:
            kwh = max(0.0, min(_BAT_CAP, kwh + h.net_kw))
            period = period_at(h.dt)
            hours_out.append({
                "ts": h.dt.isoformat(),
                "label": h.dt.strftime("%I%p").lstrip("0").replace("M", ""),
                "solar_kw": max(0.0, h.predicted_solar_kw),
                "load_kw": h.predicted_load_kw,
                "soc": round(kwh / _BAT_CAP * 100),
                "period": period.value.replace("_", " ").upper(),
                "rate": rate_at(h.dt),
                "on_peak": period == TouPeriod.ON_PEAK,
            })
    return {
        "hours": hours_out, "battery_capacity_kwh": _BAT_CAP,
        "current_soc": soc, "confidence": fc.confidence if fc else "none",
        # "Forecast health" — these were already computed above and thrown
        # away; surfacing them explains *why* today's forecast looks the
        # way it does (learned system efficiency, cloud-day derate, and
        # calibrated peak-kW estimate) instead of being a black box.
        "perf_ratio": round(perf_ratio, 3),
        "cloudy_today": cloudy,
        "system_peak_kw": round(sp, 2) if sp is not None else None,
        "error": error,
    }


@app.get("/api/accuracy", dependencies=_authed)
def api_accuracy(days: int = Query(7, ge=1, le=30)):
    state = _load_peak_state(_OUT)
    out = []
    try:
        with HistoryStore(_OUT / "history.db") as history:
            for i in range(days, 0, -1):
                d = (date.today() - timedelta(days=i))
                ds = d.isoformat()
                pred = state.get(f"predicted_kwh_{ds}")
                if not pred:  # skips both missing and a legitimately-zero forecast
                    continue
                actual = history.daily_solar_kwh_api(ds) or history.daily_solar_kwh(ds)
                if actual <= 0:
                    continue
                # Was strftime("J%d") — "J" there is a literal character, not
                # a month directive, so every date showed the same "J23"-style
                # label regardless of month; a 30-day lookback spanning a
                # month boundary showed two different dates identically.
                out.append({"date": ds, "label": d.strftime("%b %-d"),
                            "predicted": pred, "actual": round(actual, 1)})
        return {"days": out, "error": False}
    except Exception:
        return {"days": [], "error": True}


@app.get("/api/battery-health", dependencies=_authed)
def api_battery_health(weeks: int = Query(12, ge=1, le=52)):
    """Effective usable battery capacity trend, weekly median, from the same
    clean-discharge-run detection alerts.py's own capacity-fade alert uses
    (history.capacity_samples) — surfaced here for a dashboard trend view
    instead of only ever appearing as a one-off alert message."""
    out = []
    try:
        with HistoryStore(_OUT / "history.db") as history:
            today = date.today()
            for i in range(weeks, 0, -1):
                week_start = today - timedelta(days=7 * i)
                week_end = week_start + timedelta(days=6)
                samples = history.capacity_samples(week_start.isoformat(), week_end.isoformat())
                if not samples:
                    continue
                samples.sort()
                median = samples[len(samples) // 2]
                out.append({
                    "week_start": week_start.isoformat(),
                    "label": week_start.strftime("%b %-d"),
                    "capacity_kwh": round(median, 1),
                    "pct_of_nameplate": round(median / _BAT_CAP * 100, 1),
                    "samples": len(samples),
                })
        return {"weeks": out, "nameplate_kwh": _BAT_CAP, "error": False}
    except Exception:
        return {"weeks": [], "nameplate_kwh": _BAT_CAP, "error": True}


@app.get("/api/ev", dependencies=_authed)
def api_ev():
    """EV closed-loop controller status plus tonight's without/with-EV SoC
    prediction — the dashboard equivalent of the evening digest's second
    line, computed live rather than waiting for the 9-10pm alert to fire.
    """
    out = {
        "ev_charging": bool(getattr(_cfg, "ev_charging", False)),
        "control_enabled": bool(getattr(_cfg, "ev_control_enabled", False)),
        "error": False,
    }
    if not out["ev_charging"]:
        return out

    if out["control_enabled"]:
        try:
            ctl_state = json.loads((_OUT / ".ev_controller_state.json").read_text())
        except (OSError, json.JSONDecodeError):
            ctl_state = {}
        month = datetime.now().strftime("%Y-%m")
        counts = ctl_state.get("spend", {}).get(month, {})
        spend = (counts.get("data", 0) * 0.002 + counts.get("cmd", 0) * 0.001
                 + counts.get("wake", 0) * 0.02)
        recent = []
        log_path = _OUT / "ev_controller.jsonl"
        if log_path.exists():
            try:
                for line in log_path.read_text().splitlines()[-5:]:
                    recent.append(json.loads(line))
            except (OSError, json.JSONDecodeError):
                pass
        out["controller"] = {
            "dry_run": bool(_cfg.ev_dry_run),
            "session": ctl_state.get("session", "none"),
            "commanded_amps": ctl_state.get("last_commanded_amps"),
            "override_until": ctl_state.get("override_until_iso"),
            "consec_errors": ctl_state.get("consec_tesla_errors", 0),
            "last_error": ctl_state.get("last_error"),
            "calibration_samples": len(ctl_state.get("ev_draw_samples", [])),
            "spend_month_usd": round(spend, 2),
            "recent_decisions": recent,
        }

    r = _latest_reading()
    out["prediction"] = None
    if r is not None:
        try:
            peak_state = _load_peak_state(_OUT)
            with HistoryStore(_OUT / "history.db") as history:
                outlook = _fetch_outlook_cached(_cfg.lat, _cfg.lon)
                if history.has_enough_data():
                    now = datetime.now()
                    soc = r["battery_soc"]
                    # Flat assumed-baseline walk to the next sunrise (DST-
                    # proof, not a fixed clock hour), not the percentile/
                    # ground-truth forecast — mirrors alerts.py's
                    # _alert_eod_digest (see Config.no_ev_baseline_load_kw).
                    checkpoint = _next_sunrise_after(now, outlook)
                    without_ov = _predict_overnight_soc_flat(
                        now, soc, _BAT_CAP, getattr(_cfg, "no_ev_baseline_load_kw", 0.4),
                        checkpoint,
                    )
                    # Charge-to-floor, not fixed-kW-all-night — same model
                    # and rationale as alerts.py's _alert_eod_digest.
                    floor = getattr(_cfg, "ev_charge_floor_soc", 10.0)
                    with_ev_pct = None
                    if without_ov is not None:
                        with_ev_pct = floor if without_ov[0] > floor else without_ov[0]
                    out["prediction"] = {
                        "without_ev_pct": without_ov[0] if without_ov else None,
                        "with_ev_pct": with_ev_pct,
                        "hour_label": without_ov[1] if without_ov else None,
                        "ev_charge_floor_soc": floor,
                    }
        except Exception:
            out["prediction"] = None
    return out


@app.get("/api/until", dependencies=_authed)
def api_until(target: float = Query(..., ge=0, le=100)):
    """Dashboard equivalent of the chatbot's /until N — hours to reach a
    target SoC at the current instantaneous charge/discharge rate."""
    r = _latest_reading()
    if not r:
        return {"ok": False, "error": "no readings yet"}
    hours = time_to_pct(r["battery_soc"], target, _BAT_CAP, r["battery_use_kw"])
    if hours is None:
        idle = abs(r["battery_use_kw"]) < 0.1
        return {"ok": False, "error": "idle" if idle else "wrong_direction",
                "current_soc": r["battery_soc"]}
    eta = datetime.now() + timedelta(hours=hours)
    return {"ok": True, "current_soc": r["battery_soc"], "target": target,
            "hours": round(hours, 1), "eta": eta.isoformat(),
            "battery_kw": r["battery_use_kw"]}


@app.get("/api/willmake", dependencies=_authed)
def api_willmake(hours: int = Query(..., ge=1, le=24)):
    """Dashboard equivalent of the chatbot's /willmake H — projects SoC
    forward using the solar+load forecast (not just the current rate) and
    reports whether the battery stays above 0% without grid import."""
    now = datetime.now()
    r = _latest_reading()
    if not r:
        return {"ok": False, "error": "no readings yet"}
    soc = r["battery_soc"]
    state = _load_peak_state(_OUT)
    try:
        with HistoryStore(_OUT / "history.db") as history:
            outlook = _fetch_outlook_cached(_cfg.lat, _cfg.lon)
            sp = _get_system_peak_kw(state)
            cloudy = bool(outlook and outlook.avg_ghi(12) < _GHI_CLOUDY_THRESHOLD)
            fc = (predict(history, 24, outlook=outlook, system_peak_kw=sp,
                          perf_ratio=_get_performance_ratio(state, cloudy=cloudy),
                          hourly_bias=_get_hourly_bias(state))
                  if history.has_enough_data() else None)
    except Exception:
        return {"ok": False, "error": "forecast_unavailable"}
    if fc is None or fc.confidence == "none":
        return {"ok": False, "error": "insufficient_forecast"}

    kwh = soc / 100.0 * _BAT_CAP
    horizon = now + timedelta(hours=hours)
    min_soc, min_at = soc, now
    for h in fc.hours:
        if h.dt <= now or h.dt > horizon:
            continue
        kwh = max(0.0, min(_BAT_CAP, kwh + h.predicted_solar_kw - h.predicted_load_kw))
        pct = kwh / _BAT_CAP * 100.0
        if pct < min_soc:
            min_soc, min_at = pct, h.dt
    end_pct = kwh / _BAT_CAP * 100.0
    return {
        "ok": True, "will_make_it": min_soc > 0.0, "current_soc": soc,
        "min_soc": round(min_soc, 1), "min_at": min_at.isoformat(),
        "end_soc": round(end_pct, 1), "horizon": horizon.isoformat(),
        "confidence": fc.confidence, "data_days": fc.data_days,
    }


@app.get("/api/tou", dependencies=_authed)
def api_tou():
    now = datetime.now()
    base = now.replace(minute=0, second=0, microsecond=0)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ring = []
    for h in range(24):
        dt = today + timedelta(hours=h)
        ring.append({"hour": h, "period": period_at(dt).value.replace("_", " ").upper(),
                     "rate": rate_at(dt)})
    ps, pe = on_peak_window(now)
    return {
        "now": now.isoformat(),
        "period": period_at(now).value.replace("_", " ").upper(),
        "rate": rate_at(now),
        "export_rate": export_rate_at(now),
        "base_daily": BASE_SERVICE_DAILY,
        "on_peak_start": ps.isoformat(), "on_peak_end": pe.isoformat(),
        "ring": ring,
    }


def _cycle_bounds(d: date) -> tuple[date, date]:
    """Thin shim over tou.cycle_bounds so the dashboard, the digests, and the
    chatbot all derive the billing cycle from one implementation. They used to
    disagree — this endpoint said day 19, the digests said 20, and the chatbot
    computed a window a full month stale."""
    return cycle_bounds(d, _cfg.billing_cycle_start_day)


def _cycle_cost(start: date, end: date) -> dict:
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time())
    # Bound the query to this cycle (+1h slack so the trapezoidal integrator
    # below still has the one reading just past end_dt it needs to correctly
    # close out the last interval). Without the upper bound, the prior-cycle
    # lookup in api_bill() was fetching the *entire* current cycle too and
    # discarding most of it in Python after fetchall() — worse every month
    # as history.db grows.
    rows = _readings_since(datetime.combine(start, datetime.min.time()),
                           until=end_dt + timedelta(hours=1))
    # Trim to the cycle before costing — integrate_intervals needs the one
    # reading past end_dt to close the final interval, but that interval
    # itself belongs to the next cycle.
    bounded = [iv for iv in _intervals(rows) if iv[0] < end_dt]
    sv = savings.compute(bounded)
    days = (min(end, date.today()) - start).days + 1
    base = BASE_SERVICE_DAILY * days
    return {"import": sv.actual_import_cost, "export": sv.actual_export_credit,
            "base": round(base, 2),
            "net": round(sv.actual_net_energy_cost + base, 2),
            # Was self-use only; export credit is part of what the system
            # saved you, and the dashboard ticker has always counted it.
            "saved": sv.saved_vs_grid_only}


@app.get("/api/bill", dependencies=_authed)
def api_bill():
    today = date.today()
    start, end = _cycle_bounds(today)
    prior_start, prior_end = _cycle_bounds(start - timedelta(days=1))
    cur = _cycle_cost(start, end)
    prior = _cycle_cost(prior_start, prior_end)
    day_n = (today - start).days + 1
    total_days = (end - start).days + 1
    projected = round(cur["net"] / max(1, day_n) * total_days, 2)
    return {
        "cycle_start": start.isoformat(), "cycle_end": end.isoformat(),
        "day": day_n, "days": total_days,
        "net_mtd": cur["net"], "projected": projected,
        "prior_net": prior["net"], "saved_mtd": cur["saved"],
    }


@app.get("/api/auth-status")
def api_auth_status():
    """Unauthenticated on purpose — tells the frontend whether it needs to
    prompt for a token, without exposing the token itself."""
    return {"required": bool(_cfg.dashboard_token)}


@app.get("/api/attribution", dependencies=_authed)
def api_attribution(days: int = Query(14, ge=1, le=90)):
    """Where each day's home load actually came from.

    Paths, not sources: solar_kwh here is *direct* solar→home. Solar that
    charged the battery and served load after sunset is counted under
    battery_kwh — the gateway meters the path, not the ultimate origin.
    """
    out = []
    try:
        with HistoryStore(_OUT / "history.db") as history:
            today = date.today()
            for i in range(days - 1, -1, -1):
                d = today - timedelta(days=i)
                attr = history.daily_attribution(d.isoformat())
                if not attr or sum(attr) <= 0:
                    continue
                batt, sol, grid = attr
                total = batt + sol + grid
                out.append({
                    "date": d.isoformat(),
                    "label": d.strftime("%b %-d"),
                    "battery_kwh": batt,
                    "solar_kwh": sol,
                    "grid_kwh": grid,
                    "total_kwh": round(total, 2),
                    "self_sufficiency_pct": round((batt + sol) / total * 100, 1),
                })
        return {"days": out, "error": False}
    except Exception:
        return {"days": [], "error": True}


@app.get("/api/savings", dependencies=_authed)
def api_savings(days: int = Query(30, ge=1, le=365)):
    """What the battery + solar have actually saved over a trailing window.

    Only the requested window is computed from the DB; the running lifetime
    total is served from the alert state file, where the EOD digest
    accumulates it one day at a time. A full-history rescan would be both
    slow and *not reproducible* — rollup_old_readings downsamples data past
    180 days, so a lifetime figure recomputed from scratch would drift
    downward as history ages out.
    """
    try:
        since = datetime.now() - timedelta(days=days)
        rows = _readings_since(since)
        sv = savings.compute(_intervals(rows))
        out = sv.to_dict()
        state = _load_peak_state(_OUT)
        cum = state.get("savings_cumulative")
        out["cumulative"] = cum if isinstance(cum, dict) else None
        out["error"] = False
        return out
    except Exception:
        return {"error": True}


@app.get("/api/health", dependencies=_authed)
def api_health():
    """FranklinWH API connection health — the watch loop's own consecutive-
    error tracking, written to .health.json each cycle. Surfaces the same
    outage signal the CLI already sends over Telegram, without needing to
    check advisor.log."""
    try:
        data = json.loads((_OUT / ".health.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {"consec_errors": 0, "last_error": None, "updated": None}
    return data


app.mount("/", StaticFiles(directory=_STATIC, html=True), name="static")
