"""Reply to the bot with your real bill instead of running `franklinwh bill-record`.

`/bill 53.63` (or "bill $53.63") records the cycle's actual charges against the most recently closed
cycle and answers with how far the app's estimate was. Right after the "Log your real bill?" reminder,
a bare "$53.63" works too -- only then, so an unrelated number never becomes a bill.
"""

from __future__ import annotations

import types
from datetime import date, datetime

import pytest

from franklinwh_scraper import alerts, chatbot
from franklinwh_scraper.alerts import _load_peak_state
from franklinwh_scraper.chatbot import TelegramChatBot
from franklinwh_scraper.config import Config
from franklinwh_scraper.history import HistoryStore

NOW = datetime(2026, 8, 26, 8, 30)             # a week after the Jul 20 - Aug 19 cycle closed
CFG = dict(billing_cycle_start_day=20)


@pytest.fixture(autouse=True)
def _no_wall_clock_pruning(monkeypatch):
    """_save_peak_state drops actual_bill_* entries older than 30 days by the REAL clock, which would
    eat these fixed-date (Aug 2026) fixtures the moment the calendar moves on."""
    monkeypatch.setattr(alerts, "_prune_old_state", lambda state: state)


def _bot(tmp_path) -> TelegramChatBot:
    return TelegramChatBot(Config(output_dir=str(tmp_path), **CFG), api_key="x", outdir=tmp_path)


def _write(tmp_path, state: dict) -> None:
    import json
    (tmp_path / ".peak_alert_state.json").write_text(json.dumps(state))


def _insert_cycle_readings(db, month: str, days: range):
    for day in days:
        for hour in (0, 12):
            db._conn.execute(
                "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
                "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"{month}-{day:02d}T{hour:02d}:00:00", 0, hour, 1.0, 0.0, 50.0, 1.0, "normal", 0.0, 0.0),
            )
    db._conn.commit()


# ---------------------------------------------------------------- parsing

@pytest.mark.parametrize("text, expected", [
    ("/bill 53.63", (53.63, None)),
    ("/bill $53.63", (53.63, None)),
    ("/BILL 53.63", (53.63, None)),
    ("/bill@franklin_bot 53.63", (53.63, None)),
    ("bill 53.63", (53.63, None)),
    ("bill is $53.63", (53.63, None)),
    ("/bill 54", (54.0, None)),
    ("/bill -14.44", (-14.44, None)),                       # a net-credit cycle is a real bill
    ("/bill 53.63 2026-11-18", (53.63, date(2026, 11, 18))),
])
def test_explicit_bill_replies_parse(text, expected):
    assert chatbot._parse_bill_reply(text, pending=False) == expected


@pytest.mark.parametrize("text", ["/bill", "/bill abc", "/bill 53.63.1", "bill me later", "what is my bill",
                                  "/bill 53.63 tomorrow", "/bill 1e9", "hello 5", "", "/bill 99999"])
def test_things_that_are_not_a_bill_reply_are_left_alone(text):
    assert chatbot._parse_bill_reply(text, pending=True) is None


@pytest.mark.parametrize("text, expected", [("$53.63", 53.63), ("53.63", 53.63), ("$ 53.63", 53.63), ("54", 54.0)])
def test_bare_amount_counts_only_while_a_reminder_is_waiting(text, expected):
    assert chatbot._parse_bill_reply(text, pending=True) == (expected, None)
    assert chatbot._parse_bill_reply(text, pending=False) is None


# ---------------------------------------------------------------- recording

def test_bill_reply_records_the_most_recently_closed_cycle(tmp_path):
    bot = _bot(tmp_path)
    reply = bot._bill_command("/bill 53.63", now=NOW)
    assert reply is not None and "$53.63" in reply and "Aug 19" in reply
    assert _load_peak_state(tmp_path)["actual_bill_2026-08-19"] == 53.63


def test_bill_reply_with_a_cycle_end_records_that_cycle(tmp_path):
    bot = _bot(tmp_path)
    bot._bill_command("/bill 40.00 2026-08-17", now=NOW)
    assert _load_peak_state(tmp_path)["actual_bill_2026-08-17"] == 40.0


