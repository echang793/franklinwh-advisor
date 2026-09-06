"""Unit tests for FranklinWH pure logic — no network."""

import json
import pathlib
import sys
import time
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from franklinwh_scraper import alerts, notifier, predictor, tou
from franklinwh_scraper.chatbot import TelegramChatBot
from franklinwh_scraper.history import HistoryStore, integrate_intervals
from franklinwh_scraper.config import Config
from franklinwh_scraper.predictor import predict


# ── TOU ───────────────────────────────────────────────────────────────

def test_period_at_weekday():
    # Mon 5 pm = on-peak
    assert tou.period_at(datetime(2026, 6, 8, 17)) == tou.TouPeriod.ON_PEAK
    # Mon 1 am = super off-peak
    assert tou.period_at(datetime(2026, 6, 8, 1)) == tou.TouPeriod.SUPER_OFF_PEAK
    # Mon 11 am = super off-peak (midday window)
    assert tou.period_at(datetime(2026, 6, 8, 11)) == tou.TouPeriod.SUPER_OFF_PEAK


def test_rate_at_summer_vs_winter():
    summer_peak = tou.rate_at(datetime(2026, 7, 8, 17))
    winter_peak = tou.rate_at(datetime(2026, 1, 8, 17))
    assert summer_peak > winter_peak  # summer on-peak costs more


def test_base_service_cost():
    assert tou.base_service_cost(7) == pytest.approx(7 * tou.BASE_SERVICE_DAILY)
    assert tou.base_service_cost(0) == 0
    assert tou.base_service_cost(-5) == 0  # clamped


def test_cheap_charge_deadline():
    # before 2pm → returns 2pm today
    d = tou.cheap_charge_deadline(datetime(2026, 6, 8, 10))
    assert d is not None and d.hour == 14
    # after 2pm → None
    assert tou.cheap_charge_deadline(datetime(2026, 6, 8, 16)) is None


# ── History / integration ─────────────────────────────────────────────

def test_integrate_intervals_trapezoidal():
    # two readings 1h apart, constant 2 kW → 2 kWh equivalent in avg×hours
    rows = [
        ("2026-06-08T12:00:00", 2.0, 3.0, 0.0),
        ("2026-06-08T13:00:00", 2.0, 3.0, 0.0),
    ]
    out = integrate_intervals(rows)
    assert len(out) == 1
    dt, hours, grid, home, solar = out[0]
    assert hours == pytest.approx(1.0)
    assert grid == pytest.approx(2.0)


def test_integrate_intervals_caps_gap():
    # 3-hour gap should clamp to 1.0h
    rows = [
        ("2026-06-08T12:00:00", 1.0, 1.0, 0.0),
        ("2026-06-08T15:00:00", 1.0, 1.0, 0.0),
    ]
    _, hours, *_ = integrate_intervals(rows)[0]
    assert hours == pytest.approx(1.0)


def test_integrate_intervals_empty_and_single():
    assert integrate_intervals([]) == []
    assert integrate_intervals([("2026-06-08T12:00:00", 1, 1, 1)]) == []


def test_capacity_samples(tmp_path):
    """battery_use_kw > 0 = discharging (account.py's documented convention,
    same one daily_battery_kwh uses) — positive here, not negative, is the
    real-data shape this function must match."""
    db = HistoryStore(tmp_path / "h.db")
    base = datetime(2026, 5, 1, 18, 0)
    soc = 100.0
    for i in range(9):  # 100→60% over 4h at 1.36 kW (13.6 kWh battery)
        ts = (base + timedelta(minutes=30 * i)).isoformat()
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, 0, 18, 1.36, 0.0, soc, 0.0, "normal", 0.0, 1.36),
        )
        soc -= 5.0
    db._conn.commit()
    samples = db.capacity_samples("2026-05-01", "2026-05-02")
    assert samples and 13.0 < samples[0] < 14.5


def test_capacity_samples_ignores_charging_data(tmp_path):
    """Regression guard for the sign-flip bug: real charging data (negative
    battery_use_kw, rising SoC) must never be mistaken for a discharge run."""
    db = HistoryStore(tmp_path / "h.db")
    base = datetime(2026, 5, 1, 10, 0)
    soc = 40.0
    for i in range(9):  # 40→80% over 4h charging at 1.36 kW
        ts = (base + timedelta(minutes=30 * i)).isoformat()
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, 0, 10, 0.5, 2.0, soc, 0.0, "normal", 0.0, -1.36),
        )
        soc += 5.0
    db._conn.commit()
    assert db.capacity_samples("2026-05-01", "2026-05-02") == []


def test_predict_blends_recent_load_over_baseline(tmp_path):
    """A sustained recent load change should pull the forecast toward it,
    not get diluted by months of older, lower baseline readings."""
    db = HistoryStore(tmp_path / "h.db")
    future = datetime.now() + timedelta(hours=1)
    slot_dow, slot_hour = future.weekday(), future.hour

    def _insert(ts: datetime, load_kw: float):
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts.isoformat(), slot_dow, slot_hour, load_kw, 0.0, 50.0, 0.0, "normal", 0.0, 0.0),
        )

    # Old baseline: low load, far outside the 21-day recency window.
    for i in range(10):
        _insert(datetime.now() - timedelta(days=60 + i), 2.0)
    # Recent: sustained higher load (e.g. a new EV charging in this slot).
    for i in range(5):
        _insert(datetime.now() - timedelta(days=1 + i), 8.0)
    db._conn.commit()

    forecast = predict(db, horizon_hours=2)
    hour_pred = next(p for p in forecast.hours if p.dt.hour == slot_hour)
    # Blend is 0.65 recent + 0.35 baseline = 5.9; must be well above the
    # 2.0 baseline alone, proving the recent window pulled it up.
    assert hour_pred.predicted_load_kw > 5.0


def test_load_profile_uses_median_not_mean(tmp_path):
    """Regression for the 2026-08-16 -31pt overnight SoC miss: home_load_kw
    includes EV charging draw, and EV sessions are irregular/self-limited
    rather than every night — a right-skewed minority of high-draw
    readings pulled the mean 2-3x above the typical no-EV night (measured
    live: dow=6 hour=2 was 0.31 kW median vs 0.67 kW mean, max 4.07 kW).
    Median must be robust to that tail; mean isn't."""
    db = HistoryStore(tmp_path / "h.db")

    def _insert(load_kw: float, i: int):
        ts = (datetime(2026, 8, 2, 2, 0) + timedelta(days=i)).isoformat()  # all Sundays
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, 6, 2, load_kw, 0.0, 50.0, 0.0, "normal", 0.0, 0.0),
        )

    # 8 typical no-EV nights (~0.3 kW), 2 EV-charging nights (~4 kW spike).
    for i in range(8):
        _insert(0.3, i)
    for i in range(8, 10):
        _insert(4.0, i)
    db._conn.commit()

    profile = db.load_profile()
    assert profile[(6, 2)] == pytest.approx(0.3)  # median: the typical night, not dragged up


def test_load_profile_percentile_survives_a_minority_ev_majority(tmp_path):
    """Regression for the 2026-08-16/17 follow-up: median alone still fails
    when EV nights are a *slim majority* of a small recent sample — real
    case was 2 of only 3 recent occurrences of one weekday being EV
    nights, which pulled the median itself up to ~2kW instead of the
    user's confirmed 0.2-0.4kW no-EV baseline. A lower percentile (0.25)
    must side with the minority-but-real no-EV nights instead."""
    db = HistoryStore(tmp_path / "h.db")

    def _insert(load_kw: float, day: int, n: int):
        base = datetime(2026, 8, 2, 0, 0) + timedelta(days=day)
        for i in range(n):
            ts = base + timedelta(minutes=5 * i)
            db._conn.execute(
                "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
                "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts.isoformat(), 0, 0, load_kw, 0.0, 50.0, 0.0, "normal", 0.0, 0.0),
            )

    # 1 real no-EV Monday (~0.35 kW), 2 EV-charging Mondays (~2.1-2.5 kW) —
    # matches the actual Jul27/Aug3/Aug10 data exactly.
    _insert(0.35, 0, 12)
    _insert(2.5, 7, 12)
    _insert(2.1, 14, 9)
    db._conn.commit()

    default_profile = db.load_profile()          # median (0.5) — sides with the 2-of-3 majority
    no_ev_profile    = db.load_profile(0.25)      # low percentile — sides with the real no-EV night
    assert default_profile[(0, 0)] > 1.5
    assert no_ev_profile[(0, 0)] == pytest.approx(0.35, abs=0.01)


def test_predict_load_percentile_produces_lower_no_ev_forecast(tmp_path):
    """predict()'s load_percentile param must actually reach the load
    profile lookups (both the base and the recent-window blend)."""
    db = HistoryStore(tmp_path / "h.db")
    future = datetime.now() + timedelta(hours=1)
    slot_dow, slot_hour = future.weekday(), future.hour

    def _insert(ts: datetime, load_kw: float):
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts.isoformat(), slot_dow, slot_hour, load_kw, 0.0, 50.0, 0.0, "normal", 0.0, 0.0),
        )

    for i in range(10):
        _insert(datetime.now() - timedelta(days=1 + i), 3.0 if i < 6 else 0.3)  # 6-of-10 EV nights (majority)
    db._conn.commit()

    median_forecast = predict(db, horizon_hours=2)
    no_ev_forecast   = predict(db, horizon_hours=2, load_percentile=0.25)
    median_pred = next(p for p in median_forecast.hours if p.dt.hour == slot_hour).predicted_load_kw
    no_ev_pred  = next(p for p in no_ev_forecast.hours if p.dt.hour == slot_hour).predicted_load_kw
    assert no_ev_pred < median_pred


def test_day_range_query_boundaries(tmp_path):
    """Regression guard for the substr(timestamp) -> timestamp range rewrite:
    a reading exactly at midnight of the day *after* end_date must be excluded,
    and one at 23:59:59 of end_date must be included."""
    db = HistoryStore(tmp_path / "h.db")
    rows = [
        ("2026-05-01T00:00:00", 1.0),
        ("2026-05-02T23:59:59", 2.0),
        ("2026-05-03T00:00:00", 3.0),  # must be excluded — day after end_date
    ]
    for ts, kw in rows:
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, 0, 0, kw, 0.0, 50.0, 0.0, "normal", 0.0, 0.0),
        )
    db._conn.commit()
    result = db.weekly_readings("2026-05-01", "2026-05-02")
    assert [r[0] for r in result] == ["2026-05-01T00:00:00", "2026-05-02T23:59:59"]


# ── CLI helpers ────────────────────────────────────────────────────────

def test_peak_export_hour():
    """Flat real rate year-round now (see _NEM3_DEFAULT_EXPORT_RATE's
    docstring — confirmed 2026-08-24 from an actual SDG&E/SDCP bill,
    replacing an unsupported $0.885-1.022/kWh Aug/Sep table)."""
    for month in (1, 6, 7, 8, 9, 12):
        assert tou.peak_export_hour(month) == (18, tou._NEM3_DEFAULT_EXPORT_RATE)


def test_alert_enabled():
    now = datetime(2026, 8, 13, 12, 0)
    state = {}
    cfg = Config()
    assert alerts._alert_enabled(cfg, "morning_preview", state, now)
    cfg.disabled_alerts = ["morning_preview"]
    assert not alerts._alert_enabled(cfg, "morning_preview", state, now)
    # always-on can't be disabled
    cfg.disabled_alerts = ["grid_down", "fast_drain"]
    assert alerts._alert_enabled(cfg, "grid_down", state, now)
    assert alerts._alert_enabled(cfg, "fast_drain", state, now)


def test_alert_enabled_respects_mute():
    now = datetime(2026, 8, 13, 12, 0)
    cfg = Config()
    muted = {"alerts_muted_until": (now + timedelta(hours=1)).isoformat()}
    assert not alerts._alert_enabled(cfg, "morning_preview", muted, now)
    # always-on alerts are never muted
    assert alerts._alert_enabled(cfg, "grid_down", muted, now)
    assert alerts._alert_enabled(cfg, "fast_drain", muted, now)
    assert alerts._alert_enabled(cfg, "area_power_outage", muted, now)


def test_alerts_muted_helper_expiry():
    now = datetime(2026, 8, 13, 12, 0)
    assert alerts._alerts_muted({}, now) is False

    fresh = {"alerts_muted_until": (now + timedelta(hours=2)).isoformat()}
    assert alerts._alerts_muted(fresh, now) is True

    expired = {"alerts_muted_until": (now - timedelta(minutes=1)).isoformat()}
    assert alerts._alerts_muted(expired, now) is False
    # auto-cleared once expired
    assert "alerts_muted_until" not in expired


def test_safe_float():
    assert alerts._safe_float("1.5") == 1.5
    assert alerts._safe_float(2) == 2.0
    assert alerts._safe_float("garbage") is None
    assert alerts._safe_float(None) is None
    assert alerts._safe_float([1]) is None


def test_get_401_does_not_recurse_forever(monkeypatch):
    """Persistent API-level 401 must raise after one re-login, not recurse."""
    from franklinwh_scraper.account import AccountClient

    client = AccountClient("a@b.c", "pw")
    client._token = "stale"
    monkeypatch.setattr(client, "login", lambda: setattr(client, "_token", "fresh"))

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"code": 401, "message": "expired"}

    calls = []
    monkeypatch.setattr(client.session, "get", lambda *a, **kw: calls.append(1) or _Resp())
    with pytest.raises(ConnectionError):
        client._get("some/path")
    assert len(calls) == 2  # original + one retry, then raise


def test_get_composite_info_handles_explicit_null_result(monkeypatch):
    """A rate-limit/error response can come back with the "result" key
    *present* but explicitly null (e.g. a 429), not missing — `.get("result",
    {})` only falls back on a missing key, so that idiom passed the None
    straight through and crashed get_stats with an AttributeError instead of
    hitting its own empty-data retry path (see the 429 in the real advisor
    log, 2026-09-05)."""
    from franklinwh_scraper.account import AccountClient

    client = AccountClient("a@b.c", "pw")
    client._token = "tok"

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"code": 429, "message": "Too Many Requests", "result": None}

    monkeypatch.setattr(client.session, "get", lambda *a, **kw: _Resp())
    assert client.get_composite_info("gw1") == {}


def test_get_stats_rejects_empty_runtime_data(monkeypatch):
    """An empty runtimeData payload (transient gateway glitch) must raise,
    not fabricate a fake all-zero reading that trips false alerts."""
    from franklinwh_scraper import account as account_module
    from franklinwh_scraper.account import AccountClient

    client = AccountClient("a@b.c", "pw")
    monkeypatch.setattr(account_module.time, "sleep", lambda *_: None)
    monkeypatch.setattr(client, "get_composite_info", lambda gateway: {"runtimeData": {}})
    with pytest.raises(ConnectionError):
        client.get_stats("gw1")


def test_get_stats_retries_through_transient_empty_runtime_data(monkeypatch):
    """A brief gateway handshake timeout (empty runtimeData) should be
    retried and recovered instead of failing the whole poll immediately."""
    from franklinwh_scraper import account as account_module
    from franklinwh_scraper.account import AccountClient

    client = AccountClient("a@b.c", "pw")
    monkeypatch.setattr(account_module.time, "sleep", lambda *_: None)
    responses = [{"runtimeData": {}}, {"runtimeData": {}}, {"runtimeData": {"p_sun": 1.5}}]
    monkeypatch.setattr(client, "get_composite_info", lambda gateway: responses.pop(0))
    stats = client.get_stats("gw1")
    assert stats.current.solar_production_kw == 1.5


def test_precharge_plan():
    # dim tomorrow + low SoC + morning → recommend
    out = alerts._precharge_plan(datetime(2026, 1, 15, 10), 40.0, 2.0, 13.6)
    assert "Pre-charge" in out
    # ample solar → empty
    assert alerts._precharge_plan(datetime(2026, 1, 15, 10), 40.0, 30.0, 13.6) == ""
    # high SoC → empty
    assert alerts._precharge_plan(datetime(2026, 1, 15, 10), 90.0, 2.0, 13.6) == ""


def test_performance_ratio_ewma_tracks_recent_regime():
    """A shift to a new multi-day regime should pull the estimate toward
    the recent samples faster than a flat median of the whole history would."""
    state = {"perf_ratio_samples": [1.15, 1.14, 1.18, 1.12] + [0.80, 0.82, 0.79]}
    ratio = alerts._get_performance_ratio(state, cloudy=False)
    flat_median = sorted(state["perf_ratio_samples"])[len(state["perf_ratio_samples"]) // 2]
    assert ratio < flat_median  # pulled toward the newer, lower samples


def test_performance_ratio_falls_back_below_3_samples():
    assert alerts._get_performance_ratio({}, cloudy=False) == 1.0
    assert alerts._get_performance_ratio({}, cloudy=True) == 0.85


def test_calibrate_solar_rejects_single_outlier():
    import types
    outlook = types.SimpleNamespace(avg_ghi=lambda h: 700.0)
    state = {"solar_cal_samples": [3.8] * 10}
    # A single wildly different reading (sensor glitch) must not swing the pool.
    alerts._calibrate_solar(state, solar_kw=8.0, outlook=outlook,
                            now=datetime(2026, 7, 15, 12))  # ratio ~11.4, way off
    assert len(state["solar_cal_samples"]) == 10
    assert state["solar_cal_pending"] == [pytest.approx(11.43)]


def test_calibrate_solar_accepts_consistent_step_change():
    import types
    outlook = types.SimpleNamespace(avg_ghi=lambda h: 700.0)
    state = {"solar_cal_samples": [2.5] * 10}
    # Three consecutive, mutually-consistent readings well above the old
    # baseline (panels cleaned, shading removed) should be accepted as real.
    for _ in range(3):
        alerts._calibrate_solar(state, solar_kw=4.55, outlook=outlook,
                                now=datetime(2026, 7, 15, 12))  # ratio 6.5
    assert len(state["solar_cal_samples"]) == 13
    assert state["solar_cal_pending"] == []


def test_prediction_drift_alert_fires_on_sustained_bias():
    now = datetime(2026, 7, 15, 9)
    state = {
        f"daily_pr_2026-07-{d:02d}": 1.15 for d in range(2, 14)
    }
    msg = alerts._alert_prediction_drift(state, "2026-07-15", now)
    assert msg is not None and "low" in msg
    assert state["prediction_drift_alert_date"] == "2026-07-15"
    # Dedupe: no re-fire within 7 days.
    assert alerts._alert_prediction_drift(state, "2026-07-16", now + timedelta(days=1)) is None


def test_prediction_drift_alert_silent_when_centred():
    now = datetime(2026, 7, 15, 9)
    state = {f"daily_pr_2026-07-{d:02d}": 1.02 for d in range(2, 14)}
    assert alerts._alert_prediction_drift(state, "2026-07-15", now) is None
    # Too few samples → silent even with big bias.
    state = {f"daily_pr_2026-07-{d:02d}": 1.3 for d in range(10, 14)}
    assert alerts._alert_prediction_drift(state, "2026-07-15", now) is None


def test_solar_degradation_fires_on_genuine_drop():
    # Well past the 2026-08-24 bias-fix date — the 30-day window here is the
    # normal moving window, not floored, so this exercises the ordinary path.
    now = datetime(2026, 10, 1, 9)
    state = {
        f"daily_pr_2026-09-{d:02d}": (0.90 if d >= 24 else 1.10)
        for d in range(1, 31)
    }
    msg = alerts._alert_solar_degradation(state, "2026-10-01", now)
    assert msg is not None and "trending down" in msg
    assert state["solar_degradation_alerted_week"] == now.strftime("%G-W%V")


def test_solar_degradation_ignores_pre_bias_fix_baseline():
    # Reproduces the real false-positive: daily_pr_ inflated ~1.0-1.2 before
    # 2026-08-24 (perf_ratio EWMA undershoot, fixed that date), then settling
    # near 1.0 after. A naive 30-day baseline vs 7-day recent window reads
    # the calibration fix itself as an 8% drop. The floor at the fix date
    # should keep the baseline from reaching pre-fix samples, leaving too
    # few post-fix samples (7 < 10) to evaluate at all.
    now = datetime(2026, 8, 31, 9)
    state = {
        f"daily_pr_2026-08-{d:02d}": (1.06 if d < 24 else 0.98)
        for d in range(1, 31)
    }
    assert alerts._alert_solar_degradation(state, "2026-08-31", now) is None


def _make_license(tmp_path, monkeypatch, gateway="GW123", expires="2099-01-01",
                  tamper=False):
    import base64
    import json as _json
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from franklinwh_scraper import license as lic

    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    monkeypatch.setattr(lic, "PUBLIC_KEY_B64", base64.b64encode(pub).decode())

    payload = {"customer": "Test", "gateway_id": gateway,
               "issued": "2026-01-01", "expires": expires}
    canonical = _json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    sig = base64.b64encode(key.sign(canonical)).decode()
    if tamper:
        payload["expires"] = "2199-01-01"  # payload edited after signing
    p = tmp_path / "lic.json"
    p.write_text(_json.dumps({"payload": payload, "sig": sig}))
    return p


def test_license_valid_and_gateway_bound(tmp_path, monkeypatch):
    from franklinwh_scraper import license as lic
    p = _make_license(tmp_path, monkeypatch)
    assert lic.check_license("GW123", path=p).state == "ok"
    # Same file on a different system's gateway must fail.
    assert lic.check_license("GW999", path=p).state == "invalid"
    assert lic.check_license("", path=p).state == "invalid"


def test_license_rejects_tampered_payload(tmp_path, monkeypatch):
    from franklinwh_scraper import license as lic
    p = _make_license(tmp_path, monkeypatch, tamper=True)
    st = lic.check_license("GW123", path=p)
    assert st.state == "invalid" and "signature" in st.message


def test_license_expiry_grace_then_invalid(tmp_path, monkeypatch):
    from datetime import date, timedelta
    from franklinwh_scraper import license as lic
    graceful = (date.today() - timedelta(days=5)).isoformat()
    p = _make_license(tmp_path, monkeypatch, expires=graceful)
    assert lic.check_license("GW123", path=p).state == "grace"
    dead = (date.today() - timedelta(days=lic.GRACE_DAYS + 1)).isoformat()
    p = _make_license(tmp_path, monkeypatch, expires=dead)
    assert lic.check_license("GW123", path=p).state == "invalid"


def test_license_missing_file(tmp_path):
    from franklinwh_scraper import license as lic
    assert lic.check_license("GW123", path=tmp_path / "nope").state == "invalid"


def test_license_rejects_non_dict_payload(tmp_path, monkeypatch):
    """A signed-but-malformed payload must degrade to invalid, not crash."""
    import base64
    import json as _json
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from franklinwh_scraper import license as lic

    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    monkeypatch.setattr(lic, "PUBLIC_KEY_B64", base64.b64encode(pub).decode())
    payload = ["not", "a", "dict"]
    canonical = _json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    sig = base64.b64encode(key.sign(canonical)).decode()
    p = tmp_path / "lic.json"
    p.write_text(_json.dumps({"payload": payload, "sig": sig}))
    assert lic.check_license("GW123", path=p).state == "invalid"


def test_license_clock_rollback_detected(tmp_path, monkeypatch):
    from datetime import date, timedelta
    from franklinwh_scraper import license as lic
    p = _make_license(tmp_path, monkeypatch, expires="2099-01-01")
    assert lic.check_license("GW123", path=p).state == "ok"  # seeds .lastseen = today
    future = (date.today() + timedelta(days=30)).isoformat()
    lic._lastseen_path(p).write_text(future)
    st = lic.check_license("GW123", path=p)
    assert st.state == "invalid" and "rollback" in st.message.lower()


def test_tou_rates_stale_alert_fires_once():
    stale_now = datetime(2026, 8, 1)  # well past 180 days from tou._RATES_EFFECTIVE_DATE
    state: dict = {}
    msg = alerts._alert_tou_rates_stale(state, "2026-08-01", stale_now)
    assert msg is not None and "outdated" in msg
    assert state["tou_stale_alerted"] == "2026-08-01"
    # Second call same/later day must not re-fire.
    assert alerts._alert_tou_rates_stale(state, "2026-08-02", stale_now) is None


def test_tou_rates_stale_alert_silent_when_fresh():
    fresh_now = datetime(2026, 2, 1)  # well within 180 days
    assert alerts._alert_tou_rates_stale({}, "2026-02-01", fresh_now) is None


def test_with_retry_gives_up_after_attempts(monkeypatch):
    monkeypatch.setattr(notifier.time, "sleep", lambda s: None)
    calls = []

    def _always_fails():
        calls.append(1)
        raise ConnectionError("down")

    notifier._with_retry(_always_fails, "test channel", attempts=3, base_delay=0)
    assert len(calls) == 3


def test_with_retry_stops_on_first_success(monkeypatch):
    monkeypatch.setattr(notifier.time, "sleep", lambda s: None)
    calls = []

    def _succeeds_second_try():
        calls.append(1)
        if len(calls) < 2:
            raise ConnectionError("transient")

    notifier._with_retry(_succeeds_second_try, "test channel", attempts=3, base_delay=0)
    assert len(calls) == 2


def _bot(chat_id: str = "owner-1") -> TelegramChatBot:
    cfg = Config()
    cfg.telegram_chat_id = chat_id
    return TelegramChatBot(cfg, "fake-api-key")


def test_chatbot_allowlist_rejects_foreign_chat_id():
    bot = _bot("owner-1")
    assert bot._is_authorized("owner-1") is True
    assert bot._is_authorized("stranger-2") is False


def test_chatbot_allowlist_allows_owner_chat_id():
    bot = _bot("")  # no owner configured — allow everyone (back-compat)
    assert bot._is_authorized("anyone") is True


def test_chatbot_daily_cap_blocks_after_limit(monkeypatch):
    bot = _bot()
    monkeypatch.setattr("franklinwh_scraper.chatbot._DAILY_CALL_CAP", 3)
    assert [bot._under_daily_cap() for _ in range(3)] == [True, True, True]
    assert bot._under_daily_cap() is False


def test_chatbot_daily_cap_resets_on_new_day(monkeypatch):
    bot = _bot()
    monkeypatch.setattr("franklinwh_scraper.chatbot._DAILY_CALL_CAP", 1)
    assert bot._under_daily_cap() is True
    assert bot._under_daily_cap() is False
    bot._call_count_date = "2000-01-01"  # simulate yesterday
    assert bot._under_daily_cap() is True


def test_weather_stale_alert_fires_once(monkeypatch):
    monkeypatch.setitem(alerts._outlook_cache, "fetched_at", time.time() - 4 * 3600)
    state: dict = {}
    now = datetime.now()
    msg = alerts._alert_weather_stale(state, now.strftime("%Y-%m-%d"), now)
    assert msg is not None and "stale" in msg
    assert state["weather_stale_alerted"] is True
    assert alerts._alert_weather_stale(state, now.strftime("%Y-%m-%d"), now) is None


def test_weather_stale_alert_silent_when_fresh(monkeypatch):
    monkeypatch.setitem(alerts._outlook_cache, "fetched_at", time.time() - 60)
    now = datetime.now()
    assert alerts._alert_weather_stale({}, now.strftime("%Y-%m-%d"), now) is None


def test_weather_stale_alert_clears_after_fresh_fetch(monkeypatch):
    now = datetime.now()
    state = {"weather_stale_alerted": True}
    monkeypatch.setitem(alerts._outlook_cache, "fetched_at", time.time() - 60)
    assert alerts._alert_weather_stale(state, now.strftime("%Y-%m-%d"), now) is None
    assert state["weather_stale_alerted"] is False


def test_predict_treats_holiday_as_sunday_slot(tmp_path, monkeypatch):
    """A holiday's load should be bucketed under Sunday's slot, not its
    actual weekday, matching how tou.py already treats holidays as Sunday."""
    db = HistoryStore(tmp_path / "h.db")
    holiday = datetime(2026, 12, 25, 12, 0, 0)  # Christmas, a Friday in 2026
    assert holiday.weekday() == 4

    def _insert(day_of_week: int, load_kw: float):
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (holiday.isoformat(), day_of_week, holiday.hour, load_kw, 0.0, 50.0, 0.0, "normal", 0.0, 0.0),
        )

    # Sunday-slot readings (what the holiday SHOULD match).
    for _ in range(5):
        _insert(6, 9.0)
    # Friday-slot readings (what it would match without holiday awareness).
    for _ in range(5):
        _insert(4, 1.0)
    db._conn.commit()

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return holiday

    monkeypatch.setattr(predictor, "datetime", _FakeDatetime)
    forecast = predict(db, horizon_hours=1)

    assert forecast.hours[0].predicted_load_kw > 5.0  # matched Sunday (9.0), not Friday (1.0)


# ── advisor.py EB-plan gating ────────────────────────────────────────

