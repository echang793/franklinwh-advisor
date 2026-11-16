"""Weather cache shared through disk, negative caching, and the NWS fallback. Network always faked."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from franklinwh_scraper import weather
from franklinwh_scraper.weather import HourlyForecast, SolarOutlook

LAT, LON = 32.97, -117.08
TZ = "America/Los_Angeles"


def _outlook(ghi=500.0, n=24, start=datetime(2026, 11, 15, 0)):
    from datetime import timedelta
    return SolarOutlook(
        hours=[HourlyForecast(time=start + timedelta(hours=i), direct_radiation_wm2=ghi * 0.8,
                              diffuse_radiation_wm2=ghi * 0.2, cloud_cover_pct=10.0, temp_c=20.0)
               for i in range(n)],
        utc_offset_seconds=-28800,
        sunrise_times=[datetime(2026, 11, 15, 6, 20)], sunset_times=[datetime(2026, 11, 15, 16, 46)],
    )


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Clean cache, fake clock, counting fakes for both sources."""
    weather._outlook_cache.clear()
    clock = Clock()
    monkeypatch.setattr(weather, "_clock", clock)
    calls = {"om": 0, "nws": 0}
    nws = _outlook(ghi=300.0)
    nws.source = "nws"                                                # what the real fetch_nws_outlook returns
    behaviour = {"om": _outlook(), "nws": nws}

    def fake_om(lat, lon, timeout=10, retries=3):
        calls["om"] += 1
        calls["om_args"] = (timeout, retries)
        r = behaviour["om"]
        if isinstance(r, Exception):
            raise r
        return r

    def fake_nws(lat, lon, timeout=10):
        calls["nws"] += 1
        r = behaviour["nws"]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(weather, "fetch_solar_outlook", fake_om)
    monkeypatch.setattr(weather, "fetch_nws_outlook", fake_nws)
    weather.set_cache_dir(tmp_path)
    weather.set_fetch_policy(timeout=10, retries=3)
    yield clock, calls, behaviour, tmp_path
    weather.set_cache_dir(None)
    weather.set_fetch_policy(timeout=10, retries=3)
    weather._outlook_cache.clear()


# ── round trip ───────────────────────────────────────────────────────────────────────────────────

def test_outlook_survives_a_json_round_trip():
    o = _outlook()
    o.source = "nws"
    back = weather.outlook_from_dict(json.loads(json.dumps(weather.outlook_to_dict(o))))
    assert back == o


def test_round_trip_never_writes_nan():
    o = _outlook()
    o.hours[3].temp_c = float("nan")
    raw = json.dumps(weather.outlook_to_dict(o), allow_nan=False)      # raises if a NaN slipped through
    assert weather.outlook_from_dict(json.loads(raw)).hours[3].temp_c == 0.0


def test_default_source_is_open_meteo():
    assert _outlook().source == "open-meteo"


# ── cache behaviour ──────────────────────────────────────────────────────────────────────────────

def test_success_is_cached_in_memory_and_on_disk(env):
    clock, calls, _, d = env
    o = weather.fetch_solar_outlook_cached(LAT, LON)
    assert o is not None and calls["om"] == 1
    assert (d / weather.OUTLOOK_CACHE_FILE).exists()
    clock.t += 600
    weather.fetch_solar_outlook_cached(LAT, LON)
    assert calls["om"] == 1                                           # still fresh: no second fetch


def test_another_process_reads_the_disk_cache_instead_of_fetching(env):
    clock, calls, _, d = env
    weather.fetch_solar_outlook_cached(LAT, LON)                      # "the advisor" fetches and writes
    weather._outlook_cache.clear()                                    # "the dashboard": empty memory
    clock.t += 1500
    o = weather.fetch_solar_outlook_cached(LAT, LON)
    assert o is not None and calls["om"] == 1


def test_disk_cache_for_another_location_is_ignored(env):
    clock, calls, _, d = env
    weather.fetch_solar_outlook_cached(LAT, LON)
    weather._outlook_cache.clear()
    weather.fetch_solar_outlook_cached(40.0, -100.0)
    assert calls["om"] == 2


