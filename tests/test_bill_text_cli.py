"""`bill-record --from-text` compares the bill with the app's numbers, warns on rate changes, and
files the cycle in the permanent ledger (with the SBP credit bank)."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from click.testing import CliRunner

from franklinwh_scraper import billledger
from franklinwh_scraper import cli as cli_mod
from franklinwh_scraper.config import Config
from franklinwh_scraper.history import HistoryStore
from tests.test_billcompare import SEP_BILL


def _history(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    d = date(2026, 8, 19)
    while d <= date(2026, 9, 17):
        for hour in (0, 3, 9, 12, 17, 22):
            store._conn.execute(
                "INSERT INTO readings (timestamp,day_of_week,hour_of_day,home_load_kw,"
                "solar_kw,battery_soc,grid_use_kw,grid_status,solar_total_kwh,battery_use_kw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"{d.isoformat()}T{hour:02d}:00:00", d.weekday(), hour, 1.0, 0.0, 50.0,
                 1.0 if hour in (0, 3, 22) else -1.0, "normal", 0.0, 0.0))
        d += timedelta(days=1)
    store._conn.commit()
    store.close() if hasattr(store, "close") else None


def _run(tmp_path, text, *args):
    f = tmp_path / "bill.txt"
    f.write_text(text)
    cfg = Config(output_dir=str(tmp_path), billing_cycle_start_day=20)
    with patch("franklinwh_scraper.cli.load_config", return_value=cfg):
        return CliRunner().invoke(cli_mod.cli, ["bill-record", "--from-text", str(f), *args])


def test_dry_run_shows_the_comparison_and_saves_nothing(tmp_path):
    _history(tmp_path)
    res = _run(tmp_path, SEP_BILL, "--dry-run")
    assert res.exit_code == 0, res.output
    assert "Import kWh" in res.output and "Export credits" in res.output and "Total" in res.output
    assert billledger.load(tmp_path) == []


def test_recording_files_the_cycle_with_estimate_credit_bank_and_lines(tmp_path):
    _history(tmp_path)
    res = _run(tmp_path, SEP_BILL)
    assert res.exit_code == 0, res.output
    row = billledger.load(tmp_path)[0]
    assert row["end"] == "2026-09-17" and row["source"] == "bill-text"
    assert row["actual"] == -15.58 and isinstance(row["estimate"], float)
    assert row["sbp_credited"] == 25.63 and row["sbp_balance"] == 105.47
    assert row["lines"]["import_kwh"]["bill"] > 0 and "model" in row["lines"]["import_kwh"]


def test_a_changed_rate_is_called_out_before_it_is_learned(tmp_path):
    _history(tmp_path)
    res = _run(tmp_path, SEP_BILL.replace("$0.38242 .97", "$0.42000 1.07"), "--dry-run")
    assert "rate change" in res.output.lower() and "0.42" in res.output and "on-peak" in res.output.lower()


def test_no_rate_change_noise_when_nothing_moved(tmp_path):
    _history(tmp_path)
    assert "rate change" not in _run(tmp_path, SEP_BILL, "--dry-run").output.lower()


def test_the_rate_changes_are_kept_in_the_ledger(tmp_path):
    _history(tmp_path)
    _run(tmp_path, SEP_BILL.replace("$0.38242 .97", "$0.42000 1.07"))
    assert any("on-peak" in c for c in billledger.load(tmp_path)[0]["rate_changes"])


def test_without_history_the_bill_is_still_recorded_just_not_compared(tmp_path):
    res = _run(tmp_path, SEP_BILL)
    assert res.exit_code == 0, res.output
    row = billledger.load(tmp_path)[0]
    assert row["estimate"] is None and "lines" not in row


def test_the_sbp_bank_shows_in_the_ledger_summary(tmp_path):
    _history(tmp_path)
    _run(tmp_path, SEP_BILL)
    assert "105.47" in billledger.format_summary(billledger.load(tmp_path))