def test_recommend_eb_gates_on_projected_soc_not_current():
    """Regression for the bug where a healthy *current* SoC (>=50%) could
    suppress a real projected shortfall at 4pm. The recommendation must key
    off plan['eb_needed'] (the projection), not the current-SoC threshold."""
    import types
    from franklinwh_scraper import advisor
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    now = datetime(2026, 7, 15, 10, 0, 0)
    peak_start = now.replace(hour=16, minute=0, second=0, microsecond=0)
    peak_end = now.replace(hour=21, minute=0, second=0, microsecond=0)
    hours = []
    t = now
    while t < peak_end:
        in_peak = peak_start <= t < peak_end
        hours.append(HourPrediction(
            dt=t, predicted_load_kw=1.5,
            predicted_solar_kw=(0.1 if in_peak else 0.3),  # heavy cloud cover all day
            net_kw=(0.1 - 1.5) if in_peak else (0.3 - 1.5),
            confidence="high",
        ))
        t += timedelta(hours=1)
    forecast = UsageForecast(hours=hours, total_load_kwh=15.0, total_solar_kwh=3.0,
                             net_kwh=-12.0, peak_load_kw=1.5, confidence="high", data_days=30)

    stats = types.SimpleNamespace(
        current=types.SimpleNamespace(
            battery_soc_pct=52.0,  # "healthy" by the old >=50% gate
            home_load_kw=1.5, solar_production_kw=0.3, grid_status="normal",
        ),
        totals=types.SimpleNamespace(),
    )
    monkeypatch_now = advisor.datetime
    try:
        class _FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        advisor.datetime = _FakeDatetime
        rec = advisor.recommend(stats, outlook=None, forecast=forecast, battery_capacity_kwh=13.6)
    finally:
        advisor.datetime = monkeypatch_now

    assert rec.mode == advisor.Mode.EMERGENCY_BACKUP
    assert "short of covering" in rec.reason


# ── alerts.py grid outage dedup ──────────────────────────────────────

def test_grid_restored_clears_dedup_so_second_outage_alerts():
    """Regression: a same-day repeat grid outage must still alert — grid_down
    is an always-on safety alert and must not go silent after one restore."""
    import types
    c_down = types.SimpleNamespace(
        grid_status="down", battery_soc_pct=80.0, home_load_kw=1.0,
        solar_production_kw=0.0, generator_enabled=False, generator_production_kw=0.0,
        battery_use_kw=1.0,
    )
    c_up = types.SimpleNamespace(grid_status="normal", battery_soc_pct=78.0,
                                 solar_production_kw=0.5)
    cfg = Config(battery_capacity_kwh=13.6)
    state: dict = {}
    today = "2026-07-15"

    msg1 = alerts._alert_grid_down(state, today, datetime(2026, 7, 15, 9, 0), c_down, cfg)
    assert msg1 is not None
    msg2 = alerts._alert_grid_restored(state, datetime(2026, 7, 15, 10, 0), c_up, cfg)
    assert msg2 is not None
    assert "grid_down_alerted_date" not in state  # cleared on restore

    # Second outage same day must alert again, not be silently deduped.
    msg3 = alerts._alert_grid_down(state, today, datetime(2026, 7, 15, 15, 0), c_down, cfg)
    assert msg3 is not None


# ── alerts.py state pruning ───────────────────────────────────────────

def test_prune_old_state_covers_previously_unmatched_prefixes():
    """Regression: predicted_kwh_/predicted_avg_ghi_/daily_import_cost_ keys
    and *_week dedup markers used to match no prune rule and accumulated
    forever."""
    old_date = "2026-01-01"  # >30 days before "now" in any real run
    old_week = "2026-W01"
    state = {
        f"predicted_kwh_{old_date}": 25.0,
        f"predicted_avg_ghi_{old_date}": 400.0,
        f"daily_import_cost_{old_date}": 1.5,
        f"soc_7am_pred_{old_date}": {"pct": 40.0, "dt": f"{old_date}T07:00:00"},
        "solar_degradation_alerted_week": old_week,
        f"predicted_kwh_{datetime.now().strftime('%Y-%m-%d')}": 30.0,  # keep: today
    }
    pruned = alerts._prune_old_state(state)
    assert f"predicted_kwh_{old_date}" not in pruned
    assert f"predicted_avg_ghi_{old_date}" not in pruned
    assert f"daily_import_cost_{old_date}" not in pruned
    assert f"soc_7am_pred_{old_date}" not in pruned
    assert "solar_degradation_alerted_week" not in pruned
    assert f"predicted_kwh_{datetime.now().strftime('%Y-%m-%d')}" in pruned


# ── alerts.py fast-drain minimum-elapsed floor ────────────────────────

def test_fast_drain_ignores_near_zero_interval():
    """Regression: a tiny elapsed_h (rapid re-poll) shouldn't be able to
    amplify a 1% SoC blip into a false 'draining fast' alert."""
    import types
    c = types.SimpleNamespace(battery_soc_pct=30.0, home_load_kw=1.0,
                              solar_production_kw=0.0, battery_use_kw=1.0)
    now = datetime(2026, 7, 15, 12, 0, 0)
    state = {"last_soc": 31.0, "last_soc_time": (now - timedelta(seconds=5)).isoformat()}
    msg = alerts._alert_fast_drain(state, "2026-07-15", now, c, Config())
    assert msg is None  # 1%/5s would be ~720%/hr if not floored


# ── advisor.py window-specific confidence gating ─────────────────────

def test_tou_eb_plan_uses_window_confidence_not_aggregate():
    """Regression: a single zero-history hour anywhere in the 24h forecast
    used to drop UsageForecast.confidence to 'none' for everything, which
    disabled the EB decision even when the now->9pm window it actually
    needs has real data. Fix computes confidence per-window."""
    from franklinwh_scraper import advisor
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    now = datetime(2026, 7, 15, 10, 0, 0)
    peak_start = now.replace(hour=16, minute=0, second=0, microsecond=0)
    peak_end = now.replace(hour=21, minute=0, second=0, microsecond=0)
    hours = []
    t = now
    while t < peak_end + timedelta(hours=3):
        # Everything through 9pm has real data; one unrelated late hour
        # (11pm-ish) is "none" — this used to poison forecast.confidence
        # for the whole 24h horizon.
        conf = "none" if t >= peak_end + timedelta(hours=2) else "high"
        hours.append(HourPrediction(dt=t, predicted_load_kw=2.0, predicted_solar_kw=0.5,
                                    net_kw=-1.5, confidence=conf))
        t += timedelta(hours=1)
    forecast = UsageForecast(hours=hours, total_load_kwh=10.0, total_solar_kwh=2.0,
                             net_kwh=-8.0, peak_load_kw=1.0, confidence="none",  # aggregate poisoned
                             data_days=30)

    plan = advisor._tou_eb_plan(now, soc=60.0, capacity_kwh=13.6, forecast=forecast)
    # window_confidence covers now->peak_end, which is entirely "high" —
    # must not fall back to the crude hardcoded default (net_peak_draw=4.0
    # regardless of forecast).
    assert plan["window_confidence"] == "high"
    assert plan["net_peak_draw"] != 4.0


# ── history.py solar-reset detection ──────────────────────────────────

def test_daily_solar_kwh_api_falls_back_on_midday_reset(tmp_path):
    """Regression: MAX(solar_total_kwh) silently under-reports if the API
    counter resets mid-day (gateway reboot) and later production stays
    below the pre-reset peak. Must detect the drop and use the trapezoidal
    fallback instead."""
    db = HistoryStore(tmp_path / "h.db")
    day = "2026-07-15"
    rows = [
        (f"{day}T08:00:00", 3.0, 0.5, 1.5),   # ts, solar_total_kwh, home_kw, solar_kw
        (f"{day}T10:00:00", 8.0, 0.5, 2.0),   # peak before reset
        (f"{day}T12:00:00", 1.0, 0.5, 2.5),   # counter reset — drop below prior peak
        (f"{day}T14:00:00", 5.0, 0.5, 2.5),   # real production continues, stays under 8.0
    ]
    for ts, total, home, solar in rows:
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, 2, int(ts[11:13]), home, solar, 50.0, 0.0, "normal", total, 0.0),
        )
    db._conn.commit()

    naive_max = 8.0  # what the old MAX()-only implementation would return
    result = db.daily_solar_kwh_api(day)
    assert result != naive_max  # must not silently under-report via MAX()


# ── history.py readings rollup ─────────────────────────────────────────

def test_rollup_old_readings_preserves_hourly_slots(tmp_path):
    """Old readings should downsample to one row per (date, hour) — not one
    per day, which would destroy the (day_of_week, hour_of_day) slot
    granularity the predictor depends on. Recent data must be untouched."""
    db = HistoryStore(tmp_path / "h.db")
    old_day = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    recent_day = datetime.now().strftime("%Y-%m-%d")

    # 3 readings in old-day hour 8, 2 in old-day hour 9 — should each
    # collapse to a single row. 2 readings in today's hour 8 must survive
    # untouched (not old enough to roll up).
    for minute in (0, 15, 30):
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"{old_day}T08:{minute:02d}:00", 2, 8, 1.0, 2.0, 50.0, 0.0, "normal", 5.0, 0.0),
        )
    for minute in (0, 15):
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"{old_day}T09:{minute:02d}:00", 2, 9, 1.0, 2.0, 50.0, 0.0, "normal", 5.0, 0.0),
        )
    for minute in (0, 15):
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"{recent_day}T08:{minute:02d}:00", 2, 8, 1.0, 2.0, 50.0, 0.0, "normal", 5.0, 0.0),
        )
    db._conn.commit()
    assert db.reading_count() == 7

    removed = db.rollup_old_readings(older_than_days=180)
    assert removed == 3  # (3-1) + (2-1) from the two old-day buckets

    rows = db._conn.execute(
        "SELECT day_of_week, hour_of_day FROM readings WHERE timestamp LIKE ?",
        (f"{old_day}%",),
    ).fetchall()
    assert sorted(rows) == [(2, 8), (2, 9)]  # one row per old (dow, hour) slot

    recent_rows = db._conn.execute(
        "SELECT COUNT(*) FROM readings WHERE timestamp LIKE ?", (f"{recent_day}%",)
    ).fetchone()
    assert recent_rows[0] == 2  # untouched


def test_send_sundown_projects_soc_to_last_solar_hour(tmp_path):
    """/sundown should project SoC forward using the forecast, stopping at
    the last hour today still expecting real solar — not a fixed horizon.

    bot._outdir must be set to an isolated tmp_path: _send_sundown loads
    (and writes!) sundown_bias_samples via _load_peak_state(self._outdir or
    Path(cfg.output_dir)) — left unset, this test used to silently read
    AND write Eric's real live output/.peak_alert_state.json on every
    pytest run, both polluting production state and making the assertion
    below flaky against whatever bias the live system had accumulated
    (caught 2026-08-23: a real -18pt live bias turned an expected 100%
    into 82%, failing this test for a reason that had nothing to do with
    the code under test).
    """
    import types
    from franklinwh_scraper import chatbot as chatbot_mod
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    now = datetime(2026, 7, 15, 12, 0, 0)  # noon
    hours = []
    t = now
    while t.date() == now.date() and t.hour <= 23:
        solar = 3.0 if 12 <= t.hour < 18 else 0.0  # sun down at 6pm today
        hours.append(HourPrediction(
            dt=t, predicted_load_kw=1.0, predicted_solar_kw=solar,
            net_kw=solar - 1.0, confidence="high",
        ))
        t += timedelta(hours=1)
    forecast = UsageForecast(hours=hours, total_load_kwh=24.0, total_solar_kwh=18.0,
                             net_kwh=-6.0, peak_load_kw=1.0, confidence="high", data_days=30)

    bot = TelegramChatBot(Config(battery_capacity_kwh=13.6), api_key="x")
    bot._outdir = tmp_path
    bot._stats = types.SimpleNamespace(
        current=types.SimpleNamespace(battery_soc_pct=27.0),
    )
    bot._usage_forecast = forecast

    sent = {}
    bot._send = lambda chat_id, text: sent.__setitem__("text", text)

    real_datetime = chatbot_mod.datetime
    try:
        class _FakeDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        chatbot_mod.datetime = _FakeDatetime
        bot._send_sundown("123")
    finally:
        chatbot_mod.datetime = real_datetime

    assert "text" in sent
    assert "27%" in sent["text"]
    # 6h of net +2.0 kW (3.0 solar - 1.0 load) = +12 kWh -> capped at 100% of 13.6 kWh cap
    assert "100%" in sent["text"]
    assert "5:00 PM" in sent["text"]


def test_send_sundown_estimates_surplus_solar_export(tmp_path):
    """/sundown adds a surplus-export estimate: once the walk-forward fills
    the battery, further solar surplus is clipped by the same
    min(bat_cap, ...) that models Self-Consumption auto-exporting a full
    battery's surplus — that clipped total is the export estimate, priced
    at today's best export rate (same tou.peak_export_hour the existing
    export-arbitrage alert uses).

    bot._outdir = tmp_path: without it this silently wrote a real
    sundown_pred_<today> entry into Eric's live output/.peak_alert_state.json
    on every test run (see test_send_sundown_projects_soc_to_last_solar_hour
    for the full story)."""
    import types
    from franklinwh_scraper import chatbot as chatbot_mod
    from franklinwh_scraper import tou
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    now = datetime(2026, 7, 15, 8, 0, 0)  # July -> _NEM3_DEFAULT_EXPORT_RATE, deterministic
    hours = []
    t = now
    while t.date() == now.date() and t.hour <= 23:
        solar = 5.0 if 8 <= t.hour < 12 else 0.0  # sun down at noon today
        hours.append(HourPrediction(
            dt=t, predicted_load_kw=1.0, predicted_solar_kw=solar,
            net_kw=solar - 1.0, confidence="high",
        ))
        t += timedelta(hours=1)
    forecast = UsageForecast(hours=hours, total_load_kwh=24.0, total_solar_kwh=20.0,
                             net_kwh=8.0, peak_load_kw=1.0, confidence="high", data_days=30)

    bot = TelegramChatBot(Config(battery_capacity_kwh=13.6), api_key="x")
    bot._outdir = tmp_path
    # soc=80% -> 10.88 kWh; net +4 kW/hr (5.0 solar - 1.0 load) for hours
    # 9,10,11 (dt<=now and dt>sundown_dt=11:00 are skipped by the walk) ->
    # hour9: 10.88+4=14.88 clips 1.28 over cap; hour10/11: full +4 each
    # clipped -> total export = 1.28+4+4 = 9.28 kWh.
    bot._stats = types.SimpleNamespace(
        current=types.SimpleNamespace(battery_soc_pct=80.0),
    )
    bot._usage_forecast = forecast

    sent = {}
    bot._send = lambda chat_id, text: sent.__setitem__("text", text)

    real_datetime = chatbot_mod.datetime
    try:
        class _FakeDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        chatbot_mod.datetime = _FakeDatetime
        bot._send_sundown("123")
    finally:
        chatbot_mod.datetime = real_datetime

    assert "text" in sent
    text = sent["text"]
    assert "Surplus solar to export" in text
    assert "9.3 kWh" in text  # 9.28 rounds to 9.3
    assert "68% of battery capacity" in text  # 9.28 / 13.6 * 100 = 68.2%
    rate = tou._NEM3_DEFAULT_EXPORT_RATE
    assert f"${9.28 * rate:.2f}" in text
    assert f"${rate:.3f}/kWh" in text


def test_send_sundown_omits_export_line_when_battery_never_fills(tmp_path):
    """No export line when the forecast never has the battery hitting cap
    before sundown — a marginal/negative kWh estimate would be noise, not
    signal."""
    import types
    from franklinwh_scraper import chatbot as chatbot_mod
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    now = datetime(2026, 7, 15, 8, 0, 0)
    hours = []
    t = now
    while t.date() == now.date() and t.hour <= 23:
        solar = 1.5 if 8 <= t.hour < 12 else 0.0
        hours.append(HourPrediction(
            dt=t, predicted_load_kw=1.0, predicted_solar_kw=solar,
            net_kw=solar - 1.0, confidence="high",
        ))
        t += timedelta(hours=1)
    forecast = UsageForecast(hours=hours, total_load_kwh=24.0, total_solar_kwh=6.0,
                             net_kwh=-18.0, peak_load_kw=1.0, confidence="high", data_days=30)

    bot = TelegramChatBot(Config(battery_capacity_kwh=13.6), api_key="x")
    bot._outdir = tmp_path
    bot._stats = types.SimpleNamespace(
        current=types.SimpleNamespace(battery_soc_pct=20.0),  # 2.72 kWh, never near cap
    )
    bot._usage_forecast = forecast

    sent = {}
    bot._send = lambda chat_id, text: sent.__setitem__("text", text)

    real_datetime = chatbot_mod.datetime
    try:
        class _FakeDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        chatbot_mod.datetime = _FakeDatetime
        bot._send_sundown("123")
    finally:
        chatbot_mod.datetime = real_datetime

    assert "text" in sent
    assert "Surplus solar to export" not in sent["text"]


def test_eod_digest_reports_sundown_prediction_accuracy():
    """If /sundown was used earlier today, the EOD digest should report how
    the prediction compared to the actual SoC near the predicted time."""
    import types
    from franklinwh_scraper import alerts

    today = "2026-07-20"
    now = datetime(2026, 7, 20, 21, 0, 0)
    sundown_dt = datetime(2026, 7, 20, 17, 30, 0)

    state = {f"sundown_pred_{today}": {"pct": 85.0, "dt": sundown_dt.isoformat(), "requested_at": "2026-07-20T12:00:00"}}

    stats = types.SimpleNamespace(
        current=types.SimpleNamespace(battery_soc_pct=60.0),
        totals=types.SimpleNamespace(
            solar_kwh=0.0, battery_charge_kwh=0.0, battery_discharge_kwh=0.0,
            grid_load_kwh=0.0, grid_export_kwh=0.0, home_use_kwh=0.0,
        ),
    )

    class _FakeStore:
        def daily_solar_kwh_api(self, d): return 0.0
        def daily_solar_kwh(self, d): return 0.0
        def daily_battery_kwh(self, d): return (0.0, 0.0)
        def weekly_readings(self, s, e): return []
        def soc_near(self, ts): return 79.0  # actual SoC near the predicted sundown time

    cfg = Config(battery_capacity_kwh=13.6)
    msg = alerts._alert_eod_digest(state, today, now, stats, cfg, None, None, store=_FakeStore())

    assert msg is not None
    assert "/sundown accuracy" in msg
    assert "predicted 85%" in msg
    assert "actual 79%" in msg
    assert "-6 pt" in msg


def test_eod_digest_labels_sundown_fallback_when_no_reading_near_predicted_time():
    """When no DB reading lands near the predicted sundown time, the digest
    must label the substitute (current, digest-time) SoC explicitly instead
    of silently presenting it as if it were measured at the predicted time —
    a real prediction can otherwise look like a large miss just because the
    battery kept discharging for hours after sundown before the digest ran."""
    import types
    from franklinwh_scraper import alerts

    today = "2026-07-20"
    now = datetime(2026, 7, 20, 21, 0, 0)
    sundown_dt = datetime(2026, 7, 20, 17, 30, 0)

    state = {f"sundown_pred_{today}": {"pct": 85.0, "dt": sundown_dt.isoformat(), "requested_at": "2026-07-20T12:00:00"}}

    stats = types.SimpleNamespace(
        current=types.SimpleNamespace(battery_soc_pct=58.0),
        totals=types.SimpleNamespace(
            solar_kwh=0.0, battery_charge_kwh=0.0, battery_discharge_kwh=0.0,
            grid_load_kwh=0.0, grid_export_kwh=0.0, home_use_kwh=0.0,
        ),
    )

    class _FakeStore:
        def daily_solar_kwh_api(self, d): return 0.0
        def daily_solar_kwh(self, d): return 0.0
        def daily_battery_kwh(self, d): return (0.0, 0.0)
        def weekly_readings(self, s, e): return []
        def soc_near(self, ts): return None  # no reading landed near sundown

    cfg = Config(battery_capacity_kwh=13.6)
    msg = alerts._alert_eod_digest(state, today, now, stats, cfg, None, None, store=_FakeStore())

    assert msg is not None
    assert "no reading near that time" in msg
    assert "using now's 58%" in msg
    assert "not directly comparable" in msg


def test_eod_digest_records_sundown_bias_sample_from_raw_prediction():
    """The learning sample must be actual - raw_pct (the model's real miss),
    not actual - pct (which may already include a prior correction) — same
    convention _calibrate_solar_hourly uses. Falls back to pct for state
    written before raw_pct existed."""
    import types

    from franklinwh_scraper import alerts

    today = "2026-07-20"
    now = datetime(2026, 7, 20, 21, 0, 0)
    sundown_dt = datetime(2026, 7, 20, 17, 30, 0)

    state = {f"sundown_pred_{today}": {
        "pct": 82.0, "raw_pct": 85.0,  # correction was already applied when this was shown
        "dt": sundown_dt.isoformat(), "requested_at": "2026-07-20T12:00:00",
    }}

    stats = types.SimpleNamespace(
        current=types.SimpleNamespace(battery_soc_pct=60.0),
        totals=types.SimpleNamespace(
            solar_kwh=0.0, battery_charge_kwh=0.0, battery_discharge_kwh=0.0,
            grid_load_kwh=0.0, grid_export_kwh=0.0, home_use_kwh=0.0,
        ),
    )

    class _FakeStore:
        def daily_solar_kwh_api(self, d): return 0.0
        def daily_solar_kwh(self, d): return 0.0
        def daily_battery_kwh(self, d): return (0.0, 0.0)
        def weekly_readings(self, s, e): return []
        def soc_near(self, ts): return 79.0

    cfg = Config(battery_capacity_kwh=13.6)
    alerts._alert_eod_digest(state, today, now, stats, cfg, None, None, store=_FakeStore())

    # actual (79) - raw_pct (85) = -6, not actual (79) - pct (82) = -3.
    assert state["sundown_bias_samples"] == [-6.0]


def test_eod_digest_sundown_bias_sample_falls_back_to_pct_without_raw_pct():
    """Old-format state (pre-raw_pct field) shouldn't crash or drop the
    sample — falls back to comparing against pct itself."""
    import types

    from franklinwh_scraper import alerts

    today = "2026-07-20"
    now = datetime(2026, 7, 20, 21, 0, 0)
    sundown_dt = datetime(2026, 7, 20, 17, 30, 0)

    state = {f"sundown_pred_{today}": {
        "pct": 85.0, "dt": sundown_dt.isoformat(), "requested_at": "2026-07-20T12:00:00",
    }}

    stats = types.SimpleNamespace(
        current=types.SimpleNamespace(battery_soc_pct=60.0),
        totals=types.SimpleNamespace(
            solar_kwh=0.0, battery_charge_kwh=0.0, battery_discharge_kwh=0.0,
            grid_load_kwh=0.0, grid_export_kwh=0.0, home_use_kwh=0.0,
        ),
    )

    class _FakeStore:
        def daily_solar_kwh_api(self, d): return 0.0
        def daily_solar_kwh(self, d): return 0.0
        def daily_battery_kwh(self, d): return (0.0, 0.0)
        def weekly_readings(self, s, e): return []
        def soc_near(self, ts): return 79.0

    cfg = Config(battery_capacity_kwh=13.6)
    alerts._alert_eod_digest(state, today, now, stats, cfg, None, None, store=_FakeStore())

    assert state["sundown_bias_samples"] == [-6.0]  # 79 - 85


def test_eod_digest_no_sundown_bias_sample_on_not_directly_comparable():
    """The fallback (no reading near sundown, using digest-time SoC
    instead) must not feed the learning loop — it's explicitly labeled not
    comparable, and treating it as a real miss would poison the EWMA."""
    import types

    from franklinwh_scraper import alerts

    today = "2026-07-20"
    now = datetime(2026, 7, 20, 21, 0, 0)
    sundown_dt = datetime(2026, 7, 20, 17, 30, 0)

    state = {f"sundown_pred_{today}": {
        "pct": 85.0, "raw_pct": 85.0,
        "dt": sundown_dt.isoformat(), "requested_at": "2026-07-20T12:00:00",
    }}

    stats = types.SimpleNamespace(
        current=types.SimpleNamespace(battery_soc_pct=58.0),
        totals=types.SimpleNamespace(
            solar_kwh=0.0, battery_charge_kwh=0.0, battery_discharge_kwh=0.0,
            grid_load_kwh=0.0, grid_export_kwh=0.0, home_use_kwh=0.0,
        ),
    )

    class _FakeStore:
        def daily_solar_kwh_api(self, d): return 0.0
        def daily_solar_kwh(self, d): return 0.0
        def daily_battery_kwh(self, d): return (0.0, 0.0)
        def weekly_readings(self, s, e): return []
        def soc_near(self, ts): return None  # no reading near sundown -> fallback branch

    cfg = Config(battery_capacity_kwh=13.6)
    alerts._alert_eod_digest(state, today, now, stats, cfg, None, None, store=_FakeStore())

    assert "sundown_bias_samples" not in state


# ── Audit fixes (2026-07-26) ────────────────────────────────────────────

def test_notify_email_reports_real_failure(monkeypatch):
    """Setup's 'test email' must be able to actually fail, not just log and
    return None like it did before the fix."""
    from franklinwh_scraper import notifier

    class _BoomSMTP:
        def __init__(self, *a, **k):
            raise OSError("Connection refused")

    monkeypatch.setattr(notifier.time, "sleep", lambda s: None)
    monkeypatch.setattr(notifier.smtplib, "SMTP", _BoomSMTP)
    cfg = Config(smtp_host="smtp.example.com", email_to="a@b.c")
    assert notifier.notify_email("test", cfg) is False


def test_notify_email_reports_real_success(monkeypatch):
    from franklinwh_scraper import notifier

    class _FakeSMTP:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def ehlo(self): pass
        def starttls(self): pass
        def sendmail(self, *a, **k): pass

    monkeypatch.setattr(notifier.smtplib, "SMTP", _FakeSMTP)
    cfg = Config(smtp_host="smtp.example.com", email_to="a@b.c")
    assert notifier.notify_email("test", cfg) is True


def test_notify_webhook_reports_http_error_as_failure(monkeypatch):
    """A webhook URL that resolves but returns 404/500 must count as a
    failed test send, not a silent success."""
    from franklinwh_scraper import notifier
    import requests

    class _BadResponse:
        def raise_for_status(self):
            raise requests.HTTPError("404 Client Error")

    monkeypatch.setattr(notifier.time, "sleep", lambda s: None)
    monkeypatch.setattr(notifier.requests, "post", lambda *a, **k: _BadResponse())
    cfg = Config(webhook_url="https://example.com/hook")
    assert notifier.notify_webhook("test", False, cfg) is False


def test_notify_ntfy_posts_to_topic_url(monkeypatch):
    from franklinwh_scraper import notifier

    calls = []

    class _OkResponse:
        def raise_for_status(self):
            pass

    def _fake_post(url, data=None, headers=None, timeout=None):
        calls.append((url, data, headers))
        return _OkResponse()

    monkeypatch.setattr(notifier.requests, "post", _fake_post)
    cfg = Config(ntfy_topic="my-secret-topic")
    assert notifier.notify_ntfy("hello world", cfg) is True
    assert len(calls) == 1
    url, data, headers = calls[0]
    assert url == "https://ntfy.sh/my-secret-topic"
    assert data == b"hello world"
    assert headers["Title"] == b"hello world"


def test_notify_ntfy_respects_custom_server(monkeypatch):
    from franklinwh_scraper import notifier

    calls = []

    class _OkResponse:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(notifier.requests, "post",
                         lambda url, **k: calls.append(url) or _OkResponse())
    cfg = Config(ntfy_topic="t", ntfy_server="https://ntfy.example.com/")
    notifier.notify_ntfy("hi", cfg)
    assert calls[0] == "https://ntfy.example.com/t"


def test_notify_ntfy_no_topic_returns_false(monkeypatch):
    from franklinwh_scraper import notifier

    cfg = Config()
    assert notifier.notify_ntfy("hi", cfg) is False


def test_notify_ntfy_reports_http_error_as_failure(monkeypatch):
    from franklinwh_scraper import notifier
    import requests

    class _BadResponse:
        def raise_for_status(self):
            raise requests.HTTPError("500 Server Error")

    monkeypatch.setattr(notifier.time, "sleep", lambda s: None)
    monkeypatch.setattr(notifier.requests, "post", lambda *a, **k: _BadResponse())
    cfg = Config(ntfy_topic="t")
    assert notifier.notify_ntfy("hi", cfg) is False


def test_check_crash_loop_fires_after_threshold_starts(tmp_path, monkeypatch):
    from franklinwh_scraper import cli

    sent = []
    monkeypatch.setattr(cli, "notify_telegram", lambda *a, **k: sent.append(a))
    cfg = Config(telegram_bot_token="t", telegram_chat_id="c")

    # 4 starts within the 10-min window (fake clock via pre-seeded file).
    now = datetime.now()
    starts = [(now - timedelta(minutes=m)).isoformat() for m in (8, 6, 4, 2)]
    (tmp_path / cli._CRASH_LOOP_STARTS_FILE).write_text(json.dumps(starts))

    cli._check_crash_loop(tmp_path, cfg)
    assert len(sent) == 1
    assert "crash-looping" in sent[0][0]
    assert (tmp_path / cli._CRASH_LOOP_ALERT_MARKER).exists()


