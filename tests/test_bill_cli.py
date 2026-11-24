"""`franklinwh bill-record` writes the ledger too, and `franklinwh bill-ledger` shows it."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from click.testing import CliRunner

from franklinwh_scraper import billledger
from franklinwh_scraper import cli as cli_mod
from franklinwh_scraper.config import Config


def _run(tmp_path, *args):
    cfg = Config(output_dir=str(tmp_path), billing_cycle_start_day=20)
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        return CliRunner().invoke(cli_mod.cli, list(args))


def _recent() -> str:
    return (datetime.now().date() - timedelta(days=5)).isoformat()


def test_bill_record_adds_a_ledger_row(tmp_path):
    res = _run(tmp_path, "bill-record", "--amount", "53.63", "--cycle-end", _recent())
    assert res.exit_code == 0, res.output
    row = billledger.load(tmp_path)[0]
    assert (row["end"], row["actual"], row["source"]) == (_recent(), 53.63, "cli")


def test_bill_record_due_and_credit_reach_the_ledger(tmp_path):
    _run(tmp_path, "bill-record", "--amount", "53.63", "--due", "31.70", "--cycle-end", _recent())
    row = billledger.load(tmp_path)[0]
    assert row["due"] == 31.70 and row["credit_applied"] == 21.93
    other = tmp_path / "b"
    _run(other, "bill-record", "--amount", "53.63", "--credit", "21.93", "--cycle-end", _recent())
    assert billledger.load(other)[0]["due"] == 31.70


def test_bill_record_rejects_due_and_credit_together(tmp_path):
    res = _run(tmp_path, "bill-record", "--amount", "53.63", "--due", "31.70", "--credit", "21.93",
               "--cycle-end", _recent())
    assert res.exit_code != 0 and "either" in res.output.lower()


def test_bill_ledger_lists_the_rows(tmp_path):
    for end, actual, est in (("2026-09-17", -15.58, -16.38), ("2026-10-16", 39.11, 37.49), ("2026-11-18", 53.63, 50.32)):
        billledger.upsert(tmp_path, {"end": end, "actual": actual, "estimate": est, "source": "t"})
    res = _run(tmp_path, "bill-ledger")
    assert res.exit_code == 0 and "Nov 18" in res.output and "$53.63" in res.output and "low" in res.output


def test_bill_ledger_backfill_recovers_the_alert_log(tmp_path):
    (tmp_path / "alerts_log.jsonl").write_text(json.dumps({
        "ts": "2026-10-23T08:03:15",
        "body": "\U0001F4B5 <b>FranklinWH: Bill reconciliation</b>\nCycle ending Oct 16: actual $39.11 vs app estimate $37.49 — off by +$1.62 (4%)."}) + "\n")
    res = _run(tmp_path, "bill-ledger", "--backfill")
    assert res.exit_code == 0 and "1" in res.output
    assert billledger.load(tmp_path)[0]["estimate"] == 37.49
    assert "Oct 16" in _run(tmp_path, "bill-ledger").output


def test_bill_ledger_empty_says_how_to_start(tmp_path):
    res = _run(tmp_path, "bill-ledger")
    assert res.exit_code == 0 and "No bills" in res.output
