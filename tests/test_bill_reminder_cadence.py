"""The "Log your real bill?" reminder keeps coming every 3 days until a bill is recorded.

The cycle here closes Aug 19, so day N after close is Aug 19 + N. The first reminder is day 7 (the
default), then 10, 13, ... up to day 29: a bill can't be recorded for a cycle older than that
(state entries expire after 30 days), so nagging past it would ask for something that can't be saved.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from franklinwh_scraper import alerts
from franklinwh_scraper.config import Config
from franklinwh_scraper.history import HistoryStore

CLOSE = date(2026, 8, 19)
CFG = Config(billing_cycle_start_day=20)


@pytest.fixture
def db(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    for month, days in (("2026-07", range(20, 32)), ("2026-08", range(1, 20))):
        for day in days:
            for hour in (0, 12):
                store._conn.execute(
                    "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
                    "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (f"{month}-{day:02d}T{hour:02d}:00:00", 0, hour, 1.0, 0.0, 50.0, 1.0, "normal", 0.0, 0.0))
    store._conn.commit()
    return store


def _on_day(n: int, db, state: dict, hour: int = 8, cfg: Config = CFG):
    d = CLOSE + timedelta(days=n)
    now = datetime(d.year, d.month, d.day, hour, 30)
    return alerts._alert_bill_reconciliation(state, d.isoformat(), now, cfg, db)


def test_reminds_every_three_days_from_day_seven_through_day_twenty_nine(db):
    state: dict = {}
    fired = [n for n in range(1, 32) if (m := _on_day(n, db, state)) is not None and "bill" in m.lower()]
    assert fired == [7, 10, 13, 16, 19, 22, 25, 28]


def test_quiet_on_the_days_between(db):
    state: dict = {}
    assert _on_day(7, db, state) is not None
    assert _on_day(8, db, state) is None
    assert _on_day(9, db, state) is None
    assert _on_day(10, db, state) is not None


def test_same_day_is_only_sent_once(db):
    state: dict = {}
    assert _on_day(7, db, state, hour=8) is not None
    assert _on_day(7, db, state, hour=9) is None


def test_a_missed_reminder_is_sent_the_next_morning_not_three_days_later(db):
    """Advisor was down 8-9 AM on day 10; the gap since the last one is already over 3 days on day 11."""
    state: dict = {}
    assert _on_day(7, db, state) is not None
    assert _on_day(11, db, state) is not None
    assert _on_day(12, db, state) is None       # the clock restarts from the one that was sent


def test_nothing_after_the_last_day_a_bill_can_still_be_saved(db):
    state: dict = {}
    assert _on_day(29, db, state) is not None
    assert _on_day(30, db, state) is None
    assert _on_day(31, db, {}) is None


def test_repeat_reminders_say_it_is_still_missing(db):
    state: dict = {}
    first = _on_day(7, db, state)
    second = _on_day(10, db, state)
    assert "Log your real bill" in first
    assert "Still" in second and "/bill" in second and "charges" in second.lower()
    assert "closed 10 days ago" in second


def test_recording_the_bill_stops_the_reminders(db):
    state: dict = {}
    assert _on_day(7, db, state) is not None
    state["actual_bill_2026-08-19"] = 53.63
    report = _on_day(10, db, state)
    assert report is not None and "Bill reconciliation" in report       # the comparison, once
    assert all(_on_day(n, db, state) is None for n in range(11, 31))


def test_a_bill_recorded_on_day_twenty_still_gets_its_comparison(db):
    """The old 7-day tail dropped the comparison for anything recorded after day 14."""
    state = {"actual_bill_2026-08-19": 53.63}
    msg = _on_day(20, db, state)
    assert msg is not None and "Bill reconciliation" in msg


def test_a_configured_late_start_moves_the_whole_series(db):
    cfg = Config(billing_cycle_start_day=20, bill_reminder_after_days=9)
    state: dict = {}
    fired = [n for n in range(1, 32) if _on_day(n, db, state, cfg=cfg) is not None]
    assert fired == [9, 12, 15, 18, 21, 24, 27]


def test_a_bare_reply_works_until_the_next_reminder_takes_over():
    """The bare-number window matches the cadence: open for the 3 days after each reminder."""
    state = {"bill_reconcile_reminded_2026-08-19": "2026-08-26"}
    pend = lambda n: alerts.bill_reply_pending(state, CFG, CLOSE + timedelta(days=n))   # noqa: E731
    assert pend(7) == CLOSE and pend(8) == CLOSE and pend(9) == CLOSE
    assert pend(10) is None
