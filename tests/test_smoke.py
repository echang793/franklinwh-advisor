"""Integration smoke tests — alert functions + notifiers don't crash. No network."""

import pathlib
import sys
import types
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from franklinwh_scraper import alerts, notifier
from franklinwh_scraper.config import Config
from franklinwh_scraper.history import HistoryStore
from franklinwh_scraper.predictor import HourPrediction, UsageForecast  # noqa: F401  used below
from franklinwh_scraper.weather import HourlyForecast, SolarOutlook


def _fake_stats(**cur_over):
    cur = types.SimpleNamespace(
        grid_status="normal", battery_soc_pct=72.0, home_load_kw=1.2,
        solar_production_kw=2.0, battery_use_kw=-0.5, grid_use_kw=0.0,
    )
    for k, v in cur_over.items():
        setattr(cur, k, v)
    tot = types.SimpleNamespace(
        solar_kwh=30.0, grid_load_kwh=0.1, grid_export_kwh=5.0, home_use_kwh=25.0,
        battery_charge_kwh=8.0, battery_discharge_kwh=7.0,
    )
    return types.SimpleNamespace(current=cur, totals=tot)


def test_dispatch_runs_clean(tmp_path, monkeypatch):
    """Full dispatch list executes for every alert without raising."""
    sent = []
    monkeypatch.setattr(alerts, "_send_alert", lambda b, c, urgent=False, alert_name=None: sent.append(b))
    monkeypatch.setattr(alerts, "fetch_nws_storm_alerts", lambda lat, lon: [])

    store = HistoryStore(tmp_path / "h.db")
    cfg = Config(telegram_bot_token="x", telegram_chat_id="y",
                 ev_charging=True, lat=33.0, lon=-117.0)
    # Should not raise regardless of time-of-day gating
    alerts._check_peak_alerts(_fake_stats(), cfg, tmp_path, store=store)


def test_alert_export_arbitrage_renders():
    cfg = Config(battery_capacity_kwh=13.6)
    c = _fake_stats(battery_soc_pct=95.0).current
    # August noon, high SoC → fires
    msg = alerts._alert_export_arbitrage({}, "2026-08-15", datetime(2026, 8, 15, 12), c, cfg, None)
    assert msg and "export" in msg.lower()
    # July → inert
    assert alerts._alert_export_arbitrage({}, "2026-07-15", datetime(2026, 7, 15, 12), c, cfg, None) is None


def test_ev_charge_window():
    c = _fake_stats().current
    cfg = Config(ev_charging=True, ev_kwh_per_session=40)
    msg = alerts._alert_ev_charge_window({}, "2026-06-12", datetime(2026, 6, 12, 20, 30), c, cfg)
    assert msg and "EV" in msg
    # disabled
    assert alerts._alert_ev_charge_window({}, "2026-06-12", datetime(2026, 6, 12, 20, 30),
                                          c, Config(ev_charging=False)) is None


def test_low_soc_1pm_idle_battery_no_crash():
    """Zeroed API stats (idle battery, SoC < 40) must not raise int(None)."""
    c = _fake_stats(battery_soc_pct=0.0, battery_use_kw=0.0,
                    solar_production_kw=0.0, home_load_kw=0.0).current
    msg = alerts._alert_low_soc_1pm({}, "2026-07-02", datetime(2026, 7, 2, 13, 40), c, Config())
    assert msg is not None and "to empty" not in msg


def test_low_soc_1pm_uses_configured_capacity():
    """Time-to-empty must scale with cfg.battery_capacity_kwh, not the 13.6
    module fallback — a 30 kWh system lasts longer at the same drain rate."""
    c = _fake_stats(battery_soc_pct=30.0, battery_use_kw=2.0,
                    solar_production_kw=0.0, home_load_kw=2.0).current
    now = datetime(2026, 7, 2, 13, 40)
    small = alerts._alert_low_soc_1pm({}, "2026-07-02", now, c, Config(battery_capacity_kwh=13.6))
    big = alerts._alert_low_soc_1pm({}, "2026-07-02", now, c, Config(battery_capacity_kwh=30.0))
    assert small and big
    assert "to empty" in small and "to empty" in big
    assert small != big  # the bigger battery must report a longer runtime


