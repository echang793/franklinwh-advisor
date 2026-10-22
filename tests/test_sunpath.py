"""Sun position / roof-face geometry / dashboard sun-path endpoints."""
import json
import math
from datetime import date, datetime, timezone

import pytest

from franklinwh_scraper import sunpath
from franklinwh_scraper.history import HistoryStore

LAT, LON = 32.975, -117.077  # San Diego area (same as the live config)
TZ = "America/Los_Angeles"


def _scan_day(day: date, step_min: int = 2):
    """(max_elevation, azimuth_at_first_sunrise_sample) by brute-force scan."""
    best_el, sunrise_az = -90.0, None
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    start = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(TZ))
    for i in range(0, 24 * 60, step_min):
        dt = (start + timedelta(minutes=i)).astimezone(timezone.utc)
        az, el = sunpath.sun_position(dt, LAT, LON)
        best_el = max(best_el, el)
        if sunrise_az is None and el > -0.833:
            sunrise_az = az
    return best_el, sunrise_az


def test_summer_solstice_noon_elevation_and_sunrise_azimuth():
    max_el, sunrise_az = _scan_day(date(2026, 6, 21))
    assert max_el == pytest.approx(90 - LAT + 23.44, abs=0.7)   # ≈ 80.5°
    assert sunrise_az == pytest.approx(61.7, abs=2.0)           # NE-ish


def test_winter_solstice_noon_elevation():
    max_el, _ = _scan_day(date(2026, 12, 21))
    assert max_el == pytest.approx(90 - LAT - 23.44, abs=0.7)   # ≈ 33.6°


def test_incidence_south_face_at_equinox_noon():
    # sun due south at elevation 90-lat; face tilted 20° toward south
    el = 90 - LAT
    cos_t = sunpath.incidence_cos(180.0, el, 180.0, 20.0)
    assert cos_t == pytest.approx(math.cos(math.radians(abs(LAT - 20.0))), abs=1e-3)


def test_incidence_face_pointing_away_is_not_negative_in_array_output():
    # sun in the north at low elevation can't push a south face negative
    face = sunpath.Face(azimuth=180.0, tilt=20.0, weight=1.0)
    kw, _ = sunpath.array_kw(
        datetime(2026, 6, 21, 13, 0, tzinfo=timezone.utc), LAT, LON, [face], 5.0)
    assert kw >= 0.0


def test_day_curve_night_is_zero_and_faces_peak_on_either_side_of_noon():
    faces = [sunpath.Face(135.0, 20.0, 0.8), sunpath.Face(225.0, 20.0, 0.2)]
    # equal-weight comparison: per-face POA, so weight differences don't move the peak
    curve = sunpath.day_curve(date(2026, 9, 22), LAT, LON, faces, 4.7, tz=TZ, step_min=5)
    pts = curve["points"]
    assert all(p["kw"] >= 0.0 for p in pts)
    assert all(p["kw"] == 0.0 for p in pts if p["el"] <= 0.0)
    assert curve["model_kwh"] > 5.0
    se_peak = max(pts, key=lambda p: p["faces"][0] / faces[0].weight)["min"]
    sw_peak = max(pts, key=lambda p: p["faces"][1] / faces[1].weight)["min"]
    assert se_peak < curve["solar_noon_min"] < sw_peak
    assert curve["sunrise_min"] < curve["solar_noon_min"] < curve["sunset_min"]


def test_summer_day_makes_more_energy_than_winter_day():
    faces = [sunpath.Face(135.0, 20.0, 1.0)]
    s = sunpath.day_curve(date(2026, 6, 21), LAT, LON, faces, 4.7, tz=TZ)["model_kwh"]
    w = sunpath.day_curve(date(2026, 12, 21), LAT, LON, faces, 4.7, tz=TZ)["model_kwh"]
    assert s > w * 1.3