def test_a_failure_is_remembered_for_five_minutes(env):
    clock, calls, beh, _ = env
    weather.fetch_solar_outlook_cached(LAT, LON)
    clock.t += 1900                                                   # cache now due for refresh
    beh["om"] = TimeoutError("read timed out")
    stale = weather.fetch_solar_outlook_cached(LAT, LON)
    assert stale is not None and calls["om"] == 2
    clock.t += 120
    weather.fetch_solar_outlook_cached(LAT, LON)
    assert calls["om"] == 2                                           # inside the 5-min back-off: no retry
    clock.t += 200
    weather.fetch_solar_outlook_cached(LAT, LON)
    assert calls["om"] == 3                                           # back-off over: tries again


def test_fetched_at_only_moves_on_a_real_open_meteo_success(env):
    clock, calls, beh, _ = env
    weather.fetch_solar_outlook_cached(LAT, LON)
    first = weather._outlook_cache["fetched_at"]
    clock.t += 4 * 3600
    beh["om"] = TimeoutError("x")
    weather.fetch_solar_outlook_cached(LAT, LON)                      # NWS fallback kicks in
    assert weather._outlook_cache["fetched_at"] == first              # weather_stale alert still sees the age


def test_nws_fallback_only_when_the_last_good_forecast_is_older_than_three_hours(env):
    clock, calls, beh, _ = env
    weather.fetch_solar_outlook_cached(LAT, LON)
    beh["om"] = TimeoutError("x")
    clock.t += 3600
    o = weather.fetch_solar_outlook_cached(LAT, LON)
    assert calls["nws"] == 0 and o.source == "open-meteo"             # 1 h old: the stale one is better
    clock.t += 3 * 3600
    o = weather.fetch_solar_outlook_cached(LAT, LON)
    assert calls["nws"] == 1 and o.source == "nws"


def test_cold_start_with_nothing_cached_uses_nws(env):
    _, calls, beh, _ = env
    beh["om"] = ConnectionError("down")
    o = weather.fetch_solar_outlook_cached(LAT, LON)
    assert o is not None and o.source == "nws" and calls["nws"] == 1


def test_both_sources_down_and_nothing_cached_is_none(env):
    _, _, beh, _ = env
    beh["om"] = ConnectionError("down")
    beh["nws"] = ConnectionError("down too")
    assert weather.fetch_solar_outlook_cached(LAT, LON) is None


def test_fetch_policy_is_passed_through(env):
    _, calls, _, _ = env
    weather.set_fetch_policy(timeout=4, retries=1)
    weather.fetch_solar_outlook_cached(LAT, LON)
    assert calls["om_args"] == (4, 1)


def test_corrupt_disk_cache_is_ignored(env):
    _, calls, _, d = env
    (d / weather.OUTLOOK_CACHE_FILE).write_text("{nope")
    assert weather.fetch_solar_outlook_cached(LAT, LON) is not None and calls["om"] == 1


def test_no_cache_dir_still_works_in_memory(env):
    _, calls, _, d = env
    weather.set_cache_dir(None)
    weather.fetch_solar_outlook_cached(LAT, LON)
    assert calls["om"] == 1 and not (d / weather.OUTLOOK_CACHE_FILE).exists()


# ── NWS grid → outlook ───────────────────────────────────────────────────────────────────────────

def _grid(sky_pct, temp_c=18.0):
    """A gridpoint payload: Nov 15 2026 08:00-23:00 UTC (00:00-15:00 PST), one 16-hour block each."""
    return {"properties": {
        "skyCover": {"values": [{"validTime": "2026-11-15T08:00:00+00:00/PT16H", "value": sky_pct}]},
        "temperature": {"values": [{"validTime": "2026-11-15T08:00:00+00:00/PT16H", "value": temp_c}]},
    }}


def test_nws_grid_expands_durations_into_local_hours():
    o = weather.nws_outlook_from_grid(_grid(0), LAT, LON, tz=TZ)
    assert len(o.hours) == 16 and o.hours[0].time == datetime(2026, 11, 15, 0, 0)
    assert o.source == "nws" and o.utc_offset_seconds == -8 * 3600
    assert all(h.temp_c == 18.0 for h in o.hours)


