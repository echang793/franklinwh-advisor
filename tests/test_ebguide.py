"""EB guidance: the cloudy-day gate, the learned margin, the start/stop timer rules."""

import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from franklinwh_scraper import ebguide


class _Store:
    """daily_solar_kwh / day_has_solar_coverage over a {date_str: kWh} table."""

    def __init__(self, days):
        self.days = days

    def daily_solar_kwh(self, d):
        return self.days.get(d, 0.0)

    def day_has_solar_coverage(self, d, end_hour=17):
        return d in self.days


class _Outlook:
    def __init__(self, cloudy, kwh):
        self._cloudy, self._kwh = cloudy, kwh

    def is_cloudy(self, hours, thr):
        return self._cloudy

    def today_generation_kwh(self, peak, pr, bias):
        return self._kwh


def _history(today, n=20, good=30.0, bad=8.0):
    """n days before `today`: alternating good and bad days."""
    from datetime import timedelta
    return {(today - timedelta(days=i)).isoformat(): (good if i % 2 else bad) for i in range(1, n + 1)}


TODAY = date(2026, 11, 1)


def _gate(outlook, store=None):
    return ebguide.eb_day_gate(outlook, store, TODAY, 300, peak_kw=4.5, perf_ratio=1.0, hourly_bias=None)


def test_baseline_is_a_good_day_not_the_average():
    base = ebguide.good_day_baseline_kwh(_Store(_history(TODAY)), TODAY)
    assert base == pytest.approx(30.0)          # the typical sunny day, not the 19 kWh mean


def test_baseline_needs_a_week_of_days():
    assert ebguide.good_day_baseline_kwh(_Store(_history(TODAY, n=5)), TODAY) is None
    assert ebguide.good_day_baseline_kwh(None, TODAY) is None


def test_gate_opens_on_a_cloudy_day_with_a_big_drop():
    g = _gate(_Outlook(cloudy=True, kwh=12.0), _Store(_history(TODAY)))
    assert g["known"] and g["needed"]
    assert g["ratio"] == pytest.approx(0.4)


def test_gate_stays_shut_on_a_cloudy_day_with_only_a_small_drop():
    g = _gate(_Outlook(cloudy=True, kwh=24.0), _Store(_history(TODAY)))      # 80% of a good day
    assert g["known"] and not g["needed"]


def test_gate_stays_shut_on_a_clear_day():
    g = _gate(_Outlook(cloudy=False, kwh=12.0), _Store(_history(TODAY)))
    assert g["known"] and not g["needed"]


def test_gate_falls_back_to_the_sky_alone_without_a_baseline():
    g = _gate(_Outlook(cloudy=True, kwh=12.0), _Store(_history(TODAY, n=3)))
    assert g["known"] and g["needed"] and g["ratio"] is None
    g = _gate(_Outlook(cloudy=False, kwh=12.0), None)
    assert g["known"] and not g["needed"]


def test_gate_is_unknown_without_a_forecast():
    g = _gate(None, _Store(_history(TODAY)))
    assert not g["known"] and not g["needed"]


def test_gate_survives_a_broken_forecast():
    class _Broken:
        def is_cloudy(self, h, t):
            raise RuntimeError("no data")
    g = _gate(_Broken(), _Store(_history(TODAY)))
    assert not g["known"]


def test_margin_stays_at_the_base_without_history():
    assert ebguide.plan_margin([]) == 5.0
    assert ebguide.plan_margin([{"planned_eb_kwh": 3.0, "ran_eb_kwh": 3.0, "grid_kwh": 2.0}] * 3) == 5.0


def _log(n_short, n_ok):
    short = {"planned_eb_kwh": 3.0, "ran_eb_kwh": 3.0, "grid_kwh": 1.5}
    ok = {"planned_eb_kwh": 3.0, "ran_eb_kwh": 3.0, "grid_kwh": 0.0}
    return [short] * n_short + [ok] * n_ok


def test_margin_grows_when_followed_plans_still_imported():
    assert ebguide.plan_margin(_log(3, 3)) == 8.0          # half fell short
    assert ebguide.plan_margin(_log(5, 1)) == 10.0         # most did


def test_margin_ignores_days_the_plan_was_not_followed():
    not_followed = {"planned_eb_kwh": 3.0, "ran_eb_kwh": 0.0, "grid_kwh": 4.0}
    assert ebguide.plan_margin([not_followed] * 8) == 5.0
    unplanned = {"planned_eb_kwh": 0.0, "ran_eb_kwh": 0.0, "grid_kwh": 4.0}
    assert ebguide.plan_margin([unplanned] * 8) == 5.0