def test_bill_reply_says_how_far_off_the_estimate_was(tmp_path, monkeypatch):
    db = HistoryStore(tmp_path / "h.db")
    _insert_cycle_readings(db, "2026-07", range(20, 32))
    _insert_cycle_readings(db, "2026-08", range(1, 20))
    monkeypatch.setattr(alerts, "savings_compute", lambda iv: types.SimpleNamespace(
        actual_net_energy_cost=50.0 - alerts.base_service_cost(31)))
    bot = _bot(tmp_path)
    bot._hist_store = db
    reply = bot._bill_command("/bill 53.63", now=NOW)
    assert "estimate $50.00" in reply and "+$3.63" in reply and "7%" in reply


def test_bill_reply_still_records_when_the_estimate_cannot_be_computed(tmp_path):
    bot = _bot(tmp_path)                                    # no history store at all
    reply = bot._bill_command("/bill 53.63", now=NOW)
    assert "Recorded $53.63" in reply
    assert _load_peak_state(tmp_path)["actual_bill_2026-08-19"] == 53.63


def test_a_cycle_too_old_to_keep_is_refused_not_silently_dropped(tmp_path):
    bot = _bot(tmp_path)
    reply = bot._bill_command("/bill 53.63 2026-06-19", now=NOW)
    assert "too old" in reply.lower()
    assert "actual_bill_2026-06-19" not in _load_peak_state(tmp_path)


def test_a_bill_reply_replaces_the_reminder_and_the_morning_report(tmp_path):
    """The reply already carries the comparison, so tomorrow's reconciliation alert stays quiet."""
    db = HistoryStore(tmp_path / "h.db")
    _insert_cycle_readings(db, "2026-07", range(20, 32))
    _insert_cycle_readings(db, "2026-08", range(1, 20))
    bot = _bot(tmp_path)
    bot._hist_store = db
    bot._bill_command("/bill 53.63", now=NOW)
    state = _load_peak_state(tmp_path)
    nxt = datetime(2026, 8, 27, 8, 30)
    assert alerts._alert_bill_reconciliation(state, "2026-08-27", nxt, Config(**CFG), db) is None


# ---------------------------------------------------------------- bare reply after the reminder

def test_bare_amount_records_right_after_the_reminder(tmp_path):
    _write(tmp_path, {"bill_reconcile_reminded_2026-08-19": "2026-08-26"})
    bot = _bot(tmp_path)
    reply = bot._bill_command("$53.63", now=NOW)
    assert reply is not None and "Recorded $53.63" in reply
    assert _load_peak_state(tmp_path)["actual_bill_2026-08-19"] == 53.63


def test_bare_amount_is_ignored_when_no_reminder_is_waiting(tmp_path):
    bot = _bot(tmp_path)
    assert bot._bill_command("$53.63", now=NOW) is None
    assert "actual_bill_2026-08-19" not in _load_peak_state(tmp_path)


def test_bare_amount_is_ignored_once_the_bill_is_already_recorded(tmp_path):
    _write(tmp_path, {"bill_reconcile_reminded_2026-08-19": "2026-08-26", "actual_bill_2026-08-19": 50.0})
    assert _bot(tmp_path)._bill_command("80", now=NOW) is None


def test_bare_amount_is_ignored_when_the_reminder_is_stale(tmp_path):
    _write(tmp_path, {"bill_reconcile_reminded_2026-08-19": "2026-08-20"})   # six days ago
    assert _bot(tmp_path)._bill_command("$53.63", now=NOW) is None


# ---------------------------------------------------------------- the reminder tells you how

def test_reminder_says_to_just_reply_and_which_number_to_use(tmp_path):
    db = HistoryStore(tmp_path / "h.db")
    _insert_cycle_readings(db, "2026-07", range(20, 32))
    _insert_cycle_readings(db, "2026-08", range(1, 20))
    msg = alerts._alert_bill_reconciliation({}, "2026-08-26", NOW, Config(**CFG), db)
    assert "reply" in msg.lower() and "/bill" in msg
    assert "charges" in msg.lower()                         # the cycle's charges, not the amount due after credits
    assert "bill-record --amount" in msg                    # the terminal route still works


def test_help_lists_the_bill_reply():
    import inspect
    assert "/bill" in inspect.getsource(TelegramChatBot) and "real bill" in inspect.getsource(TelegramChatBot)
