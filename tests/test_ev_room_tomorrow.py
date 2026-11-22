"""Evening digest: "Tomorrow's car room" -- how much of the house's energy can go into the car tomorrow
without importing anything from the grid for the rest of the day (and overnight), given tomorrow's
solar forecast and a typical load for tomorrow's weekday.

Same model as /evroom (battery above reserve + baseline until the next sunrise, plus surplus solar that
would otherwise export), evaluated for tomorrow instead of today. Numbers below are hand-checked.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta

import pytest

from franklinwh_scraper import alerts, typicalload
from franklinwh_scraper.config import Config
from franklinwh_scraper.predictor import HourPrediction, UsageForecast
from franklinwh_scraper.weather import SolarOutlook

CAP = 13.6
NOW = datetime(2026, 11, 20, 21, 0)                    # Friday evening: the digest's run time
TOMORROW = datetime(2026, 11, 21)                      # Saturday
SUNRISE = datetime(2026, 11, 21, 6, 30)
SUNSET = datetime(2026, 11, 21, 17, 0)
OUTLOOK = SolarOutlook(hours=[], sunrise_times=[SUNRISE, datetime(2026, 11, 22, 6, 30)], sunset_times=[SUNSET])
CFG = Config(battery_capacity_kwh=CAP, battery_reserve_pct=10.0, no_ev_baseline_load_kw=0.4, ev_charging=True,
             ev_pack_kwh=75.0, ev_charge_efficiency=0.90, ev_charge_floor_soc=10.0)


def _forecast(solar_by_hour: dict[int, float], load: float = 0.4, n: int = 24, start: datetime = NOW) -> UsageForecast:
    """Hourly from `start` (21:00 tonight): tomorrow's sun is whatever `solar_by_hour` says."""
    hours = []
    for i in range(n):
        dt = start + timedelta(hours=i)
        s = solar_by_hour.get(dt.hour, 0.0) if dt.date() == TOMORROW.date() else 0.0
        hours.append(HourPrediction(dt=dt, predicted_load_kw=load, predicted_solar_kw=s, net_kw=s - load,
                                    confidence="high"))
    return UsageForecast(hours=hours, total_load_kwh=0, total_solar_kwh=0, net_kwh=0, peak_load_kw=1.0,
                         confidence="high", data_days=60)


SUNNY = {h: 3.0 for h in range(10, 16)}                # 6 h of 3 kW
DIM = {h: 1.0 for h in range(10, 16)}                  # 6 h of 1 kW


def _room(soc_sunrise: float, forecast, cfg: Config = CFG, outlook=OUTLOOK, store=None, state=None):
    return alerts._tomorrow_ev_room(state if state is not None else {}, NOW, soc_sunrise, SUNRISE, cfg, outlook,
                                    forecast, store)


# ---------------------------------------------------------------- the model

def test_room_is_battery_above_reserve_plus_exported_surplus():
    """20% (2.72 kWh) at sunrise, 0.4 kW all day, 3 kW sun 10:00-16:00, sunset 17:00.
    Walk: 2.72 -> 1.36 (the reserve) by 9:00; then +2.6 kWh/h: 3.96, 6.56, 9.16, 11.76 (13:00), the 14:00
    hour would reach 14.36 so 0.76 is clipped (export), the 15:00 hour clips all 2.6; 16:00 is -0.4: 13.2 kWh.
    So export = 0.76 + 2.6 = 3.36 kWh and sundown (17:00) is 13.2/13.6 = 97.06%. Keep = 1.36 reserve +
    0.4 kW x 13.5 h (17:00 to the next 6:30 sunrise) = 6.76; spare 13.2 - 6.76 = 6.44; room = 6.44 + 3.36
    = 9.8 kWh."""
    r = _room(20.0, _forecast(SUNNY))
    assert r is not None and r["no_sun"] is False
    assert r["sundown_dt"] == SUNSET
    assert r["export_kwh"] == pytest.approx(3.36, abs=0.01)
    assert r["soc_sundown_pct"] == pytest.approx(97.06, abs=0.1)
    assert r["budget_kwh"] == pytest.approx(9.8, abs=0.05)
    assert r["gain_pct"] == pytest.approx(9.8 * 0.90 / 75.0 * 100.0, abs=0.1)    # ~11.8% on the car
    assert r["shortfall_kwh"] == 0.0


