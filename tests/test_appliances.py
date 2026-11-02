"""Learning the appliances you run (and when) from the answers to "what was that?"."""

import pathlib
import sys
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from franklinwh_scraper import appliances

TODAY = date(2026, 11, 2)                       # a Monday
SUNDAYS = [date(2026, 11, 1) - timedelta(weeks=i) for i in range(8)]     # Nov 1 back to Sep 13


def _ev(day, name, hhmm="14:00", minutes=60, kw=4.0):
    return {"key": f"{day.isoformat()}T{hhmm}", "name": name, "kw": kw, "minutes": minutes}


def test_buttons_fit_telegram_and_name_the_appliances():
    btns = appliances.buttons("2026-10-09T07:43")
    assert all(len(b["callback_data"]) <= 64 for b in btns)
    assert {b["callback_data"].rsplit(":", 1)[1] for b in btns} == set(appliances.NAMES)


def test_an_answer_needs_the_spike_it_is_about():
    state: dict = {}
    assert appliances.record_event(state, "2026-10-09T07:43", "d") is None          # nothing remembered
    appliances.remember_spike(state, "2026-10-09T07:43", 4.1, 62)
    assert appliances.record_event(state, "2026-10-09T07:43", "d") == "dryer"
    assert state["appliance_events"] == [{"key": "2026-10-09T07:43", "name": "dryer", "kw": 4.1, "minutes": 62}]
    assert appliances.record_event(state, "2026-10-09T07:43", "zz") is None         # unknown code


def test_a_second_answer_for_the_same_spike_replaces_the_first():
    state: dict = {}
    appliances.remember_spike(state, "2026-10-09T07:43", 4.1, 62)
    appliances.record_event(state, "2026-10-09T07:43", "d")
    appliances.record_event(state, "2026-10-09T07:43", "o")
    assert [e["name"] for e in state["appliance_events"]] == ["oven"]


def test_remembered_spikes_and_events_are_capped():
    state: dict = {}
    for i in range(80):
        appliances.remember_spike(state, f"2026-01-01T00:{i:02d}", 3.0, 30)
    assert len(state["spike_facts"]) <= appliances.MAX_FACTS


def test_a_weekly_dryer_becomes_a_habit_on_that_weekday():
    events = [_ev(d, "dryer") for d in SUNDAYS[:4]]
    habits = appliances.learned_habit_loads(events, TODAY)
    assert habits == {"sunday": [["dryer", "14:00", 1.0, 4.0]]}


def test_two_uses_are_not_a_habit_yet():
    assert appliances.learned_habit_loads([_ev(d, "dryer") for d in SUNDAYS[:2]], TODAY) == {}


def test_a_rare_use_is_not_a_habit_even_with_three_uses():
    early = {"key": "2026-09-08T08:00", "name": "oven", "kw": 2.5, "minutes": 60}     # sets the start of history
    events = [early] + [_ev(d, "dryer") for d in SUNDAYS[:3]]                         # 3 of 8 Sundays
    assert "sunday" not in appliances.learned_habit_loads(events, TODAY)


def test_other_is_never_a_habit():
    assert appliances.learned_habit_loads([_ev(d, "other") for d in SUNDAYS[:5]], TODAY) == {}


def test_pieces_of_one_run_on_the_same_day_are_one_load():
    events = []
    for d in SUNDAYS[:3]:                       # oven: preheat at 17:00, bake at 17:40
        events += [_ev(d, "oven", "17:00", 30, 3.0), _ev(d, "oven", "17:40", 60, 2.0)]
    (name, start, hours, kw), = appliances.learned_habit_loads(events, TODAY)["sunday"]
    assert (name, start, hours) == ("oven", "17:00", 1.5)
    assert 2.0 < kw < 3.0


def test_start_hours_and_power_are_the_middle_of_what_you_do():
    events = [_ev(SUNDAYS[0], "dryer", "13:00", 60, 3.8), _ev(SUNDAYS[1], "dryer", "14:00", 90, 4.0),
              _ev(SUNDAYS[2], "dryer", "16:00", 60, 4.2)]
    (_n, start, hours, kw), = appliances.learned_habit_loads(events, TODAY)["sunday"]
    assert start == "14:00" and hours == 1.0 and kw == 4.0


def test_old_events_age_out():
    old = [_ev(d - timedelta(weeks=12), "dryer") for d in SUNDAYS[:4]]
    assert appliances.learned_habit_loads(old, TODAY) == {}
