"""Walk-forward replay of the solar calibration + prediction pipeline.

Runs the package's REAL code (_calibrate_solar, _calibrate_solar_hourly, the daily
perf-ratio update, predict() with its same-day nowcast, and fetch_solar_outlook's
own parser) over every recorded reading in time order. At any moment it has seen only
readings up to that moment, with weather from Open-Meteo's historical-forecast
archive: what a forecast looked like then, not the weather that actually happened.

Why it exists: it is how the 2026-10-09 solar fix (1-hour radiation shift, derated
model, per-hour sums) and the 2026-10-12 weather blend were measured, and replaying
the old code through it reproduced the live learned state to within ~1%, so its
numbers can be trusted. Run it after any change to weather/predictor/calibration
(`franklinwh solar-backtest`); the advisor also runs it monthly.

The replay swaps `datetime` in alerts/predictor/weather for a clock it controls, so it
must only run in its own process (the CLI does; never call run_replay from the
advisor's process).
"""

import logging
import sqlite3
import statistics
import time
from contextlib import contextmanager
from datetime import datetime as _RealDT
from datetime import timedelta
from pathlib import Path

import requests

from . import alerts, predictor, weather
from .history import HistoryStore

logger = logging.getLogger(__name__)

HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
_VARS = "direct_radiation,diffuse_radiation,cloud_cover,temperature_2m,wind_speed_10m"
_CLOUDY_DAY_KWH = 14.0          # a day that delivered less than this counts as cloudy
_CAP_KWH = 13.6
_FETCH_ATTEMPTS = 3
DEFAULT_SINCE = "2026-07-15"    # the 5-minute polling era; earlier history is too sparse to score


# ── weather ──────────────────────────────────────────────────────────────

def fetch_history_weather(lat: float, lon: float, first: str, last: str, timeout: int = 120) -> dict:
    """Open-Meteo historical-forecast data for [first, last + 2 days] (the replay
    hands the code a 3-day outlook), blended across the same models the live
    forecast uses. Returns {"hourly", "daily", "utc_offset_seconds"}."""
    end = (_RealDT.fromisoformat(last) + timedelta(days=2)).strftime("%Y-%m-%d")
    params = {"latitude": lat, "longitude": lon, "start_date": first, "end_date": end,
              "hourly": _VARS, "daily": "sunrise", "models": ",".join(weather._FORECAST_MODELS),
              "timezone": "auto"}
    # The archive sometimes answers HTTP 200 with a plain-text error ("Unexpected error
    # while streaming data: timeoutReached") instead of JSON; the same request works a
    # moment later, so retry before giving up.
    js, last_exc = None, None
    for attempt in range(_FETCH_ATTEMPTS):
        try:
            resp = requests.get(HISTORICAL_FORECAST_URL, params=params, timeout=timeout)
            resp.raise_for_status()
            js = resp.json()
            break
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            logger.info("weather archive attempt %d/%d failed: %s", attempt + 1, _FETCH_ATTEMPTS, exc)
            if attempt < _FETCH_ATTEMPTS - 1:
                time.sleep(5 * (attempt + 1))
    if js is None:
        raise RuntimeError(f"The weather archive did not return usable data after {_FETCH_ATTEMPTS} tries "
                           f"({last_exc}). Try again in a few minutes.")
    hourly_raw, n = js["hourly"], len(js["hourly"]["time"])
    hourly = {"time": hourly_raw["time"]}
    for var in _VARS.split(","):
        hourly[var] = [v if v is not None else 0.0 for v in weather._blend_models(hourly_raw, var, n)]
    daily_raw = js.get("daily", {})
    sunrise = daily_raw.get("sunrise") or next(
        (daily_raw[f"sunrise_{m}"] for m in weather._FORECAST_MODELS if daily_raw.get(f"sunrise_{m}")), [])
    return {"hourly": hourly, "daily": {"time": daily_raw.get("time", []), "sunrise": sunrise},
            "utc_offset_seconds": js.get("utc_offset_seconds", 0)}


# ── the replay ───────────────────────────────────────────────────────────

@contextmanager
def _frozen_clock(offset_seconds: int):
    """Swap `datetime` in alerts/predictor/weather for one whose now() is set by the
    replay; always restores the real class."""
    offset = timedelta(seconds=offset_seconds)

    class FakeDT(_RealDT):
        _now: _RealDT | None = None

        @classmethod
        def now(cls, tz=None):
            n = cls._now
            if tz is None:
                return cls(n.year, n.month, n.day, n.hour, n.minute, n.second, n.microsecond)
            u = n - offset
            return cls(u.year, u.month, u.day, u.hour, u.minute, u.second, u.microsecond, tzinfo=tz)

    mods = (alerts, predictor, weather)
    saved = [m.datetime for m in mods]
    for m in mods:
        m.datetime = FakeDT
    try:
        yield FakeDT
    finally:
        for m, d in zip(mods, saved):
            m.datetime = d


class _Resp:
    def __init__(self, js):
        self._js = js

    def raise_for_status(self):
        pass

    def json(self):
        return self._js


