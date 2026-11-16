"""Unit tests for weekly.py — synthetic profiles and rates, no store, no network."""

from __future__ import annotations

import math
import pathlib
import sys
from datetime import date, datetime, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))   # for `tests.` fixtures

from franklinwh_scraper import tou
from franklinwh_scraper.weekly import (
    describe_week,
    for_store,
    plan_week,
    typical_day,
)

MON = date(2026, 11, 16)       # a Monday, no holiday that week
SUN = date(2026, 11, 22)


def rate(dt: datetime) -> float:
    p = tou.period_at(dt)
    return {tou.TouPeriod.SUPER_OFF_PEAK: 0.10, tou.TouPeriod.OFF_PEAK: 0.30,
            tou.TouPeriod.ON_PEAK: 0.50}[p]


def export(_dt: datetime) -> float:
    return 0.05


FLAT_LOAD = {(d, h): 0.5 for d in range(7) for h in range(24)}
NO_SUN: dict[int, float] = {h: 0.0 for h in range(24)}
SUNNY = {h: (5.0 if 10 <= h < 15 else 0.0) for h in range(24)}


def _plan(**kw):
    args = dict(start=MON, solar_by_hour=NO_SUN, load_by_slot=FLAT_LOAD, soc7_pct=10.0,
                cap=13.6, reserve_pct=10.0, name="dryer", kw=4.0, hours=1.0,
                rate_at=rate, export_rate_at=export, habits=None)
    args.update(kw)
    return plan_week(**args)


# ---------------------------------------------------------------- typical day

def test_typical_day_spans_midnight_to_next_morning():
    hrs = typical_day(MON, SUNNY, FLAT_LOAD)
    assert hrs[0].dt == datetime.combine(MON, time(0))
    assert hrs[-1].dt == datetime(2026, 11, 17, 7, 0)
    assert len(hrs) == 32
    assert hrs[12].predicted_solar_kw == 5.0 and hrs[20].predicted_solar_kw == 0.0


def test_typical_day_uses_each_days_own_weekday_load():
    load = {(d, h): (2.0 if d == 1 else 0.5) for d in range(7) for h in range(24)}   # Tuesdays heavy
    hrs = typical_day(MON, NO_SUN, load)
    assert hrs[3].predicted_load_kw == 0.5          # Monday 3 AM
    assert hrs[27].predicted_load_kw == 2.0         # Tuesday 3 AM


# ---------------------------------------------------------------- plan_week

def test_plan_covers_seven_days():
    p = _plan()
    assert [d.day for d in p.days] == [date(2026, 11, 16 + i) for i in range(7)]


def test_weekday_without_sun_picks_midday_super_off_peak():
    p = _plan()
    mon = p.days[0]
    assert time(10) <= mon.best.start.time() <= time(13)
    assert math.isclose(mon.best.cost, 0.40, abs_tol=0.01)


def test_weekend_without_sun_can_start_early():
    # Weekend super-off-peak runs midnight-2 PM, so 7 AM is as cheap as noon.
    sun = _plan().days[6]
    assert sun.day == SUN
    assert sun.best.start.time() == time(7)
    assert math.isclose(sun.best.cost, 0.40, abs_tol=0.01)


def test_solar_surplus_beats_grid():
    # 1 kW house: the battery runs out overnight, so an afternoon run imports
    # later, while a midday run only gives up export (4 kWh x $0.05).
    load = {(d, h): 1.0 for d in range(7) for h in range(24)}
    p = _plan(solar_by_hour=SUNNY, load_by_slot=load)
    mon = p.days[0]
    assert time(10) <= mon.best.start.time() <= time(14)
    assert mon.best.grid_kwh == 0.0
    assert math.isclose(mon.best.cost, 0.20, abs_tol=0.01)


def test_spare_battery_makes_afternoon_free():
    # Light 0.5 kW house: the battery has room to cover an afternoon run too.
    p = _plan(solar_by_hour=SUNNY)
    assert p.days[0].best.cost == 0.0


def test_usual_time_cost_and_weekly_saving():
    habits = {"sunday": [["dryer", "16:00", 1.0, 4.0]]}
    p = _plan(habits=habits)
    sun = p.days[6]
    assert sun.usual_start == datetime(2026, 11, 22, 16, 0)
    assert sun.usual_cost is not None and sun.usual_cost > sun.best.cost
    assert math.isclose(p.weekly_saving, sun.usual_cost - sun.best.cost, abs_tol=0.01)
    assert all(d.usual_start is None for d in p.days[:6])


def test_usual_time_rounds_to_half_hour():
    p = _plan(habits={"monday": [["dryer", "14:10", 1.0, 4.0]]})
    assert p.days[0].usual_start == datetime(2026, 11, 16, 14, 0)


def test_habit_outside_window_is_ignored():
    p = _plan(habits={"monday": [["dryer", "23:30", 1.0, 4.0]]})
    assert p.days[0].usual_start is None and p.weekly_saving == 0.0


