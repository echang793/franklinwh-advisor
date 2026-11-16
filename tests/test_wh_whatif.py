"""Unit tests for wh_whatif — what other water heater modes would have cost."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

from franklinwh_scraper import tou
from franklinwh_scraper.wh_whatif import (
    MODES,
    alt_factor,
    cycle_report,
    day_delta_cost,
    describe_report,
    load_energy,
    maybe_alert,
    save_day,
)

DAY = date(2026, 11, 10)              # a Tuesday


def rate(dt: datetime) -> float:
    return {tou.TouPeriod.SUPER_OFF_PEAK: 0.10, tou.TouPeriod.OFF_PEAK: 0.30,
            tou.TouPeriod.ON_PEAK: 0.50}[tou.period_at(dt)]


def export(_dt: datetime) -> float:
    return 0.05


def _readings(day: date, solar=lambda h: 0.0, load=0.5, hours=31, skip_hour: int | None = None):
    t0 = datetime.combine(day, datetime.min.time())
    out = []
    for i in range(hours * 12):
        t = t0 + timedelta(minutes=5 * i)
        if skip_hour is not None and t.date() == day and t.hour == skip_hour:
            continue
        out.append((t.isoformat(), 0.0, load, solar(t.hour)))
    return out


class _Cfg:
    battery_capacity_kwh = 13.6
    battery_reserve_pct = 10.0
    billing_cycle_start_day = 20
    output_dir = ""
    wh_heat_pump_cop = 3.0
    wh_energy_saver_element_share = 0.10
    wh_high_demand_element_share = 0.40


# ---------------------------------------------------------------- the physics

def test_alt_factor():
    assert alt_factor(cop=3.0, element_share=0.0) == 1.0          # heat pump only
    assert math.isclose(alt_factor(3.0, 1.0), 3.0)                # all element: COP x the kWh
    assert math.isclose(alt_factor(3.0, 0.10), 1.2)               # 0.9 + 0.1 x 3


def test_modes_are_the_three_alternatives():
    assert [m for m, _label in MODES] == ["energy_saver", "high_demand", "electric"]


# ---------------------------------------------------------------- the energy file

def test_energy_round_trip(tmp_path):
    save_day(tmp_path, DAY, {14: 0.5, 2: 0.1})
    assert load_energy(tmp_path)[DAY.isoformat()] == {14: 0.5, 2: 0.1}


def test_energy_file_corrupt_is_empty(tmp_path):
    (tmp_path / "wh_energy.json").write_text("{not json")
    assert load_energy(tmp_path) == {}


def test_energy_rejects_nan(tmp_path):
    save_day(tmp_path, DAY, {14: float("nan"), 15: 0.2})
    assert load_energy(tmp_path)[DAY.isoformat()] == {15: 0.2}


# ---------------------------------------------------------------- pricing one day

def test_no_sun_empty_battery_prices_at_import_rate():
    # 0.5 kWh at 2 PM (weekday off-peak $0.30); all-electric = 1.5 kWh, so +1.0 kWh.
    kwh, cost, replayed = day_delta_cost(_readings(DAY), DAY, 10.0, 13.6, 10.0, {14: 0.5}, 3.0,
                                         rate, export)
    assert replayed
    assert math.isclose(kwh, 1.0)
    assert math.isclose(cost, 0.30, abs_tol=0.01)


def test_solar_surplus_hour_costs_only_lost_export():
    sunny = lambda h: 6.0 if 10 <= h < 16 else 0.0                       # noqa: E731
    kwh, cost, _ = day_delta_cost(_readings(DAY, solar=sunny, load=1.0), DAY, 100.0, 13.6, 10.0,
                                  {12: 0.5}, 3.0, rate, export)
    assert math.isclose(cost, 1.0 * 0.05, abs_tol=0.01)


def test_heat_pump_factor_costs_nothing():
    kwh, cost, _ = day_delta_cost(_readings(DAY), DAY, 10.0, 13.6, 10.0, {14: 0.5}, 1.0, rate, export)
    assert kwh == 0.0 and cost == 0.0


def test_gappy_readings_fall_back_to_grid_rate():
    kwh, cost, replayed = day_delta_cost(_readings(DAY, skip_hour=3), DAY, 10.0, 13.6, 10.0,
                                         {18: 0.5}, 3.0, rate, export)
    assert not replayed
    assert math.isclose(cost, 1.0 * 0.50, abs_tol=0.01)           # on-peak import rate


# ---------------------------------------------------------------- a billing cycle

class FakeStore:
    def __init__(self, readings):
        self.r = readings

    def readings_between(self, start, end):
        return [x for x in self.r if start <= x[0] < end]

    def soc_near(self, ts):
        return 10.0


def _store(days):
    rows = []
    for d in days:
        rows += [r for r in _readings(d, hours=24)]
    rows += _readings(days[-1] + timedelta(days=1), hours=8)
    return FakeStore(sorted(set(rows)))


def test_cycle_report_sums_days_with_data(tmp_path):
    days = [DAY, DAY + timedelta(days=1), DAY + timedelta(days=2)]
    for d in days[:2]:
        save_day(tmp_path, d, {14: 0.5})
    rep = cycle_report(_store(days), tmp_path, _Cfg(), days[0], days[-1], rate, export)
    assert rep.days_with_data == 2 and rep.days_in_period == 3
    assert math.isclose(rep.actual_kwh, 1.0)
    elec = rep.modes["electric"]
    assert math.isclose(elec.extra_kwh, 2.0)
    assert math.isclose(elec.extra_cost, 0.60, abs_tol=0.02)
    es = rep.modes["energy_saver"]
    assert math.isclose(es.extra_kwh, 0.2)


def test_cycle_report_without_data_is_none(tmp_path):
    assert cycle_report(_store([DAY]), tmp_path, _Cfg(), DAY, DAY, rate, export) is None


# ---------------------------------------------------------------- the message

def test_describe_report(tmp_path):
    save_day(tmp_path, DAY, {14: 0.5, 18: 0.3})
    rep = cycle_report(_store([DAY]), tmp_path, _Cfg(), DAY, DAY, rate, export)
    text = describe_report(rep)
    for word in ("Heat pump", "Energy Saver", "High Demand", "Electric", "COP 3", "more"):
        assert word in text, word
    assert "nan" not in text.lower() and "inf" not in text.lower()
    assert "&" not in text


# ---------------------------------------------------------------- the monthly alert

def _alert_env(tmp_path):
    cfg = _Cfg()
    cfg.output_dir = str(tmp_path)
    start, end = tou.cycle_bounds(date(2026, 11, 15), 20)
    for i in range(10):
        save_day(tmp_path, end - timedelta(days=i + 1), {14: 0.5})
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return cfg, _store(days), end


def test_alert_fires_once_on_cycle_end_evening(tmp_path):
    cfg, store, end = _alert_env(tmp_path)
    state: dict = {}
    at = datetime.combine(end, datetime.min.time()).replace(hour=21)
    msg = maybe_alert(state, end.isoformat(), at, store, cfg, rate, export)
    assert msg and "Electric" in msg
    assert maybe_alert(state, end.isoformat(), at, store, cfg, rate, export) is None


def test_alert_quiet_other_days_and_hours(tmp_path):
    cfg, store, end = _alert_env(tmp_path)
    noon = datetime.combine(end, datetime.min.time()).replace(hour=12)
    assert maybe_alert({}, end.isoformat(), noon, store, cfg, rate, export) is None
    before = datetime.combine(end - timedelta(days=1), datetime.min.time()).replace(hour=21)
    assert maybe_alert({}, (end - timedelta(days=1)).isoformat(), before, store, cfg, rate, export) is None


def test_alert_quiet_without_enough_data(tmp_path):
    cfg = _Cfg()
    cfg.output_dir = str(tmp_path)
    _s, end = tou.cycle_bounds(date(2026, 11, 15), 20)
    save_day(tmp_path, end - timedelta(days=1), {14: 0.5})        # one day isn't a month
    at = datetime.combine(end, datetime.min.time()).replace(hour=21)
    assert maybe_alert({}, end.isoformat(), at, _store([end - timedelta(days=1)]), cfg, rate, export) is None


def test_alert_is_registered_in_alerts():
    src = Path(__file__).resolve().parent.parent.joinpath("franklinwh_scraper", "alerts.py").read_text()
    assert '"wh_mode_report"' in src


def test_energy_file_is_plain_json(tmp_path):
    save_day(tmp_path, DAY, {14: 0.5})
    assert json.loads((tmp_path / "wh_energy.json").read_text()) == {DAY.isoformat(): {"14": 0.5}}
