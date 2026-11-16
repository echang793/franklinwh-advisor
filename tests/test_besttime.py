"""Best-time advisor: the energy-balance scorer, load sizes and command parsing. Pure, no I/O."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from franklinwh_scraper import besttime
from franklinwh_scraper.predictor import HourPrediction

NOW = datetime(2026, 11, 15, 10, 0)
CAP = 13.6


def _hour(dt, solar, load):
    return HourPrediction(dt=dt, predicted_load_kw=load, predicted_solar_kw=solar, net_kw=solar - load, confidence="high")


def _sunny_day(now=NOW):
    """10 AM to 7 AM next day: strong sun until 3 PM, a 1.5 kW evening, a 0.8 kW night."""
    out = []
    t = now.replace(minute=0)
    while t < (now + timedelta(days=1)).replace(hour=7, minute=0):
        h = t.hour
        solar = 5.0 if 10 <= h < 15 and t.date() == now.date() else (2.0 if h == 15 and t.date() == now.date() else 0.0)
        load = 0.5 if 10 <= h < 16 else (1.5 if h >= 16 else 0.8)
        out.append(_hour(t, solar, load))
        t += timedelta(hours=1)
    return out


def rate(t):
    if 16 <= t.hour < 21:
        return 0.60
    if t.hour < 6:
        return 0.10
    return 0.30


def export(t):
    return 0.05


def _score(hours=None, kw=4.0, dur=1.0, soc=50.0, reserve=10.0, now=NOW, sunset=datetime(2026, 11, 15, 16, 45)):
    return besttime.score_windows(hours or _sunny_day(now), now, soc, CAP, reserve, kw, dur, rate, export,
                                  sunset=sunset)


# ── the simulation ───────────────────────────────────────────────────────────────────────────────

def test_without_a_battery_every_deficit_is_bought_at_its_rate():
    hours = [_hour(NOW + timedelta(hours=i), 0.0, 1.0) for i in range(3)]
    r = besttime.simulate(hours, NOW, soc_kwh=0.0, cap=0.0, floor_kwh=0.0, extra=lambda t: 0.0,
                          rate_at=rate, export_rate_at=export, until=NOW + timedelta(hours=3))
    assert r.import_kwh == pytest.approx(3.0) and r.import_cost == pytest.approx(0.9)


def test_surplus_fills_the_battery_before_exporting():
    hours = [_hour(NOW + timedelta(hours=i), 3.0, 1.0) for i in range(2)]
    r = besttime.simulate(hours, NOW, soc_kwh=CAP - 1.0, cap=CAP, floor_kwh=1.36, extra=lambda t: 0.0,
                          rate_at=rate, export_rate_at=export, until=NOW + timedelta(hours=2))
    assert r.export_kwh == pytest.approx(3.0) and r.import_kwh == 0.0     # 4 kWh surplus, 1 kWh of room


def test_the_battery_stops_at_reserve():
    hours = [_hour(NOW + timedelta(hours=i), 0.0, 2.0) for i in range(2)]
    r = besttime.simulate(hours, NOW, soc_kwh=3.0, cap=CAP, floor_kwh=1.0, extra=lambda t: 0.0,
                          rate_at=rate, export_rate_at=export, until=NOW + timedelta(hours=2))
    assert r.import_kwh == pytest.approx(2.0)                            # 4 needed, 2 above reserve


def test_simulation_stops_where_the_forecast_ends():
    hours = [_hour(NOW, 0.0, 1.0)]
    r = besttime.simulate(hours, NOW, soc_kwh=0.0, cap=0.0, floor_kwh=0.0, extra=lambda t: 0.0,
                          rate_at=rate, export_rate_at=export, until=NOW + timedelta(hours=10))
    assert r.import_kwh == pytest.approx(1.0)


def test_a_partial_first_step_counts_only_what_is_left_of_it():
    now = NOW.replace(minute=45)
    hours = [_hour(NOW, 0.0, 2.0)]
    r = besttime.simulate(hours, now, soc_kwh=0.0, cap=0.0, floor_kwh=0.0, extra=lambda t: 0.0,
                          rate_at=rate, export_rate_at=export, until=NOW + timedelta(hours=1))
    assert r.import_kwh == pytest.approx(0.5)


# ── scoring windows ──────────────────────────────────────────────────────────────────────────────

def test_dryer_is_cheapest_in_the_sun_and_dearest_in_the_evening():
    res = _score()
    assert 10 <= res.best.start.hour < 14
    assert res.worst.start.hour >= 16
    assert res.best.cost < res.worst.cost
    assert res.best.cost <= 4.0 * 0.05 + 1e-9                          # at worst it costs the export it displaces
    assert res.worst.cost >= 4.0 * 0.10 - 1e-9                         # every kWh comes back from the grid later


def test_first_window_is_now_and_starts_step_every_half_hour_to_10_pm():
    res = _score(now=NOW.replace(minute=7))
    starts = [w.start for w in res.windows]
    assert starts[0] == NOW.replace(minute=7)
    assert starts[1] == NOW.replace(minute=30)
    assert all((b - a) == timedelta(minutes=30) for a, b in zip(starts[1:], starts[2:]))
    assert starts[-1] == NOW.replace(hour=22, minute=0)
    assert res.now is res.windows[0]


def test_ties_prefer_the_earlier_start():
    flat = [_hour(NOW + timedelta(hours=i), 0.0, 0.2) for i in range(24)]
    res = besttime.score_windows(flat, NOW, 90.0, 100.0, 10.0, 1.0, 1.0, lambda t: 0.3, export)
    assert res.best.cost == pytest.approx(0.0) and res.best.start == NOW


def test_sundown_delta_is_zero_after_sunset_and_never_positive():
    res = _score()
    for w in res.windows:
        assert w.sundown_delta is None or w.sundown_delta <= 1e-9
    late = [w for w in res.windows if w.start.hour >= 17]
    assert late and all(w.sundown_delta == pytest.approx(0.0) for w in late)
    mid = [w for w in res.windows if w.start.hour == 15]
    assert mid and mid[0].sundown_delta < 0                            # runs on the battery right before sunset


def test_no_sunset_means_no_sundown_delta():
    res = _score(sunset=None)
    assert all(w.sundown_delta is None for w in res.windows)


def test_after_10_pm_there_is_only_now():
    late = NOW.replace(hour=22, minute=40)
    res = _score(hours=_sunny_day(late), now=late)
    assert [w.start for w in res.windows] == [late]


def test_no_forecast_is_none():
    assert besttime.score_windows([], NOW, 50.0, CAP, 10.0, 4.0, 1.0, rate, export) is None


def test_costs_are_finite_and_rounded_to_the_cent():
    res = _score()
    for w in res.windows:
        assert w.cost == round(w.cost, 2) and w.cost == w.cost                  # not NaN


# ── load sizes ───────────────────────────────────────────────────────────────────────────────────

def test_known_loads_have_default_sizes():
    assert besttime.load_size("dryer") == (4.0, 1.0)
    assert besttime.load_size("Oven") == (2.5, 2.0)
    assert besttime.load_size("spaceship") is None


def test_three_or_more_answered_events_override_the_default():
    events = [{"key": f"2026-11-0{i}T14:00", "name": "dryer", "kw": kw, "minutes": m}
              for i, (kw, m) in enumerate([(5.0, 50), (5.4, 70), (4.8, 60)], start=1)]
    assert besttime.load_size("dryer", events) == (5.0, 1.0)            # median kW, median minutes -> hours
    assert besttime.load_size("dryer", events[:2]) == (4.0, 1.0)        # two answers are not enough yet


def test_car_size_comes_from_the_config():
    from franklinwh_scraper.config import Config
    assert besttime.load_size("car", cfg=Config(ev_charging_kw=7.6, ev_kwh_per_session=10.0)) == (7.6, round(10 / 7.6, 2))
    assert besttime.load_size("car", cfg=Config(ev_charging_kw=7.6, ev_kwh_per_session=0.0)) == (7.6, 2.0)


@pytest.mark.parametrize("text,expected", [
    ("dryer", ("dryer", None, None)),
    ("the oven", ("oven", None, None)),
    ("3.5 kw 2h", (None, 3.5, 2.0)),
    ("3kW for 90 min", (None, 3.0, 1.5)),
    ("dryer 2h", ("dryer", None, 2.0)),
    ("ac for 3 hours", ("ac", None, 3.0)),
    ("", None),
    ("something", None),
])
def test_parse_request(text, expected):
    assert besttime.parse_request(text) == expected


# ── the reply ────────────────────────────────────────────────────────────────────────────────────

def test_reply_names_the_best_and_worst_time_with_money():
    res = _score()
    text = besttime.describe(res, "dryer", 4.0, 1.0)
    assert "Best" in text and "Worst" in text and "$" in text
    assert res.best.start.strftime("%-I:%M") in text


def test_reply_says_when_it_hardly_matters():
    flat = [_hour(NOW + timedelta(hours=i), 0.0, 0.2) for i in range(24)]
    res = besttime.score_windows(flat, NOW, 90.0, 100.0, 10.0, 1.0, 1.0, lambda t: 0.3, export)
    assert "any time" in besttime.describe(res, "dishwasher", 1.0, 1.0).lower()


# ── chat bot: /when, its button, and the tool ────────────────────────────────────────────────────

def _when_bot(tmp_path, monkeypatch, now=datetime(2026, 11, 15, 10, 0), soc=50.0, state=None):
    import types

    from franklinwh_scraper import alerts
    from franklinwh_scraper.chatbot import TelegramChatBot
    from franklinwh_scraper.config import Config
    from franklinwh_scraper.predictor import UsageForecast
    from tests.test_unit import _freeze_chatbot_clock, _tracker_c
    _freeze_chatbot_clock(monkeypatch, now)
    monkeypatch.setattr(besttime, "_now", lambda: now)
    if state:
        alerts._save_peak_state(tmp_path, state)
    hours = _sunny_day(now)
    fc = UsageForecast(hours=hours, total_load_kwh=0, total_solar_kwh=0, net_kwh=0, peak_load_kw=0,
                       confidence="high", data_days=30)
    bot = TelegramChatBot(Config(battery_capacity_kwh=CAP, output_dir=str(tmp_path)), api_key="x")
    bot._outdir = tmp_path
    bot._stats = types.SimpleNamespace(current=_tracker_c(soc))
    bot._usage_forecast = fc
    bot._hist_store = None
    sent = []
    bot._send = lambda chat_id, text, reply_markup=None: sent.append((text, reply_markup))
    return bot, sent


def test_when_command_answers_with_best_and_worst(tmp_path, monkeypatch):
    bot, sent = _when_bot(tmp_path, monkeypatch)
    bot._send_when("1", besttime.parse_request("dryer"))
    text, markup = sent[-1]
    assert "When to run the dryer" in text and "Best" in text and "Worst" in text
    btn = markup["inline_keyboard"][0][0]
    assert btn["callback_data"].startswith("when:dryer:") and len(btn["callback_data"]) <= 64


def test_when_without_a_load_explains_itself(tmp_path, monkeypatch):
    bot, sent = _when_bot(tmp_path, monkeypatch)
    bot._send_when("1", None)
    assert "/when dryer" in sent[-1][0]


def test_when_with_only_kw_defaults_to_an_hour(tmp_path, monkeypatch):
    bot, sent = _when_bot(tmp_path, monkeypatch)
    bot._send_when("1", besttime.parse_request("3 kw"))
    assert "3 kW for 1 h" in sent[-1][0]


def test_when_button_adds_the_load_to_todays_plan(tmp_path, monkeypatch):
    from franklinwh_scraper import alerts, planned
    bot, sent = _when_bot(tmp_path, monkeypatch)
    bot._tap_when("1", "dryer:11.5:1:4")
    loads = planned.get_plan(alerts._load_peak_state(tmp_path), "2026-11-15")
    assert loads == [{"name": "dryer", "start": 11.5, "hours": 1.0, "kw": 4.0}]


def test_when_button_rejects_garbage(tmp_path, monkeypatch):
    from franklinwh_scraper import alerts, planned
    bot, sent = _when_bot(tmp_path, monkeypatch)
    for bad in ("dryer:99:1:4", "dryer:x:1:4", "<b>:10:1:4", "dryer:10:0:4", "dryer:10:1:50"):
        bot._tap_when("1", bad)
    assert planned.get_plan(alerts._load_peak_state(tmp_path), "2026-11-15") == []


def test_planned_loads_are_part_of_the_baseline(tmp_path, monkeypatch):
    """With an oven already planned 5-7 PM, an evening dryer costs more than without it."""
    bot, sent = _when_bot(tmp_path, monkeypatch)
    plain = besttime.for_snapshot(bot._stats.current, {}, bot._cfg, None, bot._usage_forecast, None,
                                  datetime(2026, 11, 15, 10, 0), 4.0, 1.0)
    planned_state = {"planned_loads_2026-11-15": [{"name": "oven", "start": 17.0, "hours": 2.0, "kw": 2.5}]}
    with_oven = besttime.for_snapshot(bot._stats.current, planned_state, bot._cfg, None, bot._usage_forecast, None,
                                      datetime(2026, 11, 15, 10, 0), 4.0, 1.0)
    pick = lambda r: next(w for w in r.windows if w.start.hour == 20 and w.start.minute == 0)  # noqa: E731
    assert pick(with_oven).cost >= pick(plain).cost


def test_best_time_is_a_chat_tool(tmp_path, monkeypatch):
    from franklinwh_scraper.chat_tools import APP_TOOL_NAMES
    assert "best_time" in APP_TOOL_NAMES
    bot, sent = _when_bot(tmp_path, monkeypatch)
    del bot._send                                                    # the real _send captures tool output
    out = bot._run_app_tool("1", "best_time", {"load": "oven"})
    assert "When to run the oven" in out["text"] and "<b>" not in out["text"]


def test_when_is_dispatched_from_text():
    from franklinwh_scraper.chatbot import _parse_when
    assert _parse_when("/when dryer") == ("dryer", None, None)
    assert _parse_when("/when@franklinwh_advisor_bot 3kw 2h") == (None, 3.0, 2.0)
    assert _parse_when("/when") == "help"
    assert _parse_when("when is sunset") is None


# ── dashboard: /api/besttime and the Plan panel ──────────────────────────────────────────────────

def _api(tmp_path, monkeypatch, now=datetime(2026, 11, 15, 10, 0), soc=50.0):
    import types

    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi
    from franklinwh_scraper.config import Config
    from franklinwh_scraper.predictor import UsageForecast
    fc = UsageForecast(hours=_sunny_day(now), total_load_kwh=0, total_solar_kwh=0, net_kwh=0, peak_load_kw=0,
                       confidence="high", data_days=30)
    c = types.SimpleNamespace(battery_soc_pct=soc, home_load_kw=0.5, solar_production_kw=5.0,
                              battery_use_kw=-4.0, grid_use_kw=0.0)
    monkeypatch.setattr(webapi, "_OUT", tmp_path)
    monkeypatch.setattr(webapi, "_cfg", Config(dashboard_token="", battery_capacity_kwh=CAP))
    monkeypatch.setattr(webapi, "_besttime_inputs", lambda: (c, {}, None, fc, None, now))
    return TestClient(webapi.app)


def test_api_besttime_scores_every_preset(tmp_path, monkeypatch):
    body = _api(tmp_path, monkeypatch).get("/api/besttime").json()
    assert body["ok"] and {"dryer", "oven"} <= set(body["loads"])
    d = body["loads"]["dryer"]
    assert d["kw"] == 4.0 and d["hours"] == 1.0
    assert 10 <= datetime.fromisoformat(d["best"]["start"]).hour < 14
    assert d["best"]["cost"] <= d["worst"]["cost"] and 0 <= d["best"]["start_h"] < 24


def test_api_besttime_takes_a_custom_size(tmp_path, monkeypatch):
    body = _api(tmp_path, monkeypatch).get("/api/besttime?kw=2&hours=3").json()
    assert list(body["loads"]) == ["load"] and body["loads"]["load"]["kw"] == 2.0


def test_api_besttime_rejects_silly_sizes(tmp_path, monkeypatch):
    c = _api(tmp_path, monkeypatch)
    assert c.get("/api/besttime?kw=99&hours=1").status_code == 422
    assert c.get("/api/besttime?kw=2&hours=0").status_code == 422


def test_api_besttime_without_a_forecast_says_so(tmp_path, monkeypatch):
    from franklinwh_scraper import webapi
    c = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(webapi, "_besttime_inputs", lambda: None)
    assert c.get("/api/besttime").json() == {"ok": False, "reason": "no_forecast", "loads": {}}


def test_plan_panel_uses_the_best_time():
    from pathlib import Path
    html = (Path(__file__).parent.parent / "franklinwh_scraper" / "static" / "index.html").read_text()
    assert "'/api/besttime'" in html and "S.best" in html
    render = html[html.index("function renderPlan"):]
    render = render[:render.index("\n}\n")]
    assert "S.best" in render and "start_h" in render


@pytest.mark.parametrize("hours,text", [(1.0, "1 h"), (1.5, "1 h 30 min"), (1.32, "1 h 19 min"), (0.5, "30 min"), (3.0, "3 h")])
def test_durations_read_like_a_clock(hours, text):
    assert besttime.fmt_duration(hours) == text


# ── missed-savings replay ────────────────────────────────────────────────────────────────────────

DAY = datetime(2026, 11, 12)


def _day_readings(block_start=18.0, block_kw=4.0, block_minutes=60, solar_kw=5.0, every_min=5, gap=None):
    """(timestamp, grid, load, solar) every 5 min from midnight to 7 AM the next day."""
    out = []
    t = DAY
    end = DAY + timedelta(days=1, hours=7)
    while t < end:
        h = t.hour + t.minute / 60.0 if t.date() == DAY.date() else 99
        solar = solar_kw if 10 <= h < 15 else 0.0
        base = 0.8 if (h < 7 or h == 99) else (1.5 if h >= 16 else 0.5)      # night, busy evening, quiet day
        load = base + (block_kw if block_start is not None and block_start <= h < block_start + block_minutes / 60 else 0.0)
        if gap is None or not (gap[0] <= h < gap[1]):
            out.append((t.isoformat(), max(0.0, load - solar), load, solar))
        t += timedelta(minutes=every_min)
    return out


def _replay(readings, **kw):
    args = dict(soc_start_pct=50.0, cap=CAP, reserve_pct=10.0, rate_at=rate, export_rate_at=export)
    args.update(kw)
    return besttime.replay_day(readings, DAY.date(), **args)


def test_an_evening_dryer_is_found_with_a_cheaper_time():
    missed = _replay(_day_readings())
    assert len(missed) == 1
    m = missed[0]
    assert m.start.hour == 18 and 10 <= m.better_start.hour < 14
    assert m.saving >= 0.25 and m.saving == pytest.approx(m.actual_cost - m.better_cost, abs=0.011)
    assert 3.5 <= m.kw <= 4.5 and 55 <= m.minutes <= 65


def test_a_load_already_in_the_sun_is_not_reported():
    assert _replay(_day_readings(block_start=11.0)) == []


def test_a_day_with_no_big_load_is_empty():
    assert _replay(_day_readings(block_start=None)) == []


def test_a_day_with_holes_is_skipped():
    assert _replay(_day_readings(gap=(9, 15))) == []


def test_the_reply_lists_the_biggest_savings_first():
    a = besttime.Missed(start=DAY.replace(hour=18), minutes=60, kw=4.0, actual_cost=1.2, better_start=DAY.replace(hour=11),
                        better_cost=0.1, name="dryer")
    b = besttime.Missed(start=DAY.replace(hour=19), minutes=90, kw=2.5, actual_cost=0.9, better_start=DAY.replace(hour=12),
                        better_cost=0.2, name="oven")
    text = besttime.describe_missed([b, a])
    assert "Could have saved" in text and text.index("dryer") < text.index("oven")
    assert "$1.80" in text                                            # 1.10 + 0.70


def test_the_reply_is_silent_when_the_total_is_small():
    a = besttime.Missed(start=DAY.replace(hour=18), minutes=60, kw=4.0, actual_cost=0.6, better_start=DAY.replace(hour=11),
                        better_cost=0.3, name="dryer")
    assert besttime.describe_missed([a]) is None


def test_missed_week_skips_car_days_and_names_answered_loads(tmp_path):
    class Store:
        def readings_between(self, a, b):
            d0 = datetime.fromisoformat(a)
            return [r for r in _day_readings() if a <= r[0] < b] if d0.date() == DAY.date() else []

        def soc_near(self, ts):
            return 50.0

    state = {"appliance_events": [{"key": "2026-11-12T18:00", "name": "dryer", "kw": 4.1, "minutes": 60}]}
    cfg = type("C", (), {"battery_capacity_kwh": CAP, "battery_reserve_pct": 10.0})()
    got = besttime.missed_week(Store(), DAY.date() + timedelta(days=1), state, cfg, rate_at=rate, export_rate_at=export)
    assert len(got) == 1 and got[0].name == "dryer"
    state["car_day_labels"] = {"2026-11-12": True}
    assert besttime.missed_week(Store(), DAY.date() + timedelta(days=1), state, cfg, rate_at=rate, export_rate_at=export) == []


def test_weekly_summary_carries_the_missed_savings(monkeypatch):
    from franklinwh_scraper import alerts
    a = besttime.Missed(start=DAY.replace(hour=18), minutes=60, kw=4.0, actual_cost=1.2, better_start=DAY.replace(hour=11),
                        better_cost=0.1, name="dryer")
    monkeypatch.setattr(besttime, "missed_week", lambda *a_, **k: [a])
    assert "Could have saved" in alerts._missed_block(object(), datetime(2026, 11, 15, 21, 0), {}, None)
    monkeypatch.setattr(besttime, "missed_week", lambda *a_, **k: [])
    assert alerts._missed_block(object(), datetime(2026, 11, 15, 21, 0), {}, None) == ""
    monkeypatch.setattr(besttime, "missed_week", lambda *a_, **k: 1 / 0)
    assert alerts._missed_block(object(), datetime(2026, 11, 15, 21, 0), {}, None) == ""   # never breaks the summary
    assert "_missed_block" in alerts._alert_weekly_summary.__code__.co_names


def test_missed_command_is_wired():
    from franklinwh_scraper.chatbot import TelegramChatBot
    assert hasattr(TelegramChatBot, "_send_missed")
