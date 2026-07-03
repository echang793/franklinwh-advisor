"""Unit tests for FranklinWH pure logic — no network."""

import pathlib
import sys
from datetime import datetime

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from franklinwh_scraper import alerts, tou
from franklinwh_scraper.history import HistoryStore, integrate_intervals
from franklinwh_scraper.config import Config


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
    from datetime import timedelta
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


# ── CLI helpers ────────────────────────────────────────────────────────

def test_peak_export_hour():
    assert tou.peak_export_hour(8) == (18, 1.022)
    assert tou.peak_export_hour(9) == (19, 0.673)
    assert tou.peak_export_hour(7) is None
    assert tou.peak_export_hour(12) is None


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
    alerts._calibrate_solar(state, solar_kw=8.0, outlook=outlook)  # ratio ~11.4, way off
    assert len(state["solar_cal_samples"]) == 10
    assert state["solar_cal_pending"] == [pytest.approx(11.43)]


def test_calibrate_solar_accepts_consistent_step_change():
    import types
    outlook = types.SimpleNamespace(avg_ghi=lambda h: 700.0)
    state = {"solar_cal_samples": [2.5] * 10}
    # Three consecutive, mutually-consistent readings well above the old
    # baseline (panels cleaned, shading removed) should be accepted as real.
    for _ in range(3):
        alerts._calibrate_solar(state, solar_kw=4.55, outlook=outlook)  # ratio 6.5
    assert len(state["solar_cal_samples"]) == 13
    assert state["solar_cal_pending"] == []
