"""The 4 PM-midnight load by weekday, and the battery level it asks for at sundown."""

import pathlib
import sys
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from franklinwh_scraper import evening

TODAY = date(2026, 11, 6)                      # a Friday


class _Store:
    """daily_hourly_load_means over {date_str: {hour: kW}}."""

    def __init__(self, days):
        self.days = days

    def daily_hourly_load_means(self, start, end):
        return {d: h for d, h in self.days.items() if start <= d <= end}


def _night(kwh_per_hour):
    return {h: kwh_per_hour for h in range(0, 24)}


def _fridays(levels):
    """One Friday per level (kW every hour), newest first, a week apart."""
    return {(TODAY - timedelta(weeks=i + 1)).isoformat(): _night(kw) for i, kw in enumerate(levels)}


def test_a_weekdays_numbers_come_from_its_own_evenings():
    prof = evening.evening_profiles(_Store(_fridays([0.5, 0.5, 0.5, 0.5, 1.0])), TODAY)[4]     # 4 / 4 / 4 / 4 / 8 kWh
    assert prof.name == "Friday" and prof.n == 5
    assert prof.median_kwh == pytest.approx(4.0)
    assert prof.max_kwh == pytest.approx(8.0) and prof.low_kwh == pytest.approx(4.0)
    assert prof.heavy_nights == 1                                                         # 8 kWh is over 7


def test_heavy_is_the_level_four_in_five_evenings_stay_under():
    prof = evening.evening_profiles(_Store(_fridays([0.5, 0.5, 0.5, 0.5, 1.0])), TODAY)[4]
    assert prof.heavy_kwh == pytest.approx(4.0)            # 4 of 5 are 4 kWh; only the last is 8
    prof = evening.evening_profiles(_Store(_fridays([0.5, 0.5, 0.75, 1.0, 1.25])), TODAY)[4]
    assert prof.heavy_kwh == pytest.approx(8.0)            # 4 / 4 / 6 / 8 / 10 -> index 3


def test_the_evening_is_split_into_three_parts():
    day = {h: (1.0 if h < 19 else 2.0 if h < 22 else 0.5) for h in range(24)}
    prof = evening.evening_profiles(_Store({(TODAY - timedelta(weeks=i + 1)).isoformat(): day for i in range(4)}), TODAY)[4]
    assert prof.segments == pytest.approx((3.0, 6.0, 1.0))


def test_a_weekday_needs_a_few_evenings_and_whole_ones():
    assert 4 not in evening.evening_profiles(_Store(_fridays([0.5] * 3)), TODAY)          # only three
    partial = _fridays([0.5] * 4)
    first = next(iter(partial))
    partial[first] = {h: 0.5 for h in range(0, 20)}                                       # outage: no 8 PM-midnight
    assert 4 not in evening.evening_profiles(_Store(partial), TODAY)


def test_today_and_old_days_are_left_out():
    days = _fridays([0.5] * 4)
    days[TODAY.isoformat()] = _night(5.0)                                                 # today, still happening
    days[(TODAY - timedelta(weeks=20)).isoformat()] = _night(5.0)                         # months ago
    prof = evening.evening_profiles(_Store(days), TODAY)[4]
    assert prof.n == 4 and prof.max_kwh == pytest.approx(4.0)


def test_the_battery_level_adds_the_floor_and_margin_to_the_load():
    assert evening.target_pct(4.08, 13.6, 10.0, 5.0) == pytest.approx(45.0)               # 4.08 / 13.6 = 30 points
    assert evening.target_pct(40.0, 13.6, 10.0, 5.0) == 100.0                             # never above full


def test_the_verdict_compares_the_forecast_with_both_targets():
    assert evening.verdict(86.0, typical_pct=43.0, heavy_pct=56.0) == ("heavy", 30.0)
    assert evening.verdict(50.0, typical_pct=43.0, heavy_pct=56.0) == ("typical", 7.0)
    assert evening.verdict(40.0, typical_pct=43.0, heavy_pct=56.0) == ("short", -3.0)