def test_typical_weekday_load_replaces_the_forecast_load_for_tomorrow(monkeypatch):
    """A heavier typical Saturday (1.0 kW all day) leaves less room: no export now, 12.36 kWh at sundown,
    spare 12.36 - 6.76 = 5.6 kWh."""
    seen = {}

    def fake_typical(store, today, labels=None, days=70):
        seen["day"] = today
        return types.SimpleNamespace(by_hour={h: 1.0 for h in range(24)}, weekday_name="Saturday")
    monkeypatch.setattr(typicalload, "typical_hourly_load", fake_typical)
    r = _room(20.0, _forecast(SUNNY, load=0.4), store=object())
    assert seen["day"] == TOMORROW.date()                        # tomorrow's weekday, not today's
    assert r["typical_weekday"] == "Saturday"
    assert r["export_kwh"] == pytest.approx(0.0, abs=0.01)
    assert r["budget_kwh"] == pytest.approx(5.6, abs=0.05)


def test_car_day_labels_are_passed_so_car_days_stay_out_of_typical_load(monkeypatch):
    seen = {}

    def fake_typical(store, today, labels=None, days=70):
        seen["labels"] = labels
        return None
    monkeypatch.setattr(typicalload, "typical_hourly_load", fake_typical)
    _room(20.0, _forecast(SUNNY), store=object(), state={"car_day_labels": {"2026-11-14": True}})
    assert seen["labels"] == {"2026-11-14": True}


def test_without_history_it_falls_back_to_the_forecasts_own_load():
    r = _room(20.0, _forecast(SUNNY), store=None)                # no store -> no typical profile
    assert r is not None and r["typical_weekday"] is None


def test_dim_day_has_no_room_and_says_how_short():
    """10% at sunrise, 1 kW of sun for 6 h: 1.36 -> 4.96 kWh, minus 16:00's 0.4 = 4.56 at sundown, but
    tonight needs 6.76 kWh: 2.2 kWh short, so any car charging imports."""
    r = _room(10.0, _forecast(DIM))
    assert r["budget_kwh"] == 0.0 and r["gain_pct"] == 0.0
    assert r["shortfall_kwh"] == pytest.approx(2.2, abs=0.05)


def test_no_sun_tomorrow_means_no_room_not_silence():
    r = _room(30.0, _forecast({}))
    assert r is not None and r["no_sun"] is True
    assert r["budget_kwh"] == 0.0


def test_forecast_that_stops_before_sundown_gives_no_number():
    """A 12-hour forecast ends at 9 AM tomorrow: a "sundown" read off that would be a made-up number."""
    assert _room(20.0, _forecast(SUNNY, n=12)) is None


def test_no_forecast_gives_no_number():
    assert _room(20.0, None) is None
    assert _room(20.0, types.SimpleNamespace(hours=[])) is None


# ---------------------------------------------------------------- the digest line

def _digest(cfg: Config, forecast, outlook=OUTLOOK, soc: float = 60.0, state=None):
    stats = types.SimpleNamespace(
        current=types.SimpleNamespace(battery_soc_pct=soc, home_load_kw=0.4),
        totals=types.SimpleNamespace(solar_kwh=10.0, battery_charge_kwh=5.0, battery_discharge_kwh=4.0,
                                     grid_load_kwh=2.0, grid_export_kwh=1.0, home_use_kwh=12.0))
    return alerts._alert_eod_digest(state if state is not None else {}, NOW.strftime("%Y-%m-%d"), NOW, stats, cfg,
                                    outlook, forecast, None)


def test_digest_shows_tomorrows_car_room_in_kwh_and_car_percent():
    msg = _digest(CFG, _forecast(SUNNY))
    assert msg is not None
    line = next(ln for ln in msg.splitlines() if "Tomorrow's car room" in ln)
    assert "kWh" in line and "+" in line and "%" in line and "🚗" in line
    assert "no grid import" in line.lower()


def test_digest_says_so_when_there_is_no_room():
    msg = _digest(CFG, _forecast(DIM), soc=10.0)
    line = next(ln for ln in msg.splitlines() if "Tomorrow's car room" in ln)
    assert "no room" in line.lower()


def test_digest_omits_the_line_without_an_ev():
    msg = _digest(Config(battery_capacity_kwh=CAP, ev_charging=False), _forecast(SUNNY))
    assert msg is not None and "car room" not in msg


def test_digest_omits_the_line_when_the_forecast_cannot_cover_tomorrow():
    msg = _digest(CFG, _forecast(SUNNY, n=12))
    assert msg is not None and "car room" not in msg


def test_a_failure_in_the_car_room_math_never_costs_you_the_digest(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(alerts, "_tomorrow_ev_room", boom)
    msg = _digest(CFG, _forecast(SUNNY))
    assert msg is not None and "Daily Summary" in msg and "car room" not in msg


def test_digest_lines_stay_flush_left():
    import re
    msg = _digest(CFG, _forecast(SUNNY))
    assert [ln for ln in re.sub(r"</?(?:b|code|i)>", "", msg).splitlines() if ln[:1].isspace()] == []
