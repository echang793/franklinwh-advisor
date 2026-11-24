"""Permanent bill ledger: one row per billing cycle, kept past the 30-day state pruning.

Pairs each real bill with the estimate the app had for it, so accuracy can be tracked over time and
the projection corrected for a consistent bias.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from franklinwh_scraper import billledger


def _e(end: str, actual: float, estimate: float | None, **kw) -> dict:
    return {"end": end, "actual": actual, "estimate": estimate, "source": "test", **kw}


def test_missing_or_corrupt_ledger_reads_as_empty(tmp_path):
    assert billledger.load(tmp_path) == []
    (tmp_path / billledger.LEDGER_FILE).write_text("{not json")
    assert billledger.load(tmp_path) == []
    (tmp_path / billledger.LEDGER_FILE).write_text(json.dumps({"not": "a list"}))
    assert billledger.load(tmp_path) == []


def test_upsert_creates_and_sorts_by_cycle_end(tmp_path):
    billledger.upsert(tmp_path, _e("2026-11-18", 53.63, 50.32))
    billledger.upsert(tmp_path, _e("2026-10-16", 39.11, 37.49))
    assert [r["end"] for r in billledger.load(tmp_path)] == ["2026-10-16", "2026-11-18"]
    assert not list(tmp_path.glob("*.tmp"))


def test_the_same_cycle_is_updated_not_duplicated_even_when_the_end_date_drifts(tmp_path):
    billledger.upsert(tmp_path, _e("2026-11-19", 53.63, 50.32))           # the app's computed end
    billledger.upsert(tmp_path, _e("2026-11-18", 53.63, None, due=31.70))  # the bill's real end
    rows = billledger.load(tmp_path)
    assert len(rows) == 1
    assert rows[0]["estimate"] == 50.32 and rows[0]["due"] == 31.70       # a later, thinner write keeps what we had


def test_a_new_value_replaces_the_old_one_but_none_never_erases(tmp_path):
    billledger.upsert(tmp_path, _e("2026-11-18", 50.0, 48.0))
    billledger.upsert(tmp_path, _e("2026-11-18", 53.63, None))
    row = billledger.load(tmp_path)[0]
    assert row["actual"] == 53.63 and row["estimate"] == 48.0


def test_neighbouring_cycles_stay_separate(tmp_path):
    billledger.upsert(tmp_path, _e("2026-10-16", 39.11, 37.49))
    billledger.upsert(tmp_path, _e("2026-11-18", 53.63, 50.32))           # 33 days apart
    assert len(billledger.load(tmp_path)) == 2


# ---------------------------------------------------------------- accuracy

THREE = [_e("2026-09-17", -15.58, -16.38), _e("2026-10-16", 39.11, 37.49), _e("2026-11-18", 53.63, 50.32)]


def test_accuracy_needs_at_least_three_usable_cycles():
    assert billledger.accuracy(THREE[:2]) is None
    assert billledger.accuracy([]) is None


def test_accuracy_reports_bias_spread_and_error():
    a = billledger.accuracy(THREE)
    assert a["n"] == 3
    # +0.80/16.38 = +4.9%, +1.62/37.49 = +4.3%, +3.31/50.32 = +6.6%
    assert a["bias_pct"] == pytest.approx(5.27, abs=0.05)
    assert a["low_pct"] == pytest.approx(4.32, abs=0.05) and a["high_pct"] == pytest.approx(6.58, abs=0.05)
    assert a["mean_abs_err"] == pytest.approx((0.80 + 1.62 + 3.31) / 3, abs=0.01)


def test_accuracy_skips_rows_that_would_poison_it():
    rows = THREE + [
        _e("2026-08-19", -21.73, 24.58, exclude=True),    # before the model had learned from any bill
        _e("2026-07-20", 3.0, 2.0),                         # estimate under $5: a percent of it is noise
        _e("2026-06-20", 40.0, None),                       # no estimate on file
    ]
    assert billledger.accuracy(rows)["n"] == 3


def test_accuracy_looks_at_the_most_recent_cycles_only():
    old = [_e(f"2025-0{m}-15", 100.0, 50.0) for m in range(1, 7)]       # wildly off, long ago
    a = billledger.accuracy(old + THREE, n=3)
    assert a["n"] == 3 and a["bias_pct"] == pytest.approx(5.27, abs=0.05)


def test_expected_bill_applies_the_bias_and_gives_a_range():
    stats = billledger.accuracy(THREE)
    ex = billledger.expected(50.0, stats)
    assert ex["point"] == pytest.approx(52.64, abs=0.02)
    assert ex["low"] == pytest.approx(52.16, abs=0.02) and ex["high"] == pytest.approx(53.29, abs=0.02)
    assert billledger.expected(50.0, None) is None


def test_expected_bill_for_a_credit_month_moves_the_right_way():
    """A -$20 estimate that runs 5% low means a bill about $1 higher (less credit), not a bigger credit."""
    ex = billledger.expected(-20.0, {"bias_pct": 5.0, "low_pct": 4.0, "high_pct": 6.0})
    assert ex["point"] == pytest.approx(-19.0, abs=0.01)


# ---------------------------------------------------------------- seeding from the alert log

def _log(tmp_path, *rows):
    (tmp_path / "alerts_log.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _rec(ts, text):
    return {"ts": ts, "urgent": False, "body": f"\U0001F4B5 <b>FranklinWH: Bill reconciliation</b>\n{text}"}


def test_seed_from_the_alert_log_recovers_past_cycles(tmp_path):
    _log(tmp_path,
         _rec("2026-10-23T08:03:15", "Cycle ending Oct 16: actual $39.11 vs app estimate $37.49 — off by +$1.62 (4%)."),
         _rec("2026-10-24T08:03:15", "Cycle ending Oct 16: actual $39.11 vs app estimate $37.49 — off by +$1.62 (4%)."),
         {"ts": "2026-10-25T08:00:00", "body": "something else entirely"})
    assert billledger.seed_from_alert_log(tmp_path) == 1
    row = billledger.load(tmp_path)[0]
    assert (row["end"], row["actual"], row["estimate"], row["source"]) == ("2026-10-16", 39.11, 37.49, "alert-log")


def test_seed_handles_both_minus_sign_formats_and_prefers_the_later_duplicate_of_a_cycle(tmp_path):
    _log(tmp_path,
         _rec("2026-09-25T08:02:31", "Cycle ending Sep 19: actual -$15.58 vs app estimate -$14.44 — off by -$1.14 (8%)."),
         _rec("2026-09-26T08:03:02", "Cycle ending Sep 17: actual -$15.58 vs app estimate -$16.38 — off by +$0.80 (5%)."))
    billledger.seed_from_alert_log(tmp_path)
    rows = billledger.load(tmp_path)
    assert len(rows) == 1 and rows[0]["end"] == "2026-09-17" and rows[0]["estimate"] == -16.38


def test_seed_marks_the_pre_calibration_cycle_as_excluded_from_accuracy(tmp_path):
    _log(tmp_path, _rec("2026-08-26T08:03:08", "Cycle ending Aug 19: actual $-21.73 vs app estimate $24.58 — off by $-46.31 (188%)."))
    billledger.seed_from_alert_log(tmp_path)
    row = billledger.load(tmp_path)[0]
    assert row["actual"] == -21.73 and row["exclude"] is True


def test_seed_never_overwrites_a_row_that_already_has_more_detail(tmp_path):
    billledger.upsert(tmp_path, _e("2026-10-16", 39.11, 37.49, due=39.11, source="reply"))
    _log(tmp_path, _rec("2026-10-23T08:03:15", "Cycle ending Oct 16: actual $39.11 vs app estimate $37.49 — off by +$1.62 (4%)."))
    billledger.seed_from_alert_log(tmp_path)
    rows = billledger.load(tmp_path)
    assert len(rows) == 1 and rows[0]["source"] == "reply" and rows[0]["due"] == 39.11


def test_seed_without_a_log_is_a_no_op(tmp_path):
    assert billledger.seed_from_alert_log(tmp_path) == 0 and billledger.load(tmp_path) == []


# ---------------------------------------------------------------- the table

def test_table_lists_each_cycle_with_its_miss():
    rows = THREE + [_e("2026-12-18", 60.0, None, due=38.07, credit_applied=21.93)]
    text = billledger.format_table(rows)
    assert "Nov 18" in text and "$53.63" in text and "$50.32" in text and "+$3.31" in text
    assert "Dec 18" in text and "$60.00" in text
    assert "due $38.07" in text.lower() or "$38.07" in text


def test_table_for_an_empty_ledger_says_so():
    assert "No bills" in billledger.format_table([])


def test_dates_in_the_table_are_real_dates():
    date.fromisoformat(THREE[0]["end"])