def test_eod_digest_includes_tomorrow_solar(tmp_path):
    now = datetime.now().replace(hour=21, minute=0, second=0, microsecond=0)
    tomorrow = now + timedelta(days=1)
    hours = [
        HourlyForecast(
            time=tomorrow.replace(hour=h, minute=0, second=0, microsecond=0),
            direct_radiation_wm2=500.0, diffuse_radiation_wm2=100.0,
            cloud_cover_pct=10.0, temp_c=25.0, wind_speed_ms=2.0,
        )
        for h in range(6, 20)
    ]
    outlook = SolarOutlook(hours=hours)
    state = {"solar_cal_samples": [4.0, 4.2, 3.9]}  # >=3 samples so system peak is calibrated
    cfg = Config(battery_capacity_kwh=13.6)
    stats = _fake_stats()
    msg = alerts._alert_eod_digest(state, now.strftime("%Y-%m-%d"), now, stats, cfg, outlook, None)
    assert msg is not None and "Tomorrow's solar" in msg


def test_eod_digest_format_locked(tmp_path):
    """Regression lock for the daily-summary format: no backup-hours line,
    predicted/actual/delta right-aligned to a fixed width. The "Predicted
    SoC @ ..." line was removed from the digest by request 2026-08-19 —
    the sunrise-anchored prediction (see _next_sunrise_after) is still
    computed and stashed in state for the next morning's "Sunrise SoC
    accuracy" line, just no longer shown in this message."""
    now = datetime.now().replace(hour=21, minute=0, second=0, microsecond=0)
    checkpoint = (now + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    hours = [
        HourPrediction(dt=now + timedelta(hours=h), predicted_load_kw=0.8,
                       predicted_solar_kw=(3.0 if now + timedelta(hours=h) >= checkpoint else 0.0),
                       net_kw=(2.2 if now + timedelta(hours=h) >= checkpoint else -0.8),
                       confidence="high")
        for h in range(1, 13)
    ]
    forecast = UsageForecast(hours=hours, total_load_kwh=10.0, total_solar_kwh=20.0,
                             net_kwh=10.0, peak_load_kw=1.0, confidence="high", data_days=30)
    state = {"solar_cal_samples": [4.0, 4.2, 3.9], f"predicted_kwh_{now.strftime('%Y-%m-%d')}": 100.0}
    cfg = Config(battery_capacity_kwh=13.6)
    stats = _fake_stats(battery_soc_pct=72.0)  # totals.solar_kwh=30.0 → actual "27.4"-width case covered by 30.0
    msg = alerts._alert_eod_digest(state, now.strftime("%Y-%m-%d"), now, stats, cfg, None, forecast)
    assert msg is not None
    assert "Backup:" not in msg
    assert "Predicted SoC @" not in msg
    assert f"soc_7am_pred_{checkpoint.strftime('%Y-%m-%d')}" in state  # still stashed for tomorrow's accuracy line
    assert "  Predicted: 100.0 kWh" in msg
    assert "  Actual:     30.0 kWh" in msg  # right-aligned to width 5 — extra space vs "100.0"
    assert "  Delta:     -70.0 kWh" in msg  # "-70.0" is already 5 chars, no extra pad


def test_notifiers_graceful_when_unconfigured():
    cfg = Config()  # no smtp, no webhook
    notifier.notify_email("test", cfg)          # no-op, no raise
    notifier.notify_webhook("test", False, cfg)  # no-op, no raise


def test_ping_healthcheck_noop():
    alerts._ping_healthcheck(Config())  # no url → no raise
