"""Recording a bill (Telegram reply or CLI) writes the permanent ledger row, with the estimate and,
when given, what was actually due after account credit. `/bills` shows the ledger and how the
estimate has been running."""

from __future__ import annotations

import types
from datetime import datetime

import pytest

from franklinwh_scraper import alerts, billledger, chatbot
from franklinwh_scraper.chatbot import TelegramChatBot
from franklinwh_scraper.config import Config
from franklinwh_scraper.history import HistoryStore

NOW = datetime(2026, 8, 26, 8, 30)
CFG = Config(billing_cycle_start_day=20)


@pytest.fixture(autouse=True)
def _no_wall_clock_pruning(monkeypatch):
    monkeypatch.setattr(alerts, "_prune_old_state", lambda state: state)


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


def _pin_estimate(monkeypatch, estimated: float):
    monkeypatch.setattr(alerts, "savings_compute", lambda iv: types.SimpleNamespace(
        actual_net_energy_cost=estimated - alerts.base_service_cost(31)))


def _bot(tmp_path, store=None) -> TelegramChatBot:
    bot = TelegramChatBot(Config(output_dir=str(tmp_path), billing_cycle_start_day=20), api_key="x", outdir=tmp_path)
    bot._hist_store = store
    return bot


# ---------------------------------------------------------------- parsing the extras

@pytest.mark.parametrize("text, extra", [
    ("/bill 53.63 due 31.70", {"due": 31.70}),
    ("/bill 53.63 credit 21.93", {"credit": 21.93}),
    ("/bill 53.63 2026-08-19 due $31.70", {"due": 31.70}),
    ("bill $53.63 credit $21.93", {"credit": 21.93}),
    ("/bill 53.63", {}),
])
def test_due_and_credit_ride_along_with_the_amount(text, extra):
    assert chatbot._parse_bill_extras(text) == extra
    assert chatbot._parse_bill_reply(text, pending=False)[0] == 53.63


@pytest.mark.parametrize("text", ["/bill 53.63 due", "/bill 53.63 owed 5", "/bill 53.63 due 31.70 credit 5"])
def test_malformed_extras_are_not_a_bill_reply(text):
    assert chatbot._parse_bill_reply(text, pending=False) is None


# ---------------------------------------------------------------- the ledger row

def test_recording_a_bill_writes_the_ledger_with_the_estimate(tmp_path, db, monkeypatch):
    _pin_estimate(monkeypatch, 50.0)
    reply = _bot(tmp_path, db)._bill_command("/bill 53.63", now=NOW)
    assert "+$3.63" in reply
    row = billledger.load(tmp_path)[0]
    assert (row["end"], row["actual"], row["estimate"], row["source"]) == ("2026-08-19", 53.63, 50.0, "reply")


def test_due_gives_the_credit_that_was_applied(tmp_path, db, monkeypatch):
    _pin_estimate(monkeypatch, 50.0)
    reply = _bot(tmp_path, db)._bill_command("/bill 53.63 due 31.70", now=NOW)
    row = billledger.load(tmp_path)[0]
    assert row["due"] == 31.70 and row["credit_applied"] == 21.93
    assert "$21.93" in reply and "$31.70" in reply


def test_credit_gives_what_is_due(tmp_path, db, monkeypatch):
    _pin_estimate(monkeypatch, 50.0)
    _bot(tmp_path, db)._bill_command("/bill 53.63 credit 21.93", now=NOW)
    row = billledger.load(tmp_path)[0]
    assert row["due"] == 31.70 and row["credit_applied"] == 21.93


def test_a_balance_larger_than_the_charges_is_kept_without_inventing_a_credit(tmp_path, db, monkeypatch):
    _pin_estimate(monkeypatch, 50.0)
    _bot(tmp_path, db)._bill_command("/bill 53.63 due 60", now=NOW)
    row = billledger.load(tmp_path)[0]
    assert row["due"] == 60.0 and "credit_applied" not in row


def test_without_history_the_row_is_still_written_with_no_estimate(tmp_path):
    _bot(tmp_path)._bill_command("/bill 53.63", now=NOW)
    row = billledger.load(tmp_path)[0]
    assert row["actual"] == 53.63 and row["estimate"] is None


def test_the_cli_path_records_to_the_ledger_too(tmp_path, db, monkeypatch):
    _pin_estimate(monkeypatch, 50.0)
    end = alerts.record_actual_bill(tmp_path, CFG, 53.63, today=NOW.date(), store=db, source="cli")
    row = billledger.load(tmp_path)[0]
    assert end.isoformat() == row["end"] and row["estimate"] == 50.0 and row["source"] == "cli"


def test_a_ledger_failure_never_costs_you_the_recording(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(billledger, "upsert", boom)
    end = alerts.record_actual_bill(tmp_path, CFG, 53.63, today=NOW.date())
    assert alerts._load_peak_state(tmp_path)[f"actual_bill_{end.isoformat()}"] == 53.63


# ---------------------------------------------------------------- /bills

def test_bills_command_shows_the_table_and_how_the_estimate_has_been_running(tmp_path):
    for end, actual, est in (("2026-09-17", -15.58, -16.38), ("2026-10-16", 39.11, 37.49), ("2026-11-18", 53.63, 50.32)):
        billledger.upsert(tmp_path, {"end": end, "actual": actual, "estimate": est, "source": "t"})
    text = _bot(tmp_path)._bills_command()
    assert "Nov 18" in text and "$53.63" in text and "+$3.31" in text
    assert "low" in text and "5%" in text and "3 bills" in text


def test_bills_command_before_any_bill_says_how_to_add_one(tmp_path):
    text = _bot(tmp_path)._bills_command()
    assert "No bills" in text and "/bill" in text


def test_bills_command_with_too_few_cycles_does_not_claim_a_bias(tmp_path):
    billledger.upsert(tmp_path, {"end": "2026-11-18", "actual": 53.63, "estimate": 50.32, "source": "t"})
    text = _bot(tmp_path)._bills_command()
    assert "$53.63" in text and "running" not in text and "need" in text.lower()