def run_replay(db_path: Path, wx: dict, first: str, last: str) -> dict:
    """Replay [first, last] over the readings in db_path. Returns
    {"records": [...per-day dicts...], "state": learned state, "final": {...}}."""
    H = wx["hourly"]
    idx = {t: i for i, t in enumerate(H["time"])}
    sunrise_by_day = dict(zip(wx["daily"]["time"], wx["daily"]["sunrise"]))
    store = HistoryStore(Path(db_path))
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT timestamp, solar_kw FROM readings WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
        (first, (_RealDT.fromisoformat(last) + timedelta(days=1)).strftime("%Y-%m-%d"))).fetchall()
    conn.close()
    by_day: dict[str, list] = {}
    for ts, s in rows:
        by_day.setdefault(ts[:10], []).append((ts, s))

    state: dict = {}
    records: list[dict] = []

    def build_outlook(day: str):
        d0 = _RealDT.fromisoformat(day)
        i0 = idx[d0.strftime("%Y-%m-%dT00:00")]
        sl = slice(i0, i0 + 72)
        js = {"hourly": {k: H[k][sl] for k in H},
              "daily": {"sunrise": [sunrise_by_day[(d0 + timedelta(days=n)).strftime("%Y-%m-%d")] for n in range(3)]},
              "utc_offset_seconds": wx["utc_offset_seconds"]}
        if len(js["hourly"]["time"]) != 72:
            raise KeyError(day)
        real_get = weather.requests.get
        weather.requests.get = lambda *a, **k: _Resp(js)
        try:
            return weather.fetch_solar_outlook(0.0, 0.0, retries=1)
        finally:
            weather.requests.get = real_get

    def update_perf_ratio(y: str) -> None:
        """Mirror of the perf-ratio update in alerts._alert_morning_preview."""
        yest_pred = state.get(f"predicted_kwh_{y}", 0.0)
        if yest_pred <= 0:
            return
        yest_actual = store.daily_solar_kwh_api(y) or store.daily_solar_kwh(y)
        if not store.day_has_solar_coverage(y):
            state.pop(f"predicted_cloudy_{y}", None)
            state.pop(f"perf_ratio_used_{y}", None)
            return
        cloudy_day = state.pop(f"predicted_cloudy_{y}",
                               state.get(f"predicted_avg_ghi_{y}", 400.0) < alerts._GHI_CLOUDY_THRESHOLD)
        used = state.pop(f"perf_ratio_used_{y}", 1.0)
        if yest_pred >= (0.5 if cloudy_day else 3.0) and yest_actual >= 0.3:
            ratio = round(yest_actual / yest_pred, 3)
            if ratio >= 0.65:
                bucket = "perf_ratio_cloudy_samples" if cloudy_day else "perf_ratio_samples"
                state[bucket] = (state.get(bucket, []) + [ratio * used])[-30:]
            state[f"daily_pr_{y}"] = ratio

    def hours_for(fc, day: str, after=None) -> dict[int, float]:
        out: dict[int, float] = {}
        for h in fc.hours:
            if h.dt.strftime("%Y-%m-%d") != day:
                continue
            if after is not None and h.dt < after:   # live: `now` is read just BEFORE predict()'s, so entry 0 counts
                continue
            out[h.dt.hour] = out.get(h.dt.hour, 0.0) + h.predicted_solar_kw
        return out

    with _frozen_clock(wx["utc_offset_seconds"]) as clock:
        def snapshot(outlook, day: str, align: bool = False):
            peak = alerts._get_system_peak_kw(state)
            if peak is None:
                return None
            cloudy = outlook.is_cloudy(12, alerts._GHI_CLOUDY_THRESHOLD)
            pr = alerts._get_performance_ratio(state, cloudy=cloudy)
            hb = alerts._get_hourly_bias(state)
            saved = clock._now
            if align:                                   # hour-aligned entries: each is one clock-hour bin
                clock._now = _RealDT.fromisoformat(day + "T06:00:00")
            try:
                fc = predictor.predict(store, 24, outlook=outlook, system_peak_kw=peak,
                                       perf_ratio=pr, hourly_bias=hb)
            finally:
                clock._now = saved
            return {"peak": peak, "pr": pr, "cloudy": cloudy, "fc": fc,
                    "gen": outlook.today_generation_kwh(peak, pr, hb), "avg_ghi": outlook.avg_ghi(12)}

        for day in sorted(by_day):
            try:
                outlook = build_outlook(day)
                sunrise = _RealDT.fromisoformat(sunrise_by_day[day]) + timedelta(minutes=5)
            except KeyError:
                continue
            rec: dict = {"date": day}
            done_am = done_pm = False
            for ts, solar in by_day[day]:
                t = _RealDT.fromisoformat(ts)
                clock._now = t
                alerts._calibrate_solar(state, solar, outlook, t)
                alerts._calibrate_solar_hourly(state, solar, outlook, t)
                if not done_am and t >= sunrise:
                    done_am = True
                    update_perf_ratio((t - timedelta(days=1)).strftime("%Y-%m-%d"))
                    a = snapshot(outlook, day, align=True)
                    if a:
                        state[f"predicted_kwh_{day}"] = round(a["gen"], 1)
                        state[f"predicted_avg_ghi_{day}"] = round(a["avg_ghi"], 1)
                        state[f"predicted_cloudy_{day}"] = a["cloudy"]
                        state[f"perf_ratio_used_{day}"] = a["pr"]
                        rec["A"] = {"gen": a["gen"], "hours": hours_for(a["fc"], day)}
                if not done_pm and t.hour == 15 and t.minute >= 30:
                    done_pm = True
                    b = snapshot(outlook, day)
                    if b:
                        rec["B"] = {"at": ts, "hours": hours_for(b["fc"], day, after=t)}
            hourly: dict[int, list] = {}
            for ts, s in by_day[day]:
                hourly.setdefault(int(ts[11:13]), []).append(max(0.0, s))
            rec["actual"] = {h: sum(v) / len(v) for h, v in hourly.items() if len(v) >= 8}
            rec["cov"] = store.day_has_solar_coverage(day)
            rec["actual_kwh"] = store.daily_solar_kwh_api(day) or store.daily_solar_kwh(day)
            if "B" in rec:
                rec["rem_actual"] = _remaining_actual(by_day[day], rec["B"]["at"])
            records.append(rec)

        final = {
            "pr_sunny": alerts._get_performance_ratio(state, cloudy=False),
            "pr_cloudy": alerts._get_performance_ratio(state, cloudy=True),
            "peak": alerts._get_system_peak_kw(state),
        }
    return {"records": records, "state": state, "final": final}