def _seed_clear_and_cloudy_days(store, faces, scale, days):
    """Write 5-min readings: clear days follow the model exactly, cloudy days
    are scaled-down noise-free junk the fitter must ignore."""
    rows = []
    for d, factor in days:
        curve = sunpath.day_curve(d, LAT, LON, faces, scale, tz=TZ, step_min=5)
        for p in curve["points"]:
            h, m = divmod(p["min"], 60)
            ts = datetime(d.year, d.month, d.day, h, m).isoformat()
            rows.append((ts, d.weekday(), h, 1.0, p["kw"] * factor, 50.0, 0.0, "Normal"))
    store._conn.executemany(
        "INSERT INTO readings (timestamp, day_of_week, hour_of_day, home_load_kw,"
        " solar_kw, battery_soc, grid_use_kw, grid_status) VALUES (?,?,?,?,?,?,?,?)", rows)
    store._conn.commit()


def test_fit_faces_recovers_synthetic_split(tmp_path):
    true_faces = [sunpath.Face(135.0, 20.0, 0.8), sunpath.Face(225.0, 20.0, 0.2)]
    days = [(date(2026, 8, 1 + i), 1.0) for i in range(8)]
    days += [(date(2026, 9, 1 + i), 0.3) for i in range(6)]  # cloudy decoys
    with HistoryStore(tmp_path / "h.db") as store:
        _seed_clear_and_cloudy_days(store, true_faces, 4.7, days)
        fit = sunpath.fit_faces(store, LAT, LON, tilt=20.0, tz=TZ)
    assert fit is not None
    assert fit["se_weight"] == pytest.approx(0.8, abs=0.05)
    assert fit["scale_kw"] == pytest.approx(4.7, rel=0.08)
    assert fit["rmse_kw"] < 0.3
    assert fit["days_used"] >= 3


def test_fit_faces_returns_none_without_data(tmp_path):
    with HistoryStore(tmp_path / "h.db") as store:
        assert sunpath.fit_faces(store, LAT, LON, tilt=20.0, tz=TZ) is None


# ── endpoints ────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi
    from franklinwh_scraper.config import Config

    monkeypatch.setattr(webapi, "_cfg", Config(lat=LAT, lon=LON), raising=False)
    monkeypatch.setattr(webapi, "_OUT", tmp_path, raising=False)
    monkeypatch.setattr(webapi, "_SUNPATH_YEAR_CACHE", {}, raising=False)
    monkeypatch.setattr(sunpath, "default_tz", lambda: TZ)   # CI runs in UTC
    return TestClient(webapi.app), tmp_path


def _no_nan(obj):
    # json.loads accepts NaN/Infinity by default; forbid them explicitly
    json.loads(json.dumps(obj), parse_constant=lambda c: pytest.fail(f"bad constant {c}"))


def test_sunpath_endpoint_with_empty_history(client):
    c, _ = client
    r = c.get("/api/sunpath?date=2026-06-21")
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "2026-06-21"
    assert len(body["faces"]) == 2
    assert abs(sum(f["weight"] for f in body["faces"]) - 1.0) < 1e-6
    assert len(body["points"]) >= 280
    assert body["actual"] is None and body["actual_kwh"] is None
    assert body["model_kwh"] > 0
    _no_nan(body)


def test_sunpath_endpoint_returns_actuals_when_history_exists(client):
    c, out = client
    faces = [sunpath.Face(135.0, 20.0, 0.8), sunpath.Face(225.0, 20.0, 0.2)]
    with HistoryStore(out / "history.db") as store:
        _seed_clear_and_cloudy_days(store, faces, 4.7, [(date(2026, 8, 3), 1.0)])
    body = c.get("/api/sunpath?date=2026-08-03").json()
    assert body["actual"] and len(body["actual"]) > 100
    assert body["actual_kwh"] > 5.0
    _no_nan(body)


