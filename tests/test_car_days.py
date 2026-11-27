"""Sundown accuracy graded separately for car days and quiet days.

The projection assumes no car charging, so its miss on a car day is the car, not forecast error.
Mixed together they widened the "likely range" on quiet days (12:30: 23 points wide, 16 without).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from franklinwh_scraper import alerts


class _Store:
    """readings_between -> (ts, grid_kw, home_kw, solar_kw), one row each 5 minutes."""

    def __init__(self, blocks=(), base_kw=0.4, day="2026-11-24"):
        self.rows = []
        t = datetime.fromisoformat(f"{day}T08:00")
        while t < datetime.fromisoformat(f"{day}T18:00"):
            kw = base_kw
            for start, minutes, load in blocks:
                s = datetime.fromisoformat(f"{day}T{start}")
                if s <= t < s + timedelta(minutes=minutes):
                    kw = load
            self.rows.append((t.isoformat(), 0.0, kw, 1.0))
            t += timedelta(minutes=5)

    def readings_between(self, start, end):
        return [r for r in self.rows if start <= r[0] < end]


def _car(store, labels=None, start="12:30", end="16:45"):
    return alerts._car_charged_between(store, f"2026-11-24T{start}", f"2026-11-24T{end}", labels or {})


def test_a_steady_afternoon_block_counts_as_the_car():
    assert _car(_Store([("14:00", 120, 3.2)])) is True


def test_a_quiet_afternoon_and_a_short_spike_do_not():
    assert _car(_Store()) is False
    assert _car(_Store([("14:00", 15, 4.0)])) is False


def test_a_charge_that_finished_before_the_snapshot_does_not_count():
    assert _car(_Store([("09:00", 90, 3.2)])) is False


def test_your_answer_beats_the_detector():
    assert _car(_Store([("14:00", 120, 3.2)]), {"2026-11-24": False}) is False      # "that was the dryer"
    assert _car(_Store([("14:00", 20, 3.0)]), {"2026-11-24": True}) is True         # a short top-off you named
    assert _car(_Store(), {"2026-11-24": True}) is False                            # charged outside this window


def test_a_store_that_cannot_answer_reads_as_no_car():
    class Broken:
        def readings_between(self, *_a):
            raise RuntimeError("db gone")
    assert alerts._car_charged_between(Broken(), "2026-11-24T12:30", "2026-11-24T16:45", {}) is False


def _row(day, slot, err, **kw):
    return {"day": day, "slot": slot, "err": err, "grid": False, **kw}


def test_grading_records_whether_the_car_charged():
    state = {"sundown_shadow_2026-11-24": {"12:30": {"pct": 80.0, "dt": "2026-11-24T16:45:00", "soc0": 50.0,
                                                     "at": "2026-11-24T12:30:00"}}}
    store = _Store([("14:00", 120, 3.2)])
    store.soc_near = lambda *_a: 55.0
    store.grid_charged_between = lambda *_a: False
    alerts._grade_sundown_snapshots(state, datetime(2026, 11, 24, 21, 0), store)
    row = state["sundown_resid"][0]
    assert row["err"] == -25.0 and row["car"] is True


def test_rows_graded_before_this_get_their_car_flag_filled_in_once():
    state = {"sundown_resid": [_row("2026-11-24", "12:30", -20.0), _row("2026-11-24", "15:30", -1.0),
                               _row("2026-11-24", "09:00", 3.0, car=False)]}
    alerts._backfill_sundown_car(state, _Store([("14:00", 60, 3.2)]))
    flags = {r["slot"]: r["car"] for r in state["sundown_resid"]}
    assert flags == {"12:30": True, "15:30": False, "09:00": False}          # an existing flag is left alone


def test_the_likely_range_is_built_from_quiet_days_only():
    quiet = [_row(f"2026-11-{d:02d}", "12:30", e, car=False) for d, e in zip(range(1, 13), [-2, -1, 0, 1, 2, -3, 3, 0, 1, -1, 2, -2])]
    cars = [_row(f"2026-11-{d:02d}", "12:30", -30.0, car=True) for d in range(13, 19)]
    lo, hi, n = alerts._sundown_band({"sundown_resid": quiet + cars}, datetime(2026, 11, 24, 12, 45))
    assert n == len(quiet) and lo > -6 and hi < 6


def test_drift_watchdog_ignores_car_days():
    cars = [_row(f"2026-11-{d:02d}", "14:00", -30.0, car=True) for d in range(14, 24)]
    assert alerts._alert_sundown_drift({"sundown_resid": cars}, "2026-11-24", datetime(2026, 11, 24, 9, 0)) is None


def test_accuracy_report_splits_quiet_days_from_car_days():
    state = {"sundown_resid": [_row("2026-11-20", "12:30", -2.0, car=False), _row("2026-11-21", "12:30", 4.0, car=False),
                               _row("2026-11-22", "12:30", -24.0, car=True)]}
    text = alerts._accuracy_report(state, datetime(2026, 11, 24, 12, 0))
    assert "quiet days" in text.lower() and "car days" in text.lower()
    quiet_block = text.lower().split("car days")[0]
    assert "miss  3.0" in quiet_block and "24.0" not in quiet_block