def test_other_appliance_habits_ignored():
    p = _plan(habits={"monday": [["oven", "17:00", 2.0, 2.5]]})
    assert p.days[0].usual_start is None


def test_no_profiles_means_no_plan():
    assert _plan(solar_by_hour={}, load_by_slot={}) is None


# ---------------------------------------------------------------- describe

def test_describe_has_delay_end_and_no_nan():
    text = describe_week(_plan(habits={"sunday": [["dryer", "16:00", 1.0, 4.0]]}))
    assert "Delay End" in text
    assert "11:00 AM" in text or "8:00 AM" in text      # Mon best ends 11, Sun ends 8
    assert "nan" not in text.lower() and "inf" not in text.lower()
    assert "Sun" in text and "4:00 PM" in text


def test_describe_without_habits_has_no_saving_line():
    text = describe_week(_plan())
    assert "usual" not in text.lower()


# ---------------------------------------------------------------- for_store adapter

class FakeStore:
    def recent_solar_profile(self, days):
        return {(d, h): SUNNY[h] for d in range(7) for h in range(24)}

    def recent_load_profile(self, days, percentile=0.5):
        return dict(FLAT_LOAD)

    def soc_near(self, ts):
        return 60.0


class _Cfg:
    battery_capacity_kwh = 13.6
    battery_reserve_pct = 10.0
    habit_loads: dict = {}


def test_for_store_builds_plan():
    p = for_store(FakeStore(), _Cfg(), {}, MON, "dryer", rate_at=rate, export_rate_at=export,
                  reserve_pct=10.0)
    assert p is not None and len(p.days) == 7
    assert p.days[0].day == date(2026, 11, 17)      # starts tomorrow: today's 7 AM has passed


def test_for_store_none_store():
    assert for_store(None, _Cfg(), {}, MON, "dryer") is None


def test_for_store_store_error_is_none():
    class Broken(FakeStore):
        def recent_solar_profile(self, days):
            raise RuntimeError("db locked")
    assert for_store(Broken(), _Cfg(), {}, MON, "dryer", rate_at=rate, export_rate_at=export,
                     reserve_pct=10.0) is None


def test_for_store_unknown_load_is_none():
    assert for_store(FakeStore(), _Cfg(), {}, MON, "jacuzzi", rate_at=rate, export_rate_at=export,
                     reserve_pct=10.0) is None


# ---------------------------------------------------------------- chat bot: /week and the tool

def test_week_is_dispatched_from_text():
    from franklinwh_scraper.chatbot import _parse_week
    assert _parse_week("/week") == "dryer"
    assert _parse_week("/week dishwasher") == "dishwasher"
    assert _parse_week("/week@franklinwh_advisor_bot oven") == "oven"
    assert _parse_week("/week purple") == "help"
    assert _parse_week("/weekly") is None
    assert _parse_week("what about this week") is None


def test_week_command_sends_the_plan(tmp_path, monkeypatch):
    from franklinwh_scraper import weekly
    from tests.test_besttime import _when_bot
    bot, sent = _when_bot(tmp_path, monkeypatch)
    monkeypatch.setattr(weekly, "for_store", lambda *a, **k: _plan())
    bot._send_week("1", "dryer")
    assert "Best time to run the dryer" in sent[-1][0]


def test_week_without_history_explains(tmp_path, monkeypatch):
    from franklinwh_scraper import weekly
    from tests.test_besttime import _when_bot
    bot, sent = _when_bot(tmp_path, monkeypatch)
    monkeypatch.setattr(weekly, "for_store", lambda *a, **k: None)
    bot._send_week("1", "dryer")
    assert "history" in sent[-1][0].lower()


def test_week_help(tmp_path, monkeypatch):
    from tests.test_besttime import _when_bot
    bot, sent = _when_bot(tmp_path, monkeypatch)
    bot._send_week("1", None)
    assert "/week dryer" in sent[-1][0]


def test_weekly_schedule_is_a_chat_tool(tmp_path, monkeypatch):
    from franklinwh_scraper import weekly
    from franklinwh_scraper.chat_tools import APP_TOOL_NAMES
    from tests.test_besttime import _when_bot
    assert "weekly_schedule" in APP_TOOL_NAMES
    bot, sent = _when_bot(tmp_path, monkeypatch)
    monkeypatch.setattr(weekly, "for_store", lambda *a, **k: _plan())
    del bot._send
    out = bot._run_app_tool("1", "weekly_schedule", {"load": "dryer"})
    assert "Best time to run the dryer" in out["text"]


def test_describe_says_when_usual_time_is_already_cheap():
    # Weekend super-off-peak: a usual 9 AM Saturday run costs the same as the 7 AM best.
    text = describe_week(_plan(habits={"saturday": [["dryer", "09:00", 1.0, 4.0]]}))
    assert "Sat 9:00 AM is already" in text
    assert "saves about" not in text