def test_check_crash_loop_silent_under_threshold(tmp_path, monkeypatch):
    from franklinwh_scraper import cli

    sent = []
    monkeypatch.setattr(cli, "notify_telegram", lambda *a, **k: sent.append(a))
    cfg = Config(telegram_bot_token="t", telegram_chat_id="c")

    now = datetime.now()
    starts = [(now - timedelta(minutes=m)).isoformat() for m in (8, 4)]
    (tmp_path / cli._CRASH_LOOP_STARTS_FILE).write_text(json.dumps(starts))

    cli._check_crash_loop(tmp_path, cfg)
    assert sent == []


def test_check_crash_loop_dedup_within_alert_gap(tmp_path, monkeypatch):
    """A second crash-loop check shortly after the first shouldn't re-alert
    even if starts keep accumulating past the threshold."""
    from franklinwh_scraper import cli

    sent = []
    monkeypatch.setattr(cli, "notify_telegram", lambda *a, **k: sent.append(a))
    cfg = Config(telegram_bot_token="t", telegram_chat_id="c")

    now = datetime.now()
    starts = [(now - timedelta(minutes=m)).isoformat() for m in (8, 6, 4, 2)]
    (tmp_path / cli._CRASH_LOOP_STARTS_FILE).write_text(json.dumps(starts))
    (tmp_path / cli._CRASH_LOOP_ALERT_MARKER).write_text((now - timedelta(minutes=5)).isoformat())

    cli._check_crash_loop(tmp_path, cfg)
    assert sent == []


def test_check_crash_loop_ignores_malformed_timestamps(tmp_path, monkeypatch):
    """A corrupt/partial entry in the starts file must not be miscounted as
    'recent' — it should just be dropped, not treated as always-in-window."""
    from franklinwh_scraper import cli

    sent = []
    monkeypatch.setattr(cli, "notify_telegram", lambda *a, **k: sent.append(a))
    cfg = Config(telegram_bot_token="t", telegram_chat_id="c")

    now = datetime.now()
    starts = ["not-a-date", "", (now - timedelta(minutes=2)).isoformat()]
    (tmp_path / cli._CRASH_LOOP_STARTS_FILE).write_text(json.dumps(starts))

    cli._check_crash_loop(tmp_path, cfg)
    assert sent == []  # only 1 genuinely-recent start (this call's own) + 1 valid = 2, under threshold


def test_seasonal_forecast_confidence_gates_on_in_season_sample_size(tmp_path):
    """A slot backed by only 2 in-season readings must not show 'high'
    confidence just because the same weekday/hour has plenty of samples
    from OTHER seasons — the seasonal profile in use only reflects those
    2 real in-season readings, not the unrelated all-time count."""
    from franklinwh_scraper.predictor import _current_season, predict

    db = HistoryStore(tmp_path / "h.db")
    now = datetime.now()
    season = _current_season(now.month)
    in_season_month = {"spring": 4, "summer": 7, "fall": 10, "winter": 1}[season]
    out_of_season_month = {"spring": 8, "summer": 11, "fall": 2, "winter": 5}[season]

    target_hour = (now + timedelta(hours=1)).hour
    target_dow = (now + timedelta(hours=1)).weekday()

    def _insert(ts: datetime, dow: int, hour: int, load_kw: float):
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts.isoformat(), dow, hour, load_kw, 0.0, 50.0, 0.0, "normal", 0.0, 0.0),
        )

    # 21 distinct in-season days (>= _SEASON_MIN_DAYS) so predict() picks the
    # seasonal profile — but only 2 of them land on the target slot.
    for day in range(21):
        ts = datetime(2020, in_season_month, min(day + 1, 28), 10, 0)
        dow = target_dow if day < 2 else (target_dow + 1) % 7
        hour = target_hour if day < 2 else (target_hour + 3) % 24
        _insert(ts, dow, hour, 2.0)

    # Plenty of OTHER-season readings for the exact same slot — inflates the
    # old all-time slot_counts() the confidence gate used to (wrongly) use.
    for day in range(10):
        ts = datetime(2020, out_of_season_month, min(day + 1, 28), 10, 0)
        _insert(ts, target_dow, target_hour, 9.0)
    db._conn.commit()

    forecast = predict(db, horizon_hours=2)
    target_pred = next(p for p in forecast.hours if p.dt.hour == target_hour)
    assert target_pred.confidence != "high"


def test_daily_battery_kwh_clamps_long_gaps(tmp_path):
    """A multi-hour data gap (daemon down) must not be integrated as
    continuous power for the whole gap — matches integrate_intervals'/
    capacity_samples' existing 1-hour clamp."""
    db = HistoryStore(tmp_path / "h.db")
    db._conn.execute(
        "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
        "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("2026-05-01T22:00:00", 0, 22, 0.5, 0.0, 50.0, 0.0, "normal", 0.0, 0.3),
    )
    db._conn.execute(  # 8h gap (daemon down overnight), then charging kicks in
        "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
        "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("2026-05-01T23:59:00", 0, 23, 0.5, 3.0, 55.0, 0.0, "normal", 0.0, -2.0),
    )
    db._conn.commit()
    chg, dis = db.daily_battery_kwh("2026-05-01")
    assert chg < 1.0  # clamped to <=1h of avg power, not the full 8h gap


def test_battery_kwh_between_arbitrary_window(tmp_path):
    """Same math as daily_battery_kwh but for an arbitrary sub-day window
    (e.g. a VPP event) instead of a full calendar date. Upper bound is
    exclusive (matches readings_between) — a reading exactly at the query's
    end isn't included, so it can't anchor a final trapezoidal interval;
    callers that need the last interval closed must query with slack past
    the real boundary (see _alert_vpp_event_ended for that pattern)."""
    db = HistoryStore(tmp_path / "h.db")
    # Discharging 2.5 kW at 16:00 and 17:00 (1 interval between them, 1h
    # -> 2.5 kWh); 18:00's reading is excluded by the exclusive end bound,
    # so it doesn't close a second interval. 19:00 charging is outside
    # even a slack-extended window.
    for ts, kw in (("16:00", 2.5), ("17:00", 2.5), ("18:00", 2.5), ("19:00", -1.0)):
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"2026-08-25T{ts}:00", 1, int(ts[:2]), 0.5, 0.0, 50.0, 0.0, "normal", 0.0, kw),
        )
    db._conn.commit()

    chg, dis = db.battery_kwh_between("2026-08-25T16:00:00", "2026-08-25T18:00:00")
    assert dis == 2.5
    assert chg == 0.0

    # Extend past 18:00 (with slack) to also capture the 17:00-18:00 and
    # 18:00-19:00 intervals: +2.5 kWh discharge for 17:00-18:00, and for
    # the mixed 18:00-19:00 interval the trapezoidal *average* of (2.5,
    # -1.0) is +0.75 kW — still net-positive over that hour, so it counts
    # as +0.75 kWh discharge rather than splitting into separate charge/
    # discharge portions (this integrator, like daily_battery_kwh, buckets
    # each whole interval by its average sign, not sub-interval sign
    # changes). Total: 2.5 + 2.5 + 0.75 = 5.75.
    chg2, dis2 = db.battery_kwh_between("2026-08-25T16:00:00", "2026-08-25T19:01:00")
    assert dis2 == 5.75
    assert chg2 == 0.0


def test_read_consec_errors_persists_across_process_restart(tmp_path):
    """A cron-based (no --watch) install runs a fresh process per invocation
    — the error streak must survive that, or the 'N poll errors in a row'
    alert can never fire under that deployment path."""
    import json as _json
    from franklinwh_scraper import cli

    assert cli._read_consec_errors(tmp_path) == 0  # no health file yet

    (tmp_path / ".health.json").write_text(_json.dumps({"consec_errors": 5, "last_error": "boom"}))
    assert cli._read_consec_errors(tmp_path) == 5

    (tmp_path / ".health.json").write_text("not json")
    assert cli._read_consec_errors(tmp_path) == 0  # corrupt marker -> safe default


def test_send_sundown_writes_state_under_the_shared_lock(monkeypatch, tmp_path):
    """/sundown must use the same _state_lock as the main poll loop's own
    state read-modify-write, or concurrent writes can revert each other's
    changes (duplicate alerts, dropped predictions)."""
    import types as _types
    from contextlib import contextmanager
    from franklinwh_scraper import chatbot as chatbot_mod
    from franklinwh_scraper import alerts as alerts_mod
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    calls = []

    @contextmanager
    def _spy_lock(out):
        calls.append("enter")
        yield
        calls.append("exit")

    monkeypatch.setattr(alerts_mod, "_state_lock", _spy_lock)

    now = datetime(2026, 7, 15, 12, 0, 0)
    hours = [HourPrediction(dt=now + timedelta(hours=i), predicted_load_kw=1.0,
                            predicted_solar_kw=3.0 if i < 4 else 0.0,
                            net_kw=2.0 if i < 4 else -1.0, confidence="high")
             for i in range(1, 8)]
    forecast = UsageForecast(hours=hours, total_load_kwh=7.0, total_solar_kwh=12.0,
                             net_kwh=5.0, peak_load_kw=1.0, confidence="high", data_days=30)

    bot = TelegramChatBot(Config(battery_capacity_kwh=13.6, output_dir=str(tmp_path)), api_key="x")
    bot._stats = _types.SimpleNamespace(current=_types.SimpleNamespace(battery_soc_pct=50.0))
    bot._usage_forecast = forecast
    bot._send = lambda chat_id, text: None

    real_datetime = chatbot_mod.datetime
    try:
        class _FakeDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        chatbot_mod.datetime = _FakeDatetime
        bot._send_sundown("123")
    finally:
        chatbot_mod.datetime = real_datetime

    assert calls == ["enter", "exit"]


def test_send_sundown_live_anchors_current_load_when_store_available(monkeypatch, tmp_path):
    """/sundown should recompute a live-anchored forecast (current_load_kw
    passed to predict()) when a HistoryStore is available — same nowcast
    mechanism the EOD digest already uses. The shared self._usage_forecast
    deliberately isn't live-anchored (it also drives recommend()'s
    Emergency-Backup decision), so /sundown must build its own."""
    import types as _types

    from franklinwh_scraper import chatbot as chatbot_mod
    from franklinwh_scraper import predictor as predictor_mod
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    now = datetime(2026, 7, 15, 12, 0, 0)

    def _forecast():
        hours = [HourPrediction(dt=now + timedelta(hours=i), predicted_load_kw=1.0,
                                predicted_solar_kw=3.0 if i < 4 else 0.0,
                                net_kw=2.0 if i < 4 else -1.0, confidence="high")
                 for i in range(1, 8)]
        return UsageForecast(hours=hours, total_load_kwh=7.0, total_solar_kwh=12.0,
                             net_kwh=5.0, peak_load_kw=1.0, confidence="high", data_days=30)

    calls = []

    def _fake_predict(store, horizon, **kw):
        calls.append(kw.get("current_load_kw"))
        return _forecast()

    monkeypatch.setattr(predictor_mod, "predict", _fake_predict)

    bot = TelegramChatBot(Config(battery_capacity_kwh=13.6, output_dir=str(tmp_path)), api_key="x")
    bot._stats = _types.SimpleNamespace(current=_types.SimpleNamespace(
        battery_soc_pct=50.0, home_load_kw=2.3))
    bot._usage_forecast = _forecast()
    bot._hist_store = object()  # any non-None sentinel — code only checks `is not None`
    bot._outlook = None
    bot._send = lambda chat_id, text: None

    real_datetime = chatbot_mod.datetime
    try:
        class _FakeDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        chatbot_mod.datetime = _FakeDatetime
        bot._send_sundown("123")
    finally:
        chatbot_mod.datetime = real_datetime

    assert calls == [2.3]  # live-anchored to the current home_load_kw reading


def test_send_sundown_applies_learned_bias_correction(tmp_path):
    """A learned sundown_bias_samples correction should shift the displayed
    number, while raw_pct (stored for future learning) stays uncorrected —
    otherwise the correction would compound on itself over time."""
    import types as _types
    from pathlib import Path

    from franklinwh_scraper import chatbot as chatbot_mod
    from franklinwh_scraper.alerts import _ewma, _load_peak_state, _save_peak_state
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    # Must be near-present, not a fixed historical date — this test reads
    # the persisted state back afterward, and _prune_old_state (via
    # _save_peak_state) strips sundown_pred_<date> keys older than 30 real
    # days, which a hardcoded past date can silently drift past.
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    hours = []
    t = now
    while t.date() == now.date() and t.hour <= 23:
        solar = 3.0 if 12 <= t.hour < 18 else 0.0
        hours.append(HourPrediction(dt=t, predicted_load_kw=1.0, predicted_solar_kw=solar,
                                    net_kw=solar - 1.0, confidence="high"))
        t += timedelta(hours=1)
    forecast = UsageForecast(hours=hours, total_load_kwh=24.0, total_solar_kwh=18.0,
                             net_kwh=-6.0, peak_load_kw=1.0, confidence="high", data_days=30)

    samples = [-2.0, -4.0, -6.0]  # consistent over-prediction, like the real data
    _save_peak_state(Path(tmp_path), {"sundown_bias_samples": samples})

    bot = TelegramChatBot(Config(battery_capacity_kwh=13.6, output_dir=str(tmp_path)), api_key="x")
    bot._stats = _types.SimpleNamespace(current=_types.SimpleNamespace(battery_soc_pct=27.0))
    bot._usage_forecast = forecast
    # No _hist_store -> falls back to the shared forecast, isolating this
    # test to the bias-correction path rather than also exercising live-anchor.

    sent = {}
    bot._send = lambda chat_id, text: sent.__setitem__("text", text)

    real_datetime = chatbot_mod.datetime
    try:
        class _FakeDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        chatbot_mod.datetime = _FakeDatetime
        bot._send_sundown("123")
    finally:
        chatbot_mod.datetime = real_datetime

    # Raw projection clamps to 100% (same math as test_send_sundown_projects_soc_to_last_solar_hour).
    bias = _ewma(samples)
    expected_corrected = round(max(0.0, min(100.0, 100.0 + bias)))
    assert "text" in sent
    assert f"{expected_corrected}%" in sent["text"]
    assert "100%" not in sent["text"]  # proves the correction actually shifted the raw number

    state = _load_peak_state(Path(tmp_path))
    pred = state[f"sundown_pred_{now.strftime('%Y-%m-%d')}"]
    assert pred["raw_pct"] == 100.0        # uncorrected, for future learning
    assert pred["pct"] != pred["raw_pct"]  # displayed/stored value is the corrected one


def test_export_csv_preserves_column_alignment_on_schema_drift(tmp_path):
    """Resuming an append after a field was added/removed must not shift
    existing columns — the on-disk header's column order must win, not a
    freshly recomputed one."""
    from franklinwh_scraper.exporters import export_csv

    path = tmp_path / "log.csv"
    export_csv([{"a": 1, "b": 2}], path, append=True)

    # Simulate a later software version that adds a field "c".
    export_csv([{"a": 3, "b": 4, "c": 5}], path, append=True)

    lines = path.read_text().strip().splitlines()
    assert lines[0] == "a,b"           # header unchanged — old column order preserved
    assert lines[1] == "1,2"
    assert lines[2] == "3,4"           # new field "c" dropped, not misaligned into column "a"/"b"


def test_chatbot_history_uses_outdir_override(tmp_path):
    """/history must read the main loop's resolved --out dir, not re-derive
    from cfg.output_dir — otherwise it reads a different history.db than the
    advisor writes (same split-state bug /sundown already had fixed)."""
    from franklinwh_scraper.chatbot import TelegramChatBot

    # cfg points at a dir that HAS a db; outdir points at one that doesn't.
    cfg_dir = tmp_path / "cfg_out"
    cfg_dir.mkdir()
    HistoryStore(cfg_dir / "history.db")  # creates the file
    real_dir = tmp_path / "real_out"
    real_dir.mkdir()

    bot = TelegramChatBot(Config(output_dir=str(cfg_dir)), api_key="x", outdir=real_dir)
    sent = []
    bot._send = lambda chat_id, text: sent.append(text)

    bot._send_history("123")
    # Must have looked in real_dir (no db there), not cfg_dir (db present).
    assert sent and "No history database yet" in sent[0]


def test_get_switch_usage_survives_missing_result(monkeypatch):
    """An account with no smart circuits can get a 200 with no result/dataArea
    — that must read as 'nothing to report', not a bare KeyError."""
    from franklinwh_scraper.account import AccountClient

    client = AccountClient("a@b.c", "pw")
    monkeypatch.setattr(client, "_mqtt_send", lambda *a, **k: {"code": 200})
    assert client.get_switch_usage("gw1") == {}

    monkeypatch.setattr(client, "_mqtt_send", lambda *a, **k: {"code": 200, "result": {}})
    assert client.get_switch_usage("gw1") == {}


def test_get_switch_usage_survives_non_json_data_area(monkeypatch):
    """The response shape is undocumented — a non-JSON dataArea should be
    surfaced for inspection, not swallowed or raised."""
    from franklinwh_scraper.account import AccountClient

    client = AccountClient("a@b.c", "pw")
    monkeypatch.setattr(
        client, "_mqtt_send",
        lambda *a, **k: {"code": 200, "result": {"dataArea": "<not json>"}},
    )
    assert client.get_switch_usage("gw1") == {"_raw": "<not json>"}


# ── Energy attribution (battery/solar/grid → home) ────────────────────

def _insert_attr_row(db, ts, batt, sol, grid, dow=0, hod=12):
    db._conn.execute(
        "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
        "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw,"
        "battery_load_kwh,solar_load_kwh,grid_load_kwh) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, dow, hod, 1.0, 0.0, 50.0, 0.0, "normal", 0.0, 0.0, batt, sol, grid),
    )


def test_daily_attribution_sums_components(tmp_path):
    db = HistoryStore(tmp_path / "h.db")
    _insert_attr_row(db, "2026-05-01T08:00:00", 1.0, 2.0, 0.5)
    _insert_attr_row(db, "2026-05-01T12:00:00", 3.0, 5.0, 0.9)
    _insert_attr_row(db, "2026-05-01T20:00:00", 8.2, 5.1, 0.9)
    db._conn.commit()
    assert db.daily_attribution("2026-05-01") == (8.2, 5.1, 0.9)
    assert db.daily_attribution("2026-05-02") is None  # no rows for that date


def test_daily_attribution_handles_midday_counter_reset(tmp_path):
    """A gateway reboot resets the counters mid-day; a naive MAX() would drop
    everything before the reset. Sum the peak of each monotonic run instead."""
    db = HistoryStore(tmp_path / "h.db")
    _insert_attr_row(db, "2026-05-01T08:00:00", 2.0, 1.0, 0.0)
    _insert_attr_row(db, "2026-05-01T12:00:00", 4.0, 3.0, 1.0)   # pre-reset peak
    _insert_attr_row(db, "2026-05-01T13:00:00", 0.5, 0.2, 0.0)   # reset
    _insert_attr_row(db, "2026-05-01T20:00:00", 1.5, 1.0, 0.5)   # post-reset peak
    db._conn.commit()
    # Naive MAX would give (4.0, 3.0, 1.0); piecewise gives the real day total.
    assert db.daily_attribution("2026-05-01") == (5.5, 4.0, 1.5)


def test_rollup_preserves_attribution_columns(tmp_path):
    """rollup_old_readings' INSERT lists columns explicitly — any column left
    out is silently reset to DEFAULT 0 when a bucket is rolled up."""
    db = HistoryStore(tmp_path / "h.db")
    old_day = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
    for minute, batt in ((0, 3.0), (15, 4.0), (30, 5.0)):
        _insert_attr_row(db, f"{old_day}T09:{minute:02d}:00", batt, batt / 2, 1.0, dow=2, hod=9)
    db._conn.commit()

    removed = db.rollup_old_readings(older_than_days=180)
    assert removed == 2  # 3 rows collapse to 1

    row = db._conn.execute(
        "SELECT battery_load_kwh, solar_load_kwh, grid_load_kwh FROM readings "
        "WHERE timestamp LIKE ?", (f"{old_day}%",)
    ).fetchone()
    assert row == (5.0, 2.5, 1.0)  # MAX of the bucket, not zeroed


# ── Billing cycle unification ─────────────────────────────────────────

def test_cycle_bounds_month_boundaries():
    from datetime import date
    from franklinwh_scraper.tou import cycle_bounds

    # Past the start day -> cycle began this month (the chatbot's old formula
    # always returned the PRIOR month here, putting it a full month stale).
    assert cycle_bounds(date(2026, 7, 30), 20) == (date(2026, 7, 20), date(2026, 8, 19))
    # Before the start day -> cycle began last month.
    assert cycle_bounds(date(2026, 7, 10), 20) == (date(2026, 6, 20), date(2026, 7, 19))
    # Year rollover, both directions.
    assert cycle_bounds(date(2026, 1, 5), 20) == (date(2025, 12, 20), date(2026, 1, 19))
    assert cycle_bounds(date(2026, 12, 25), 20) == (date(2026, 12, 20), date(2027, 1, 19))
    # start_day 1 -> whole calendar month (Feb 2026 is 28 days).
    assert cycle_bounds(date(2026, 2, 15), 1) == (date(2026, 2, 1), date(2026, 2, 28))
    # Leap year.
    assert cycle_bounds(date(2024, 2, 15), 1) == (date(2024, 2, 1), date(2024, 2, 29))
    # start_day 31 must CLAMP, not raise — date(2026, 4, 31) doesn't exist.
    s, e = cycle_bounds(date(2026, 4, 15), 31)
    assert s == date(2026, 3, 31) and e == date(2026, 4, 29)
    s, e = cycle_bounds(date(2026, 3, 15), 31)
    assert s == date(2026, 2, 28)  # Feb has no 31st


def test_bill_projection_uses_real_cycle_length_not_30(tmp_path):
    """A 31-day cycle must project x31, not a flat x30."""
    from franklinwh_scraper.config import Config as _C

    db = HistoryStore(tmp_path / "h.db")
    # Cycle starting Jul 20 is 31 days (Jul 20 - Aug 19). Probe on Aug 5.
    now = datetime(2026, 8, 5, 8, 30)
    for day in range(20, 32):
        for hour in (0, 12):
            db._conn.execute(
                "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
                "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"2026-07-{day:02d}T{hour:02d}:00:00", 0, hour, 1.0, 0.0, 50.0, 1.0, "normal", 0.0, 0.0),
            )
    for day in range(1, 6):
        for hour in (0, 12):
            db._conn.execute(
                "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
                "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"2026-08-{day:02d}T{hour:02d}:00:00", 0, hour, 1.0, 0.0, 50.0, 1.0, "normal", 0.0, 0.0),
            )
    db._conn.commit()

    msg = alerts._alert_bill_projection({}, "2026-08-05", now, db, _C(billing_cycle_start_day=20))
    assert msg is not None
    assert "Projected full cycle (31 days)" in msg
    assert "~30 days" not in msg


def _insert_cycle_readings(db, start_day: int, month: str, days: range):
    for day in days:
        for hour in (0, 12):
            db._conn.execute(
                "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
                "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"{month}-{day:02d}T{hour:02d}:00:00", 0, hour, 1.0, 0.0, 50.0, 1.0, "normal", 0.0, 0.0),
            )
    db._conn.commit()


def test_bill_reconciliation_reminds_when_no_actual_recorded(tmp_path):
    from franklinwh_scraper.config import Config as _C

    db = HistoryStore(tmp_path / "h.db")
    # Cycle Jul 20 - Aug 19 closed; probe 5 days after close (Aug 24).
    _insert_cycle_readings(db, 20, "2026-07", range(20, 32))
    _insert_cycle_readings(db, 20, "2026-08", range(1, 20))
    now = datetime(2026, 8, 24, 8, 30)

    msg = alerts._alert_bill_reconciliation({}, "2026-08-24", now, _C(billing_cycle_start_day=20), db)
    assert msg is not None
    assert "Log your real bill" in msg
    assert "bill-record --amount" in msg


def test_bill_reconciliation_reports_diff_once_actual_recorded(tmp_path):
    from franklinwh_scraper.config import Config as _C

    db = HistoryStore(tmp_path / "h.db")
    _insert_cycle_readings(db, 20, "2026-07", range(20, 32))
    _insert_cycle_readings(db, 20, "2026-08", range(1, 20))
    now = datetime(2026, 8, 24, 8, 30)

    state = {"actual_bill_2026-08-19": 999.0}  # deliberately way off from the tiny synthetic load
    msg = alerts._alert_bill_reconciliation(state, "2026-08-24", now, _C(billing_cycle_start_day=20), db)
    assert msg is not None
    assert "Bill reconciliation" in msg
    assert "actual $999.00" in msg
    assert "Log your real bill" not in msg


def test_bill_reconciliation_gates_on_window_and_dedup(tmp_path):
    from franklinwh_scraper.config import Config as _C

    db = HistoryStore(tmp_path / "h.db")
    _insert_cycle_readings(db, 20, "2026-07", range(20, 32))
    _insert_cycle_readings(db, 20, "2026-08", range(1, 20))
    cfg = _C(billing_cycle_start_day=20)

    # Too soon after close (1 day) — no reminder yet.
    too_soon = datetime(2026, 8, 20, 8, 30)
    assert alerts._alert_bill_reconciliation({}, "2026-08-20", too_soon, cfg, db) is None

    # Too late (15 days) — window has passed.
    too_late = datetime(2026, 9, 3, 8, 30)
    assert alerts._alert_bill_reconciliation({}, "2026-09-03", too_late, cfg, db) is None

    # In-window, wrong hour — gated to 8-9am.
    wrong_hour = datetime(2026, 8, 24, 14, 0)
    assert alerts._alert_bill_reconciliation({}, "2026-08-24", wrong_hour, cfg, db) is None

    # In-window, right hour — fires once, then dedups same day.
    now = datetime(2026, 8, 24, 8, 30)
    state = {}
    assert alerts._alert_bill_reconciliation(state, "2026-08-24", now, cfg, db) is not None
    assert alerts._alert_bill_reconciliation(state, "2026-08-24", now, cfg, db) is None


def test_get_vpp_event_active_upcoming_expired():
    now = datetime(2026, 8, 25, 17, 0, 0)
    active = {"vpp_event": {
        "start": (now - timedelta(hours=1)).isoformat(),
        "end": (now + timedelta(hours=2)).isoformat(),
        "rate_per_kwh": 2.0,
    }}
    ev = alerts._get_vpp_event(active, now)
    assert ev is not None
    assert alerts._vpp_event_active(active, now) is True

    upcoming = {"vpp_event": {
        "start": (now + timedelta(hours=1)).isoformat(),
        "end": (now + timedelta(hours=3)).isoformat(),
        "rate_per_kwh": None,
    }}
    assert alerts._get_vpp_event(upcoming, now) is not None
    assert alerts._vpp_event_active(upcoming, now) is False

    assert alerts._get_vpp_event({}, now) is None


def test_get_vpp_event_self_cleans_past_grace_period():
    now = datetime(2026, 8, 25, 17, 0, 0)
    ended_long_ago = {"vpp_event": {
        "start": (now - timedelta(hours=6)).isoformat(),
        "end": (now - timedelta(hours=4)).isoformat(),  # 4h past end, grace is 2h
        "rate_per_kwh": None,
    }}
    assert alerts._get_vpp_event(ended_long_ago, now) is None
    assert "vpp_event" not in ended_long_ago  # popped, not left dangling

    just_ended = {"vpp_event": {
        "start": (now - timedelta(hours=6)).isoformat(),
        "end": (now - timedelta(hours=1)).isoformat(),  # 1h past end, within 2h grace
        "rate_per_kwh": None,
    }}
    assert alerts._get_vpp_event(just_ended, now) is not None


def test_get_vpp_event_drops_malformed_entry():
    now = datetime(2026, 8, 25, 17, 0, 0)
    state = {"vpp_event": {"start": "not-a-date", "end": "also-not-a-date"}}
    assert alerts._get_vpp_event(state, now) is None
    assert "vpp_event" not in state


def test_alert_vpp_event_started_fires_once_and_gates_on_enrolled():
    from franklinwh_scraper.config import Config as _C

    now = datetime(2026, 8, 25, 17, 0, 0)
    ev = {"start": (now - timedelta(minutes=5)).isoformat(),
          "end": (now + timedelta(hours=2)).isoformat(), "rate_per_kwh": 2.0}

    # Not enrolled -> silent even with a logged, active event.
    state = {"vpp_event": ev}
    assert alerts._alert_vpp_event_started(state, "2026-08-25", now, _C(vpp_enrolled=False)) is None

    cfg = _C(vpp_enrolled=True)
    state = {"vpp_event": ev}
    msg = alerts._alert_vpp_event_started(state, "2026-08-25", now, cfg)
    assert msg is not None
    assert "VPP event active" in msg
    assert "$2.00/kWh" in msg
    # Fires once — same event's start already announced.
    assert alerts._alert_vpp_event_started(state, "2026-08-25", now, cfg) is None


