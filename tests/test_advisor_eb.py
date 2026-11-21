"""Emergency Backup recommendations: the "battery at 4 pm" projection walks hour by hour, and the
soft (non-critical) EB calls only fire while the grid is in a super off-peak window.

Regression: 2026-11-20 9:02 PM, "Switch to EMERGENCY BACKUP ... projects to reach 4 pm on-peak at 0%
SoC". The old projection added up all load and all solar until 4 pm and clamped the total, so an
early-morning dip to empty (grid-covered, off-peak) counted as a debt tomorrow's solar had to repay.
Walking the hours showed ~35% left at 4 pm, enough to cover the evening peak.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta

import pytest

from franklinwh_scraper import advisor
from franklinwh_scraper.predictor import HourPrediction, UsageForecast

CAP = 13.6

# Friday 2026-11-20 21:00 -> Saturday 15:00 (19 h), as the live forecast had it at 9 PM that night.
TONIGHT = datetime(2026, 11, 20, 21, 0)
_LOADS = [0.39, 0.51, 0.53, 0.39, 0.35, 1.76, 2.15, 2.09, 2.08, 2.03,
          2.53, 2.60, 2.83, 1.97, 1.17, 0.69, 0.83, 0.71, 0.89]
_SOLAR = [0, 0, 0, 0, 0, 0, 0, 0, 0.02, 0.30,
          1.31, 2.12, 2.68, 2.77, 2.79, 2.30, 1.85, 0.97, 0.30]


def _forecast(start: datetime, loads: list[float], solars: list[float], peak_load: float = 0.622,
              n: int = 24) -> UsageForecast:
    """Hourly forecast from `start`: the given loads/solars, then flat `peak_load` and no sun."""
    hours = []
    for i in range(n):
        load = loads[i] if i < len(loads) else peak_load
        solar = solars[i] if i < len(solars) else 0.0
        hours.append(HourPrediction(dt=start + timedelta(hours=i), predicted_load_kw=load,
                                    predicted_solar_kw=solar, net_kw=solar - load, confidence="high"))
    return UsageForecast(hours=hours, total_load_kwh=sum(h.predicted_load_kw for h in hours),
                         total_solar_kwh=sum(h.predicted_solar_kw for h in hours), net_kwh=0.0,
                         peak_load_kw=3.0, confidence="high", data_days=200)


def _frozen(fixed: datetime):
    class _Fake(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed
    return _Fake


def _stats(soc: float, load: float = 0.4, solar: float = 0.0):
    return types.SimpleNamespace(current=types.SimpleNamespace(
        battery_soc_pct=soc, home_load_kw=load, solar_production_kw=solar, grid_status="normal"),
        totals=types.SimpleNamespace())


def _recommend(monkeypatch, now: datetime, soc: float, forecast, outlook=None):
    monkeypatch.setattr(advisor, "datetime", _frozen(now))
    return advisor.recommend(_stats(soc), outlook=outlook, forecast=forecast, battery_capacity_kwh=CAP)


# ---------------------------------------------------------------- the hourly walk

def test_plan_walks_the_hours_instead_of_clamping_one_lump_sum():
    """Tonight's numbers: empty by ~5 AM, refilled by tomorrow's sun to ~4.7 kWh (35%) at 4 pm."""
    plan = advisor._tou_eb_plan(TONIGHT, 62.7, CAP, _forecast(TONIGHT, _LOADS, _SOLAR))
    assert plan["soc_at_4pm"] == pytest.approx(34.7, abs=1.0)
    assert plan["next_day_peak"] is True
    assert plan["net_peak_draw"] == pytest.approx(3.11, abs=0.01)
    assert plan["eb_needed"] is False          # 4.72 kWh at 4 pm >= 3.11 kWh peak draw + 1.36 kWh buffer


def test_plan_clips_midday_solar_at_a_full_battery():
    """Full battery, 5 sunny hours of surplus (clipped at 100%), then one heavy hour: the surplus
    that had nowhere to go must not offset the hour that follows."""
    now = datetime(2026, 7, 15, 10, 0)                        # Wednesday, 6 h before the peak
    plan = advisor._tou_eb_plan(now, 100.0, CAP, _forecast(now, [1.0] * 5 + [5.0], [4.0] * 5))
    assert plan["soc_at_4pm"] == pytest.approx((CAP - 5.0) / CAP * 100.0, abs=0.5)   # 63%, not 100%


def test_plan_never_goes_below_empty_or_above_full():
    plan = advisor._tou_eb_plan(TONIGHT, 5.0, CAP, _forecast(TONIGHT, [3.0] * 19, [0.0] * 19))
    assert plan["soc_at_4pm"] == 0.0
    plan = advisor._tou_eb_plan(TONIGHT, 100.0, CAP, _forecast(TONIGHT, [0.1] * 19, [5.0] * 19))
    assert plan["soc_at_4pm"] == 100.0


def test_the_night_the_lump_sum_cried_wolf(monkeypatch):
    """The real 9 PM situation: no EB call at all."""
    rec = _recommend(monkeypatch, TONIGHT, 62.7, _forecast(TONIGHT, _LOADS, _SOLAR))
    assert rec.mode is not advisor.Mode.EMERGENCY_BACKUP
    assert "short of covering" not in rec.reason


# ---------------------------------------------------------------- super off-peak gate

def test_no_soft_eb_call_outside_super_off_peak_even_when_a_shortfall_is_real(monkeypatch):
    """A flat 1 kW load with no sun really is short for tomorrow's peak, but 9 PM is plain off-peak."""
    fc = _forecast(TONIGHT, [1.0] * 24, [0.0] * 24, peak_load=1.0)
    assert advisor._tou_eb_plan(TONIGHT, 20.0, CAP, fc)["eb_needed"] is True    # the gate is what silences it
    rec = _recommend(monkeypatch, TONIGHT, 20.0, fc)
    assert rec.mode is not advisor.Mode.EMERGENCY_BACKUP