def test_margin_stays_when_plans_work():
    assert ebguide.plan_margin(_log(0, 8)) == 5.0


def test_timer_says_start_in_the_five_minutes_before_start_by():
    assert ebguide.timer_decision(running=False, now_dec=12.92, start_by=13.0, eb_kwh=3.0) == "start"
    assert ebguide.timer_decision(running=False, now_dec=13.4, start_by=13.0, eb_kwh=3.0) == "start"   # late: still say so


def test_timer_waits_before_start_by():
    assert ebguide.timer_decision(running=False, now_dec=12.0, start_by=13.0, eb_kwh=3.0) is None


def test_timer_is_quiet_when_no_eb_is_needed_or_already_told():
    assert ebguide.timer_decision(running=False, now_dec=13.0, start_by=13.0, eb_kwh=0.0) is None
    assert ebguide.timer_decision(running=False, now_dec=13.0, start_by=13.0, eb_kwh=3.0, started=True) is None


def test_timer_is_quiet_after_the_deadline():
    assert ebguide.timer_decision(running=False, now_dec=14.2, start_by=13.0, eb_kwh=3.0) is None


def test_timer_says_stop_when_the_target_is_reached():
    assert ebguide.timer_decision(running=True, now_dec=13.2, start_by=13.0, eb_kwh=0.0) == "stop"


def test_timer_says_stop_at_the_deadline_even_if_short():
    assert ebguide.timer_decision(running=True, now_dec=14.0, start_by=13.0, eb_kwh=1.0) == "stop"


def test_timer_lets_a_running_eb_keep_running():
    assert ebguide.timer_decision(running=True, now_dec=13.2, start_by=13.0, eb_kwh=1.0) is None
    assert ebguide.timer_decision(running=True, now_dec=13.9, start_by=13.0, eb_kwh=0.0, stopped=True) is None


def test_heavy_day_needs_a_real_gap_and_real_load():
    assert ebguide.is_heavy(recent_kw=1.6, quiet_kw=0.8)
    assert not ebguide.is_heavy(recent_kw=1.0, quiet_kw=0.8)         # a little over: noise
    assert not ebguide.is_heavy(recent_kw=0.8, quiet_kw=0.3)         # 2.7x but under a kilowatt: standby noise
    assert not ebguide.is_heavy(recent_kw=None, quiet_kw=0.8)


def _day(ratio, grid, ran=0.0, cloudy=True):
    return {"gate_cloudy": cloudy, "gate_ratio": ratio, "grid_kwh": grid, "ran_eb_kwh": ran}


def _quiet_days(n, ratio=0.7):
    return [_day(ratio, 0.0) for _ in range(n)]


def test_gate_threshold_can_be_passed_in():
    g = ebguide.eb_day_gate(_Outlook(cloudy=True, kwh=19.8), _Store(_history(TODAY)), TODAY, 300,
                            peak_kw=4.5, ratio_threshold=0.70)      # 0.66 of a 30 kWh day
    assert g["needed"] and g["threshold"] == 0.70
    assert not _gate(_Outlook(cloudy=True, kwh=19.8), _Store(_history(TODAY)))["needed"]


def test_tuned_ratio_stays_at_the_default_without_history():
    assert ebguide.tuned_ratio([]) == ebguide.SIGNIFICANT_RATIO
    assert ebguide.tuned_ratio(_quiet_days(6) + [_day(0.6, 3.0)] * 3) == ebguide.SIGNIFICANT_RATIO   # 9 days, < 10


def test_tuned_ratio_needs_a_few_days_that_really_imported():
    log = _quiet_days(12) + [_day(0.7, 3.0)] * 2
    assert ebguide.tuned_ratio(log) == ebguide.SIGNIFICANT_RATIO


def test_tuned_ratio_rises_when_milder_days_still_imported_without_eb():
    log = _quiet_days(8, 0.8) + [_day(r, 2.5) for r in (0.5, 0.6, 0.7, 0.75)]
    assert ebguide.tuned_ratio(log) == pytest.approx(0.72)           # upper quartile of 0.5..0.75, +0.02


def test_tuned_ratio_ignores_days_eb_ran_and_clear_days():
    log = _quiet_days(10) + [_day(0.84, 4.0, ran=2.0)] * 4 + [_day(0.84, 4.0, cloudy=False)] * 4
    assert ebguide.tuned_ratio(log) == ebguide.SIGNIFICANT_RATIO


def test_tuned_ratio_is_clamped():
    assert ebguide.tuned_ratio(_quiet_days(10) + [_day(0.99, 5.0)] * 3) == 0.85
    assert ebguide.tuned_ratio(_quiet_days(10) + [_day(0.1, 5.0)] * 3) == 0.5