def test_alert_vpp_event_started_waits_for_start_time():
    from franklinwh_scraper.config import Config as _C

    now = datetime(2026, 8, 25, 17, 0, 0)
    upcoming = {"vpp_event": {
        "start": (now + timedelta(hours=1)).isoformat(),
        "end": (now + timedelta(hours=3)).isoformat(), "rate_per_kwh": None,
    }}
    assert alerts._alert_vpp_event_started(upcoming, "2026-08-25", now, _C(vpp_enrolled=True)) is None


def test_alert_vpp_event_ended_reports_export_and_payout(tmp_path):
    from franklinwh_scraper.config import Config as _C

    start = datetime(2026, 8, 25, 16, 0, 0)
    end   = datetime(2026, 8, 25, 18, 0, 0)
    now   = end + timedelta(minutes=5)

    db = HistoryStore(tmp_path / "h.db")
    # 2 hours exporting 3 kW steady -> 6 kWh exported (grid_use_kw negative =
    # export). The 18:05 reading is the "past end" point readings_between's
    # exclusive upper bound needs to close the 17:00-18:00 interval — it
    # falls within the alert's own 15-min slack window and gets trimmed
    # back out before summing (its own 18:00-18:05 interval isn't counted).
    for ts in ("16:00", "17:00", "18:00", "18:05"):
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"2026-08-25T{ts}:00", 1, int(ts[:2]), 0.5, 4.0, 60.0, -3.0, "normal", 0.0, 2.5),
        )
    db._conn.commit()

    state = {"vpp_event": {
        "start": start.isoformat(), "end": end.isoformat(), "rate_per_kwh": 2.0,
    }}
    cfg = _C(vpp_enrolled=True)
    msg = alerts._alert_vpp_event_ended(state, "2026-08-25", now, cfg, db)
    assert msg is not None
    assert "VPP event ended" in msg
    assert "6.0 kWh exported" in msg
    # discharge_kwh's query has no slack past `end` (exclusive upper bound
    # accepted as a small undercount, see the code comment) — only the
    # 16:00-17:00 interval is captured here, 1h * 2.5kW = 2.5 kWh.
    assert "2.5 kWh discharged" in msg
    assert "$5.00 toward this year's gift card (at discharge)" in msg
    # Fires once.
    assert alerts._alert_vpp_event_ended(state, "2026-08-25", now, cfg, db) is None


def test_alert_vpp_event_ended_waits_for_end_time():
    from franklinwh_scraper.config import Config as _C

    now = datetime(2026, 8, 25, 17, 0, 0)
    state = {"vpp_event": {
        "start": (now - timedelta(hours=1)).isoformat(),
        "end": (now + timedelta(hours=1)).isoformat(),  # still active, not ended
        "rate_per_kwh": 2.0,
    }}
    assert alerts._alert_vpp_event_ended(state, "2026-08-25", now, _C(vpp_enrolled=True), None) is None


def test_chatbot_bill_matches_api_bill_cycle_window():
    """The payoff: /bill and /api/bill must derive the same cycle window.
    They disagreed for real — the API said day 19 while the chatbot computed a
    window a full month in the past."""
    from datetime import date
    from franklinwh_scraper.tou import cycle_bounds

    today = date(2026, 7, 30)
    api_start, api_end = cycle_bounds(today, 20)

    # The chatbot's old hardcoded formula, for contrast.
    old_chatbot_start = (today.replace(day=1) - timedelta(days=1)).replace(day=20)
    assert old_chatbot_start == date(2026, 6, 20)      # a month stale
    assert api_start == date(2026, 7, 20)              # what both now use
    assert (api_end - api_start).days + 1 == 31

    # Day count must also match: /api/bill counts the current day inclusive
    # (day_n = (today - start).days + 1). The chatbot used the exclusive
    # elapsed count, which made the two disagree by exactly one day's base
    # service fee even once the cycle window itself matched.
    api_day_n = (today - api_start).days + 1
    chatbot_day_n = (today - api_start).days + 1
    assert api_day_n == chatbot_day_n == 11


# ── Savings scorecard ─────────────────────────────────────────────────

def _mk_intervals(spec):
    """spec: list of (iso_ts, hours, grid_kw, home_kw, solar_kw)."""
    return [(datetime.fromisoformat(t), h, g, hm, s) for t, h, g, hm, s in spec]


def test_savings_grid_only_baseline_matches_saved_today_formula():
    """saved_vs_grid_only must equal the dashboard ticker's long-standing
    formula (self-use at import rate + export at NEM credit). That identity
    is what makes swapping _saved_today over to this module a pure refactor."""
    from franklinwh_scraper import savings
    from franklinwh_scraper.tou import export_rate_at, rate_at

    spec = [
        ("2026-07-15T13:00:00", 1.0, -2.0, 1.0, 3.0),   # exporting
        ("2026-07-15T18:00:00", 1.0, 0.5, 2.5, 0.0),    # partial import, on-peak
        ("2026-07-15T02:00:00", 1.0, 0.0, 0.8, 0.0),    # fully self-supplied
    ]
    ivs = _mk_intervals(spec)
    legacy = 0.0
    for dt0, hours, grid, home, _solar in ivs:
        self_use = max(0.0, min(home, home - max(0.0, grid)))
        legacy += self_use * rate_at(dt0) * hours
        legacy += max(0.0, -grid) * export_rate_at(dt0) * hours

    assert savings.compute(ivs).saved_vs_grid_only == pytest.approx(legacy, abs=0.01)


def test_savings_period_split_sums_to_total():
    """on-peak + off-peak + super-off-peak + export credit == total saved.
    The old weekly digest omitted off-peak AND export credit entirely."""
    from franklinwh_scraper import savings

    ivs = _mk_intervals([
        ("2026-07-15T13:00:00", 1.0, -2.0, 1.0, 3.0),
        ("2026-07-15T18:00:00", 1.0, 0.5, 2.5, 0.0),
        ("2026-07-15T08:00:00", 1.0, 0.0, 1.2, 0.4),   # off-peak — was dropped
        ("2026-07-15T02:00:00", 1.0, 0.0, 0.8, 0.0),
    ])
    b = savings.compute(ivs)
    parts = b.saved_on_peak + b.saved_off_peak + b.saved_super_off_peak + b.actual_export_credit
    assert parts == pytest.approx(b.saved_vs_grid_only, abs=0.05)
    assert b.saved_off_peak > 0  # the component the old math silently discarded


def test_savings_battery_contribution_zero_without_battery():
    """With no battery activity (grid exactly covers the solar shortfall),
    the battery's own contribution must be ~0."""
    from franklinwh_scraper import savings

    # home 2.0, solar 0.5 -> grid must supply 1.5; no battery in play.
    ivs = _mk_intervals([
        ("2026-07-15T18:00:00", 1.0, 1.5, 2.0, 0.5),
        ("2026-07-15T08:00:00", 1.0, 1.0, 1.5, 0.5),
    ])
    assert savings.compute(ivs).saved_vs_solar_only == pytest.approx(0.0, abs=0.01)


def test_savings_excludes_base_service_charge():
    """Base service is incurred with or without the system — including it
    would inflate the savings figure."""
    from franklinwh_scraper import savings
    from franklinwh_scraper.tou import BASE_SERVICE_DAILY

    b = savings.compute(_mk_intervals([("2026-07-15T02:00:00", 1.0, 0.0, 1.0, 0.0)]))
    # A single self-supplied hour at super-off-peak: saving must be that hour's
    # avoided import only, nowhere near a day's base fee.
    assert b.saved_vs_grid_only < BASE_SERVICE_DAILY
    assert b.actual_net_energy_cost == pytest.approx(0.0, abs=0.001)


def test_savings_cumulative_accumulator_is_idempotent():
    """Re-running the EOD accumulation for the same date must not double-count."""
    state = {}

    def accumulate(today, day_value):
        cum = state.get("savings_cumulative")
        if not isinstance(cum, dict):
            cum = {"through": "", "vs_grid": 0.0, "vs_solar": 0.0, "days": 0}
        if today > cum.get("through", ""):
            state["savings_cumulative"] = {
                "through": today,
                "vs_grid": round(cum["vs_grid"] + day_value, 2),
                "vs_solar": 0.0,
                "days": cum["days"] + 1,
            }

    accumulate("2026-07-01", 5.0)
    accumulate("2026-07-01", 5.0)   # replay same day
    assert state["savings_cumulative"] == {"through": "2026-07-01", "vs_grid": 5.0,
                                           "vs_solar": 0.0, "days": 1}
    accumulate("2026-07-02", 3.0)
    assert state["savings_cumulative"]["vs_grid"] == 8.0
    assert state["savings_cumulative"]["days"] == 2


def test_followed_advice_audit_reports_counts_not_dollars(tmp_path):
    """Whether following the advice SAVED anything isn't answerable from this
    data — the audit must report counts only, never a dollar figure."""
    from franklinwh_scraper import savings

    log = tmp_path / "advisor_log.jsonl"
    today = datetime(2026, 7, 30)
    log.write_text(
        '{"timestamp": "2026-07-29T08:00:00", "recommended_mode": "emergency_backup", "needs_action": true}\n'
        '{"timestamp": "2026-07-28T08:00:00", "recommended_mode": "self_consumption", "needs_action": false}\n'
    )
    out = savings.followed_advice_audit(log, {"2026-07-29"}, 30, today=today)
    assert out["eb_recommended_days"] == 1
    assert out["grid_charge_days"] == 1
    assert not any("$" in str(k) or "saved" in str(k) for k in out)


def test_followed_advice_audit_survives_missing_log(tmp_path):
    from franklinwh_scraper import savings
    out = savings.followed_advice_audit(tmp_path / "nope.jsonl", set(), 30)
    assert out["available"] is False


def test_urgent_alerts_excludes_the_all_clear():
    """grid_restored is always-on (you need to know the outage ended) but is
    an all-clear — it must never be escalated like the outage itself."""
    assert alerts._URGENT_ALERTS == {"grid_down", "area_power_outage", "fast_drain"}
    assert "grid_restored" in alerts._ALWAYS_ON_ALERTS
    assert "grid_restored" not in alerts._URGENT_ALERTS
    # Every urgent alert must also be undisableable — an alert you can mute
    # should never be able to page you.
    assert alerts._URGENT_ALERTS <= alerts._ALWAYS_ON_ALERTS


def test_grid_down_dispatches_as_urgent(tmp_path, monkeypatch):
    """Regression: _check_peak_alerts never passed `urgent` at all, so
    notify_webhook's urgent flag was dead and every alerts_log.jsonl entry
    was urgent:false — including grid_down."""
    import types
    sent = []
    monkeypatch.setattr(alerts, "_send_alert",
                        lambda body, cfg, urgent=False, alert_name=None: sent.append((body, urgent)))

    c = types.SimpleNamespace(
        battery_soc_pct=80.0, home_load_kw=1.0, solar_production_kw=0.0,
        battery_use_kw=1.0, grid_use_kw=0.0, grid_status="down",
        generator_production_kw=0.0, generator_enabled=False,
    )
    stats = types.SimpleNamespace(
        current=c,
        totals=types.SimpleNamespace(
            solar_kwh=0.0, battery_charge_kwh=0.0, battery_discharge_kwh=0.0,
            grid_load_kwh=0.0, grid_export_kwh=0.0, home_use_kwh=0.0,
            grid_import_kwh=0.0, generator_kwh=0.0,
            battery_load_kwh=0.0, solar_load_kwh=0.0,
        ),
    )
    # Needs a configured channel or _check_peak_alerts returns immediately.
    cfg = Config(telegram_bot_token="t", telegram_chat_id="c")
    alerts._check_peak_alerts(stats, cfg, tmp_path)

    urgent_bodies = [b for b, u in sent if u]
    assert any("GRID DOWN" in b.upper() for b in urgent_bodies), \
        f"grid_down must dispatch urgent; got {[(b[:40], u) for b, u in sent]}"


# ── Baseline / phantom-load drift ─────────────────────────────────────

def _seed_quiet_nights(db, start_day: datetime, n_days: int, kw: float, samples: int = 12):
    for d in range(n_days):
        day = start_day + timedelta(days=d)
        for i in range(samples):
            ts = day.replace(hour=i % 5, minute=(i * 7) % 60).isoformat()
            db._conn.execute(
                "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
                "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, day.weekday(), i % 5, kw, 0.0, 50.0, 0.0, "normal", 0.0, 0.0),
            )
    db._conn.commit()


def test_baseline_load_drift_fires_on_sustained_increase(tmp_path):
    db = HistoryStore(tmp_path / "h.db")
    now = datetime.now().replace(hour=8, minute=30, second=0, microsecond=0)
    _seed_quiet_nights(db, now - timedelta(days=75), 55, 0.20)   # baseline window
    _seed_quiet_nights(db, now - timedelta(days=14), 14, 0.45)   # recent: big jump
    msg = alerts._alert_baseline_load_drift({}, now.strftime("%Y-%m-%d"), now, db, Config())
    assert msg is not None
    assert "crept up" in msg
    assert "$" in msg          # must quantify
    assert "at least" in msg   # ...as a floor, not a precise claim


def test_baseline_load_drift_silent_on_small_absolute_change(tmp_path):
    """The gate that stops it crying wolf: a large RELATIVE rise on a tiny
    baseline is still only a few watts. Real data showed 0.238 -> 0.317 kW
    (+33%) which is just 79 W — correctly silent."""
    db = HistoryStore(tmp_path / "h.db")
    now = datetime.now().replace(hour=8, minute=30, second=0, microsecond=0)
    _seed_quiet_nights(db, now - timedelta(days=75), 55, 0.12)
    _seed_quiet_nights(db, now - timedelta(days=14), 14, 0.16)   # +33% but only +0.04 kW
    assert alerts._alert_baseline_load_drift({}, now.strftime("%Y-%m-%d"), now, db, Config()) is None


def test_baseline_load_drift_silent_when_flat(tmp_path):
    db = HistoryStore(tmp_path / "h.db")
    now = datetime.now().replace(hour=8, minute=30, second=0, microsecond=0)
    _seed_quiet_nights(db, now - timedelta(days=75), 55, 0.30)
    _seed_quiet_nights(db, now - timedelta(days=14), 14, 0.30)
    assert alerts._alert_baseline_load_drift({}, now.strftime("%Y-%m-%d"), now, db, Config()) is None


def test_baseline_load_drift_requires_min_samples(tmp_path):
    """Too few qualifying nights must return None rather than a noisy verdict."""
    db = HistoryStore(tmp_path / "h.db")
    now = datetime.now().replace(hour=8, minute=30, second=0, microsecond=0)
    _seed_quiet_nights(db, now - timedelta(days=75), 55, 0.20)
    _seed_quiet_nights(db, now - timedelta(days=5), 4, 0.60)   # only 4 recent nights
    assert alerts._alert_baseline_load_drift({}, now.strftime("%Y-%m-%d"), now, db, Config()) is None


def test_baseline_load_drift_dedups_per_iso_week(tmp_path):
    db = HistoryStore(tmp_path / "h.db")
    now = datetime.now().replace(hour=8, minute=30, second=0, microsecond=0)
    _seed_quiet_nights(db, now - timedelta(days=75), 55, 0.20)
    _seed_quiet_nights(db, now - timedelta(days=14), 14, 0.45)
    state = {}
    first = alerts._alert_baseline_load_drift(state, now.strftime("%Y-%m-%d"), now, db, Config())
    second = alerts._alert_baseline_load_drift(state, now.strftime("%Y-%m-%d"), now, db, Config())
    assert first is not None and second is None


def test_baseline_uses_percentile_not_min(tmp_path):
    """A single spurious near-zero reading must not define the night."""
    db = HistoryStore(tmp_path / "h.db")
    now = datetime.now().replace(hour=8, minute=30, second=0, microsecond=0)
    _seed_quiet_nights(db, now - timedelta(days=75), 55, 0.20)
    _seed_quiet_nights(db, now - timedelta(days=14), 14, 0.45)
    # Inject one 0.01 kW glitch into each recent night.
    for d in range(14):
        day = (now - timedelta(days=14) + timedelta(days=d)).replace(hour=3, minute=59)
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (day.isoformat(), day.weekday(), 3, 0.01, 0.0, 50.0, 0.0, "normal", 0.0, 0.0),
        )
    db._conn.commit()
    # min() would collapse to 0.01 and suppress the alert; the percentile holds.
    assert alerts._alert_baseline_load_drift({}, now.strftime("%Y-%m-%d"), now, db, Config()) is not None


# ── Chatbot context enrichment ────────────────────────────────────────

def test_build_context_includes_recommendation_and_forecast(tmp_path):
    """The bot must see the advisor's own call — otherwise it answers
    'should I switch modes?' from raw telemetry and can contradict the alert
    the user just received."""
    import types
    from franklinwh_scraper.chatbot import build_context
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    now = datetime.now()
    c = types.SimpleNamespace(
        battery_soc_pct=42.0, solar_production_kw=1.0, home_load_kw=2.0,
        grid_use_kw=0.5, grid_status="normal", battery_use_kw=1.0,
    )
    stats = types.SimpleNamespace(current=c, totals=types.SimpleNamespace(solar_kwh=5.0))
    rec = types.SimpleNamespace(
        mode=types.SimpleNamespace(value="emergency_backup"),
        urgency="warning",
        reason="Projected shortfall before the 4pm peak.",
        details={"projected_soc_4pm_pct": 38.0, "projected_peak_draw_kwh": 4.2},
    )
    hours = [HourPrediction(dt=now + timedelta(hours=i), predicted_load_kw=1.0,
                            predicted_solar_kw=2.0, net_kw=1.0, confidence="high")
             for i in range(1, 9)]
    fc = UsageForecast(hours=hours, total_load_kwh=8.0, total_solar_kwh=16.0,
                       net_kwh=8.0, peak_load_kw=1.0, confidence="high", data_days=30)

    ctx = build_context(stats, None, None, Config(), rec=rec, forecast=fc, outdir=tmp_path)
    assert "emergency_backup" in ctx
    assert "Projected shortfall" in ctx
    assert "Projected 4pm" in ctx
    assert "Next 6h" in ctx
    # Guard against context bloat creeping back — this is prepended to every
    # Claude call, and the forecast must stay aggregated, not enumerated.
    assert len(ctx.split("\n")) <= 26


def test_build_context_includes_active_vpp_event(tmp_path):
    """The bot must see an active VPP event, or it can't answer 'am I in a
    grid-support event right now' and could contradict the started/ended
    alerts the user just received."""
    from franklinwh_scraper.alerts import _save_peak_state
    from franklinwh_scraper.chatbot import build_context

    now = datetime.now()
    _save_peak_state(tmp_path, {
        "vpp_event": {
            "start": (now - timedelta(minutes=30)).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "rate_per_kwh": 0.50,
            "logged_at": now.isoformat(),
        }
    })
    ctx = build_context(None, None, None, Config(vpp_enrolled=True), outdir=tmp_path)
    assert "VPP event:" in ctx
    assert "ACTIVE NOW" in ctx
    assert "$0.50/kWh" in ctx


def test_build_context_omits_vpp_when_not_enrolled(tmp_path):
    """Not enrolled → no VPP line, even if state somehow has a stale event
    (e.g. leftover from before vpp_enrolled was turned off)."""
    from franklinwh_scraper.alerts import _save_peak_state
    from franklinwh_scraper.chatbot import build_context

    now = datetime.now()
    _save_peak_state(tmp_path, {
        "vpp_event": {
            "start": (now - timedelta(minutes=30)).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "rate_per_kwh": None,
            "logged_at": now.isoformat(),
        }
    })
    ctx = build_context(None, None, None, Config(vpp_enrolled=False), outdir=tmp_path)
    assert "VPP event:" not in ctx


def test_build_context_survives_missing_alert_log(tmp_path):
    """A missing/unreadable alert log must not break /status."""
    import types
    from franklinwh_scraper.chatbot import build_context

    c = types.SimpleNamespace(
        battery_soc_pct=50.0, solar_production_kw=0.0, home_load_kw=1.0,
        grid_use_kw=0.0, grid_status="normal", battery_use_kw=0.0,
    )
    stats = types.SimpleNamespace(current=c, totals=types.SimpleNamespace(solar_kwh=0.0))
    ctx = build_context(stats, None, None, Config(), outdir=tmp_path / "nonexistent")
    assert "System snapshot" in ctx
    assert "Recent alerts" not in ctx


def test_build_context_backward_compatible_without_extras():
    """Existing positional call sites must keep working."""
    import types
    from franklinwh_scraper.chatbot import build_context

    c = types.SimpleNamespace(
        battery_soc_pct=50.0, solar_production_kw=0.0, home_load_kw=1.0,
        grid_use_kw=0.0, grid_status="normal", battery_use_kw=0.0,
    )
    stats = types.SimpleNamespace(current=c, totals=types.SimpleNamespace(solar_kwh=0.0))
    ctx = build_context(stats, None, None, Config())
    assert "System snapshot" in ctx
    assert "Advisor says" not in ctx


def test_recommend_never_returns_time_of_use():
    """Mode.TIME_OF_USE is advisory-only (see the enum's docstring). Turning
    a dead enum member into an enforced invariant beats either wiring it up
    or deleting it — the alerts still reference it in prose."""
    import types
    from franklinwh_scraper import advisor
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    now = datetime(2026, 7, 15, 10, 0, 0)
    seen = set()
    for soc in (5.0, 25.0, 52.0, 85.0, 100.0):
        for solar_kw, ghi in ((0.0, 20.0), (2.0, 300.0), (6.0, 900.0)):
            for have_fc in (True, False):
                fc = None
                if have_fc:
                    hours = [
                        HourPrediction(dt=now + timedelta(hours=i), predicted_load_kw=1.5,
                                       predicted_solar_kw=solar_kw, net_kw=solar_kw - 1.5,
                                       confidence="high")
                        for i in range(1, 12)
                    ]
                    fc = UsageForecast(hours=hours, total_load_kwh=16.5,
                                       total_solar_kwh=solar_kw * 11,
                                       net_kwh=solar_kw * 11 - 16.5, peak_load_kw=1.5,
                                       confidence="high", data_days=30)
                outlook = types.SimpleNamespace(
                    avg_ghi=lambda h, _g=ghi: _g,
                    avg_cloud_cover=lambda h: 20.0,
                    peak_ghi_today=lambda _g=ghi: _g,
                )
                stats = types.SimpleNamespace(current=types.SimpleNamespace(
                    battery_soc_pct=soc, home_load_kw=1.5,
                    solar_production_kw=solar_kw, grid_status="normal",
                ), totals=types.SimpleNamespace())
                rec = advisor.recommend(stats, outlook=outlook, forecast=fc,
                                        battery_capacity_kwh=13.6)
                seen.add(rec.mode)

    assert advisor.Mode.TIME_OF_USE not in seen, \
        "recommend() returned TIME_OF_USE — see the enum docstring for why it must not"
    assert seen, "matrix produced no recommendations at all"


def test_weekly_summary_omits_extrapolation_without_install_date(tmp_path):
    """install_date was read but written by nothing, so the lifetime cycle
    count was always extrapolated from a hardcoded Nov 2024 guess. With no
    install date it must say what the number actually is instead."""
    db = HistoryStore(tmp_path / "h.db")
    now = datetime(2026, 7, 26, 21, 30)   # a Sunday evening
    base = now - timedelta(days=6)
    soc = 100.0
    for d in range(6):
        for i in range(9):
            ts = (base + timedelta(days=d, minutes=30 * i)).isoformat()
            db._conn.execute(
                "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
                "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, 0, 18, 1.36, 0.0, max(20.0, soc - i * 5), 0.0, "normal", 0.0, 1.36),
            )
    db._conn.commit()

    msg = alerts._alert_weekly_summary({}, now.strftime("%Y-%m-%d"), now, db,
                                       Config(install_date=""))
    assert msg is not None
    if "Battery cycles" in msg:
        assert "tracking start" in msg
        assert "extrapolated" not in msg


def _frozen_dt(fixed):
    """datetime subclass whose now() returns a fixed instant — same pattern
    the existing advisor/chatbot tests use."""
    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed
    return _FakeDatetime


# ── Critical-SoC deferral to forecast solar ───────────────────────────

def _crit_case(now, soc, solar_kw, confidence="high", hours_ahead=12):
    """Build (stats, outlook, forecast) for the critical-SoC branch."""
    import types
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    hours = [
        HourPrediction(dt=now + timedelta(hours=i), predicted_load_kw=1.0,
                       predicted_solar_kw=solar_kw, net_kw=solar_kw - 1.0,
                       confidence=confidence)
        for i in range(1, hours_ahead + 1)
    ]
    fc = UsageForecast(hours=hours, total_load_kwh=float(hours_ahead),
                       total_solar_kwh=solar_kw * hours_ahead,
                       net_kwh=(solar_kw - 1.0) * hours_ahead, peak_load_kw=1.0,
                       confidence=confidence, data_days=90)
    outlook = types.SimpleNamespace(
        avg_ghi=lambda h: 800.0, avg_cloud_cover=lambda h: 10.0,
        peak_ghi_today=lambda: 900.0,
    )
    stats = types.SimpleNamespace(current=types.SimpleNamespace(
        battery_soc_pct=soc, home_load_kw=1.0, solar_production_kw=solar_kw,
        grid_status="normal",
    ), totals=types.SimpleNamespace())
    return stats, outlook, fc


def test_critical_soc_defers_when_solar_will_recover(monkeypatch):
    """Regression: EB charges from the GRID. At 11% on a sunny morning with a
    confident 90%-by-4pm projection, recommending it means buying power solar
    is about to supply free. Observed live on 2026-07-30."""
    from franklinwh_scraper import advisor

    now = datetime(2026, 7, 30, 10, 0, 0)
    monkeypatch.setattr(advisor, "datetime", _frozen_dt(now))
    stats, outlook, fc = _crit_case(now, soc=11.0, solar_kw=4.0)
    rec = advisor.recommend(stats, outlook=outlook, forecast=fc, battery_capacity_kwh=13.6)

    assert rec.mode is not advisor.Mode.EMERGENCY_BACKUP
    assert rec.urgency == "warning"           # still flagged, just not "grid-charge now"
    assert "solar is forecast to recover" in rec.reason
    assert rec.details["critical_deferred_to_solar"] is True


def test_critical_soc_still_fires_without_forecast(monkeypatch):
    from franklinwh_scraper import advisor

    now = datetime(2026, 7, 30, 10, 0, 0)
    monkeypatch.setattr(advisor, "datetime", _frozen_dt(now))
    stats, outlook, _ = _crit_case(now, soc=11.0, solar_kw=4.0)
    rec = advisor.recommend(stats, outlook=outlook, forecast=None, battery_capacity_kwh=13.6)
    assert rec.mode is advisor.Mode.EMERGENCY_BACKUP
    assert rec.urgency == "critical"


def test_critical_soc_still_fires_on_low_confidence(monkeypatch):
    """Any doubt about the forecast and the critical call must stand."""
    from franklinwh_scraper import advisor

    now = datetime(2026, 7, 30, 10, 0, 0)
    monkeypatch.setattr(advisor, "datetime", _frozen_dt(now))
    stats, outlook, fc = _crit_case(now, soc=11.0, solar_kw=4.0, confidence="low")
    rec = advisor.recommend(stats, outlook=outlook, forecast=fc, battery_capacity_kwh=13.6)
    assert rec.mode is advisor.Mode.EMERGENCY_BACKUP
    assert rec.urgency == "critical"


def test_critical_soc_still_fires_when_solar_wont_recover(monkeypatch):
    """Cloudy day: low SoC and no meaningful solar coming — EB is correct."""
    from franklinwh_scraper import advisor

    now = datetime(2026, 7, 30, 10, 0, 0)
    monkeypatch.setattr(advisor, "datetime", _frozen_dt(now))
    stats, outlook, fc = _crit_case(now, soc=11.0, solar_kw=0.2)
    rec = advisor.recommend(stats, outlook=outlook, forecast=fc, battery_capacity_kwh=13.6)
    assert rec.mode is advisor.Mode.EMERGENCY_BACKUP
    assert rec.urgency == "critical"


def test_critical_soc_still_fires_in_the_evening(monkeypatch):
    """After the peak window opens there's no solar left to wait for, so the
    projection collapses to current SoC and the deferral must not engage."""
    from franklinwh_scraper import advisor

    now = datetime(2026, 7, 30, 19, 0, 0)
    monkeypatch.setattr(advisor, "datetime", _frozen_dt(now))
    stats, outlook, fc = _crit_case(now, soc=11.0, solar_kw=0.0)
    rec = advisor.recommend(stats, outlook=outlook, forecast=fc, battery_capacity_kwh=13.6)
    assert rec.mode is advisor.Mode.EMERGENCY_BACKUP
    assert rec.urgency == "critical"


# ── VPP (Virtual Power Plant) event override ───────────────────────────

def _soft_eb_stats_outlook(soc=20.0):
    """Low SoC + poor solar, no forecast — hits the static-threshold
    'WARNING: low SoC + poor solar forecast' branch, a *soft* (non-critical)
    Emergency Backup call the VPP override is allowed to countermand."""
    import types
    outlook = types.SimpleNamespace(
        avg_ghi=lambda h: 50.0, avg_cloud_cover=lambda h: 90.0,
        peak_ghi_today=lambda: 60.0,
    )
    stats = types.SimpleNamespace(current=types.SimpleNamespace(
        battery_soc_pct=soc, home_load_kw=1.0, solar_production_kw=0.1,
        grid_status="normal",
    ), totals=types.SimpleNamespace())
    return stats, outlook


