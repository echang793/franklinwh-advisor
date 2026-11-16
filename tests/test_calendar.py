"""Year heatmap: /api/calendar per-day values and the panel."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from franklinwh_scraper.history import HistoryStore

TODAY = date(2026, 11, 15)


def _seed(path: Path, days: dict[date, tuple[float, float, float]]):
    """Hourly readings for each day; cumulative path counters end at (battery, solar, grid) kWh."""
    with HistoryStore(path) as db:
        for d, (b, s, g) in days.items():
            for h in range(24):
                f = (h + 1) / 24
                t = datetime(d.year, d.month, d.day, h, 30)
                db._conn.execute(
                    "INSERT INTO readings (timestamp, day_of_week, hour_of_day, home_load_kw, solar_kw, battery_soc,"
                    " grid_use_kw, grid_status, solar_total_kwh, battery_use_kw, battery_load_kwh, solar_load_kwh,"
                    " grid_load_kwh) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t.isoformat(), t.weekday(), h, (b + s + g) / 24, 3.0 if 10 <= h < 15 else 0.0, 50.0,
                     g / 24, "normal", s * f, 0.2, b * f, s * f, g * f))
        db._conn.commit()


def _client(tmp_path, monkeypatch, days):
    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi
    from franklinwh_scraper.config import Config
    _seed(tmp_path / "history.db", days)
    monkeypatch.setattr(webapi, "_OUT", tmp_path)
    monkeypatch.setattr(webapi, "_cfg", Config(dashboard_token=""))
    monkeypatch.setattr(webapi, "_calendar_today", lambda: TODAY)
    webapi._CAL_CACHE.clear()
    return TestClient(webapi.app)


def test_self_sufficiency_per_day_in_order(tmp_path, monkeypatch):
    days = {TODAY - timedelta(days=2): (6.0, 3.0, 1.0), TODAY - timedelta(days=1): (4.0, 4.0, 2.0)}
    body = _client(tmp_path, monkeypatch, days).get("/api/calendar?metric=self&days=30").json()
    assert body["metric"] == "self" and [d["date"] for d in body["days"]] == [
        (TODAY - timedelta(days=2)).isoformat(), (TODAY - timedelta(days=1)).isoformat()]
    assert body["days"][0]["value"] == 90.0 and body["days"][1]["value"] == 80.0
    assert body["days"][0]["detail"] == {"battery_kwh": 6.0, "solar_kwh": 3.0, "grid_kwh": 1.0}


def test_saved_per_day_is_finite_money(tmp_path, monkeypatch):
    days = {TODAY - timedelta(days=1): (4.0, 4.0, 2.0)}
    body = _client(tmp_path, monkeypatch, days).get("/api/calendar?metric=saved&days=30").json()
    assert len(body["days"]) == 1
    v = body["days"][0]["value"]
    assert isinstance(v, float) and math.isfinite(v)


def test_days_without_data_are_left_out_and_never_nan(tmp_path, monkeypatch):
    days = {TODAY - timedelta(days=3): (0.0, 0.0, 0.0), TODAY - timedelta(days=1): (1.0, 1.0, 2.0)}
    body = _client(tmp_path, monkeypatch, days).get("/api/calendar?metric=self&days=30").json()
    assert [d["date"] for d in body["days"]] == [(TODAY - timedelta(days=1)).isoformat()]


def test_past_days_are_cached_and_today_is_not(tmp_path, monkeypatch):
    from franklinwh_scraper import webapi
    days = {TODAY - timedelta(days=1): (4.0, 4.0, 2.0), TODAY: (1.0, 1.0, 1.0)}
    c = _client(tmp_path, monkeypatch, days)
    c.get("/api/calendar?metric=self&days=7")
    assert ("self", (TODAY - timedelta(days=1)).isoformat()) in webapi._CAL_CACHE
    assert ("self", TODAY.isoformat()) not in webapi._CAL_CACHE


@pytest.mark.parametrize("q", ["metric=bogus", "days=0", "days=401"])
def test_bad_queries_are_refused(tmp_path, monkeypatch, q):
    assert _client(tmp_path, monkeypatch, {}).get("/api/calendar?" + q).status_code == 422


def test_calendar_panel_is_on_the_page():
    html = (Path(__file__).parent.parent / "franklinwh_scraper" / "static" / "index.html").read_text()
    assert "'calendar'" in html[html.index("PANEL_IDS = ["):][:400] and "calendar:'YEAR'" in html
    assert "function renderCalendar" in html and "/api/calendar?metric=" in html
    assert 'id="cal-info"' in html