def test_nws_clear_sky_noon_is_bright_and_night_is_dark():
    o = weather.nws_outlook_from_grid(_grid(0), LAT, LON, tz=TZ)
    by_hour = {h.time.hour: h for h in o.hours}
    assert by_hour[2].ghi_wm2 == 0.0
    assert 450 < by_hour[11].ghi_wm2 < 800                           # mid-November noon at 33 N, clear


def test_nws_overcast_cuts_irradiance_hard():
    clear = {h.time.hour: h.ghi_wm2 for h in weather.nws_outlook_from_grid(_grid(0), LAT, LON, tz=TZ).hours}
    dull = {h.time.hour: h.ghi_wm2 for h in weather.nws_outlook_from_grid(_grid(100), LAT, LON, tz=TZ).hours}
    assert dull[11] <= 0.3 * clear[11]
    assert all(h.cloud_cover_pct == 100 for h in weather.nws_outlook_from_grid(_grid(100), LAT, LON, tz=TZ).hours)


def test_nws_outlook_has_sunrise_and_sunset():
    o = weather.nws_outlook_from_grid(_grid(0), LAT, LON, tz=TZ)
    sr, ss = o.sunrise_on(datetime(2026, 11, 15)), o.sunset_on(datetime(2026, 11, 15))
    assert sr and ss and 6 <= sr.hour <= 7 and 16 <= ss.hour <= 17


def test_nws_grid_missing_sky_cover_raises():
    with pytest.raises(ValueError):
        weather.nws_outlook_from_grid({"properties": {}}, LAT, LON, tz=TZ)


def test_fetch_nws_outlook_follows_the_points_lookup(monkeypatch):
    seen = []

    class Resp:
        def __init__(self, js): self._js = js
        def raise_for_status(self): pass
        def json(self): return self._js

    def fake_get(url, **kw):
        seen.append(url)
        if "/points/" in url:
            return Resp({"properties": {"forecastGridData": "https://api.weather.gov/gridpoints/SGX/1,2"}})
        return Resp(_grid(20))

    monkeypatch.setattr(weather.requests, "get", fake_get)
    o = weather.fetch_nws_outlook(LAT, LON)
    assert seen[0].endswith(f"/points/{LAT:.4f},{LON:.4f}") and seen[1].endswith("/gridpoints/SGX/1,2")
    assert o.source == "nws" and o.hours


# ── wiring: per-call overrides, the dashboard's quick policy, forecast source ───────────────────

def test_per_call_timeout_and_retries_override_the_policy(env):
    _, calls, _, _ = env
    weather.fetch_solar_outlook_cached(LAT, LON, timeout=4, retries=1)
    assert calls["om_args"] == (4, 1)


def test_dashboard_asks_once_quickly_and_shares_the_advisors_disk_copy(tmp_path, monkeypatch):
    from franklinwh_scraper import webapi
    seen = {}

    def fake(lat, lon, **kw):
        seen.update(kw)
        return None

    monkeypatch.setattr(webapi, "_fetch_outlook_cached", fake)
    monkeypatch.setattr(webapi, "_OUT", tmp_path)
    webapi._outlook()
    assert seen == {"timeout": 4, "retries": 1} and weather._cache_dir[0] == tmp_path
    weather.set_cache_dir(None)


def test_forecast_endpoint_says_where_the_forecast_came_from(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi
    from franklinwh_scraper.config import Config
    nws = _outlook()
    nws.source = "nws"
    monkeypatch.setattr(webapi, "_fetch_outlook_cached", lambda lat, lon, **kw: nws)
    monkeypatch.setattr(webapi, "_OUT", tmp_path)
    monkeypatch.setattr(webapi, "_cfg", Config(dashboard_token=""))
    body = TestClient(webapi.app).get("/api/forecast").json()
    assert body["forecast_source"] == "nws"
    assert "forecast_age_min" in body
    weather.set_cache_dir(None)


def test_advisor_shares_its_forecast_through_the_output_dir():
    import inspect

    from franklinwh_scraper import cli
    assert "set_cache_dir(outdir)" in inspect.getsource(cli)