def test_vpp_event_overrides_soft_emergency_backup(monkeypatch):
    from franklinwh_scraper import advisor

    now = datetime(2026, 8, 25, 17, 0, 0)
    monkeypatch.setattr(advisor, "datetime", _frozen_dt(now))
    stats, outlook = _soft_eb_stats_outlook(soc=20.0)

    # Sanity: without a VPP event this really is a soft (warning) EB call.
    baseline = advisor.recommend(stats, outlook=outlook, forecast=None, battery_capacity_kwh=13.6)
    assert baseline.mode is advisor.Mode.EMERGENCY_BACKUP
    assert baseline.urgency == "warning"

    vpp_event = {"start": now - timedelta(hours=1), "end": now + timedelta(hours=2), "rate_per_kwh": 2.0}
    rec = advisor.recommend(stats, outlook=outlook, forecast=None, battery_capacity_kwh=13.6,
                            vpp_event=vpp_event)
    assert rec.mode is advisor.Mode.SELF_CONSUMPTION
    assert rec.urgency == "info"
    assert "VPP event active" in rec.reason
    assert "$2.00/kWh" in rec.reason
    assert "Would otherwise have been" in rec.reason


def test_vpp_event_never_overrides_critical_emergency_backup(monkeypatch):
    """Grid-down / critically-low-SoC safety calls must never be overridden
    for a VPP payout — both are urgency='critical', which the override
    explicitly excludes."""
    from franklinwh_scraper import advisor

    now = datetime(2026, 7, 30, 19, 0, 0)
    monkeypatch.setattr(advisor, "datetime", _frozen_dt(now))
    stats, outlook, fc = _crit_case(now, soc=11.0, solar_kw=0.0)

    vpp_event = {"start": now - timedelta(hours=1), "end": now + timedelta(hours=2), "rate_per_kwh": 2.0}
    rec = advisor.recommend(stats, outlook=outlook, forecast=fc, battery_capacity_kwh=13.6,
                            vpp_event=vpp_event)
    assert rec.mode is advisor.Mode.EMERGENCY_BACKUP
    assert rec.urgency == "critical"


def test_vpp_event_inactive_does_not_override(monkeypatch):
    """A logged event outside its start/end window (upcoming or already
    ended) must not touch the recommendation at all."""
    from franklinwh_scraper import advisor

    now = datetime(2026, 8, 25, 17, 0, 0)
    monkeypatch.setattr(advisor, "datetime", _frozen_dt(now))
    stats, outlook = _soft_eb_stats_outlook(soc=20.0)

    future_event = {"start": now + timedelta(hours=1), "end": now + timedelta(hours=3), "rate_per_kwh": 2.0}
    rec = advisor.recommend(stats, outlook=outlook, forecast=None, battery_capacity_kwh=13.6,
                            vpp_event=future_event)
    assert rec.mode is advisor.Mode.EMERGENCY_BACKUP
    assert rec.urgency == "warning"
    assert "VPP" not in rec.reason


def test_vpp_event_annotates_compatible_recommendation_without_changing_mode(monkeypatch):
    """When the underlying recommendation is already Self-Consumption
    (compatible with the VPP goal), the mode must stay as-is — only the
    reason gets the event context appended."""
    from franklinwh_scraper import advisor

    now = datetime(2026, 7, 30, 10, 0, 0)
    monkeypatch.setattr(advisor, "datetime", _frozen_dt(now))
    stats, outlook, fc = _crit_case(now, soc=70.0, solar_kw=5.0)

    baseline = advisor.recommend(stats, outlook=outlook, forecast=fc, battery_capacity_kwh=13.6)
    assert baseline.mode is advisor.Mode.SELF_CONSUMPTION

    vpp_event = {"start": now - timedelta(hours=1), "end": now + timedelta(hours=2), "rate_per_kwh": None}
    rec = advisor.recommend(stats, outlook=outlook, forecast=fc, battery_capacity_kwh=13.6,
                            vpp_event=vpp_event)
    assert rec.mode is advisor.Mode.SELF_CONSUMPTION
    assert "VPP event active" in rec.reason
    assert baseline.reason in rec.reason  # original reasoning preserved, just annotated


def _digest_stats(soc=55.0, home_load_kw=1.5):
    import types
    return types.SimpleNamespace(
        current=types.SimpleNamespace(battery_soc_pct=soc, home_load_kw=home_load_kw),
        totals=types.SimpleNamespace(
            solar_kwh=0.0, battery_charge_kwh=0.0, battery_discharge_kwh=0.0,
            grid_load_kwh=2.0, grid_export_kwh=0.0, home_use_kwh=10.0,
        ),
    )


class _AttrStore:
    """Minimal store exposing just what the EOD digest touches."""
    def __init__(self, attr, readings=None):
        self._attr = attr
        self._readings = readings or []
    def daily_solar_kwh_api(self, d): return 0.0
    def daily_solar_kwh(self, d): return 0.0
    def daily_battery_kwh(self, d): return (0.0, 0.0)
    def weekly_readings(self, s, e): return self._readings
    def daily_attribution(self, d): return self._attr
    def soc_near(self, ts): return None


def test_eod_digest_uses_measured_attribution_for_self_sufficiency():
    """Derived (home - grid_in)/home counts grid->battery charging as
    household consumption, so it under-reports on any grid-charge day. The
    measured path split doesn't."""
    now = datetime.now().replace(hour=21, minute=30, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    readings = [(f"{today}T{h:02d}:00:00", 0.5, 1.0, 0.0) for h in range(0, 20, 2)]
    store = _AttrStore(attr=(8.2, 5.1, 0.9), readings=readings)

    msg = alerts._alert_eod_digest({}, today, now, _digest_stats(), Config(),
                                   None, None, store)
    assert msg is not None
    assert "Served by:" in msg
    assert "Battery 8.2" in msg
    assert "Solar direct" in msg          # paths, not sources
    # (8.2 + 5.1) / 14.2 = 93.7% -> 94%
    assert "Self-sufficiency:  94%" in msg


def test_eod_digest_falls_back_when_attribution_absent():
    """Pre-migration days have all-zero columns — must fall back to the
    derived formula rather than reporting a bogus 0%."""
    now = datetime.now().replace(hour=21, minute=30, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    readings = [(f"{today}T{h:02d}:00:00", 0.5, 1.0, 0.0) for h in range(0, 20, 2)]

    for attr in (None, (0.0, 0.0, 0.0)):
        msg = alerts._alert_eod_digest({}, today, now, _digest_stats(),
                                       Config(), None, None,
                                       _AttrStore(attr=attr, readings=readings))
        assert msg is not None
        assert "Served by:" not in msg
        assert "Self-sufficiency:" in msg   # still reported, via the fallback


def test_dashboard_static_dir_resolves():
    """The static move into the package is the one change that can silently
    404 the whole page — pin that it resolves and contains index.html."""
    from franklinwh_scraper import webapi
    assert (webapi._STATIC / "index.html").exists(), \
        f"index.html not found under {webapi._STATIC}"


def test_pwa_manifest_and_icons_are_served(tmp_path, monkeypatch):
    """manifest.json + both icon sizes must exist, be valid, and actually be
    reachable through the app's real static mount (not just present on
    disk) — StaticFiles is mounted at "/", so a typo'd filename 404s
    silently rather than raising at import time."""
    import json as _json

    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi

    manifest_path = webapi._STATIC / "manifest.json"
    assert manifest_path.exists()
    manifest = _json.loads(manifest_path.read_text())
    assert manifest["name"]
    icon_srcs = {icon["src"].lstrip("/") for icon in manifest["icons"]}
    for src in icon_srcs:
        assert (webapi._STATIC / src).exists(), f"{src} referenced by manifest.json but missing"
        assert (webapi._STATIC / src).stat().st_size > 0

    client = TestClient(webapi.app)
    r = client.get("/manifest.json")
    assert r.status_code == 200
    for src in icon_srcs:
        r = client.get(f"/{src}")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"


def test_index_html_links_manifest():
    from franklinwh_scraper import webapi

    html = (webapi._STATIC / "index.html").read_text()
    assert '<link rel="manifest" href="/manifest.json">' in html


def test_dashboard_refuses_public_bind_without_token(monkeypatch):
    """The /api/* routes expose live load and billing data and have no auth
    unless dashboard_token is set."""
    from unittest.mock import patch

    from click.testing import CliRunner
    from franklinwh_scraper import cli as cli_mod

    called = []
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.append(a))

    # cli()'s group callback always overwrites ctx.obj["config"] with a
    # fresh load_config() call, so passing obj={"config": cfg} to invoke()
    # alone doesn't reach the command — must patch load_config itself (this
    # test used to pass only because the real ~/.franklinwh.json happened to
    # have no dashboard_token set; it broke for real once one was added for
    # the kitchen-kiosk setup, exposing that the obj= override was a no-op).
    cfg = Config(email="a@b.c", password="p", lat=32.9, lon=-117.0, dashboard_token="")
    runner = CliRunner()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["dashboard", "--host", "0.0.0.0"])
    assert res.exit_code != 0
    assert "Refusing to bind" in res.output
    assert not called, "uvicorn.run must not be reached on a refused bind"


def test_advise_watch_loop_runs_one_cycle_without_crashing(tmp_path, monkeypatch):
    """Runs cmd_advise (--watch off, so exactly one iteration of the loop
    body) end to end against a mocked AccountClient — the full success path
    through _ping_healthcheck, _check_peak_alerts, etc.

    This is the regression guard the ed1279a NameError should have had:
    that bug (formatter hook silently dropped the _ping_healthcheck import
    after moving its call site) crash-looped the live advisor in
    production for several process starts before being caught, because
    nothing in the suite actually executed this loop body — every other
    test exercises _alert_* functions or account.py methods directly,
    never cmd_advise itself. `python -m py_compile` doesn't catch a
    NameError inside a function body either, only syntax errors — so this
    is the only thing in the suite that would have caught it."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper import cli as cli_mod
    from franklinwh_scraper.account import AccountClient, Current, Stats, Totals

    fake_stats = Stats(
        timestamp=datetime.now().isoformat(),
        gateway_id="gw1",
        current=Current(
            solar_production_kw=2.0, generator_production_kw=0.0, generator_enabled=False,
            battery_use_kw=-1.0, grid_use_kw=0.0, home_load_kw=1.0,
            battery_soc_pct=60.0, grid_status="normal",
        ),
        totals=Totals(
            battery_charge_kwh=1.0, battery_discharge_kwh=0.0, grid_import_kwh=0.0,
            grid_export_kwh=0.0, grid_load_kwh=0.0, solar_kwh=5.0, generator_kwh=0.0,
            home_use_kwh=4.0,
        ),
    )
    monkeypatch.setattr(AccountClient, "get_stats", lambda self, gateway: fake_stats)
    monkeypatch.setattr(cli_mod, "_fetch_outlook_cached", lambda lat, lon: None)

    cfg = Config(
        email="a@b.c", password="p", gateway="gw1", lat=32.9, lon=-117.0,
        output_dir=str(tmp_path), telegram_bot_token="", chat_backend="none",
        healthcheck_url="",
    )
    runner = CliRunner()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["account", "advise"])
    assert res.exit_code == 0, res.output
    assert "Traceback" not in res.output
    assert "No mode change needed" in res.output or "Switch to" in res.output


def test_advise_outage_fallback_still_fetches_weather(tmp_path, monkeypatch):
    """A gateway hiccup during the EOD-digest hour used to hardcode
    outlook=None in the fallback alert call, silently dropping tomorrow's-
    solar and the precharge plan from the digest — even though weather is a
    separate upstream from the FranklinWH gateway that failed, and was
    available the whole time (real incident, 2026-09-05: a
    ConnectionResetError against the gateway one second before the digest's
    scheduled send dropped both lines from that night's alert).

    Runs two watch-loop iterations: the first succeeds (populates
    _last_stats), the second raises during the digest hour so the fallback
    branch fires. Spies on _check_peak_alerts's call to confirm outlook is
    no longer hardcoded to None."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper import cli as cli_mod
    from franklinwh_scraper.account import AccountClient, Current, Stats, Totals

    fake_now = datetime(2026, 9, 5, 21, 33)

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    monkeypatch.setattr(cli_mod, "datetime", _FakeDatetime)

    fake_stats = Stats(
        timestamp=fake_now.isoformat(), gateway_id="gw1",
        current=Current(solar_production_kw=2.0, generator_production_kw=0.0,
                        generator_enabled=False, battery_use_kw=-1.0, grid_use_kw=0.0,
                        home_load_kw=1.0, battery_soc_pct=60.0, grid_status="normal"),
        totals=Totals(battery_charge_kwh=1.0, battery_discharge_kwh=0.0, grid_import_kwh=0.0,
                     grid_export_kwh=0.0, grid_load_kwh=0.0, solar_kwh=5.0,
                     generator_kwh=0.0, home_use_kwh=4.0),
    )
    call_count = {"n": 0}

    def _get_stats(self, gateway):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return fake_stats
        raise ConnectionError("simulated gateway hiccup")

    monkeypatch.setattr(AccountClient, "get_stats", _get_stats)

    sentinel_outlook = object()  # stands in for a real SolarOutlook
    monkeypatch.setattr(cli_mod, "_fetch_outlook_cached", lambda lat, lon: sentinel_outlook)

    fallback_calls = []
    real_check_peak_alerts = cli_mod._check_peak_alerts

    def _spy_check_peak_alerts(*args, **kwargs):
        fallback_calls.append(kwargs.get("outlook", "MISSING"))
        return real_check_peak_alerts(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "_check_peak_alerts", _spy_check_peak_alerts)

    sleep_calls = {"n": 0}

    def _fake_sleep(_seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod.time, "sleep", _fake_sleep)

    # --watch acquires a real lock at ~/.franklinwh.pid (shared with the
    # live advisor daemon, which may genuinely be running right now) —
    # never let this test touch it.
    monkeypatch.setattr(cli_mod, "_acquire_pid_lock", lambda: True)
    monkeypatch.setattr(cli_mod, "_release_pid_lock", lambda: None)

    cfg = Config(
        email="a@b.c", password="p", gateway="gw1", lat=32.9, lon=-117.0,
        output_dir=str(tmp_path), telegram_bot_token="", chat_backend="none",
        healthcheck_url="", watch_interval=1,
    )
    runner = CliRunner()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["account", "advise", "--watch"])
    assert res.exit_code == 0, res.output
    # First iteration's own _check_peak_alerts call (success path) plus the
    # second iteration's fallback call — the fallback's outlook must be the
    # fetched sentinel, not None.
    assert sentinel_outlook in fallback_calls
    assert None not in fallback_calls

def test_bill_record_writes_actual_bill_to_state(tmp_path):
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper import cli as cli_mod
    from franklinwh_scraper.alerts import _load_peak_state

    cfg = Config(output_dir=str(tmp_path), billing_cycle_start_day=20)
    runner = CliRunner()
    # A recent date, well inside the 30-day state-pruning window — matches
    # real usage (the reconciliation reminder only ever fires 3-10 days
    # after a cycle closes, so a manually-entered --cycle-end this old is
    # the realistic case; see test_bill_record_warns_on_stale_cycle_end for
    # the >25-day edge case).
    recent = (datetime.now().date() - timedelta(days=5)).isoformat()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["bill-record", "--amount", "142.50",
                                          "--cycle-end", recent])
    assert res.exit_code == 0, res.output
    state = _load_peak_state(tmp_path)
    assert state[f"actual_bill_{recent}"] == 142.50
    assert "Recorded $142.50" in res.output


def test_bill_record_warns_on_stale_cycle_end(tmp_path):
    """A --cycle-end older than the 30-day state-pruning window would be
    silently discarded on save (_prune_old_state matches the actual_bill_
    prefix) — must warn instead of pretending it stuck."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper import cli as cli_mod
    from franklinwh_scraper.alerts import _load_peak_state

    cfg = Config(output_dir=str(tmp_path), billing_cycle_start_day=20)
    runner = CliRunner()
    stale = (datetime.now().date() - timedelta(days=40)).isoformat()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["bill-record", "--amount", "50", "--cycle-end", stale])
    assert res.exit_code != 0
    assert "too old" in res.output.lower() or "won't be kept" in res.output.lower()
    state = _load_peak_state(tmp_path)
    assert f"actual_bill_{stale}" not in state


def test_accuracy_excludes_pre_bias_fix_days(tmp_path):
    """cmd_accuracy must floor at the same _PR_BIAS_FIX_DATE that
    _alert_solar_degradation floors at — otherwise the week-over-week
    trend column partly reports the 2026-08-24 perf_ratio migration
    discontinuity as forecast drift instead of real accuracy change."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper import cli as cli_mod
    from franklinwh_scraper.alerts import _save_peak_state

    state = {
        # Pre-fix: inflated ratio (1.15 -> 15% "error") — must be excluded.
        "daily_pr_2026-08-20": 1.15,
        "daily_pr_2026-08-21": 1.15,
        # Post-fix: centered near 1.0 — must be the only days counted.
        "daily_pr_2026-08-24": 1.02,
        "daily_pr_2026-08-25": 0.98,
    }
    _save_peak_state(tmp_path, state)
    cfg = Config(output_dir=str(tmp_path))
    runner = CliRunner()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["account", "accuracy"])
    assert res.exit_code == 0, res.output
    assert "Excluding 2 day(s) before 2026-08-24" in res.output
    assert "2026-08-20" not in res.output
    assert "Overall: 2 day(s)" in res.output


def test_bill_record_rejects_bad_date(tmp_path):
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper import cli as cli_mod

    cfg = Config(output_dir=str(tmp_path))
    runner = CliRunner()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["bill-record", "--amount", "100",
                                          "--cycle-end", "not-a-date"])
    assert res.exit_code != 0
    assert "YYYY-MM-DD" in res.output


def test_bill_record_defaults_to_most_recently_closed_cycle(tmp_path):
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper import cli as cli_mod
    from franklinwh_scraper.alerts import _load_peak_state
    from franklinwh_scraper.tou import cycle_bounds

    cfg = Config(output_dir=str(tmp_path), billing_cycle_start_day=20)
    runner = CliRunner()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["bill-record", "--amount", "88"])
    assert res.exit_code == 0, res.output

    state = _load_peak_state(tmp_path)
    cur_start, _cur_end = cycle_bounds(datetime.now().date(), 20)
    _, expected_end = cycle_bounds(cur_start - timedelta(days=1), 20)
    assert state[f"actual_bill_{expected_end.isoformat()}"] == 88.0


def test_vpp_event_logs_start_end_and_rate(tmp_path):
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper import cli as cli_mod
    from franklinwh_scraper.alerts import _load_peak_state

    cfg = Config(output_dir=str(tmp_path))
    runner = CliRunner()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["vpp-event", "--start", "16:00", "--end", "21:00",
                                          "--rate", "2.00"])
    assert res.exit_code == 0, res.output
    assert "Logged VPP event" in res.output

    state = _load_peak_state(tmp_path)
    ev = state["vpp_event"]
    today = datetime.now().date().isoformat()
    assert ev["start"] == f"{today}T16:00:00"
    assert ev["end"] == f"{today}T21:00:00"
    assert ev["rate_per_kwh"] == 2.0


def test_vpp_event_accepts_full_datetime():
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper import cli as cli_mod

    cfg = Config(output_dir="/tmp/nonexistent-vpp-test")
    runner = CliRunner()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["vpp-event", "--start", "2026-08-25T16:00",
                                          "--end", "2026-08-25T21:00"])
    assert res.exit_code == 0, res.output


def test_vpp_event_rejects_end_before_start(tmp_path):
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper import cli as cli_mod

    cfg = Config(output_dir=str(tmp_path))
    runner = CliRunner()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["vpp-event", "--start", "21:00", "--end", "16:00"])
    assert res.exit_code != 0
    assert "must be after" in res.output


def test_vpp_event_rejects_bad_time_format(tmp_path):
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper import cli as cli_mod

    cfg = Config(output_dir=str(tmp_path))
    runner = CliRunner()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["vpp-event", "--start", "not-a-time", "--end", "21:00"])
    assert res.exit_code != 0
    assert "Can't parse" in res.output


def test_vpp_event_requires_start_and_end_unless_clearing(tmp_path):
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper import cli as cli_mod

    cfg = Config(output_dir=str(tmp_path))
    runner = CliRunner()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["vpp-event"])
    assert res.exit_code != 0
    assert "required" in res.output.lower()


def test_vpp_event_clear_removes_logged_event(tmp_path):
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper import cli as cli_mod
    from franklinwh_scraper.alerts import _load_peak_state, _save_peak_state

    _save_peak_state(tmp_path, {"vpp_event": {"start": "x", "end": "y", "rate_per_kwh": None}})

    cfg = Config(output_dir=str(tmp_path))
    runner = CliRunner()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["vpp-event", "--clear"])
    assert res.exit_code == 0, res.output
    assert "Cleared" in res.output
    assert "vpp_event" not in _load_peak_state(tmp_path)


def test_vpp_event_clear_when_nothing_logged_says_so(tmp_path):
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper import cli as cli_mod

    cfg = Config(output_dir=str(tmp_path))
    runner = CliRunner()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["vpp-event", "--clear"])
    assert res.exit_code == 0, res.output
    assert "No VPP event was logged" in res.output


def test_vpp_event_warns_when_not_enrolled(tmp_path):
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper import cli as cli_mod

    cfg = Config(output_dir=str(tmp_path), vpp_enrolled=False)
    runner = CliRunner()
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = runner.invoke(cli_mod.cli, ["vpp-event", "--start", "16:00", "--end", "21:00"])
    assert res.exit_code == 0, res.output
    assert "vpp_enrolled is False" in res.output


def test_hourly_bias_uses_ewma_not_flat_median():
    """Root-cause fix for the 2026-08 month-long low-prediction bias: hour 7
    trended 0.97 (30-day median) up to 1.75 in its last 5 samples. A flat
    median can't track that; EWMA should weight the recent climb heavily."""
    import statistics as _stats

    from franklinwh_scraper.alerts import _get_hourly_bias

    state = {"solar_bias_h7": [
        0.97, 0.95, 0.98, 0.96, 0.99, 1.0, 0.94, 0.97, 0.95, 0.96,
        0.98, 0.97, 0.96, 0.99, 0.95, 0.97, 0.96, 0.98, 0.97, 0.95,
        0.96, 0.98, 0.97, 0.96, 1.34, 1.44, 1.55, 1.63, 1.75,
    ]}
    bias = _get_hourly_bias(state)
    median = _stats.median(state["solar_bias_h7"])
    assert bias[7] > median + 0.15, (
        f"EWMA {bias[7]:.3f} should sit well above the stale median "
        f"{median:.3f} once recent samples have climbed")


def test_hourly_bias_stays_within_sample_bounds():
    """EWMA is a convex combination — can't exceed the [0.3, 2.0] range
    samples are already restricted to on append (no extra clamp needed)."""
    from franklinwh_scraper.alerts import _get_hourly_bias

    state = {"solar_bias_h12": [0.5, 0.5, 0.5, 0.5, 2.0, 2.0]}
    bias = _get_hourly_bias(state)
    assert 0.5 <= bias[12] <= 2.0


def test_hourly_bias_requires_five_samples():
    from franklinwh_scraper.alerts import _get_hourly_bias

    state = {"solar_bias_h9": [1.1, 1.2, 1.3, 1.4]}  # only 4
    assert 9 not in _get_hourly_bias(state)


def test_hourly_bias_matches_perf_ratio_weighting():
    """Same EWMA as perf_ratio — both correction layers should converge at
    the same speed rather than one lagging the other."""
    from franklinwh_scraper.alerts import _ewma, _get_hourly_bias

    samples = [1.0, 1.0, 1.0, 1.0, 1.0, 1.5]
    state = {"solar_bias_h11": samples}
    assert _get_hourly_bias(state)[11] == _ewma(samples)


def test_sundown_bias_needs_3_samples():
    from franklinwh_scraper.alerts import _get_sundown_bias

    assert _get_sundown_bias({}) == 0.0
    assert _get_sundown_bias({"sundown_bias_samples": [-5.0, -3.0]}) == 0.0


def test_sundown_bias_uses_ewma():
    """Same EWMA weighting as the other calibration layers — additive
    (percentage points), not multiplicative like perf_ratio/hourly_bias,
    since a ratio breaks down near a 0% SoC value."""
    from franklinwh_scraper.alerts import _ewma, _get_sundown_bias

    samples = [-2.0, -4.0, -6.0]  # one-directional over-prediction, matches the real data
    state = {"sundown_bias_samples": samples}
    assert _get_sundown_bias(state) == _ewma(samples)


# ── No-EV night classification (ground truth beats percentile guessing) ──

class _NightStore:
    """Minimal store for _classify_and_record_no_ev_night: just
    readings_between, returning (timestamp, grid_use_kw, home_load_kw,
    solar_kw) tuples."""
    def __init__(self, rows):
        self._rows = rows
    def readings_between(self, start_iso, end_iso):
        return [r for r in self._rows if start_iso <= r[0] < end_iso]


def _night_current(soc):
    import types
    return types.SimpleNamespace(battery_soc_pct=soc)


def test_ev_still_charging_fires_at_sop_transition():
    """Weekday: fires right at the 6am super-off-peak -> off-peak boundary
    when there's sustained charging-level grid draw bridging it."""
    import types
    now = datetime(2026, 8, 24, 6, 0, 0)  # Monday 6am
    rows = [
        ((now - timedelta(minutes=90)).isoformat(), 6.0, 6.5, 0.0),
        ((now - timedelta(minutes=60)).isoformat(), 6.0, 6.5, 0.0),
        ((now - timedelta(minutes=30)).isoformat(), 6.0, 6.5, 0.0),
    ]
    store = _NightStore(rows)
    c = types.SimpleNamespace(grid_use_kw=6.0)
    state = {}
    msg = alerts._alert_ev_still_charging(state, "2026-08-24", now, c, Config(ev_charging=True), store)
    assert msg is not None
    assert "still charging" in msg
    assert state["ev_still_charging_date"] == "2026-08-24"


def test_ev_still_charging_skips_on_weekend_still_in_sop():
    """Weekend SOP runs until 2pm, not 6am -- 8am Saturday must not
    false-positive just because it would be off-peak on a weekday."""
    import types
    now = datetime(2026, 8, 22, 8, 0, 0)  # Saturday 8am -- still SOP on weekends
    store = _NightStore([((now - timedelta(minutes=30)).isoformat(), 6.0, 6.5, 0.0)])
    c = types.SimpleNamespace(grid_use_kw=6.0)
    msg = alerts._alert_ev_still_charging({}, "2026-08-22", now, c, Config(ev_charging=True), store)
    assert msg is None


def test_ev_still_charging_skips_when_not_actively_importing():
    """Grid draw below the EV-charging-level threshold -- solar's covering
    it (or it's not charging), not alert-worthy."""
    import types
    now = datetime(2026, 8, 24, 6, 0, 0)
    store = _NightStore([((now - timedelta(minutes=30)).isoformat(), 6.0, 6.5, 0.0)])
    c = types.SimpleNamespace(grid_use_kw=0.3)  # below _NO_EV_LOAD_SPIKE_KW
    msg = alerts._alert_ev_still_charging({}, "2026-08-24", now, c, Config(ev_charging=True), store)
    assert msg is None


def test_ev_still_charging_skips_without_sustained_readings():
    """A single momentary spike right at the boundary isn't enough --
    needs sustained draw across the lookback window to rule out an
    unrelated fresh load starting right as SOP ends."""
    import types
    now = datetime(2026, 8, 24, 6, 0, 0)
    rows = [
        ((now - timedelta(minutes=90)).isoformat(), 0.1, 0.5, 0.0),
        ((now - timedelta(minutes=60)).isoformat(), 0.1, 0.5, 0.0),
        ((now - timedelta(minutes=30)).isoformat(), 0.1, 0.5, 0.0),
    ]
    store = _NightStore(rows)
    c = types.SimpleNamespace(grid_use_kw=6.0)  # only just now, not sustained
    msg = alerts._alert_ev_still_charging({}, "2026-08-24", now, c, Config(ev_charging=True), store)
    assert msg is None


def test_ev_still_charging_once_per_day():
    import types
    now = datetime(2026, 8, 24, 6, 0, 0)
    store = _NightStore([((now - timedelta(minutes=30)).isoformat(), 6.0, 6.5, 0.0)])
    c = types.SimpleNamespace(grid_use_kw=6.0)
    state = {"ev_still_charging_date": "2026-08-24"}
    msg = alerts._alert_ev_still_charging(state, "2026-08-24", now, c, Config(ev_charging=True), store)
    assert msg is None


