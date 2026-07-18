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
    _fetch_outlook_cached,
    _GHI_CLOUDY_THRESHOLD,
    _load_peak_state,
)
from .advisor import _tou_eb_plan
from .config import load as load_config
from .history import HistoryStore, integrate_intervals
from .predictor import predict
from .tou import (BASE_SERVICE_DAILY, TouPeriod, export_rate_at, on_peak_window,
                  period_at, rate_at)

app = FastAPI(title="FranklinWH Advisor API", docs_url=None, redoc_url=None)

_cfg     = load_config()
_ROOT    = Path(__file__).parent.parent
_OUT     = Path(_cfg.output_dir)
if not _OUT.is_absolute():
    _OUT = _ROOT / _OUT   # server may be launched from any CWD
_BAT_CAP = _cfg.battery_capacity_kwh or 13.6
_POLL_S  = (_cfg.watch_interval or 5) * 60

_CYCLE_START_DAY = 19  # SDG&E billing cycle boundary


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
    """Battery+solar savings today: discharge & peak solar valued at import rate,
    export at NEM3 credit — same integration the ticker in the design expects."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = _readings_since(start)
    saved = 0.0
    for dt0, hours, grid_avg, home_avg, solar_avg in integrate_intervals(
            [(r["timestamp"], r["grid_use_kw"], r["home_load_kw"], r["solar_kw"]) for r in rows]):
        rate = rate_at(dt0)
        export_kw = max(0.0, -grid_avg)
        self_use_kw = max(0.0, min(home_avg, home_avg - max(0.0, grid_avg)))
        saved += self_use_kw * rate * hours + export_kw * export_rate_at(dt0) * hours
    return round(saved, 2)


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
    with HistoryStore(_OUT / "history.db") as history:
        outlook = _fetch_outlook_cached(_cfg.lat, _cfg.lon)
        sp = _get_system_peak_kw(state)
        cloudy = bool(outlook and outlook.avg_ghi(12) < _GHI_CLOUDY_THRESHOLD)
        perf_ratio = _get_performance_ratio(state, cloudy=cloudy)
        hourly_bias = _get_hourly_bias(state)
        fc = (predict(history, 12, outlook=outlook, system_peak_kw=sp,
                      perf_ratio=perf_ratio, hourly_bias=hourly_bias)
              if history.has_enough_data() else None)
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
    }


@app.get("/api/accuracy", dependencies=_authed)
def api_accuracy(days: int = Query(7, ge=1, le=30)):
    state = _load_peak_state(_OUT)
    out = []
    with HistoryStore(_OUT / "history.db") as history:
        for i in range(days, 0, -1):
            d = (date.today() - timedelta(days=i))
            ds = d.isoformat()
            pred = state.get(f"predicted_kwh_{ds}")
            if pred is None:
                continue
            actual = history.daily_solar_kwh_api(ds) or history.daily_solar_kwh(ds)
            if actual <= 0:
                continue
            out.append({"date": ds, "label": d.strftime("J%d").replace("J0", "J"),
                        "predicted": pred, "actual": round(actual, 1)})
    return {"days": out}


@app.get("/api/battery-health", dependencies=_authed)
def api_battery_health(weeks: int = Query(12, ge=1, le=52)):
    """Effective usable battery capacity trend, weekly median, from the same
    clean-discharge-run detection alerts.py's own capacity-fade alert uses
    (history.capacity_samples) — surfaced here for a dashboard trend view
    instead of only ever appearing as a one-off alert message."""
    out = []
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
    return {"weeks": out, "nameplate_kwh": _BAT_CAP}


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
    if d.day >= _CYCLE_START_DAY:
        start = d.replace(day=_CYCLE_START_DAY)
    else:
        prev = (d.replace(day=1) - timedelta(days=1))
        start = prev.replace(day=_CYCLE_START_DAY)
    nxt = (start.replace(day=1) + timedelta(days=32)).replace(day=_CYCLE_START_DAY)
    return start, nxt - timedelta(days=1)


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
    imp = exp_credit = saved = 0.0
    for dt0, hours, grid_avg, home_avg, solar_avg in integrate_intervals(
            [(r["timestamp"], r["grid_use_kw"], r["home_load_kw"], r["solar_kw"]) for r in rows]):
        if dt0 >= end_dt:
            break
        rate = rate_at(dt0)
        imp += max(0.0, grid_avg) * rate * hours
        exp_credit += max(0.0, -grid_avg) * export_rate_at(dt0) * hours
        self_use = max(0.0, home_avg - max(0.0, grid_avg))
        saved += self_use * rate * hours
    days = (min(end, date.today()) - start).days + 1
    return {"import": round(imp, 2), "export": round(exp_credit, 2),
            "base": round(BASE_SERVICE_DAILY * days, 2),
            "net": round(imp - exp_credit + BASE_SERVICE_DAILY * days, 2),
            "saved": round(saved, 2)}


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


app.mount("/", StaticFiles(directory=_ROOT / "static", html=True), name="static")
