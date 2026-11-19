"""Outage assistant: how long the battery lasts with the grid down, and whether it reaches sunrise.

Pure numbers (no network, no wall clock): 13.6 kWh battery, hourly forecast slices.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from franklinwh_scraper import outage
from franklinwh_scraper.predictor import HourPrediction

CAP = 13.6
NOW = datetime(2026, 11, 18, 18, 0)        # evening, no more sun today
SUNRISE = datetime(2026, 11, 19, 6, 30)


def _hours(start: datetime, n: int, solar_by_hour: dict[int, float] | None = None) -> list[HourPrediction]:
    solar_by_hour = solar_by_hour or {}
    out = []
    for i in range(n):
        dt = start + timedelta(hours=i)
        s = solar_by_hour.get(dt.hour, 0.0)
        out.append(HourPrediction(dt=dt, predicted_load_kw=1.0, predicted_solar_kw=s, net_kw=s - 1.0, confidence="high"))
    return out


def test_runtime_with_no_solar_is_energy_over_load():
    r = outage.outage_report(50.0, CAP, 1.0, _hours(NOW, 24), NOW, SUNRISE)
    assert math.isclose(r.runtime_h, 6.8, abs_tol=0.01)          # 6.8 kWh at 1 kW
    assert r.empty_at == NOW + timedelta(hours=6.8)
    assert r.survives is False and r.makes_sunrise is False
    assert r.soc_at_sunrise == 0.0


def test_shedding_to_essentials_can_make_the_difference():
    """12.5 h to sunrise: 1.0 kW drains 12.5 kWh (empty), essentials 0.4 kW drain 5 kWh (13% left)."""
    r = outage.outage_report(50.0, CAP, 1.0, _hours(NOW, 24), NOW, SUNRISE)
    assert r.essentials_kw == 0.4
    assert r.makes_sunrise is False and r.makes_sunrise_if_shed is True
    assert math.isclose(r.shed_soc_at_sunrise, 13.24, abs_tol=0.1)
    assert r.shed_runtime_h is None or r.shed_runtime_h > 12.5


def test_solar_the_next_day_keeps_a_light_load_alive_and_charges_to_full():
    sun = {h: 3.0 for h in range(9, 16)}
    r = outage.outage_report(50.0, CAP, 0.4, _hours(NOW, 24, sun), NOW, SUNRISE)
    assert r.makes_sunrise is True and r.survives is True and r.runtime_h is None
    assert 0 < r.soc_at_sunrise < 50


def test_charging_is_capped_at_full():
    sun = {h: 10.0 for h in range(0, 24)}
    r = outage.outage_report(95.0, CAP, 0.2, _hours(NOW, 24, sun), NOW, SUNRISE)
    assert r.soc_at_sunrise == 100.0


def test_zero_or_missing_load_never_produces_nan_or_negative_runtime():
    for load in (0.0, 0.01, -1.0):
        r = outage.outage_report(50.0, CAP, load, _hours(NOW, 24), NOW, SUNRISE)
        assert r.runtime_h is None or r.runtime_h > 0
        assert "NaN" not in json.dumps(outage.to_dict(r)) and "nan" not in json.dumps(outage.to_dict(r))
    r = outage.outage_report(50.0, 0.0, 1.0, _hours(NOW, 24), NOW, SUNRISE)   # unknown capacity
    assert r is None


def test_empty_battery_has_no_runtime_left():
    r = outage.outage_report(0.0, CAP, 1.0, _hours(NOW, 24), NOW, SUNRISE)
    assert r.runtime_h == 0.0 and r.makes_sunrise is False


def test_no_sunrise_means_unknown_not_false():
    r = outage.outage_report(50.0, CAP, 0.4, _hours(NOW, 24), NOW, None)
    assert r.makes_sunrise is None and r.makes_sunrise_if_shed is None


def test_partial_first_hour_counts_only_the_time_left():
    now = datetime(2026, 11, 18, 18, 30)
    r = outage.outage_report(50.0, CAP, 1.0, _hours(datetime(2026, 11, 18, 18, 0), 24), now, SUNRISE)
    assert math.isclose(r.runtime_h, 6.8, abs_tol=0.01)


def test_biggest_loads_come_from_learned_appliance_events():
    events = [{"key": "2026-11-01T14:00", "name": "dryer", "kw": 4.0, "minutes": 60},
              {"key": "2026-11-08T14:00", "name": "dryer", "kw": 4.2, "minutes": 60},
              {"key": "2026-11-02T17:00", "name": "oven", "kw": 2.5, "minutes": 40},
              {"key": "2026-11-03T12:00", "name": "other", "kw": 9.0, "minutes": 5},
              {"key": "2026-11-04T12:00", "name": "dishwasher", "kw": 0.1, "minutes": 60}]
    assert outage.learned_loads(events) == [("dryer", 4.1), ("oven", 2.5)]   # 'other' and tiny loads dropped
    assert outage.learned_loads([]) == []


def test_text_says_the_verdict_and_what_to_shed():
    events = [{"key": "2026-11-01T14:00", "name": "dryer", "kw": 4.0, "minutes": 60}]
    r = outage.outage_report(50.0, CAP, 1.0, _hours(NOW, 24), NOW, SUNRISE, loads=outage.learned_loads(events))
    live = outage.format_report(r, NOW, grid_down=True)
    assert "Won't reach sunrise" in live and "dryer" in live and "essentials" in live
    plan = outage.format_report(r, NOW, grid_down=False)
    assert plan.startswith("🧪") and "If the grid dropped now" in plan
    ok = outage.outage_report(50.0, CAP, 0.4, _hours(NOW, 24), NOW, SUNRISE)
    assert "Reaches sunrise" in outage.format_report(ok, NOW, grid_down=True)


def test_to_dict_is_json_safe():
    r = outage.outage_report(50.0, CAP, 1.0, _hours(NOW, 24), NOW, SUNRISE)
    d = json.loads(json.dumps(outage.to_dict(r)))
    assert d["makes_sunrise"] is False and d["makes_sunrise_if_shed"] is True
    assert datetime.fromisoformat(d["empty_at"]) == r.empty_at


# ---------------------------------------------------------------- alerts, bot and API wiring

import types  # noqa: E402

from franklinwh_scraper import alerts  # noqa: E402
from franklinwh_scraper.config import Config  # noqa: E402
from franklinwh_scraper.predictor import UsageForecast  # noqa: E402
from franklinwh_scraper.weather import SolarOutlook  # noqa: E402


def _forecast(start=NOW, n=24, solar=None):
    return UsageForecast(hours=_hours(start, n, solar), total_load_kwh=0, total_solar_kwh=0, net_kwh=0,
                         peak_load_kw=0, confidence="high", data_days=30)


def _outlook():
    return SolarOutlook(hours=[], sunrise_times=[SUNRISE], sunset_times=[])


def _c(grid="down", soc=50.0, load=1.0):
    return types.SimpleNamespace(grid_status=grid, battery_soc_pct=soc, home_load_kw=load, solar_production_kw=0.0,
                                 battery_use_kw=load, grid_use_kw=0.0, generator_production_kw=0.0, generator_enabled=False)


CFG = Config(battery_capacity_kwh=CAP)


def test_grid_down_alert_carries_the_sunrise_verdict_and_arms_the_hourly_update():
    state: dict = {}
    body = alerts._alert_grid_down(state, "2026-11-18", NOW, _c(), CFG, _outlook(), _forecast(), None)
    assert "GRID DOWN" in body and "Won't reach sunrise" in body and "essentials" in body
    assert state["outage_last_update"] == NOW.isoformat() and state["grid_down_start"] == NOW.isoformat()


def test_grid_down_alert_still_works_without_a_forecast():
    body = alerts._alert_grid_down({}, "2026-11-18", NOW, _c(), CFG)   # old call shape, no outlook
    assert "GRID DOWN" in body and "Backup" in body


def test_outage_update_fires_hourly_not_before():
    state = {"grid_down_start": NOW.isoformat(), "outage_last_update": NOW.isoformat()}
    args = ("2026-11-18", NOW + timedelta(minutes=30), _c(), CFG, _outlook(), _forecast(), None)
    assert alerts._alert_outage_update(state, *args) is None
    later = NOW + timedelta(hours=1, minutes=1)
    body = alerts._alert_outage_update(state, "2026-11-18", later, _c(soc=40.0), CFG, _outlook(), _forecast(later), None)
    assert body and "Outage update" in body and "1h" in body
    assert state["outage_last_update"] == later.isoformat()
    assert alerts._alert_outage_update(state, "2026-11-18", later + timedelta(minutes=10), _c(), CFG,
                                       _outlook(), _forecast(later), None) is None


def test_outage_update_is_loud_only_when_sunrise_is_out_of_reach():
    state = {"grid_down_start": NOW.isoformat(), "outage_last_update": (NOW - timedelta(hours=2)).isoformat()}
    alerts._alert_outage_update(state, "d", NOW, _c(load=1.0), CFG, _outlook(), _forecast(), None)
    assert state.get("outage_update_force_loud") is True
    state = {"grid_down_start": NOW.isoformat(), "outage_last_update": (NOW - timedelta(hours=2)).isoformat()}
    alerts._alert_outage_update(state, "d", NOW, _c(load=0.3, soc=80.0), CFG, _outlook(), _forecast(), None)
    assert not state.get("outage_update_force_loud")


def test_outage_update_needs_an_active_outage():
    assert alerts._alert_outage_update({}, "d", NOW, _c(), CFG, _outlook(), _forecast(), None) is None
    state = {"grid_down_start": NOW.isoformat(), "outage_last_update": (NOW - timedelta(hours=2)).isoformat()}
    assert alerts._alert_outage_update(state, "d", NOW, _c(grid="normal"), CFG, _outlook(), _forecast(), None) is None


def test_grid_restored_clears_the_update_clock():
    state = {"grid_down_start": NOW.isoformat(), "grid_down_soc": 50.0, "outage_last_update": NOW.isoformat(),
             "outage_update_force_loud": True}
    assert alerts._alert_grid_restored(state, NOW + timedelta(hours=2), _c(grid="normal"), CFG)
    assert "outage_last_update" not in state and "outage_update_force_loud" not in state


def test_outage_update_is_an_always_on_safety_alert():
    assert "outage_update" in alerts._ALWAYS_ON_ALERTS
    assert "outage_update" in alerts.known_alert_names()


def test_dispatcher_sends_a_forced_loud_update_as_loud(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "_send_alert",
                        lambda body, cfg, urgent=False, alert_name=None, loud=None, **_k: sent.append((alert_name, loud)))
    start = datetime.now() - timedelta(hours=3)
    alerts._save_peak_state(tmp_path, {"grid_down_start": start.isoformat(), "grid_down_alerted_date": start.strftime("%Y-%m-%d"),
                                       "outage_last_update": start.isoformat()})
    c = _c(load=1.0)
    totals = types.SimpleNamespace(solar_kwh=0.0, battery_charge_kwh=0.0, battery_discharge_kwh=0.0, grid_load_kwh=0.0,
                                   grid_export_kwh=0.0, home_use_kwh=0.0, grid_import_kwh=0.0, generator_kwh=0.0,
                                   battery_load_kwh=0.0, solar_load_kwh=0.0)
    n = datetime.now()
    fc = _forecast(n.replace(minute=0, second=0, microsecond=0))
    out = SolarOutlook(hours=[], sunrise_times=[n + timedelta(hours=12)], sunset_times=[])
    cfg = Config(telegram_bot_token="t", telegram_chat_id="c", battery_capacity_kwh=CAP)
    alerts._check_peak_alerts(types.SimpleNamespace(current=c, totals=totals), cfg, tmp_path, outlook=out, usage_forecast=fc)
    assert ("outage_update", True) in sent


def test_bot_outage_text_plans_when_the_grid_is_up():
    from franklinwh_scraper.chatbot import TelegramChatBot
    bot = TelegramChatBot(Config(battery_capacity_kwh=CAP), api_key="x")
    stats = types.SimpleNamespace(current=_c(grid="normal"))
    bot._snapshot_for_report = lambda: (stats, _forecast(), _outlook(), None, {})
    txt = bot._outage_text(NOW)
    assert "If the grid dropped now" in txt and "sunrise" in txt
    bot._snapshot_for_report = lambda: (None, None, None, None, {})
    assert "No data yet" in bot._outage_text(NOW)


def test_api_outage(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi
    c = types.SimpleNamespace(battery_soc_pct=50.0, home_load_kw=1.0, solar_production_kw=0.0, battery_use_kw=1.0, grid_use_kw=0.0)
    monkeypatch.setattr(webapi, "_OUT", tmp_path)
    monkeypatch.setattr(webapi, "_cfg", Config(dashboard_token="", battery_capacity_kwh=CAP))
    monkeypatch.setattr(webapi, "_besttime_inputs", lambda: (c, {}, _outlook(), _forecast(), None, NOW))
    body = TestClient(webapi.app).get("/api/outage").json()
    assert body["ok"] and body["grid_down"] is False
    assert body["report"]["makes_sunrise"] is False and body["report"]["makes_sunrise_if_shed"] is True
    monkeypatch.setattr(webapi, "_besttime_inputs", lambda: None)
    assert TestClient(webapi.app).get("/api/outage").json()["ok"] is False