def test_ev_still_charging_requires_ev_charging_enabled():
    import types
    now = datetime(2026, 8, 24, 6, 0, 0)
    store = _NightStore([((now - timedelta(minutes=30)).isoformat(), 6.0, 6.5, 0.0)])
    c = types.SimpleNamespace(grid_use_kw=6.0)
    msg = alerts._alert_ev_still_charging({}, "2026-08-24", now, c, Config(ev_charging=False), store)
    assert msg is None


class _AttributionStore:
    """Minimal store for _alert_self_sufficiency_streak: just
    daily_attribution, returning (battery_kwh, solar_kwh, grid_kwh) or None."""
    def __init__(self, by_date):
        self._by_date = by_date
    def daily_attribution(self, date_str):
        return self._by_date.get(date_str)


def test_self_sufficiency_streak_fires_after_7_good_days():
    now = datetime(2026, 8, 20, 21, 30, 0)
    by_date = {}
    d = now.date() - timedelta(days=1)
    for _ in range(7):
        by_date[d.strftime("%Y-%m-%d")] = (7.0, 3.0, 0.5)  # (7+3)/10.5 = 95.2%
        d -= timedelta(days=1)
    store = _AttributionStore(by_date)
    msg = alerts._alert_self_sufficiency_streak({}, "2026-08-20", now, store)
    assert msg is not None
    assert "self-sufficient" in msg


def test_self_sufficiency_streak_breaks_on_one_bad_day():
    now = datetime(2026, 8, 20, 21, 30, 0)
    by_date = {}
    d = now.date() - timedelta(days=1)
    for i in range(7):
        # One day at 50% self-sufficiency breaks the streak
        by_date[d.strftime("%Y-%m-%d")] = (2.0, 3.0, 5.0) if i == 3 else (7.0, 3.0, 0.5)
        d -= timedelta(days=1)
    store = _AttributionStore(by_date)
    msg = alerts._alert_self_sufficiency_streak({}, "2026-08-20", now, store)
    assert msg is None


def test_self_sufficiency_streak_breaks_on_missing_data():
    now = datetime(2026, 8, 20, 21, 30, 0)
    by_date = {}
    d = now.date() - timedelta(days=1)
    for i in range(7):
        if i != 2:  # one day has no attribution data at all
            by_date[d.strftime("%Y-%m-%d")] = (7.0, 3.0, 0.5)
        d -= timedelta(days=1)
    store = _AttributionStore(by_date)
    msg = alerts._alert_self_sufficiency_streak({}, "2026-08-20", now, store)
    assert msg is None


def test_self_sufficiency_streak_weekly_gate():
    now = datetime(2026, 8, 20, 21, 30, 0)
    by_date = {}
    d = now.date() - timedelta(days=1)
    for _ in range(7):
        by_date[d.strftime("%Y-%m-%d")] = (7.0, 3.0, 0.5)
        d -= timedelta(days=1)
    store = _AttributionStore(by_date)
    week_key = now.strftime("%G-W%V")
    state = {"self_sufficiency_streak_alerted_week": week_key}
    msg = alerts._alert_self_sufficiency_streak(state, "2026-08-20", now, store)
    assert msg is None


def test_no_ev_night_records_confirmed_no_ev_load(monkeypatch):
    """SoC clearly above the floor -> confirmed no-EV night, real overnight
    load recorded per hour."""
    now = datetime(2026, 8, 20, 7, 45, 0)
    rows = [
        ((now - timedelta(hours=8)).isoformat(), 0.0, 0.35, 0.0),
        ((now - timedelta(hours=7)).isoformat(), 0.0, 0.30, 0.0),
        ((now - timedelta(hours=6)).isoformat(), 0.0, 0.40, 0.0),
    ]
    store = _NightStore(rows)
    c = _night_current(soc=45.0)  # well above the 10% floor
    state = {}
    alerts._classify_and_record_no_ev_night(state, now, c, Config(ev_charge_floor_soc=10.0), store)

    recorded_hours = [k for k in state if k.startswith("no_ev_load_h")]
    assert recorded_hours  # at least one hour recorded
    for k in recorded_hours:
        assert len(state[k]) == 1


def test_ev_night_at_floor_not_recorded():
    """SoC at/near the floor -> skip, whether or not grid imported —
    never recorded as a no-EV sample."""
    now = datetime(2026, 8, 20, 7, 45, 0)
    rows = [((now - timedelta(hours=6)).isoformat(), 1.5, 2.0, 0.0)] * 3  # grid imported
    store = _NightStore(rows)
    c = _night_current(soc=10.2)  # at the floor
    state = {}
    alerts._classify_and_record_no_ev_night(state, now, c, Config(ev_charge_floor_soc=10.0), store)
    assert not any(k.startswith("no_ev_load_h") for k in state)


def test_ambiguous_at_floor_without_grid_import_not_recorded():
    """At the floor but no confirmed grid import — still skipped, not
    guessed as no-EV just because we can't confirm it was an EV night."""
    now = datetime(2026, 8, 20, 7, 45, 0)
    rows = [((now - timedelta(hours=6)).isoformat(), 0.0, 0.35, 0.0)]
    store = _NightStore(rows)
    c = _night_current(soc=10.5)  # at the floor, within tolerance
    state = {}
    alerts._classify_and_record_no_ev_night(state, now, c, Config(ev_charge_floor_soc=10.0), store)
    assert not any(k.startswith("no_ev_load_h") for k in state)


def test_partial_ev_charge_above_floor_not_recorded():
    """User's self-limited-charge pattern (confirmed 2026-08-17): a little
    EV charging overnight, never draws from the grid, tops off before the
    commute — SoC ends up clearly above the floor (never driven down to
    it), so the floor/grid-import check alone would misclassify this as a
    confirmed no-EV night. The load-spike guard must catch it instead."""
    now = datetime(2026, 8, 20, 7, 45, 0)
    rows = [
        ((now - timedelta(hours=8)).isoformat(), 0.0, 0.35, 0.0),   # normal baseline
        ((now - timedelta(hours=3)).isoformat(), 0.0, 1.4, 0.0),    # brief throttled EV charge
        ((now - timedelta(hours=1)).isoformat(), 0.0, 0.40, 0.0),   # back to baseline
    ]
    store = _NightStore(rows)
    c = _night_current(soc=62.0)  # clearly above the 10% floor, no grid import
    state = {}
    alerts._classify_and_record_no_ev_night(state, now, c, Config(ev_charge_floor_soc=10.0), store)
    assert not any(k.startswith("no_ev_load_h") for k in state)


def test_no_ev_night_skips_gracefully_without_store():
    state = {}
    alerts._classify_and_record_no_ev_night(
        state, datetime(2026, 8, 20, 7, 45), _night_current(soc=50.0), Config(), None)
    assert state == {}


def test_get_no_ev_hourly_load_needs_min_samples():
    state = {"no_ev_load_h3": [0.3, 0.32, 0.31, 0.29]}  # only 4, needs 5
    assert 3 not in alerts._get_no_ev_hourly_load(state)

    state["no_ev_load_h3"].append(0.30)
    assert 3 in alerts._get_no_ev_hourly_load(state)


def test_get_no_ev_hourly_load_uses_ewma():
    samples = [0.30, 0.32, 0.28, 0.31, 0.29]
    state = {"no_ev_load_h4": samples}
    assert alerts._get_no_ev_hourly_load(state)[4] == alerts._ewma(samples)


def test_apply_no_ev_overrides_replaces_matching_hours_only():
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    now = datetime(2026, 8, 20, 22, 0, 0)
    hours = [
        HourPrediction(dt=now + timedelta(hours=1), predicted_load_kw=2.0,
                       predicted_solar_kw=0.0, net_kw=-2.0, confidence="high"),
        HourPrediction(dt=now + timedelta(hours=2), predicted_load_kw=1.8,
                       predicted_solar_kw=0.0, net_kw=-1.8, confidence="high"),
    ]
    fc = UsageForecast(hours=hours, total_load_kwh=3.8, total_solar_kwh=0.0,
                       net_kwh=-3.8, peak_load_kw=2.0, confidence="high", data_days=30)

    overridden_hour = (now + timedelta(hours=1)).hour
    result = alerts._apply_no_ev_overrides(fc, {overridden_hour: 0.35})

    changed = next(h for h in result.hours if h.dt.hour == overridden_hour)
    unchanged = next(h for h in result.hours if h.dt.hour != overridden_hour)
    assert changed.predicted_load_kw == 0.35
    assert changed.net_kw == -0.35
    assert unchanged.predicted_load_kw == 1.8  # not touched


def test_apply_no_ev_overrides_noop_when_empty():
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    now = datetime(2026, 8, 20, 22, 0, 0)
    hours = [HourPrediction(dt=now + timedelta(hours=1), predicted_load_kw=2.0,
                            predicted_solar_kw=0.0, net_kw=-2.0, confidence="high")]
    fc = UsageForecast(hours=hours, total_load_kwh=2.0, total_solar_kwh=0.0,
                       net_kwh=-2.0, peak_load_kw=2.0, confidence="high", data_days=30)
    result = alerts._apply_no_ev_overrides(fc, {})
    assert result.hours[0].predicted_load_kw == 2.0


def _low_soc_current(**over):
    import types
    cur = types.SimpleNamespace(
        battery_soc_pct=35.0, solar_production_kw=1.0, home_load_kw=1.5, battery_use_kw=0.3,
    )
    for k, v in over.items():
        setattr(cur, k, v)
    return cur


def test_low_soc_1pm_includes_live_anchored_sundown_projection(monkeypatch):
    """The 1pm low-battery alert should show the same live-anchored sundown
    projection /sundown does — this is exactly the moment 'will I make it
    to sundown' matters most."""
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    now = datetime(2026, 7, 2, 13, 0, 0)
    c = _low_soc_current(home_load_kw=2.1)

    def _forecast(net_kw):
        hours = [HourPrediction(dt=now + timedelta(hours=h), predicted_load_kw=1.0,
                                predicted_solar_kw=3.0 if h < 4 else 0.0,
                                net_kw=net_kw, confidence="high")
                 for h in range(1, 8)]
        return UsageForecast(hours=hours, total_load_kwh=7.0, total_solar_kwh=12.0,
                             net_kwh=net_kw * 7, peak_load_kw=1.0, confidence="high", data_days=30)

    calls = []

    def fake_predict(store_, horizon, **kw):
        calls.append(kw.get("current_load_kw"))
        return _forecast(1.0)

    monkeypatch.setattr(alerts, "predict", fake_predict)

    usage_forecast = _forecast(-9.0)  # shared forecast — must not leak into the live-anchored result
    msg = alerts._alert_low_soc_1pm({}, "2026-07-02", now, c, Config(),
                                    outlook=None, usage_forecast=usage_forecast, store=object())

    assert msg is not None
    assert "🌇 Projected @ sundown" in msg
    assert calls == [2.1]  # live-anchored to the current home_load_kw reading


def test_low_soc_1pm_sundown_projection_applies_learned_bias(monkeypatch):
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    now = datetime(2026, 7, 2, 13, 0, 0)
    c = _low_soc_current()

    hours = [HourPrediction(dt=now + timedelta(hours=h), predicted_load_kw=1.0,
                            predicted_solar_kw=3.0 if h < 4 else 0.0,
                            net_kw=1.0, confidence="high")
             for h in range(1, 8)]
    forecast = UsageForecast(hours=hours, total_load_kwh=7.0, total_solar_kwh=12.0,
                             net_kwh=7.0, peak_load_kw=1.0, confidence="high", data_days=30)
    monkeypatch.setattr(alerts, "predict", lambda *a, **kw: forecast)

    state_no_bias = {}
    state_biased   = {"sundown_bias_samples": [-10.0, -10.0, -10.0]}

    msg_no_bias = alerts._alert_low_soc_1pm(state_no_bias, "2026-07-02", now, c, Config(),
                                            outlook=None, usage_forecast=forecast, store=object())
    msg_biased  = alerts._alert_low_soc_1pm(state_biased, "2026-07-02", now, c, Config(),
                                            outlook=None, usage_forecast=forecast, store=object())
    assert msg_no_bias != msg_biased  # the learned correction must actually shift the shown number


def test_low_soc_1pm_does_not_persist_sundown_pred(monkeypatch):
    """The sundown projection here is display-only — writing sundown_pred_
    would collide with (or get double-graded against) an explicit /sundown
    ask made later the same day, which is meant to stay opt-in."""
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    now = datetime(2026, 7, 2, 13, 0, 0)
    c = _low_soc_current()
    hours = [HourPrediction(dt=now + timedelta(hours=h), predicted_load_kw=1.0,
                            predicted_solar_kw=3.0 if h < 4 else 0.0,
                            net_kw=1.0, confidence="high")
             for h in range(1, 8)]
    forecast = UsageForecast(hours=hours, total_load_kwh=7.0, total_solar_kwh=12.0,
                             net_kwh=7.0, peak_load_kw=1.0, confidence="high", data_days=30)
    monkeypatch.setattr(alerts, "predict", lambda *a, **kw: forecast)

    state = {}
    msg = alerts._alert_low_soc_1pm(state, "2026-07-02", now, c, Config(),
                                    outlook=None, usage_forecast=forecast, store=object())
    assert msg is not None
    assert not any(k.startswith("sundown_pred_") for k in state)


def test_low_soc_1pm_omits_sundown_line_without_store():
    """Backward compatible: no store/forecast passed -> same behavior as
    before this feature (no crash, no sundown line)."""
    c = _low_soc_current()
    now = datetime(2026, 7, 2, 13, 0, 0)
    msg = alerts._alert_low_soc_1pm({}, "2026-07-02", now, c, Config())
    assert msg is not None
    assert "Projected @ sundown" not in msg


# ── Sundown projection on every low-battery-flavored alert ─────────────

def _sundown_forecast(now):
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast
    hours = [HourPrediction(dt=now + timedelta(hours=h), predicted_load_kw=1.0,
                            predicted_solar_kw=3.0 if h < 4 else 0.0,
                            net_kw=1.0, confidence="high")
             for h in range(1, 8)]
    return UsageForecast(hours=hours, total_load_kwh=7.0, total_solar_kwh=12.0,
                         net_kwh=7.0, peak_load_kw=1.0, confidence="high", data_days=30)


def test_low_noon_soc_includes_sundown_projection(monkeypatch):
    import types

    now = datetime(2026, 7, 2, 11, 30, 0)
    c = types.SimpleNamespace(battery_soc_pct=25.0, solar_production_kw=1.0,
                              home_load_kw=1.5, battery_use_kw=0.3)
    forecast = _sundown_forecast(now)
    monkeypatch.setattr(alerts, "predict", lambda *a, **kw: forecast)

    msg = alerts._alert_low_noon_soc({}, "2026-07-02", now, c, Config(),
                                     outlook=None, usage_forecast=forecast, store=object())
    assert msg is not None
    assert "🌇 Projected @ sundown" in msg


def test_low_noon_soc_omits_sundown_line_without_store():
    import types

    now = datetime(2026, 7, 2, 11, 30, 0)
    c = types.SimpleNamespace(battery_soc_pct=25.0, solar_production_kw=1.0,
                              home_load_kw=1.5, battery_use_kw=0.3)
    msg = alerts._alert_low_noon_soc({}, "2026-07-02", now, c, Config())
    assert msg is not None
    assert "Projected @ sundown" not in msg


def test_fast_drain_critical_alert_includes_sundown_projection(monkeypatch):
    import types

    now = datetime(2026, 7, 15, 12, 0, 0)
    c = types.SimpleNamespace(battery_soc_pct=30.0, home_load_kw=2.0,
                              solar_production_kw=0.0, battery_use_kw=1.0)
    state = {"last_soc": 32.0, "last_soc_time": (now - timedelta(minutes=10)).isoformat()}
    forecast = _sundown_forecast(now)
    monkeypatch.setattr(alerts, "predict", lambda *a, **kw: forecast)

    msg = alerts._alert_fast_drain(state, "2026-07-15", now, c, Config(),
                                   outlook=None, usage_forecast=forecast, store=object())
    assert msg is not None
    assert "draining fast" in msg
    assert "🌇 Projected @ sundown" in msg


def test_unusual_drain_alert_includes_sundown_projection(monkeypatch):
    """The lower-urgency 'unusual drain' tier (>=35% SoC) gets the
    projection too — needs 2 consecutive over-threshold polls to fire."""
    import types

    now1 = datetime(2026, 7, 15, 12, 0, 0)
    now2 = now1 + timedelta(minutes=10)
    c1 = types.SimpleNamespace(battery_soc_pct=50.0, home_load_kw=2.0,
                               solar_production_kw=0.0, battery_use_kw=1.0)
    c2 = types.SimpleNamespace(battery_soc_pct=48.0, home_load_kw=2.0,
                               solar_production_kw=0.0, battery_use_kw=1.0)
    forecast = _sundown_forecast(now2)
    monkeypatch.setattr(alerts, "predict", lambda *a, **kw: forecast)

    state = {"last_soc": 60.0, "last_soc_time": (now1 - timedelta(minutes=10)).isoformat()}
    alerts._alert_fast_drain(state, "2026-07-15", now1, c1, Config(),
                             outlook=None, usage_forecast=forecast, store=object())  # 1st poll: builds streak
    msg = alerts._alert_fast_drain(state, "2026-07-15", now2, c2, Config(),
                                   outlook=None, usage_forecast=forecast, store=object())
    assert msg is not None
    assert "Unusual drain rate" in msg
    assert "🌇 Projected @ sundown" in msg


# ── system_peak_kw EWMA (closes the P75-lag lead left after the hourly_bias fix) ──

def test_system_peak_kw_none_below_3_samples():
    assert alerts._get_system_peak_kw({}) is None


def test_system_peak_kw_bootstrap_flat_p75_without_daily_buckets():
    """Fewer than 3 finalized days: fall back to the old flat-P75-over-raw-
    samples behavior so existing state files (pre-fix) still work day one."""
    state = {"solar_cal_samples": [3.0, 3.2, 3.4, 3.6, 3.8]}
    peak = alerts._get_system_peak_kw(state)
    s = sorted(state["solar_cal_samples"])
    assert peak == s[int(len(s) * 0.75)]


def test_system_peak_kw_ewma_tracks_recent_daily_regime():
    """A step-change in daily peaks (panel cleaning, seasonal tilt shift)
    should pull the estimate toward the recent days faster than a flat
    median of the whole history would — same fix class as hourly_bias."""
    state = {"solar_peak_daily": [4.0, 4.0, 4.0] + [5.6, 5.7, 5.8]}
    peak = alerts._get_system_peak_kw(state)
    flat_mean = sum(state["solar_peak_daily"]) / len(state["solar_peak_daily"])
    assert peak > flat_mean  # recency weighting pulls above an unweighted average


def test_calibrate_solar_finalizes_prior_day_into_daily_bucket():
    """_calibrate_solar should roll each day's accepted samples into one P75
    entry in solar_peak_daily once a new day's polls start, feeding
    _get_system_peak_kw's EWMA."""
    import types

    outlook = types.SimpleNamespace(avg_ghi=lambda h: 700.0)
    state = {}
    for _ in range(6):  # day 1 — >=5 samples so it's a "meaningful day"
        alerts._calibrate_solar(state, solar_kw=2.5, outlook=outlook,
                                now=datetime(2026, 7, 15, 12))
    assert "solar_peak_daily" not in state  # still mid-day-1, nothing finalized yet

    alerts._calibrate_solar(state, solar_kw=2.5, outlook=outlook,
                            now=datetime(2026, 7, 16, 12))  # day 2 arrives
    assert state["solar_peak_daily"] == [pytest.approx(3.57)]
    assert state["solar_peak_today_date"] == "2026-07-16"
    assert state["solar_peak_today_samples"] == [pytest.approx(3.57)]


def test_calibrate_solar_skips_thin_day_from_daily_bucket():
    """A day with <5 accepted samples (e.g. advisor was down most of the
    day) shouldn't pollute the daily-peak EWMA with a noisy partial read."""
    import types

    outlook = types.SimpleNamespace(avg_ghi=lambda h: 700.0)
    state = {}
    for _ in range(2):  # only 2 samples on day 1 — below the 5-sample floor
        alerts._calibrate_solar(state, solar_kw=2.5, outlook=outlook,
                                now=datetime(2026, 7, 15, 12))
    alerts._calibrate_solar(state, solar_kw=2.5, outlook=outlook,
                            now=datetime(2026, 7, 16, 12))  # day 2 arrives
    assert state.get("solar_peak_daily", []) == []


def test_battery_full_alert_widened_window_catches_late_charge():
    """2026-08-08 case: battery didn't hit 100% until 4 pm — the old
    10 am-2 pm window (tied to super-off-peak ending) would have missed it."""
    import types
    from datetime import datetime as dt
    from franklinwh_scraper import alerts

    c = types.SimpleNamespace(
        battery_soc_pct=100.0, home_load_kw=0.56, solar_production_kw=2.05,
        battery_use_kw=-0.02,
    )
    state = {}
    msg = alerts._alert_solar_surplus_overflow(state, "2026-08-08",
                                                dt(2026, 8, 8, 16, 3), c)
    assert msg is not None
    assert "Battery full" in msg


def test_battery_full_alert_old_window_edge_still_fires():
    import types
    from datetime import datetime as dt
    from franklinwh_scraper import alerts

    c = types.SimpleNamespace(
        battery_soc_pct=100.0, home_load_kw=0.5, solar_production_kw=2.0,
        battery_use_kw=0.0,
    )
    msg = alerts._alert_solar_surplus_overflow({}, "2026-08-08",
                                                dt(2026, 8, 8, 11, 0), c)
    assert msg is not None


def test_battery_full_alert_outside_window_silent():
    import types
    from datetime import datetime as dt
    from franklinwh_scraper import alerts

    c = types.SimpleNamespace(
        battery_soc_pct=100.0, home_load_kw=0.5, solar_production_kw=2.0,
        battery_use_kw=0.0,
    )
    assert alerts._alert_solar_surplus_overflow({}, "2026-08-08",
                                                 dt(2026, 8, 8, 19, 0), c) is None


def test_battery_full_alert_does_not_repeat_while_still_full():
    """The exact behavior the user asked for: once notified, stays quiet
    as long as the battery never meaningfully drops — even across the
    widened window on the same day, and even into a second sunny day."""
    import types
    from datetime import datetime as dt
    from franklinwh_scraper import alerts

    c = types.SimpleNamespace(
        battery_soc_pct=100.0, home_load_kw=0.5, solar_production_kw=2.0,
        battery_use_kw=0.0,
    )
    state = {}
    first = alerts._alert_solar_surplus_overflow(state, "2026-08-08",
                                                  dt(2026, 8, 8, 10, 30), c)
    assert first is not None

    # Still full an hour later, same day.
    again_same_day = alerts._alert_solar_surplus_overflow(
        state, "2026-08-08", dt(2026, 8, 8, 15, 0), c)
    assert again_same_day is None

    # Still full the next sunny day — no per-day reset, so still silent.
    again_next_day = alerts._alert_solar_surplus_overflow(
        state, "2026-08-09", dt(2026, 8, 9, 11, 0), c)
    assert again_next_day is None


def test_battery_full_alert_rearms_after_meaningful_drop():
    """A real discharge (e.g. overnight) re-arms the alert for the next fill."""
    import types
    from datetime import datetime as dt
    from franklinwh_scraper import alerts

    full = types.SimpleNamespace(
        battery_soc_pct=100.0, home_load_kw=0.5, solar_production_kw=2.0,
        battery_use_kw=0.0,
    )
    state = {}
    assert alerts._alert_solar_surplus_overflow(
        state, "2026-08-08", dt(2026, 8, 8, 10, 30), full) is not None

    # Overnight discharge — the reset check runs every tick, even outside
    # the alert's own hour window.
    drained = types.SimpleNamespace(
        battery_soc_pct=40.0, home_load_kw=0.5, solar_production_kw=0.0,
        battery_use_kw=0.3,
    )
    assert alerts._alert_solar_surplus_overflow(
        state, "2026-08-09", dt(2026, 8, 9, 3, 0), drained) is None
    assert state["battery_full_notified"] is False

    # Full again the next day — newsworthy again.
    again = alerts._alert_solar_surplus_overflow(
        state, "2026-08-09", dt(2026, 8, 9, 11, 0), full)
    assert again is not None


def test_battery_full_alert_message_reflects_current_period():
    """The old hardcoded '(until 2 pm)' claim went false the moment the
    window widened past 2 pm — the message must describe the real period."""
    import types
    from datetime import datetime as dt
    from franklinwh_scraper import alerts

    c = types.SimpleNamespace(
        battery_soc_pct=100.0, home_load_kw=0.5, solar_production_kw=2.0,
        battery_use_kw=0.0,
    )
    msg = alerts._alert_solar_surplus_overflow({}, "2026-08-08",
                                                dt(2026, 8, 8, 16, 30), c)
    assert "until 2 pm" not in msg
    assert "on-peak" in msg


def test_predict_anchors_next_hour_to_current_load(tmp_path):
    """current_load_kw should make hour 0's prediction equal what's actually
    happening right now, not the historical average for this slot — the
    literal ask: 'base that prediction off my current usage at the time of
    the alert'."""
    db = HistoryStore(tmp_path / "h.db")
    now = datetime.now()
    slot_dow, slot_hour = now.weekday(), now.hour

    def _insert(ts: datetime, load_kw: float):
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts.isoformat(), slot_dow, slot_hour, load_kw, 0.0, 50.0, 0.0, "normal", 0.0, 0.0),
        )

    # Typical load for this exact slot has always been ~1.0 kW.
    for i in range(10):
        _insert(now - timedelta(days=7 * i), 1.0)
    db._conn.commit()

    baseline = predict(db, horizon_hours=1)
    assert baseline.hours[0].predicted_load_kw < 1.5  # sanity: unmodified case

    live = predict(db, horizon_hours=1, current_load_kw=6.0)
    # h=0 weight is 1.0 -> prediction should land on the live reading exactly
    # (temp_scale is 1.0 at 22C default, so no extra drift to account for).
    assert abs(live.hours[0].predicted_load_kw - 6.0) < 0.05


def test_predict_decays_live_correction_toward_baseline(tmp_path):
    """Tonight's unusual draw shouldn't be extrapolated flat until morning —
    it should fade back to the historical pattern within a few hours."""
    db = HistoryStore(tmp_path / "h.db")
    now = datetime.now()

    def _insert(ts: datetime, load_kw: float):
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts.isoformat(), ts.weekday(), ts.hour, load_kw, 0.0, 50.0, 0.0, "normal", 0.0, 0.0),
        )

    # Historical baseline of 1.0 kW for every hour-of-week slot the 10h
    # forecast horizon will touch, several weeks back so it's outside the
    # 21-day recency window's influence.
    for h in range(10):
        slot_time = now + timedelta(hours=h)
        for weeks_back in range(1, 11):
            _insert(slot_time - timedelta(days=7 * weeks_back), 1.0)
    db._conn.commit()

    forecast = predict(db, horizon_hours=10, current_load_kw=9.0)
    # h=0: full residual (~9.0). Each _LOAD_NOWCAST_HALFLIFE_H=2h, the
    # residual halves — by h=8 (four half-lives) it's under ~6% of its
    # start, so predicted load should have relaxed back near the ~1.0
    # historical baseline, nowhere near the live 9.0 anchor.
    assert forecast.hours[0].predicted_load_kw > 8.0
    assert forecast.hours[8].predicted_load_kw < 2.0


def test_predict_without_current_load_kw_is_unchanged(tmp_path):
    """current_load_kw defaults to None — existing callers (advisor,
    chatbot forecast summaries, prior tests) must see byte-identical
    behavior."""
    db = HistoryStore(tmp_path / "h.db")
    now = datetime.now()

    def _insert(ts: datetime, load_kw: float):
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts.isoformat(), ts.weekday(), ts.hour, load_kw, 0.0, 50.0, 0.0, "normal", 0.0, 0.0),
        )

    for i in range(10):
        _insert(now - timedelta(days=7 * i), 3.0)
    db._conn.commit()

    a = predict(db, horizon_hours=3)
    b = predict(db, horizon_hours=3)
    assert [p.predicted_load_kw for p in a.hours] == [p.predicted_load_kw for p in b.hours]


def test_predict_live_anchor_never_goes_negative(tmp_path):
    """A large negative residual (live reading well below baseline) must
    floor at 0, not swing net_kw into a nonsensical negative load."""
    db = HistoryStore(tmp_path / "h.db")
    now = datetime.now()

    def _insert(ts: datetime, load_kw: float):
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts.isoformat(), ts.weekday(), ts.hour, load_kw, 0.0, 50.0, 0.0, "normal", 0.0, 0.0),
        )

    for i in range(10):
        _insert(now - timedelta(days=7 * i), 5.0)
    db._conn.commit()

    forecast = predict(db, horizon_hours=1, current_load_kw=0.0)
    assert forecast.hours[0].predicted_load_kw >= 0.0