def _remaining_actual(day_rows: list, after_ts: str) -> float:
    """Actual kWh produced from `after_ts` to the end of the day's readings."""
    rows = [r for r in day_rows if r[0] >= after_ts]
    total = 0.0
    for i, (ts, s) in enumerate(rows):
        nxt = _RealDT.fromisoformat(rows[i + 1][0]) if i + 1 < len(rows) else None
        dt = (nxt - _RealDT.fromisoformat(ts)).total_seconds() / 3600 if nxt else 5 / 60
        total += max(0.0, s) * min(dt, 10 / 60)
    return total


# ── scoring ──────────────────────────────────────────────────────────────

def summarize(records: list[dict], since: str = DEFAULT_SINCE) -> dict | None:
    """Accuracy of the replayed predictions on days since `since` that have full
    polling coverage and both snapshots. None if no day qualifies."""
    days = []
    for r in records:
        if r["date"] < since or not r.get("cov") or "A" not in r or "B" not in r:
            continue
        act = {int(k): v for k, v in r["actual"].items()}
        if not all(h in act for h in range(8, 17)):
            continue
        days.append((r, act))
    if not days:
        return None
    day_err, hour_err, rem_err, cloudy_err = [], [], [], []
    for r, act in days:
        a = {int(k): v for k, v in r["A"]["hours"].items()}
        b = {int(k): v for k, v in r["B"]["hours"].items()}
        err = sum(a.values()) - r["actual_kwh"]
        day_err.append(err)
        if r["actual_kwh"] < _CLOUDY_DAY_KWH:
            cloudy_err.append(abs(err))
        hour_err.extend(abs(a.get(h, 0.0) - act.get(h, 0.0)) for h in range(8, 19))
        rem_err.append(sum(b.values()) - r["rem_actual"])
    n = len(days)
    return {
        "n_days": n, "n_cloudy": len(cloudy_err),
        "day_mae": statistics.mean(abs(e) for e in day_err), "day_bias": statistics.mean(day_err),
        "hour_mae": statistics.mean(hour_err),
        "rem_mae_kwh": statistics.mean(abs(e) for e in rem_err),
        "rem_mae_pts": 100 * statistics.mean(abs(e) for e in rem_err) / _CAP_KWH,
        "rem_bias_kwh": statistics.mean(rem_err),
        "cloudy_mae": statistics.mean(cloudy_err) if cloudy_err else None,
    }


def format_report(s: dict | None, previous: dict | None = None) -> str:
    if s is None:
        return "Solar backtest: no days with full coverage yet."

    def delta(key: str, lower_is_better: bool = True) -> str:
        if not previous or previous.get(key) is None or s.get(key) is None:
            return ""
        d = s[key] - previous[key]
        mark = "↓" if d < 0 else "↑"
        return f"  ({mark}{abs(d):.2f} vs last)" if abs(d) >= 0.005 else "  (unchanged vs last)"

    lines = [
        f"☀️ Solar forecast backtest — {s['n_days']} days replayed",
        f"Daily total:  {s['day_mae']:.2f} kWh avg error (bias {s['day_bias']:+.2f}){delta('day_mae')}",
        f"Hourly:       {s['hour_mae']:.3f} kW avg error{delta('hour_mae')}",
        f"3:30 PM → sundown: {s['rem_mae_pts']:.1f} SoC pts avg error (bias {s['rem_bias_kwh']:+.2f} kWh){delta('rem_mae_pts')}",
    ]
    if s["cloudy_mae"] is not None:
        lines.append(f"Cloudy days:  {s['cloudy_mae']:.2f} kWh avg error over {s['n_cloudy']} day(s){delta('cloudy_mae')}")
    return "\n".join(lines)