@pytest.mark.parametrize("when, expect_eb", [
    (datetime(2026, 11, 20, 3, 0), True),      # Friday, midnight-6 AM super off-peak
    (datetime(2026, 11, 20, 12, 0), True),     # Friday, 10 AM-2 PM super off-peak
    (datetime(2026, 11, 21, 11, 0), True),     # Saturday, midnight-2 PM super off-peak
    (datetime(2026, 11, 20, 8, 0), False),     # Friday 6-10 AM off-peak
    (datetime(2026, 11, 20, 15, 0), False),    # Friday 2-4 PM off-peak
    (datetime(2026, 11, 21, 15, 0), False),    # Saturday 2-4 PM off-peak
    (datetime(2026, 11, 20, 18, 0), False),    # on-peak
    (datetime(2026, 11, 20, 22, 0), False),    # after 9 PM off-peak
])
def test_soft_eb_only_in_super_off_peak(monkeypatch, when, expect_eb):
    """A battery that will not cover the peak (20%, flat 1 kW load, no sun) in every period."""
    fc = _forecast(when, [1.0] * 24, [0.0] * 24, peak_load=1.0)
    rec = _recommend(monkeypatch, when, 20.0, fc)
    assert (rec.mode is advisor.Mode.EMERGENCY_BACKUP) is expect_eb
    if expect_eb:
        assert rec.urgency == "warning"


def test_static_fallback_rules_are_gated_too(monkeypatch):
    """No usable forecast: the old 'low SoC + poor solar' rule must also wait for a super off-peak window."""
    outlook = types.SimpleNamespace(avg_ghi=lambda h: 50.0, avg_cloud_cover=lambda h: 90.0, peak_ghi_today=lambda: 60.0)
    on_peak = datetime(2026, 8, 25, 17, 0)                    # Tuesday
    assert _recommend(monkeypatch, on_peak, 20.0, None, outlook).mode is not advisor.Mode.EMERGENCY_BACKUP
    midday = datetime(2026, 8, 25, 11, 0)
    rec = _recommend(monkeypatch, midday, 20.0, None, outlook)
    assert rec.mode is advisor.Mode.EMERGENCY_BACKUP and rec.urgency == "warning"


def test_critical_and_grid_down_calls_are_not_gated(monkeypatch):
    """Safety calls keep working at any hour: 11% SoC at 7 PM (on-peak), and a grid outage."""
    on_peak = datetime(2026, 11, 20, 19, 0)
    rec = _recommend(monkeypatch, on_peak, 11.0, _forecast(on_peak, [1.0] * 24, [0.0] * 24))
    assert rec.mode is advisor.Mode.EMERGENCY_BACKUP and rec.urgency == "critical"
    monkeypatch.setattr(advisor, "datetime", _frozen(on_peak))
    down = _stats(80.0)
    down.current.grid_status = "down"
    rec = advisor.recommend(down, outlook=None, forecast=None, battery_capacity_kwh=CAP)
    assert rec.mode is advisor.Mode.EMERGENCY_BACKUP and rec.urgency == "critical"
