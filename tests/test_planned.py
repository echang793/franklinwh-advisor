"""planned.py: the loads you tell the advisor about for today (/plan)."""

from franklinwh_scraper import planned

DRYER = {"name": "dryer", "start": 16.0, "hours": 1.0, "kw": 4.0}
OVEN = {"name": "oven", "start": 17.0, "hours": 2.0, "kw": 2.5}


def test_set_plan_stores_by_day_and_replaces_the_same_appliance():
    st: dict = {}
    planned.set_plan(st, "2026-11-08", [DRYER, OVEN])
    planned.set_plan(st, "2026-11-08", [dict(DRYER, start=15.0)])
    names = {ld["name"]: ld for ld in planned.get_plan(st, "2026-11-08")}
    assert set(names) == {"dryer", "oven"} and names["dryer"]["start"] == 15.0
    assert planned.get_plan(st, "2026-11-09") == []


def test_clear_plan_removes_it():
    st: dict = {}
    planned.set_plan(st, "2026-11-08", [DRYER])
    planned.clear_plan(st, "2026-11-08")
    assert planned.get_plan(st, "2026-11-08") == []


def test_get_plan_ignores_garbage():
    assert planned.get_plan({"planned_loads_2026-11-08": "oops"}, "2026-11-08") == []
    assert planned.get_plan({"planned_loads_2026-11-08": [{"name": "x"}]}, "2026-11-08") == []


def test_evening_kwh_counts_only_what_runs_after_the_cutoff():
    assert planned.evening_kwh([DRYER, OVEN]) == 4.0 * 1.0 + 2.5 * 2.0
    early = {"name": "dryer", "start": 14.0, "hours": 3.0, "kw": 4.0}                    # 2-5 PM: one hour after 4
    assert planned.evening_kwh([early]) == 4.0
    assert planned.evening_kwh([]) == 0.0


def test_describe_lists_each_load_with_its_time_and_power():
    text = planned.describe([DRYER, OVEN])
    assert "dryer 4–5 PM (4 kW)" in text and "oven 5–7 PM (2.5 kW)" in text
