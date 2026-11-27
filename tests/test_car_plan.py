"""Telling the advisor whether the car charges today, and showing both outcomes until you do.

The sundown projection assumes no charging; that was the biggest source of morning misses. Your
past charging days don't predict the next one (weekday and sun scored no better than always saying
"no"), so it asks instead of guessing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from franklinwh_scraper import alerts, planned
from franklinwh_scraper.chatbot import _parse_ebplan, _parse_plan
from franklinwh_scraper.config import Config
from franklinwh_scraper.history import HistoryStore
from tests.test_unit import (_GLOOM, _SUNNY, _car_day, _day_forecast, _press_button, _quiet_day, _run_at, _seed_loads,
                             _sundown_bot_with_pattern, _tracker_c)

DAY = "2026-10-26"


@pytest.fixture(autouse=True)
def _no_wall_clock_pruning(monkeypatch):
    monkeypatch.setattr(alerts, "_prune_old_state", lambda state: state)


def _car_history(tmp_path, today=datetime(2026, 10, 26), every=3, start=15):
    db = HistoryStore(tmp_path / "car.db")
    for i in range(1, 22):
        d = (today - timedelta(days=i)).date()
        _seed_loads(db, d, _car_day(start) if i % every == 0 else _quiet_day())
    return db


# ---------------------------------------------------------------- the stored answer

def test_the_answer_is_yes_no_or_not_given():
    st: dict = {}
    assert planned.car_answer(st, DAY) is None and planned.car_load(st, DAY) is None
    planned.set_car(st, DAY, {"start": 15.0, "hours": 2.5, "kw": 3.0})
    assert planned.car_answer(st, DAY) is True
    assert planned.car_load(st, DAY) == {"name": "car", "start": 15.0, "hours": 2.5, "kw": 3.0}
    planned.set_car(st, DAY, None)
    assert planned.car_answer(st, DAY) is False and planned.car_load(st, DAY) is None


def test_answering_about_the_car_does_not_answer_for_your_other_loads():
    """Saying the car charges must not switch off the "usual Sunday dryer" assumption."""
    st: dict = {}
    planned.set_car(st, DAY, {"start": 15.0, "hours": 2.0, "kw": 3.0})
    assert planned.declared(st, DAY) is False and planned.get_plan(st, DAY) == []


def test_with_car_adds_it_to_a_load_list_once():
    st: dict = {}
    dryer = [{"name": "dryer", "start": 14.0, "hours": 1.0, "kw": 4.0}]
    assert planned.with_car(st, DAY, dryer) == dryer
    planned.set_car(st, DAY, {"start": 15.0, "hours": 2.0, "kw": 3.0})
    names = [ld["name"] for ld in planned.with_car(st, DAY, dryer)]
    assert names == ["dryer", "car"]
    assert planned.evening_kwh(planned.with_car(st, DAY, dryer)) == pytest.approx(3.0)   # 4-5 PM of the car's 2 h


def test_clearing_the_plan_clears_the_car_answer_too():
    st: dict = {}
    planned.set_car(st, DAY, None)
    planned.clear_plan(st, DAY)
    assert planned.car_answer(st, DAY) is None


# ---------------------------------------------------------------- what "the car" means

def test_default_car_load_comes_from_your_own_sessions(tmp_path):
    profile = alerts._afternoon_profile(_car_history(tmp_path), datetime(2026, 10, 26).date())
    load = alerts._car_load_for(profile, now_hour=8.0)
    assert load["start"] == 15.0 and load["hours"] == pytest.approx(3.0) and load["kw"] == pytest.approx(2.8, abs=0.05)


def test_default_car_load_without_history_and_with_a_late_or_given_start():
    assert alerts._car_load_for(None, now_hour=8.0) == {"start": 15.0, "hours": 2.5, "kw": 3.0}
    assert alerts._car_load_for(None, now_hour=16.5)["start"] == 16.5          # the usual time has passed: from now
    given = alerts._car_load_for(None, now_hour=8.0, start=13.0, hours=1.5)
    assert (given["start"], given["hours"]) == (13.0, 1.5)


# ---------------------------------------------------------------- /plan

@pytest.mark.parametrize("text, expected", [
    ("/plan car", {"car": {"start": None, "hours": None}}),
    ("/plan car 3pm", {"car": {"start": 15, "hours": None}}),
    ("/plan car at 1pm for 90m", {"car": {"start": 13, "hours": 1.5}}),
    ("/plan no car", {"car": False}),
    ("/plan nocar", {"car": False}),
])
def test_plan_command_takes_the_car(text, expected):
    assert _parse_plan(text) == expected


def test_car_and_appliances_in_one_command():
    got = _parse_plan("/plan dryer 4pm, car 3pm")
    assert got["car"] == {"start": 15, "hours": None} and [ld["name"] for ld in got["loads"]] == ["dryer"]


def test_bad_input_and_ebplan_are_unchanged():
    assert _parse_plan("/plan banana car") is None
    assert _parse_plan("/plan car 25pm") is None
    assert _parse_plan("/plan dryer 4pm") == {"loads": [{"name": "dryer", "start": 16.0, "hours": 1.0, "kw": 4.0}]}
    assert [ld["name"] for ld in _parse_ebplan("/ebplan dryer 4pm")["loads"]] == ["dryer"]


# ---------------------------------------------------------------- buttons

def _callbacks(rows):
    return [b["callback_data"] for row in rows for b in row]


def test_car_buttons_are_offered_until_you_answer(tmp_path):
    db = _car_history(tmp_path)
    now = datetime(2026, 10, 26, 8, 0)
    assert {"plan:car", "plan:nocar"} <= set(_callbacks(alerts._plan_prompt_rows({}, db, now)))
    st: dict = {}
    planned.set_none(st, DAY)                                   # "no big loads" is about appliances, not the car
    assert _callbacks(alerts._plan_prompt_rows(st, db, now)) == ["plan:car", "plan:nocar"]
    planned.set_car(st, DAY, None)
    assert alerts._plan_prompt_rows(st, db, now) == []


def test_no_car_buttons_for_a_house_that_never_charges_one(tmp_path):
    db = HistoryStore(tmp_path / "q.db")
    for i in range(1, 22):
        _seed_loads(db, (datetime(2026, 10, 26) - timedelta(days=i)).date(), _quiet_day())
    assert "plan:car" not in _callbacks(alerts._plan_prompt_rows({}, db, datetime(2026, 10, 26, 8, 0)))


@pytest.mark.parametrize("button, answer", [("plan:car", True), ("plan:nocar", False)])
def test_tapping_a_car_button_stores_the_answer_and_replies(tmp_path, monkeypatch, button, answer):
    now = datetime(2026, 7, 15, 8, 0)
    bot, sent = _sundown_bot_with_pattern(tmp_path, monkeypatch, now)
    _run_at(now, _press_button, bot, button, monkeypatch)
    state = alerts._load_peak_state(tmp_path)
    assert planned.car_answer(state, "2026-07-15") is answer
    assert "car" in sent["text"].lower()


# ---------------------------------------------------------------- the morning plan

def _plan(monkeypatch, store, solar, state=None):
    now = datetime(2026, 10, 26, 7, 30)
    fc = _day_forecast(now.date(), solar, load=0.5, start_hour=7)
    monkeypatch.setattr(alerts, "predict", lambda *a, **k: fc)     # the store only supplies the car history
    st = {} if state is None else state
    text = alerts._daily_plan(st, DAY, now, _tracker_c(40.0), Config(battery_capacity_kwh=13.6), None, fc, store)
    return text, st

def test_morning_plan_shows_both_outcomes_until_answered(tmp_path, monkeypatch):
    text, state = _plan(monkeypatch, _car_history(tmp_path), _SUNNY)
    assert "Sundown" in text and "If you charge the car" in text
    quiet = state[f"daily_plan_{DAY}"]["sundown_pct"]
    assert quiet > 0


def test_morning_plan_after_yes_leads_with_the_car_and_records_that_number(tmp_path, monkeypatch):
    db = _car_history(tmp_path)
    _text, base = _plan(monkeypatch, db, _GLOOM)
    st: dict = {}
    planned.set_car(st, DAY, {"start": 15.0, "hours": 3.0, "kw": 2.8})
    text, state = _plan(monkeypatch, db, _GLOOM, st)
    assert "With the car" in text and "If you charge the car" not in text
    assert state[f"daily_plan_{DAY}"]["sundown_pct"] < base[f"daily_plan_{DAY}"]["sundown_pct"] - 10


def test_morning_plan_after_no_shows_only_the_quiet_number(tmp_path, monkeypatch):
    st: dict = {}
    planned.set_car(st, DAY, None)
    text, _state = _plan(monkeypatch, _car_history(tmp_path), _SUNNY, st)
    assert "If you charge the car" not in text and "With the car" not in text and "Sundown" in text


# ---------------------------------------------------------------- /sundown

def _sundown_with(tmp_path, monkeypatch, car):
    now = datetime(2026, 7, 15, 12, 0)
    bot, sent = _sundown_bot_with_pattern(tmp_path, monkeypatch, now)
    if car is not None:
        st: dict = {}
        planned.set_car(st, "2026-07-15", {"start": 15.0, "hours": 3.0, "kw": 2.8} if car else None)
        alerts._save_peak_state(tmp_path, st)
    _run_at(now, bot._send_sundown, "123")
    return sent["text"]


def test_sundown_offers_the_car_case_while_unanswered(tmp_path, monkeypatch):
    assert "If you charge the car" in _sundown_with(tmp_path, monkeypatch, None)


def test_sundown_after_no_drops_the_car_case(tmp_path, monkeypatch):
    text = _sundown_with(tmp_path, monkeypatch, False)
    assert "If you charge the car" not in text and "🚗" not in text


def test_sundown_after_yes_states_the_outcome_with_the_car(tmp_path, monkeypatch):
    text = _sundown_with(tmp_path, monkeypatch, True)
    assert "If you charge the car" not in text
    assert "With the car" in text and "before the car" in text