def test_sunpath_endpoint_rejects_bad_date(client):
    c, _ = client
    assert c.get("/api/sunpath?date=nope").status_code == 400
    assert c.get("/api/sunpath?date=2026-13-40").status_code == 400


def test_sunpath_year_endpoint(client):
    c, _ = client
    r = c.get("/api/sunpath/year")
    assert r.status_code == 200
    rows = r.json()["days"]
    assert len(rows) == 365
    assert rows[0]["doy"] == 1 and rows[-1]["doy"] == 365
    by_doy = {x["doy"]: x for x in rows}
    assert by_doy[172]["noon_el"] > by_doy[355]["noon_el"] + 40   # Jun 21 vs Dec 21
    assert by_doy[172]["model_kwh"] > by_doy[355]["model_kwh"]
    assert all(x["actual_kwh"] is None for x in rows)
    # browser computes the sun locally: needs DST-correct offsets + noon per day
    assert by_doy[1]["utc_offset_min"] == -480          # PST
    assert by_doy[182]["utc_offset_min"] == -420        # PDT
    assert 11 * 60 < by_doy[172]["solar_noon_min"] < 14 * 60
    cfg = r.json()["config"]
    assert cfg["lat"] == LAT and cfg["lon"] == LON and cfg["tz"]
    assert len(cfg["faces"]) == 2 and cfg["scale_kw"] > 0
    _no_nan(r.json())


def test_sunpath_static_asset_served_and_linked():
    from fastapi.testclient import TestClient

    from franklinwh_scraper import webapi

    assert (webapi._STATIC / "sunpath.js").exists()
    assert '<script src="/sunpath.js' in (webapi._STATIC / "index.html").read_text()
    assert TestClient(webapi.app).get("/sunpath.js").status_code == 200


def test_browser_sun_math_matches_python_all_day():
    """sunpath.js reimplements sun_position/clear_sky/POA so scrubbing needs no
    network. JS `%` keeps the dividend's sign (Python's doesn't), which once
    mirrored the azimuth after ~5 PM — so check the whole day, not just noon."""
    import shutil
    import subprocess
    import textwrap

    from franklinwh_scraper import webapi

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    js = (webapi._STATIC / "sunpath.js").read_text()
    runner = textwrap.dedent("""
        const vm = require('vm');
        const ctx = { window: {}, CanvasRenderingContext2D: { prototype: { roundRect() {} } },
                      document: {}, console };
        vm.createContext(ctx);
        vm.runInContext(process.argv[1], ctx);
        const T = ctx.window.SunPath._test, out = [];
        for (const d of [[2026,0,5],[2026,2,20],[2026,5,21],[2026,7,27],[2026,11,21]])
          for (let m = 0; m < 1440; m += 37) {
            const ms = Date.UTC(d[0], d[1], d[2]) + m * 60000;
            const p = T.sunPos(ms, 32.975, -117.077), sky = T.clearSky(p.el);
            out.push([ms, p.az, p.el, T.poaFrac(sky, p.az, p.el, 135, 20)]);
          }
        console.log(JSON.stringify(out));
    """)
    res = subprocess.run([node, "-e", runner, js], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    rows = json.loads(res.stdout)
    assert len(rows) == 5 * len(range(0, 1440, 37))
    bad = []
    for ms, az, el, poa in rows:
        pa, pe = sunpath.sun_position(datetime.fromtimestamp(ms / 1000, tz=timezone.utc), LAT, LON)
        if abs(pe - el) > 1e-6 or (pe > -5 and min(abs(pa - az), 360 - abs(pa - az)) > 1e-6):
            bad.append((ms, az, pa))
        if pe > 0:
            dni, dhi, ghi = sunpath.clear_sky(pe)
            want = sunpath._poa(dni, dhi, ghi, pa, pe, 135.0, 20.0) / 1000.0
            assert poa == pytest.approx(want, abs=1e-6)
    assert not bad, f"JS/Python sun position differ at {len(bad)} points, e.g. {bad[:3]}"