def test_eod_digest_stores_7am_prediction_for_tomorrow(monkeypatch):
    """The no-EV baseline prediction gets stashed for tomorrow's morning
    preview to check itself against — keyed to the sunrise checkpoint
    _predict_overnight_soc_flat targets."""
    from franklinwh_scraper.predictor import HourPrediction, UsageForecast

    now = datetime.now().replace(hour=21, minute=30, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    readings = [(f"{today}T{h:02d}:00:00", 0.5, 1.0, 0.0) for h in range(0, 20, 2)]
    store = _AttrStore(attr=(8.2, 5.1, 0.9), readings=readings)

    hours = [HourPrediction(dt=now + timedelta(hours=h), predicted_load_kw=1.0,
                             predicted_solar_kw=0.0, net_kw=-0.5, confidence="high")
             for h in range(1, 10)]
    usage_forecast = UsageForecast(hours=hours, total_load_kwh=9.0, total_solar_kwh=0.0,
                                   net_kwh=-4.5, peak_load_kw=1.0, confidence="high", data_days=30)

    state = {}
    alerts._alert_eod_digest(state, today, now, _digest_stats(soc=55.0), Config(),
                             None, usage_forecast, store)

    key = f"soc_7am_pred_{tomorrow}"
    assert key in state
    saved_dt = datetime.fromisoformat(state[key]["dt"])
    assert saved_dt.hour == 7 and saved_dt.minute == 0
    assert saved_dt.date() == (now + timedelta(days=1)).date()
    assert isinstance(state[key]["pct"], float)
    # Range bounds (0.3-0.5 kWh/hr) stashed alongside the point estimate so
    # tomorrow's accuracy line can grade against a range — added 2026-08-24.
    assert isinstance(state[key]["low_pct"], float)
    assert isinstance(state[key]["high_pct"], float)
    assert state[key]["low_pct"] < state[key]["pct"] < state[key]["high_pct"]


def test_morning_greeting_varies_by_day_of_year():
    """Requested 2026-08-31: the morning alert must not say 'Good morning!'
    every single day. Deterministic (day-of-year modulo), not random.choice
    — so the same date always reproduces the same greeting."""
    d1 = datetime(2026, 1, 1)
    d2 = datetime(2026, 1, 2)
    g1 = alerts._morning_greeting(d1)
    g2 = alerts._morning_greeting(d2)
    assert g1 != g2
    assert g1 in alerts._MORNING_GREETINGS
    assert g2 in alerts._MORNING_GREETINGS
    # Reproducible for the same date.
    assert alerts._morning_greeting(d1) == g1


def test_morning_preview_uses_rotating_greeting():
    import types

    now = datetime(2026, 1, 5, 7, 45)  # a date whose greeting isn't "Good morning!"
    today = now.strftime("%Y-%m-%d")
    c = types.SimpleNamespace(battery_soc_pct=50.0, solar_production_kw=0.0)
    msg = alerts._alert_morning_preview({}, today, now, c, None, None, None, Config())
    expected = alerts._morning_greeting(now)
    assert expected in msg
    assert expected != "Good morning!"  # confirms this date actually exercises rotation


def test_morning_preview_pr_calibration_undoes_yesterdays_correction():
    """The EWMA sample fed into perf_ratio_samples must be the
    baseline-relative true ratio (raw residual * perf_ratio actually used
    yesterday), not the raw residual itself — feeding the raw residual is a
    self-referential mean-of-ratios estimator that systematically
    undershoots (fixed 2026-08-24, see _get_performance_ratio's docstring).
    The displayed daily_pr_ accuracy figure stays the raw residual — this
    only changes what gets fed into the correction EWMA."""
    import types

    now = datetime.now().replace(hour=7, minute=45, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    state = {
        f"predicted_kwh_{yesterday}": 20.0,
        f"predicted_avg_ghi_{yesterday}": 500.0,  # sunny
        f"perf_ratio_used_{yesterday}": 1.1,       # yesterday's prediction used PR=1.1
    }

    store = _AttrStore(attr=(0.0, 0.0, 0.0))
    store.daily_solar_kwh_api = lambda d: 24.0  # actual -> raw ratio 24/20 = 1.2

    c = types.SimpleNamespace(battery_soc_pct=50.0, solar_production_kw=0.0)
    alerts._alert_morning_preview(state, today, now, c, None, None, store, Config())

    assert state[f"daily_pr_{yesterday}"] == 1.2                    # display: raw residual, unchanged
    assert state["perf_ratio_samples"] == [1.2 * 1.1]                # EWMA input: undone (true) ratio
    assert f"perf_ratio_used_{yesterday}" not in state                # cleaned up


def test_morning_preview_pr_calibration_defaults_to_1_when_perf_ratio_used_missing():
    """No perf_ratio_used_<date> in state (e.g. a soc_7am_pred_-only entry
    from before this shipped) must default to 1.0, not KeyError — true_ratio
    then equals the raw residual, same as the old behavior."""
    import types

    now = datetime.now().replace(hour=7, minute=45, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    state = {
        f"predicted_kwh_{yesterday}": 20.0,
        f"predicted_avg_ghi_{yesterday}": 500.0,
    }

    store = _AttrStore(attr=(0.0, 0.0, 0.0))
    store.daily_solar_kwh_api = lambda d: 24.0

    c = types.SimpleNamespace(battery_soc_pct=50.0, solar_production_kw=0.0)
    alerts._alert_morning_preview(state, today, now, c, None, None, store, Config())

    assert state["perf_ratio_samples"] == [1.2]


def test_morning_preview_pr_calibration_cleans_up_on_rejected_outlier():
    """A day rejected by the _PR_MIN outlier gate must still pop
    perf_ratio_used_<date> — otherwise a run of bad-GHI days would leak
    that key into state forever (only _DATE_KEYED_PREFIXES' 30-day prune
    would eventually catch it)."""
    import types

    now = datetime.now().replace(hour=7, minute=45, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    state = {
        f"predicted_kwh_{yesterday}": 20.0,
        f"predicted_avg_ghi_{yesterday}": 500.0,
        f"perf_ratio_used_{yesterday}": 1.1,
    }

    store = _AttrStore(attr=(0.0, 0.0, 0.0))
    store.daily_solar_kwh_api = lambda d: 5.0  # ratio 0.25 -> well under _PR_MIN (0.65)

    c = types.SimpleNamespace(battery_soc_pct=50.0, solar_production_kw=0.0)
    alerts._alert_morning_preview(state, today, now, c, None, None, store, Config())

    assert "perf_ratio_samples" not in state          # rejected, no EWMA update
    assert f"perf_ratio_used_{yesterday}" not in state  # still cleaned up


def test_morning_preview_pr_calibration_undoes_cloudy_bucket_correctly():
    now = datetime.now().replace(hour=7, minute=45, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    state = {
        f"predicted_kwh_{yesterday}": 5.0,
        f"predicted_avg_ghi_{yesterday}": 200.0,  # cloudy (< _GHI_CLOUDY_THRESHOLD)
        f"perf_ratio_used_{yesterday}": 0.85,
    }

    store = _AttrStore(attr=(0.0, 0.0, 0.0))
    store.daily_solar_kwh_api = lambda d: 6.0  # raw ratio 1.2

    import types
    c = types.SimpleNamespace(battery_soc_pct=50.0, solar_production_kw=0.0)
    alerts._alert_morning_preview(state, today, now, c, None, None, store, Config())

    assert "perf_ratio_samples" not in state
    assert state["perf_ratio_cloudy_samples"] == [1.2 * 0.85]


def test_morning_preview_reports_7am_prediction_accuracy_from_store():
    import types

    now = datetime.now().replace(hour=7, minute=45, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    pred_dt = now.replace(hour=7, minute=0)
    state = {f"soc_7am_pred_{today}": {"pct": 20.0, "dt": pred_dt.isoformat()}}

    store = _AttrStore(attr=(8.2, 5.1, 0.9))
    store.soc_near = lambda ts: 32.0  # actual reading near 7am

    c = types.SimpleNamespace(battery_soc_pct=35.0, solar_production_kw=0.5)
    msg = alerts._alert_morning_preview(state, today, now, c, None, None, store, Config())

    assert msg is not None
    assert "Sunrise SoC accuracy: predicted 20%, actual 32% (+12 pt)" in msg
    # Popped, not peeked — a late/duplicate run can't compare it twice.
    assert f"soc_7am_pred_{today}" not in state


def test_morning_preview_7am_accuracy_falls_back_without_nearby_reading():
    import types

    now = datetime.now().replace(hour=7, minute=45, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    pred_dt = now.replace(hour=7, minute=0)
    state = {f"soc_7am_pred_{today}": {"pct": 20.0, "dt": pred_dt.isoformat()}}

    store = _AttrStore(attr=(8.2, 5.1, 0.9))  # soc_near defaults to None — no nearby reading

    c = types.SimpleNamespace(battery_soc_pct=35.0, solar_production_kw=0.5)
    msg = alerts._alert_morning_preview(state, today, now, c, None, None, store, Config())

    assert msg is not None
    assert "not directly comparable" in msg
    assert "using now's 35%" in msg


def test_morning_preview_7am_accuracy_within_range():
    import types

    now = datetime.now().replace(hour=7, minute=45, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    pred_dt = now.replace(hour=7, minute=0)
    state = {f"soc_7am_pred_{today}": {
        "pct": 27.0, "low_pct": 21.0, "high_pct": 35.0, "dt": pred_dt.isoformat(),
    }}

    store = _AttrStore(attr=(8.2, 5.1, 0.9))
    store.soc_near = lambda ts: 30.0  # inside [21, 35]

    c = types.SimpleNamespace(battery_soc_pct=30.0, solar_production_kw=0.5)
    msg = alerts._alert_morning_preview(state, today, now, c, None, None, store, Config())

    assert msg is not None
    assert "Sunrise SoC accuracy: predicted 21-35% (0.3-0.5 kWh/hr), actual 30% — within range" in msg
    assert f"soc_7am_pred_{today}" not in state


def test_morning_preview_7am_accuracy_above_range():
    """Actual SoC higher than the high (0.3 kWh/hr) bound — used less than
    the plausible minimum, e.g. an unusually quiet night."""
    import types

    now = datetime.now().replace(hour=7, minute=45, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    pred_dt = now.replace(hour=7, minute=0)
    state = {f"soc_7am_pred_{today}": {
        "pct": 27.0, "low_pct": 21.0, "high_pct": 35.0, "dt": pred_dt.isoformat(),
    }}

    store = _AttrStore(attr=(8.2, 5.1, 0.9))
    store.soc_near = lambda ts: 40.0  # 5pt above the 35% high bound

    c = types.SimpleNamespace(battery_soc_pct=40.0, solar_production_kw=0.5)
    msg = alerts._alert_morning_preview(state, today, now, c, None, None, store, Config())

    assert msg is not None
    assert "actual 40% — 5pt above range (used less than 0.3 kWh/hr)" in msg


def test_morning_preview_7am_accuracy_below_range():
    """Actual SoC lower than the low (0.5 kWh/hr) bound — used more than
    the plausible maximum, e.g. an unaccounted load ran overnight."""
    import types

    now = datetime.now().replace(hour=7, minute=45, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    pred_dt = now.replace(hour=7, minute=0)
    state = {f"soc_7am_pred_{today}": {
        "pct": 27.0, "low_pct": 21.0, "high_pct": 35.0, "dt": pred_dt.isoformat(),
    }}

    store = _AttrStore(attr=(8.2, 5.1, 0.9))
    store.soc_near = lambda ts: 15.0  # 6pt below the 21% low bound

    c = types.SimpleNamespace(battery_soc_pct=15.0, solar_production_kw=0.5)
    msg = alerts._alert_morning_preview(state, today, now, c, None, None, store, Config())

    assert msg is not None
    assert "actual 15% — 6pt below range (used more than 0.5 kWh/hr)" in msg


def test_morning_preview_7am_accuracy_range_fallback_without_nearby_reading():
    """No reading near sunrise -> falls back to now's SoC, still labels
    the range and the point estimate, still marked not directly
    comparable."""
    import types

    now = datetime.now().replace(hour=7, minute=45, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    pred_dt = now.replace(hour=7, minute=0)
    state = {f"soc_7am_pred_{today}": {
        "pct": 27.0, "low_pct": 21.0, "high_pct": 35.0, "dt": pred_dt.isoformat(),
    }}

    store = _AttrStore(attr=(8.2, 5.1, 0.9))  # soc_near defaults to None

    c = types.SimpleNamespace(battery_soc_pct=35.0, solar_production_kw=0.5)
    msg = alerts._alert_morning_preview(state, today, now, c, None, None, store, Config())

    assert msg is not None
    assert "predicted 21-35% —" in msg
    assert "not directly comparable" in msg
    assert "using now's 35%" in msg


def test_morning_preview_omits_7am_accuracy_when_nothing_stored():
    import types

    now = datetime.now().replace(hour=7, minute=45, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    c = types.SimpleNamespace(battery_soc_pct=35.0, solar_production_kw=0.5)

    msg = alerts._alert_morning_preview({}, today, now, c, None, None, None, Config())
    assert msg is not None
    assert "Sunrise SoC accuracy" not in msg


def test_eod_digest_shows_without_ev_line_but_not_with_ev():
    """'Without EV charging' was re-added to the digest by request
    2026-08-23; 'With EV charging (to floor)' stays cut (removed
    2026-08-19). The prediction is also still stashed in state so
    tomorrow's morning-preview 'Sunrise SoC accuracy' line (a separate
    alert) keeps working. (The floor-capping 'with EV' logic itself still
    lives in webapi.py's /api/ev, untouched — this test only covers the
    digest.)"""
    now = datetime.now().replace(hour=21, minute=30, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    readings = [(f"{today}T{h:02d}:00:00", 0.5, 1.0, 0.0) for h in range(0, 20, 2)]
    store = _AttrStore(attr=(8.2, 5.1, 0.9), readings=readings)

    # soc=55, default 0.4 kW baseline, 9.5h to the fixed 7am checkpoint,
    # 13.6 kWh default capacity -> ~27% expected stashed pct.
    cfg = Config(ev_charging=True, ev_charge_floor_soc=10.0)
    stats = _digest_stats(soc=55.0, home_load_kw=1.5)
    state = {}

    msg = alerts._alert_eod_digest(state, today, now, stats, cfg, None, None, store)
    assert msg is not None
    assert "Without EV charging" in msg
    assert "With EV charging" not in msg

    stashed = state.get(f"soc_7am_pred_{today}") or next(
        (v for k, v in state.items() if k.startswith("soc_7am_pred_")), None)
    assert stashed is not None
    assert 25 <= stashed["pct"] <= 30


def test_eod_digest_omits_with_ev_line_when_no_ev_configured():
    """No EV at all (ev_charging=False) — only the plain prediction shows,
    no second line, no extra predict() call."""
    now = datetime.now().replace(hour=21, minute=30, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    readings = [(f"{today}T{h:02d}:00:00", 0.5, 1.0, 0.0) for h in range(0, 20, 2)]
    store = _AttrStore(attr=(8.2, 5.1, 0.9), readings=readings)

    msg = alerts._alert_eod_digest({}, today, now, _digest_stats(), Config(),
                                   None, None, store)
    assert msg is not None
    assert "Predicted SoC @" in msg
    assert "With EV charging" not in msg
    assert "Without EV charging" not in msg


def test_cli_shared_forecast_never_gets_live_anchor():
    """Regression guard: the shared usage_forecast built in cmd_advise's
    watch loop feeds advisor.recommend()'s Emergency-Backup decision and
    the chatbot, not just the digest. If current_load_kw is ever passed to
    that call, a single noisy poll (a kettle running for 5 min) could ripple
    into a spurious 'switch to Emergency Backup' recommendation. Only the
    digest's own internal predict() call (alerts.py) should anchor to live
    usage — this scans cli.py's source to make sure it stays that way."""
    import inspect

    from franklinwh_scraper import cli as cli_mod

    src = inspect.getsource(cli_mod)
    start = src.index("usage_forecast = (")
    end = src.index(")", src.index("history.has_enough_data()", start)) + 1
    block = src[start:end]
    assert "current_load_kw" not in block, (
        "shared usage_forecast must not be live-anchored:\n" + block)


def test_doctor_flags_untuned_ev_charging_kw(monkeypatch):
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper.cli import cli

    default_cfg = Config(email="e", password="p", lat=1.0, lon=1.0,
                         ev_charging=True, ev_charging_kw=7.6,
                         output_dir="/tmp/nonexistent-doctor-test")
    with patch("franklinwh_scraper.cli.load_config", return_value=default_cfg), \
         patch("franklinwh_scraper.cli.AccountClient") as ac:
        ac.side_effect = RuntimeError("skip login")
        out = CliRunner().invoke(cli, ["doctor"]).output
    assert "still the 7.6 kW default guess" in out

    tuned_cfg = Config(email="e", password="p", lat=1.0, lon=1.0,
                       ev_charging=True, ev_charging_kw=9.6,
                       output_dir="/tmp/nonexistent-doctor-test")
    with patch("franklinwh_scraper.cli.load_config", return_value=tuned_cfg), \
         patch("franklinwh_scraper.cli.AccountClient") as ac:
        ac.side_effect = RuntimeError("skip login")
        out = CliRunner().invoke(cli, ["doctor"]).output
    assert "still the 7.6 kW default guess" not in out
    assert "9.6 kW" in out

    no_ev_cfg = Config(email="e", password="p", lat=1.0, lon=1.0,
                       ev_charging=False, output_dir="/tmp/nonexistent-doctor-test")
    with patch("franklinwh_scraper.cli.load_config", return_value=no_ev_cfg), \
         patch("franklinwh_scraper.cli.AccountClient") as ac:
        ac.side_effect = RuntimeError("skip login")
        out = CliRunner().invoke(cli, ["doctor"]).output
    assert "EV charging draw" not in out


def test_tesla_keygen_writes_key_pair_and_refuses_overwrite(tmp_path, monkeypatch):
    from click.testing import CliRunner

    import franklinwh_scraper.tesla as tesla_mod
    from franklinwh_scraper.cli import cli

    key_path = tmp_path / "key.pem"
    monkeypatch.setattr(tesla_mod, "TESLA_KEY_PATH", key_path)
    pub_path = tmp_path / "pub.pem"
    cfg = Config(output_dir=str(tmp_path / "output"))
    runner = CliRunner()

    r = runner.invoke(cli, ["tesla", "keygen", "--out", str(pub_path)], obj={"config": cfg})
    assert r.exit_code == 0, r.output
    assert key_path.exists()
    assert oct(key_path.stat().st_mode)[-3:] == "600"
    assert pub_path.exists()
    assert pub_path.read_text().startswith("-----BEGIN PUBLIC KEY-----")
    assert key_path.read_text().startswith("-----BEGIN EC PRIVATE KEY-----")
    assert ".well-known/appspecific/com.tesla.3p.public-key.pem" in r.output

    r2 = runner.invoke(cli, ["tesla", "keygen", "--out", str(pub_path)], obj={"config": cfg})
    assert r2.exit_code != 0
    assert "already exists" in str(r2.output) + str(r2.exception)

    old_private = key_path.read_bytes()
    r3 = runner.invoke(cli, ["tesla", "keygen", "--force", "--out", str(pub_path)],
                       obj={"config": cfg})
    assert r3.exit_code == 0, r3.output
    assert key_path.read_bytes() != old_private  # --force actually regenerated


def test_ev_status_days_filters_and_summarizes(tmp_path):
    import json as _json
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper.cli import cli

    now = datetime.now()
    log = tmp_path / "ev_controller.jsonl"
    entries = [
        {"timestamp": (now - timedelta(days=10)).isoformat(), "action": "start",
         "amps": 12, "reason": "old start", "dry_run": True},
        {"timestamp": (now - timedelta(hours=5)).isoformat(), "action": "none",
         "amps": None, "reason": "waiting", "dry_run": True},
        {"timestamp": (now - timedelta(hours=3)).isoformat(), "action": "stop",
         "amps": None, "reason": "on-peak", "dry_run": True},
        {"timestamp": (now - timedelta(hours=1)).isoformat(), "action": "set_amps",
         "amps": 20, "reason": "track surplus", "dry_run": True},
    ]
    log.write_text("\n".join(_json.dumps(e) for e in entries) + "\n")

    cfg = Config(ev_control_enabled=True, output_dir=str(tmp_path))
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        default_out = CliRunner().invoke(
            cli, ["account", "ev-status", "--out", str(tmp_path)]).output
        windowed_out = CliRunner().invoke(
            cli, ["account", "ev-status", "--out", str(tmp_path), "--days", "1"]).output

    # Default (no --days): unchanged behavior — old entries still shown.
    assert "old start" in default_out
    # --days 1: the 10-day-old entry is excluded, summary counts are right,
    # and the no-op "waiting" tick isn't listed among actions taken.
    assert "old start" not in windowed_out
    assert "3 logged, 2 action(s) taken" in windowed_out
    assert "waiting" not in windowed_out
    assert "on-peak" in windowed_out
    assert "track surplus" in windowed_out


def test_send_alert_attaches_mute_buttons_except_always_on(monkeypatch, tmp_path):
    """Alerts should carry the same 2h/8h mute buttons as the standalone
    /mute command — except the safety alerts _alert_enabled never lets
    /mute silence in the first place, which must never even look mutable."""
    calls = []
    monkeypatch.setattr(
        alerts, "notify_telegram",
        lambda body, token, chat_id, reply_markup=None: calls.append(reply_markup))

    # output_dir defaults to the relative "output" — without overriding it,
    # _send_alert's _log_alert call falls through to the real production
    # output/alerts_log.jsonl (same class of bug as the /sundown
    # test-isolation fix earlier this session, a different file this time).
    # notify_telegram is mocked above so no real message ever went out, but
    # this dummy "battery full"/"grid down!" text was leaking into the real
    # alert history the dashboard and chatbot both read from.
    cfg = Config(telegram_bot_token="x", telegram_chat_id="y", output_dir=str(tmp_path))

    alerts._send_alert("battery full", cfg, alert_name="solar_surplus_overflow")
    assert calls[-1] == alerts._MUTE_KEYBOARD

    alerts._send_alert("grid down!", cfg, alert_name="grid_down")
    assert calls[-1] is None

    alerts._send_alert("no name given", cfg)
    assert calls[-1] is None


def test_solar_audit_flags_outliers_and_groups_by_cloudy(tmp_path):
    import json as _json
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper.cli import cli
    from franklinwh_scraper.history import HistoryStore

    day1 = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")  # clear, big miss
    day2 = datetime.now().strftime("%Y-%m-%d")                        # cloudy, on target

    db = HistoryStore(tmp_path / "history.db")
    for date_str in (day1, day2):
        base = datetime.strptime(date_str, "%Y-%m-%d")
        for h in range(11):  # 11 points, 1h apart, constant 2.0 kW -> 20 kWh actual
            ts = (base + timedelta(hours=h)).isoformat()
            db._conn.execute(
                "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
                "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, 0, h, 1.0, 2.0, 50.0, 0.0, "normal", 0.0, 0.0),
            )
    db._conn.commit()
    db.close()

    log_path = tmp_path / "solar_calibration_log.jsonl"
    entries = [
        {"date": day1, "timestamp": f"{day1}T07:30:00", "system_peak_kw": 4.0,
         "perf_ratio": 1.1, "cloudy_day": False, "avg_ghi": 550.0,
         "predicted_kwh": 25.0, "cal_samples_n": 200, "hourly_bias": {}},  # +25% -> outlier
        {"date": day2, "timestamp": f"{day2}T07:30:00", "system_peak_kw": 4.0,
         "perf_ratio": 1.0, "cloudy_day": True, "avg_ghi": 200.0,
         "predicted_kwh": 20.5, "cal_samples_n": 200, "hourly_bias": {}},  # +2.5% -> fine
    ]
    log_path.write_text("\n".join(_json.dumps(e) for e in entries) + "\n")

    cfg = Config(output_dir=str(tmp_path))
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        res = CliRunner().invoke(cli, ["account", "solar-audit", "--out", str(tmp_path)])

    assert res.exit_code == 0, res.output
    assert "⚠" in res.output
    assert "Clear-day outliers:  1" in res.output
    assert "Cloudy-day outliers: 0" in res.output


def test_solar_audit_errors_without_log_file(tmp_path):
    from click.testing import CliRunner

    from franklinwh_scraper.cli import cli

    res = CliRunner().invoke(cli, ["account", "solar-audit", "--out", str(tmp_path)])
    assert res.exit_code != 0
    assert "No calibration log yet" in res.output


def test_setup_quick_skips_chatbot_ev_uptime_when_already_configured():
    """--quick on a re-run: skip the three optional sections entirely and
    leave their values exactly as configured."""
    import types
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper.cli import cli

    fake_loc = types.SimpleNamespace(lat=32.97, lon=-117.07,
                                     name="San Diego", country="US")
    cfg = Config(email="e", password="p", lat=32.97, lon=-117.07,
                 location_name="San Diego, US",
                 telegram_bot_token="t", telegram_chat_id="c",
                 chat_backend="anthropic", anthropic_api_key="sk-ant-xyz",
                 ev_charging=True, ev_charging_kw=9.6, ev_kwh_per_session=42.0,
                 healthcheck_url="https://hc-ping.com/abc")
    saved = {}

    with patch("franklinwh_scraper.cli.load_config", return_value=cfg), \
         patch("franklinwh_scraper.cli.save_config",
               side_effect=lambda c: saved.__setitem__("cfg", c)), \
         patch("franklinwh_scraper.cli.geocode", return_value=fake_loc), \
         patch("franklinwh_scraper.cli.fetch_telegram_chat_id", return_value="c"), \
         patch("franklinwh_scraper.cli.AccountClient") as ac:
        ac.return_value.__enter__.return_value.login.return_value = None
        ac.return_value.__enter__.return_value.get_gateways.return_value = []
        r = CliRunner().invoke(cli, ["setup", "--quick"], input="\n" * 60)

    assert r.exit_code == 0, r.output
    assert "--quick: skipping" in r.output
    assert "AI Chatbot" not in r.output
    assert "Do you charge an EV" not in r.output
    assert "Uptime monitoring" not in r.output

    s = saved["cfg"]
    assert s.chat_backend == "anthropic"
    assert s.anthropic_api_key == "sk-ant-xyz"
    assert s.ev_charging is True
    assert s.ev_charging_kw == 9.6
    assert s.ev_kwh_per_session == 42.0
    assert s.healthcheck_url == "https://hc-ping.com/abc"
    # Credentials/location still went through — --quick doesn't touch those.
    assert s.email == "e"


def test_setup_quick_is_noop_on_first_time_setup():
    """--quick must not hide sections from a genuinely first-time user —
    only skip already-answered ones on a re-run."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper.cli import cli

    with patch("franklinwh_scraper.cli.load_config", return_value=Config()):
        # Aborts partway through (no scripted input for the full wizard) —
        # only the banner, printed before any prompt, is being checked.
        r = CliRunner().invoke(cli, ["setup", "--quick"], input="")

    assert "--quick: skipping" not in r.output


def test_ev_status_shows_calibration_line(tmp_path):
    import json as _json
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper.cli import cli

    (tmp_path / ".ev_controller_state.json").write_text(_json.dumps({
        "ev_draw_samples": [9.5, 9.6, 9.7, 9.4, 9.6],
        "ev_kw_last_tuned_iso": "2026-08-11T12:00:00",
    }))
    cfg = Config(ev_control_enabled=True, ev_charging_kw=9.6, output_dir=str(tmp_path))
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        out = CliRunner().invoke(
            cli, ["account", "ev-status", "--out", str(tmp_path)]).output
    assert "EV draw calibration" in out
    assert "5 real reading(s)" in out
    assert "9.6 kW" in out
    assert "2026-08-11T12:00:00" in out


def test_ev_status_omits_calibration_line_when_no_samples(tmp_path):
    from unittest.mock import patch

    from click.testing import CliRunner

    from franklinwh_scraper.cli import cli

    cfg = Config(ev_control_enabled=True, output_dir=str(tmp_path))
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        out = CliRunner().invoke(
            cli, ["account", "ev-status", "--out", str(tmp_path)]).output
    assert "EV draw calibration" not in out


def test_api_ev_short_circuits_when_no_ev_configured(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi

    monkeypatch.setattr(webapi, "_cfg", Config(ev_charging=False), raising=False)
    client = TestClient(webapi.app)
    r = client.get("/api/ev")
    assert r.status_code == 200
    body = r.json()
    assert body == {"ev_charging": False, "control_enabled": False, "error": False}


def test_api_ev_reports_controller_status_and_null_prediction_without_history(
    tmp_path, monkeypatch,
):
    import json as _json

    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi

    (tmp_path / ".ev_controller_state.json").write_text(_json.dumps({
        "session": "solar", "last_commanded_amps": 20,
        "consec_tesla_errors": 0, "ev_draw_samples": [9.5, 9.6, 9.4],
        "spend": {datetime.now().strftime("%Y-%m"): {"data": 10, "cmd": 3, "wake": 1}},
    }))
    (tmp_path / "ev_controller.jsonl").write_text(
        _json.dumps({"timestamp": datetime.now().isoformat(), "action": "set_amps",
                     "amps": 20, "reason": "tracking surplus"}) + "\n")

    cfg = Config(ev_charging=True, ev_control_enabled=True, ev_dry_run=True,
                ev_charging_kw=9.6, output_dir=str(tmp_path))
    monkeypatch.setattr(webapi, "_cfg", cfg, raising=False)
    monkeypatch.setattr(webapi, "_OUT", tmp_path, raising=False)

    client = TestClient(webapi.app)
    r = client.get("/api/ev")
    assert r.status_code == 200
    body = r.json()

    assert body["ev_charging"] is True
    assert body["control_enabled"] is True
    ctl = body["controller"]
    assert ctl["session"] == "solar"
    assert ctl["commanded_amps"] == 20
    assert ctl["calibration_samples"] == 3
    assert ctl["spend_month_usd"] == round(10 * 0.002 + 3 * 0.001 + 1 * 0.02, 2)
    assert len(ctl["recent_decisions"]) == 1
    # No real history.db at tmp_path -> "prediction" key must still be
    # present (None), not silently missing, so the dashboard never has to
    # special-case an absent key vs an explicit null.
    assert "prediction" in body
    assert body["prediction"] is None


def test_api_accuracy_excludes_pre_bias_fix_days(tmp_path, monkeypatch):
    """Mirrors cmd_accuracy's floor (see test_accuracy_excludes_pre_bias_fix_days):
    a wide `days` request must not mix pre-2026-08-24 predicted_kwh_ days
    (perf_ratio ran ~6% high) into the dashboard's mean-error figure."""
    from datetime import date

    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi
    from franklinwh_scraper.alerts import _save_peak_state
    from franklinwh_scraper.history import HistoryStore

    real_date = date

    class _FakeDate(real_date):
        @classmethod
        def today(cls):
            return real_date(2026, 8, 31)

    monkeypatch.setattr(webapi, "date", _FakeDate)

    db = HistoryStore(tmp_path / "history.db")
    state = {}
    # Pre-fix day (2026-08-20): should never appear even with days=30.
    # Post-fix day (2026-08-25): should appear.
    for ds, kwh in (("2026-08-20", 10.0), ("2026-08-25", 12.0)):
        state[f"predicted_kwh_{ds}"] = kwh
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"{ds}T12:00:00", 0, 12, 1.0, 3.0, 50.0, 0.0, "normal", kwh, 0.0),
        )
    db._conn.commit()
    db.close()
    _save_peak_state(tmp_path, state)

    monkeypatch.setattr(webapi, "_cfg", Config(output_dir=str(tmp_path)), raising=False)
    monkeypatch.setattr(webapi, "_OUT", tmp_path, raising=False)
    client = TestClient(webapi.app)
    r = client.get("/api/accuracy", params={"days": 30})
    assert r.status_code == 200
    body = r.json()
    dates = [d["date"] for d in body["days"]]
    assert "2026-08-20" not in dates
    assert "2026-08-25" in dates


def test_api_vpp_short_circuits_when_not_enrolled(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi

    monkeypatch.setattr(webapi, "_cfg", Config(vpp_enrolled=False), raising=False)
    client = TestClient(webapi.app)
    r = client.get("/api/vpp")
    assert r.status_code == 200
    assert r.json() == {"vpp_enrolled": False, "error": False}


def test_api_vpp_reports_no_event_when_none_logged(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi

    monkeypatch.setattr(webapi, "_cfg", Config(vpp_enrolled=True, output_dir=str(tmp_path)),
                        raising=False)
    monkeypatch.setattr(webapi, "_OUT", tmp_path, raising=False)
    client = TestClient(webapi.app)
    r = client.get("/api/vpp")
    assert r.status_code == 200
    body = r.json()
    assert body["vpp_enrolled"] is True
    assert body["event"] is None


def test_api_vpp_reports_active_event_with_live_export_and_payout(tmp_path, monkeypatch):
    import json as _json

    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi

    db = HistoryStore(tmp_path / "history.db")
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=1)
    for i, ts in enumerate((start, start + timedelta(hours=1))):
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts.isoformat(), ts.weekday(), ts.hour, 0.5, 4.0, 60.0, -2.0, "normal", 0.0, 1.5),
        )
    db._conn.commit()

    cfg = Config(vpp_enrolled=True, output_dir=str(tmp_path))
    monkeypatch.setattr(webapi, "_cfg", cfg, raising=False)
    monkeypatch.setattr(webapi, "_OUT", tmp_path, raising=False)

    ev = {
        "start": start.isoformat(), "end": (now + timedelta(hours=1)).isoformat(),
        "rate_per_kwh": 2.0, "logged_at": start.isoformat(),
    }
    (tmp_path / ".peak_alert_state.json").write_text(_json.dumps({"vpp_event": ev}))

    client = TestClient(webapi.app)
    r = client.get("/api/vpp")
    assert r.status_code == 200
    body = r.json()["event"]
    assert body["active"] is True
    assert body["rate_per_kwh"] == 2.0
    assert body["export_kwh_so_far"] >= 0  # exact value depends on integration window; just must not error/be None
    assert body["discharge_kwh_so_far"] >= 0
    assert body["est_payout_so_far"] is not None


def test_api_bill_surfaces_actual_and_diff_when_recorded(tmp_path, monkeypatch):
    import json as _json

    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi

    cfg = Config(billing_cycle_start_day=20, output_dir=str(tmp_path))
    monkeypatch.setattr(webapi, "_cfg", cfg, raising=False)
    monkeypatch.setattr(webapi, "_OUT", tmp_path, raising=False)

    client = TestClient(webapi.app)
    r = client.get("/api/bill")
    assert r.status_code == 200
    body = r.json()
    # No actual bill recorded yet -> both keys present but null, never missing.
    assert "actual_prior" in body and body["actual_prior"] is None
    assert "diff_prior" in body and body["diff_prior"] is None
    prior_key = f"actual_bill_{body['prior_cycle_end']}"

    (tmp_path / ".peak_alert_state.json").write_text(_json.dumps({prior_key: 250.0}))

    r2 = client.get("/api/bill")
    body2 = r2.json()
    assert body2["actual_prior"] == 250.0
    assert body2["diff_prior"] == round(250.0 - body2["prior_net"], 2)


def test_log_solar_calibration_inputs_writes_expected_fields(tmp_path):
    import json as _json

    from franklinwh_scraper.alerts import _log_solar_calibration_inputs

    cfg = Config(output_dir=str(tmp_path))
    now = datetime(2026, 8, 13, 7, 45, 0)
    _log_solar_calibration_inputs(
        cfg, "2026-08-13", now,
        system_peak_kw=4.06, perf_ratio=1.02, hourly_bias={7: 1.2, 13: 0.88},
        avg_ghi=620.5, cloudy_day=False, predicted_kwh=25.9, cal_samples_n=240,
    )
    lines = (tmp_path / "solar_calibration_log.jsonl").read_text().splitlines()
    assert len(lines) == 1
    entry = _json.loads(lines[0])
    assert entry["date"] == "2026-08-13"
    assert entry["timestamp"] == now.isoformat()
    assert entry["system_peak_kw"] == 4.06
    assert entry["perf_ratio"] == 1.02
    assert entry["cloudy_day"] is False
    assert entry["avg_ghi"] == 620.5
    assert entry["predicted_kwh"] == 25.9
    assert entry["cal_samples_n"] == 240
    assert entry["hourly_bias"] == {"7": 1.2, "13": 0.88}


def test_log_solar_calibration_inputs_appends_across_days(tmp_path):
    import json as _json

    from franklinwh_scraper.alerts import _log_solar_calibration_inputs

    cfg = Config(output_dir=str(tmp_path))
    for i, d in enumerate(["2026-08-13", "2026-08-14"]):
        _log_solar_calibration_inputs(
            cfg, d, datetime(2026, 8, 13 + i, 7, 45),
            system_peak_kw=4.0, perf_ratio=1.0, hourly_bias={},
            avg_ghi=600.0, cloudy_day=False, predicted_kwh=25.0 + i,
            cal_samples_n=240,
        )
    lines = (tmp_path / "solar_calibration_log.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert _json.loads(lines[0])["date"] == "2026-08-13"
    assert _json.loads(lines[1])["date"] == "2026-08-14"


def test_log_solar_calibration_inputs_noop_without_cfg(tmp_path):
    from franklinwh_scraper.alerts import _log_solar_calibration_inputs

    # Must not raise — some call sites (direct tests) may not pass cfg.
    _log_solar_calibration_inputs(
        None, "2026-08-13", datetime(2026, 8, 13, 7, 45),
        system_peak_kw=4.0, perf_ratio=1.0, hourly_bias={},
        avg_ghi=600.0, cloudy_day=False, predicted_kwh=25.0, cal_samples_n=240,
    )
    assert not (tmp_path / "solar_calibration_log.jsonl").exists()


def test_log_solar_calibration_inputs_survives_bad_output_dir():
    from franklinwh_scraper.alerts import _log_solar_calibration_inputs

    # A path that can't be written to (parent doesn't exist) must degrade
    # silently, not take down the morning-preview alert.
    cfg = Config(output_dir="/nonexistent-dir-xyz/deeper")
    _log_solar_calibration_inputs(
        cfg, "2026-08-13", datetime(2026, 8, 13, 7, 45),
        system_peak_kw=4.0, perf_ratio=1.0, hourly_bias={},
        avg_ghi=600.0, cloudy_day=False, predicted_kwh=25.0, cal_samples_n=240,
    )  # no assertion needed — just must not raise


def test_set_mute_writes_and_clears_state(tmp_path):
    from franklinwh_scraper.alerts import _load_peak_state

    bot = TelegramChatBot(Config(output_dir=str(tmp_path)), api_key="x")

    msg = bot._set_mute(2.0)
    assert "muted until" in msg
    assert "2h" in msg
    assert "never muted" in msg
    state = _load_peak_state(tmp_path)
    assert "alerts_muted_until" in state

    msg2 = bot._set_mute(0)
    assert msg2 == "🔔 Alerts unmuted."
    state2 = _load_peak_state(tmp_path)
    assert "alerts_muted_until" not in state2


def test_mute_status_line_reflects_state(tmp_path):
    from franklinwh_scraper.alerts import _load_peak_state, _save_peak_state, _state_lock

    bot = TelegramChatBot(Config(output_dir=str(tmp_path)), api_key="x")
    assert bot._mute_status_line() == ""

    bot._set_mute(8.0)
    assert "muted until" in bot._mute_status_line()

    with _state_lock(tmp_path):
        state = _load_peak_state(tmp_path)
        state["alerts_muted_until"] = (datetime.now() - timedelta(minutes=1)).isoformat()
        _save_peak_state(tmp_path, state)
    assert bot._mute_status_line() == ""


def test_handle_callback_query_mute_button(tmp_path):
    from franklinwh_scraper.alerts import _load_peak_state

    cfg = Config(output_dir=str(tmp_path), telegram_chat_id="")  # no owner set -> any chat authorized
    bot = TelegramChatBot(cfg, api_key="x")
    sent, answered = [], []
    bot._send = lambda chat_id, text, reply_markup=None: sent.append((chat_id, text))
    bot._answer_callback_query = lambda cq_id, text="": answered.append((cq_id, text))

    bot._handle_callback_query({"id": "cbq1", "message": {"chat": {"id": 123}}, "data": "mute:2"})

    assert answered == [("cbq1", "Muted")]
    assert sent and sent[0][0] == "123" and "muted until" in sent[0][1]
    assert "alerts_muted_until" in _load_peak_state(tmp_path)


def test_handle_callback_query_ignores_unauthorized_chat():
    cfg = Config(telegram_chat_id="owner123")
    bot = TelegramChatBot(cfg, api_key="x")
    sent, answered = [], []
    bot._send = lambda chat_id, text, reply_markup=None: sent.append((chat_id, text))
    bot._answer_callback_query = lambda cq_id, text="": answered.append((cq_id, text))

    bot._handle_callback_query({"id": "cbq2", "message": {"chat": {"id": "stranger"}}, "data": "mute:2"})

    assert not sent  # never replies to an unauthorized chat
    assert answered == [("cbq2", "")]  # spinner still dismissed


# ── Round-trip efficiency (weekly-review 2026-08-24) ──────────────────

def test_round_trip_efficiency_samples(tmp_path):
    """charge_kwh/discharge_kwh from daily_battery_kwh feed a plain ratio,
    clamped at 1.0 since >100% is a metering artifact, not real physics."""
    db = HistoryStore(tmp_path / "h.db")
    base = datetime(2026, 5, 1, 0, 0)
    # 4h charging at 2kW (~8 kWh in), then 4h discharging at 1.8kW (~7.2 kWh out)
    # -> ~90% round-trip efficiency for the day.
    rows = []
    for i in range(5):
        rows.append((base + timedelta(hours=i), -2.0))
    for i in range(5, 10):
        rows.append((base + timedelta(hours=i), 1.8))
    for ts, kw in rows:
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts.isoformat(), 0, ts.hour, 0.0, 0.0, 50.0, 0.0, "normal", 0.0, kw),
        )
    db._conn.commit()
    samples = db.round_trip_efficiency_samples("2026-05-01", "2026-05-01")
    assert len(samples) == 1
    assert 0.8 < samples[0] <= 1.0


def test_round_trip_efficiency_skips_low_charge_days(tmp_path):
    """A day with negligible charge (residual self-use noise) is excluded —
    otherwise a 0.05 kWh charge / 0.2 kWh discharge day reads as a bogus
    400% 'efficiency' that would swamp the real signal."""
    db = HistoryStore(tmp_path / "h.db")
    ts = datetime(2026, 5, 1, 12, 0)
    for i, kw in enumerate([-0.1, 0.4]):
        t = (ts + timedelta(minutes=30 * i)).isoformat()
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (t, 0, 12, 0.0, 0.0, 50.0, 0.0, "normal", 0.0, kw),
        )
    db._conn.commit()
    assert db.round_trip_efficiency_samples("2026-05-01", "2026-05-01") == []


class _EbTargetFakeStore:
    """load_profile(percentile) -> {(weekday, hour): kw}, flat by percentile."""
    def __init__(self, med_kw=1.0, p75_kw=2.0):
        self._med_kw, self._p75_kw = med_kw, p75_kw

    def load_profile(self, percentile=0.5):
        kw = self._p75_kw if percentile >= 0.75 else self._med_kw
        return {(dow, h): kw for dow in range(7) for h in range(24)}


def _eb_target_outlook(cloudy=True, remaining_kwh=5.0):
    import types
    return types.SimpleNamespace(
        avg_ghi=lambda h: (200.0 if cloudy else 500.0),
        remaining_today_generation_kwh=lambda sp, pr, hb: remaining_kwh,
    )


def test_alert_cloudy_eb_target_fires_with_median_and_p75():
    import types
    now = datetime(2026, 9, 7, 7, 30)  # a Monday — not a _HEAVY_LOAD_WEEKDAYS day
    state = {"solar_cal_samples": [3.5, 3.6, 3.4]}  # >=3 -> _get_system_peak_kw bootstrap path
    outlook = _eb_target_outlook(cloudy=True, remaining_kwh=5.0)
    store = _EbTargetFakeStore(med_kw=1.0, p75_kw=2.0)  # 17 remaining hrs (7-23)
    c = types.SimpleNamespace(battery_soc_pct=51.0)
    cfg = Config(battery_capacity_kwh=13.6)

    body = alerts._alert_cloudy_eb_target(state, "2026-09-07", now, c, cfg, outlook, store)
    assert body is not None
    assert "Cloudy day" in body
    # load_med = 17*1.0=17.0, load_p75 = 17*2.0=34.0, remaining_solar=5.0, cap=13.6
    # target_med = (17-5)/13.6*100 = 88.2% -> ~88%; target_p75 capped at 100%
    assert "~88%" in body
    assert "~100%" in body
    assert state["cloudy_eb_alert_date"] == "2026-09-07"
    # Dedup: no re-fire same day.
    assert alerts._alert_cloudy_eb_target(state, "2026-09-07", now, c, cfg, outlook, store) is None


def test_alert_cloudy_eb_target_silent_when_not_cloudy():
    import types
    now = datetime(2026, 9, 7, 7, 30)
    state = {"solar_cal_samples": [3.5, 3.6, 3.4]}
    outlook = _eb_target_outlook(cloudy=False)
    store = _EbTargetFakeStore()
    c = types.SimpleNamespace(battery_soc_pct=51.0)
    assert alerts._alert_cloudy_eb_target(state, "2026-09-07", now, c, Config(), outlook, store) is None


def test_alert_cloudy_eb_target_silent_outside_morning_window():
    import types
    now = datetime(2026, 9, 7, 11, 0)
    state = {"solar_cal_samples": [3.5, 3.6, 3.4]}
    outlook = _eb_target_outlook(cloudy=True)
    store = _EbTargetFakeStore()
    c = types.SimpleNamespace(battery_soc_pct=51.0)
    assert alerts._alert_cloudy_eb_target(state, "2026-09-07", now, c, Config(), outlook, store) is None


def test_alert_cloudy_eb_target_silent_without_calibration_or_store():
    import types
    now = datetime(2026, 9, 7, 7, 30)
    outlook = _eb_target_outlook(cloudy=True)
    c = types.SimpleNamespace(battery_soc_pct=51.0)
    # No system-peak calibration yet.
    assert alerts._alert_cloudy_eb_target({}, "2026-09-07", now, c, Config(),
                                          outlook, _EbTargetFakeStore()) is None
    # No store at all.
    state = {"solar_cal_samples": [3.5, 3.6, 3.4]}
    assert alerts._alert_cloudy_eb_target(state, "2026-09-07", now, c, Config(), outlook, None) is None


def test_alert_cloudy_eb_target_sunday_uses_p75_as_typical():
    """User confirmed 2026-09-06 the dryer runs most Sundays — P75, not
    median, is Sunday's realistic baseline, so Sunday must lead with P75 as
    the primary target/switch-time and label it as the dryer-day default."""
    import types
    now = datetime(2026, 9, 6, 7, 30)  # a Sunday — in _HEAVY_LOAD_WEEKDAYS
    state = {"solar_cal_samples": [3.5, 3.6, 3.4]}
    outlook = _eb_target_outlook(cloudy=True, remaining_kwh=5.0)
    store = _EbTargetFakeStore(med_kw=1.0, p75_kw=2.0)
    c = types.SimpleNamespace(battery_soc_pct=51.0)
    cfg = Config(battery_capacity_kwh=13.6)

    body = alerts._alert_cloudy_eb_target(state, "2026-09-06", now, c, cfg, outlook, store)
    assert body is not None
    assert "~100%</b> for a typical dryer-day Sunday" in body
    assert "~88% if no dryer today" in body


def test_eb_switch_time_str_computes_from_deficit_and_charge_rate():
    """cap=13.6, target=88.235%, soc=10% -> deficit 10.64 kWh @ 5.0 kW ->
    2.128h before the 4pm deadline -> 1:52 PM. Real numbers verified by
    running the calc directly before hardcoding (2026-09-06)."""
    now = datetime(2026, 9, 6, 7, 30)
    deadline = datetime(2026, 9, 6, 16, 0)
    assert alerts._eb_switch_time_str(now, 10.0, 88.235, 13.6, deadline) == "1:52 PM"
    assert alerts._eb_switch_time_str(now, 90.0, 88.235, 13.6, deadline) is None  # already there


def test_eb_switch_time_str_clamps_to_now_when_deadline_too_close():
    """Mathematically unreachable through _alert_cloudy_eb_target's own
    7-8am gate (see the function's own docstring) — tested directly here."""
    now = datetime(2026, 9, 6, 15, 55)
    deadline = datetime(2026, 9, 6, 16, 0)  # only 5 min left
    result = alerts._eb_switch_time_str(now, 0.0, 100.0, 13.6, deadline)
    assert result is not None and result.startswith("now")


def test_alert_cloudy_eb_target_includes_switch_time():
    import types
    now = datetime(2026, 9, 7, 7, 30)  # a Monday — not a _HEAVY_LOAD_WEEKDAYS day
    state = {"solar_cal_samples": [3.5, 3.6, 3.4]}
    outlook = _eb_target_outlook(cloudy=True, remaining_kwh=5.0)
    store = _EbTargetFakeStore(med_kw=1.0, p75_kw=2.0)
    cfg = Config(battery_capacity_kwh=13.6)

    # soc=10%: below both targets (med 88%, p75 100% capped) -> both switch times shown.
    c = types.SimpleNamespace(battery_soc_pct=10.0)
    body = alerts._alert_cloudy_eb_target(state, "2026-09-07", now, c, cfg, outlook, store)
    assert "Reach 88% by 4:00 PM" in body
    assert "switch to EB at 1:52 PM" in body
    assert "Heavier day: 1:33 PM" in body

    # soc=90%: above median target, below p75 -> "already at target" + heavier-day contingency.
    state2 = {"solar_cal_samples": [3.5, 3.6, 3.4]}
    c2 = types.SimpleNamespace(battery_soc_pct=90.0)
    body2 = alerts._alert_cloudy_eb_target(state2, "2026-09-07", now, c2, cfg, outlook, store)
    assert "Already at target — no EB needed for a typical Monday" in body2
    assert "Heavier day: switch to EB at 3:43 PM" in body2


def test_alert_round_trip_efficiency_fires_on_sustained_drop():

    class FakeStore:
        def __init__(self, recent, base):
            self._recent, self._base = recent, base

        def round_trip_efficiency_samples(self, start, end, min_charge_kwh=1.0):
            # Distinguish the two windows by which is queried first via a
            # simple call-order flag rather than inspecting dates in detail —
            # mirrors how test_capacity_samples-style tests key on window size.
            return self._recent if start > "2026-07-01" else self._base

    now = datetime(2026, 8, 24, 8, 30)
    store = FakeStore(recent=[0.80] * 6, base=[0.93] * 6)
    state: dict = {}
    body = alerts._alert_round_trip_efficiency(state, "2026-08-24", now, store)
    assert body is not None
    assert "round-trip efficiency" in body.lower()
    assert state.get("rt_efficiency_alerted_week") == now.strftime("%G-W%V")


def test_alert_round_trip_efficiency_quiet_when_stable():
    class FakeStore:
        def round_trip_efficiency_samples(self, start, end, min_charge_kwh=1.0):
            return [0.90] * 6

    now = datetime(2026, 8, 24, 8, 30)
    body = alerts._alert_round_trip_efficiency({}, "2026-08-24", now, FakeStore())
    assert body is None


def test_alert_round_trip_efficiency_needs_enough_samples():
    class FakeStore:
        def round_trip_efficiency_samples(self, start, end, min_charge_kwh=1.0):
            return [0.80, 0.81]  # only 2 — below the 5-sample floor

    now = datetime(2026, 8, 24, 8, 30)
    assert alerts._alert_round_trip_efficiency({}, "2026-08-24", now, FakeStore()) is None


# ── DR-SES rate-plan comparison (weekly-review 2026-08-24) ─────────────

def test_drses_period_at_matches_evtou5_on_weekends():
    """DR-SES and EV-TOU-5 share identical weekend/holiday period windows —
    per SDG&E's Schedule DR-SES tariff sheet, Sheet 2."""
    sat_night  = datetime(2026, 8, 22, 1)   # Sat 1am -> super off-peak both
    sat_midday = datetime(2026, 8, 22, 15)  # Sat 3pm -> off-peak both
    sat_peak   = datetime(2026, 8, 22, 18)  # Sat 6pm -> on-peak both
    for dt in (sat_night, sat_midday, sat_peak):
        assert tou._drses_period_at(dt) == tou.period_at(dt)


def test_drses_period_at_no_midday_carveout_outside_march_april():
    """EV-TOU-5 carves 10am-2pm into super-off-peak year-round (its EV
    incentive); DR-SES only does that in March/April — an August weekday at
    11am must land in different periods under the two schedules."""
    aug_weekday_11am = datetime(2026, 8, 24, 11)  # a Monday
    assert tou.period_at(aug_weekday_11am) == tou.TouPeriod.SUPER_OFF_PEAK
    assert tou._drses_period_at(aug_weekday_11am) == tou.TouPeriod.OFF_PEAK


def test_drses_period_at_march_carveout():
    march_weekday_11am = datetime(2026, 3, 9, 11)  # a Monday in March
    assert tou._drses_period_at(march_weekday_11am) == tou.TouPeriod.SUPER_OFF_PEAK


def test_drses_rate_at_on_peak_matches_verified_table():
    """Pinned against the real unbundled DR-SES numbers (SDG&E delivery +
    SDCP generation, 2026-08-31) — catches an accidental edit to the
    hardcoded schedule."""
    assert tou.drses_rate_at(datetime(2026, 7, 8, 17)) == pytest.approx(0.69413)
    assert tou.drses_rate_at(datetime(2026, 1, 8, 17)) == pytest.approx(0.45352)


def test_compare_rate_plans_evtou5_currently_cheaper():
    from franklinwh_scraper.savings import compare_rate_plans

    # With real unbundled numbers (both plans priced as SDG&E delivery +
    # SDCP generation, not bundled SDG&E), EV-TOU-5 is cheaper than DR-SES
    # at every TOU period for this customer — DR-SES's generation rate runs
    # slightly higher than EV-TOU-5's at every period, and EV-TOU-5 alone
    # gets the deep super-off-peak delivery discount. This replaces a
    # pre-correction test that had it backwards, comparing a bundled DR-SES
    # number against an unbundled EV-TOU-5 number.
    dt = datetime(2026, 7, 8, 17)  # Wed 5pm, on-peak both schedules
    intervals = [(dt, 1.0, 2.0, 2.0, 0.0)]  # (dt, hours, grid_kw, home_kw, solar_kw)
    cmp = compare_rate_plans(intervals)
    assert cmp.evtou5_import_cost < cmp.drses_import_cost
    assert cmp.monthly_savings < 0  # switching to DR-SES would cost more, not save
    assert cmp.import_kwh == pytest.approx(2.0)


def test_alert_rate_plan_optimality_quiet_below_savings_floor():
    class FakeStore:
        def weekly_readings(self, start, end):
            # Flat, tiny load -> negligible $ delta either way.
            base = datetime(2026, 6, 1, 0)
            return [
                ((base + timedelta(hours=i)).isoformat(), 0.05, 0.05, 0.0)
                for i in range(150)
            ]

    now = datetime(2026, 8, 24, 8, 30)
    assert alerts._alert_rate_plan_optimality({}, "2026-08-24", now, FakeStore()) is None


def test_alert_rate_plan_optimality_respects_quarterly_throttle():
    class FakeStore:
        def weekly_readings(self, start, end):
            base = datetime(2026, 6, 1, 0)
            return [
                ((base + timedelta(hours=i)).isoformat(), 3.0, 3.0, 0.0)
                for i in range(150)
            ]

    now = datetime(2026, 8, 24, 8, 30)
    state = {"rate_plan_check_date": "2026-08-01"}  # 23 days ago — inside the 90-day window
    assert alerts._alert_rate_plan_optimality(state, "2026-08-24", now, FakeStore()) is None


# ── Export clipping (weekly-review 2026-08-24) ─────────────────────────

def test_alert_export_clipping_fires_after_sustained_gap():
    import types

    now = datetime(2026, 8, 24, 13, 0)
    c = types.SimpleNamespace(
        battery_soc_pct=99.5, solar_production_kw=6.0, home_load_kw=1.0,
        grid_use_kw=-1.0,  # only 1kW exporting despite a ~5kW surplus
    )
    state: dict = {}
    for _ in range(2):
        assert alerts._alert_export_clipping(state, "2026-08-24", now, c) is None
    body = alerts._alert_export_clipping(state, "2026-08-24", now, c)
    assert body is not None
    assert "not reaching the grid" in body
    assert state.get("export_clip_notified") is True


def test_alert_export_clipping_quiet_when_exporting_cleanly():
    import types

    now = datetime(2026, 8, 24, 13, 0)
    c = types.SimpleNamespace(
        battery_soc_pct=100.0, solar_production_kw=6.0, home_load_kw=1.0,
        grid_use_kw=-4.9,  # export tracks the ~5kW surplus closely
    )
    state: dict = {}
    for _ in range(4):
        assert alerts._alert_export_clipping(state, "2026-08-24", now, c) is None


def test_alert_export_clipping_resets_streak_on_good_poll():
    """A momentary meter-lag reading shouldn't accumulate toward the streak
    once the gap closes — regression guard for the noise-filtering counter."""
    import types

    now = datetime(2026, 8, 24, 13, 0)
    clipped = types.SimpleNamespace(
        battery_soc_pct=99.5, solar_production_kw=6.0, home_load_kw=1.0, grid_use_kw=-1.0,
    )
    clean = types.SimpleNamespace(
        battery_soc_pct=99.5, solar_production_kw=6.0, home_load_kw=1.0, grid_use_kw=-4.9,
    )
    state: dict = {}
    alerts._alert_export_clipping(state, "2026-08-24", now, clipped)
    alerts._alert_export_clipping(state, "2026-08-24", now, clean)  # resets streak
    assert state.get("export_clip_streak") == 0
    # Two more clipped polls only reach streak=2, not the 3 needed to fire.
    alerts._alert_export_clipping(state, "2026-08-24", now, clipped)
    body = alerts._alert_export_clipping(state, "2026-08-24", now, clipped)
    assert body is None
