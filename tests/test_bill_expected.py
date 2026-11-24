"""Projections say what to expect on the REAL bill, correcting for how the estimate has run before.

The monthly projection alert, /bill and /api/bill all add it once the ledger has three usable cycles.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from franklinwh_scraper import alerts, billledger
from franklinwh_scraper.chatbot import TelegramChatBot
from franklinwh_scraper.config import Config
from franklinwh_scraper.history import HistoryStore

THREE = [("2026-09-17", -15.58, -16.38), ("2026-10-16", 39.11, 37.49), ("2026-11-18", 53.63, 50.32)]


def _seed(out, rows=THREE):
    for end, actual, est in rows:
        billledger.upsert(out, {"end": end, "actual": actual, "estimate": est, "source": "t"})


def test_expected_line_is_empty_until_there_are_three_cycles(tmp_path):
    _seed(tmp_path, THREE[:2])
    assert billledger.expected_line(billledger.load(tmp_path), 50.0) == ""
    assert billledger.expected_line([], 50.0) == ""


def test_expected_line_gives_the_corrected_figure_range_and_why(tmp_path):
    _seed(tmp_path)
    line = billledger.expected_line(billledger.load(tmp_path), 50.0)
    assert "$52.63" in line and "$52.16" in line and "$53.29" in line
    assert "5% low" in line and "3 bills" in line


def test_expected_line_for_a_model_that_runs_high_says_so(tmp_path):
    _seed(tmp_path, [("2026-09-17", 30.0, 33.0), ("2026-10-16", 30.0, 32.0), ("2026-11-18", 30.0, 34.0)])
    line = billledger.expected_line(billledger.load(tmp_path), 40.0)
    assert "high" in line and "$36" in line


def _db(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    for month, days in (("2026-07", range(20, 32)), ("2026-08", range(1, 6))):
        for day in days:
            for hour in (0, 12):
                store._conn.execute(
                    "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
                    "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (f"{month}-{day:02d}T{hour:02d}:00:00", 0, hour, 1.0, 0.0, 50.0, 1.0, "normal", 0.0, 0.0))
    store._conn.commit()
    return store


def test_the_monthly_projection_alert_adds_the_expectation(tmp_path):
    _seed(tmp_path)
    msg = alerts._alert_bill_projection({}, "2026-08-05", datetime(2026, 8, 5, 8, 30), _db(tmp_path),
                                        Config(billing_cycle_start_day=20), out=tmp_path)
    assert msg is not None and "Projected full cycle" in msg and "Expect on the real bill" in msg


def test_the_projection_alert_is_unchanged_without_a_ledger(tmp_path):
    msg = alerts._alert_bill_projection({}, "2026-08-05", datetime(2026, 8, 5, 8, 30), _db(tmp_path),
                                        Config(billing_cycle_start_day=20), out=tmp_path)
    assert msg is not None and "Expect on the real bill" not in msg
    old = alerts._alert_bill_projection({}, "2026-08-05", datetime(2026, 8, 5, 8, 30), _db(tmp_path),
                                        Config(billing_cycle_start_day=20))
    assert old is not None and "Expect on the real bill" not in old


def test_the_bill_command_adds_the_expectation(tmp_path, monkeypatch):
    _seed(tmp_path)
    store = _db(tmp_path)
    cfg = Config(output_dir=str(tmp_path), billing_cycle_start_day=20)
    bot = TelegramChatBot(cfg, api_key="x", outdir=tmp_path)
    sent: list[str] = []
    monkeypatch.setattr(bot, "_send", lambda chat, text, **k: sent.append(text))
    # pin "today" inside the seeded cycle
    import franklinwh_scraper.chatbot as cb
    real = cb.datetime

    class _Frozen(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 8, 5, 12, 0)
    monkeypatch.setattr(cb, "datetime", _Frozen)
    bot._send_bill("chat")
    assert sent and "Expect on the real bill" in sent[0], sent
    store.close() if hasattr(store, "close") else None


def test_api_bill_carries_the_expectation_and_the_accuracy(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi
    _seed(tmp_path)
    monkeypatch.setattr(webapi, "_cfg", Config(billing_cycle_start_day=20, output_dir=str(tmp_path)), raising=False)
    monkeypatch.setattr(webapi, "_OUT", tmp_path, raising=False)
    body = TestClient(webapi.app).get("/api/bill").json()
    assert body["expected"] is not None and body["expected"]["n"] == 3
    assert body["expected"]["point"] == pytest.approx(body["projected"] * 1.0527, abs=0.05) or body["projected"] <= 0
    assert body["accuracy"]["n"] == 3


def test_api_bill_expectation_is_null_without_enough_history(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi
    monkeypatch.setattr(webapi, "_cfg", Config(billing_cycle_start_day=20, output_dir=str(tmp_path)), raising=False)
    monkeypatch.setattr(webapi, "_OUT", tmp_path, raising=False)
    body = TestClient(webapi.app).get("/api/bill").json()
    assert "expected" in body and body["expected"] is None and body["accuracy"] is None
