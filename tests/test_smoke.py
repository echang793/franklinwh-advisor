"""Integration smoke tests — alert functions + notifiers don't crash. No network."""

import pathlib
import sys
import types
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from franklinwh_scraper import cli, notifier
from franklinwh_scraper.config import Config
from franklinwh_scraper.history import HistoryStore


def _fake_stats(**cur_over):
    cur = types.SimpleNamespace(
        grid_status="normal", battery_soc_pct=72.0, home_load_kw=1.2,
        solar_production_kw=2.0, battery_use_kw=-0.5, grid_use_kw=0.0,
    )
    for k, v in cur_over.items():
        setattr(cur, k, v)
    tot = types.SimpleNamespace(
        solar_kwh=30.0, grid_load_kwh=0.1, grid_export_kwh=5.0, home_use_kwh=25.0,
    )
    return types.SimpleNamespace(current=cur, totals=tot)


def test_dispatch_runs_clean(tmp_path, monkeypatch):
    """Full dispatch list executes for every alert without raising."""
    sent = []
    monkeypatch.setattr(cli, "_send_alert", lambda b, c, urgent=False: sent.append(b))
    monkeypatch.setattr(cli, "fetch_nws_storm_alerts", lambda lat, lon: [])

    store = HistoryStore(tmp_path / "h.db")
    cfg = Config(telegram_bot_token="x", telegram_chat_id="y",
                 ev_charging=True, lat=33.0, lon=-117.0)
    # Should not raise regardless of time-of-day gating
    cli._check_peak_alerts(_fake_stats(), cfg, tmp_path, store=store)


def test_alert_export_arbitrage_renders():
    cfg = Config(battery_capacity_kwh=13.6)
    c = _fake_stats(battery_soc_pct=95.0).current
    # August noon, high SoC → fires
    msg = cli._alert_export_arbitrage({}, "2026-08-15", datetime(2026, 8, 15, 12), c, cfg, None)
    assert msg and "export" in msg.lower()
    # July → inert
    assert cli._alert_export_arbitrage({}, "2026-07-15", datetime(2026, 7, 15, 12), c, cfg, None) is None


def test_ev_charge_window():
    c = _fake_stats().current
    cfg = Config(ev_charging=True, ev_kwh_per_session=40)
    msg = cli._alert_ev_charge_window({}, "2026-06-12", datetime(2026, 6, 12, 20, 30), c, cfg)
    assert msg and "EV" in msg
    # disabled
    assert cli._alert_ev_charge_window({}, "2026-06-12", datetime(2026, 6, 12, 20, 30),
                                       c, Config(ev_charging=False)) is None


def test_notifiers_graceful_when_unconfigured():
    cfg = Config()  # no smtp, no webhook
    notifier.notify_email("test", cfg)          # no-op, no raise
    notifier.notify_webhook("test", False, cfg)  # no-op, no raise


def test_ping_healthcheck_noop():
    cli._ping_healthcheck(Config())  # no url → no raise
