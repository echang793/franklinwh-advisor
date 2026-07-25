"""Unit tests for FranklinWH pure logic — no network."""

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
    db = HistoryStore(tmp_path / "h.db")
    base = datetime(2026, 5, 1, 18, 0)
    soc = 100.0
    for i in range(9):  # 100→60% over 4h at 1.36 kW (13.6 kWh battery)
        ts = (base + timedelta(minutes=30 * i)).isoformat()
        db._conn.execute(
            "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
            "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, 0, 18, 1.36, 0.0, soc, 0.0, "normal", 0.0, -1.36),
        )
        soc -= 5.0
    db._conn.commit()
    samples = db.capacity_samples("2026-05-01", "2026-05-02")
    assert samples and 13.0 < samples[0] < 14.5


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
    assert tou.peak_export_hour(8) == (18, 1.022)
    assert tou.peak_export_hour(9) == (19, 0.673)
    # Outside Aug/Sep, falls back to the flat NBT floor rate instead of the
    # old hard None gate — we don't have SDG&E's published per-hour export
    # schedule for other months, so this is an honest flat number, not a
    # fabricated hourly one.
    assert tou.peak_export_hour(7) == (18, tou._NEM3_DEFAULT_EXPORT_RATE)
    assert tou.peak_export_hour(12) == (18, tou._NEM3_DEFAULT_EXPORT_RATE)


def test_alert_enabled():
    cfg = Config()
    assert alerts._alert_enabled(cfg, "morning_preview")
    cfg.disabled_alerts = ["morning_preview"]
    assert not alerts._alert_enabled(cfg, "morning_preview")
    # always-on can't be disabled
    cfg.disabled_alerts = ["grid_down", "fast_drain"]
    assert alerts._alert_enabled(cfg, "grid_down")
    assert alerts._alert_enabled(cfg, "fast_drain")


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
        "solar_degradation_alerted_week": old_week,
        f"predicted_kwh_{datetime.now().strftime('%Y-%m-%d')}": 30.0,  # keep: today
    }
    pruned = alerts._prune_old_state(state)
    assert f"predicted_kwh_{old_date}" not in pruned
    assert f"predicted_avg_ghi_{old_date}" not in pruned
    assert f"daily_import_cost_{old_date}" not in pruned
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
    msg = alerts._alert_fast_drain(state, "2026-07-15", now, c)
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


def test_send_sundown_projects_soc_to_last_solar_hour():
    """/sundown should project SoC forward using the forecast, stopping at
    the last hour today still expecting real solar — not a fixed horizon."""
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
