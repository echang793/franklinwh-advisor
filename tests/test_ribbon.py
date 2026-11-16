"""Day ribbon: one midnight-to-midnight view of rates, sun, battery and plans."""

from __future__ import annotations

import math
import types
from datetime import datetime, timedelta
from pathlib import Path

from franklinwh_scraper import besttime
from tests.test_besttime import CAP, _sunny_day

NOW = datetime(2026, 11, 15, 10, 0)


def test_simulate_can_trace_the_battery():
    trace = []
    besttime.simulate(_sunny_day(NOW), NOW, 6.8, CAP, 1.36, lambda t: 0.0, lambda t: 0.3, lambda t: 0.05,
                      NOW.replace(hour=23, minute=59), trace=trace)
    assert trace[0] == (NOW, 6.8)
    assert all(b[0] > a[0] for a, b in zip(trace, trace[1:]))
    assert max(s for _t, s in trace) <= CAP and min(s for _t, s in trace) >= 1.36 - 1e-9


def test_projection_is_bent_to_land_on_the_sundown_number():
    raw = [(NOW + timedelta(minutes=30 * i), 50.0 + i) for i in range(30)]          # 50% now, rising 1 pt / step
    sundown = NOW + timedelta(hours=6)                                                # raw there: 62%
    out = besttime.pin_to(raw, NOW, sundown, 70.0)
    at = dict(out)
    assert at[NOW] == 50.0                                                            # now is still now
    assert math.isclose(at[sundown], 70.0)                                            # lands on the headline
    assert math.isclose(at[NOW + timedelta(hours=3)], 56 + 4)                         # halfway: raw 56%, half the 8-pt lift
    assert math.isclose(at[sundown + timedelta(hours=1)], 64 + 8)                     # after sundown the lift stays
    assert all(0.0 <= v <= 100.0 for _t, v in besttime.pin_to(raw, NOW, sundown, 140.0))


def _api(tmp_path, monkeypatch, now=NOW):
    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi
    from franklinwh_scraper.config import Config
    from franklinwh_scraper.predictor import UsageForecast
    fc = UsageForecast(hours=_sunny_day(now), total_load_kwh=0, total_solar_kwh=0, net_kwh=0, peak_load_kw=0,
                       confidence="high", data_days=30)
    c = types.SimpleNamespace(battery_soc_pct=50.0, home_load_kw=0.5, solar_production_kw=5.0,
                              battery_use_kw=-4.0, grid_use_kw=0.0)
    monkeypatch.setattr(webapi, "_OUT", tmp_path)
    monkeypatch.setattr(webapi, "_cfg", Config(dashboard_token="", battery_capacity_kwh=CAP))
    monkeypatch.setattr(webapi, "_besttime_inputs", lambda: (c, {}, None, fc, None, now))
    monkeypatch.setattr(webapi, "_sundown_now", lambda: now)
    monkeypatch.setattr(webapi, "_ribbon_sundown", lambda *a: {"ok": True, "sundown_at": now.replace(hour=16, minute=45).isoformat(),
                                                               "projected_pct": 88.0, "band": {"lo": 80.0, "hi": 95.0, "n": 9},
                                                               "plan": [{"name": "oven", "start": 17.0, "hours": 2.0, "kw": 2.5}]})
    monkeypatch.setattr(webapi, "_ribbon_actual", lambda day, now_: [{"ts": day.replace(hour=h).isoformat(), "soc": 40.0 + h,
                                                                      "solar": 0.0, "load": 0.5} for h in range(10)])
    return TestClient(webapi.app)


def test_api_ribbon_has_everything_the_panel_draws(tmp_path, monkeypatch):
    body = _api(tmp_path, monkeypatch).get("/api/ribbon").json()
    assert body["ok"] and body["day"] == "2026-11-15"
    assert len(body["tou"]) == 24 and {"hour", "period"} <= set(body["tou"][0])
    assert body["actual"][0]["soc"] == 40.0
    proj = body["projection"]
    assert proj[0]["ts"] == NOW.isoformat() and datetime.fromisoformat(proj[-1]["ts"]) <= NOW.replace(hour=23, minute=59)
    at_sundown = [p for p in proj if p["ts"] == NOW.replace(hour=16, minute=45).isoformat()]
    assert at_sundown and abs(at_sundown[0]["soc"] - 88.0) < 0.01
    assert body["sunset"].startswith("2026-11-15T16:45") and body["band"] == {"lo": 80.0, "hi": 95.0}
    assert body["plan"][0]["name"] == "oven"
    for p in proj:
        assert all(isinstance(p[k], (int, float)) and math.isfinite(p[k]) for k in ("soc", "solar", "load"))


def test_api_ribbon_without_inputs_still_shows_the_day_so_far(tmp_path, monkeypatch):
    from franklinwh_scraper import webapi
    c = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(webapi, "_besttime_inputs", lambda: None)
    body = c.get("/api/ribbon").json()
    assert body["ok"] and body["projection"] == [] and len(body["actual"]) == 10


def test_ribbon_panel_is_on_the_page_first():
    html = (Path(__file__).parent.parent / "franklinwh_scraper" / "static" / "index.html").read_text()
    assert "PANEL_IDS = ['ribbon'," in html and "ribbon:'DAY RIBBON'" in html
    assert "function renderRibbon" in html and "'/api/ribbon'" in html
    assert 'id="rb-info"' in html                                     # scrub readout on its own fixed line
