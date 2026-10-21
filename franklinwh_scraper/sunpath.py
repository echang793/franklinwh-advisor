"""Sun position, roof-face geometry and a clear-sky PV model.

Pure math (stdlib only). Powers the dashboard's sun-path panel: where the sun
is, how directly it hits each roof face, and what a clear day should produce.
The roof orientation isn't configured anywhere, so ``fit_faces`` estimates the
SE/SW panel split from measured history on clear days.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_ALBEDO = 0.2
_SUNRISE_EL = -0.833          # geometric elevation of the sun's centre at sunrise/sunset
_SE_AZ, _SW_AZ = 135.0, 225.0
FALLBACK_TZ = "America/Los_Angeles"


@dataclass(frozen=True)
class Face:
    azimuth: float   # compass degrees the panels face (180 = south)
    tilt: float      # degrees from horizontal
    weight: float    # share of the array on this face (weights sum to 1)


def default_tz() -> str:
    """IANA name of the machine's timezone (DB timestamps are naive local)."""
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            name = link.split("zoneinfo/", 1)[1]
            ZoneInfo(name)
            return name
    except Exception:
        pass
    return FALLBACK_TZ


def utc_offset_min(day: date, tz: str) -> int:
    """UTC offset in minutes (negative west of Greenwich) at local noon of ``day``."""
    return int(datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(tz))
               .utcoffset().total_seconds() // 60)


def sun_position(dt_utc: datetime, lat: float, lon: float) -> tuple[float, float]:
    """(azimuth°, elevation°) of the sun — NOAA solar-position algorithm.
    Azimuth is compass degrees clockwise from north; elevation is geometric."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    dt_utc = dt_utc.astimezone(timezone.utc)
    jd = dt_utc.timestamp() / 86400.0 + 2440587.5
    t = (jd - 2451545.0) / 36525.0
    rad = math.radians
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    c = (math.sin(rad(m)) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(rad(2 * m)) * (0.019993 - 0.000101 * t)
         + math.sin(rad(3 * m)) * 0.000289)
    true_long = l0 + c
    omega = 125.04 - 1934.136 * t
    lam = true_long - 0.00569 - 0.00478 * math.sin(rad(omega))
    eps0 = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    eps = eps0 + 0.00256 * math.cos(rad(omega))
    decl = math.asin(math.sin(rad(eps)) * math.sin(rad(lam)))
    y = math.tan(rad(eps) / 2.0) ** 2
    eot = 4.0 * math.degrees(
        y * math.sin(2 * rad(l0)) - 2 * e * math.sin(rad(m))
        + 4 * e * y * math.sin(rad(m)) * math.cos(2 * rad(l0))
        - 0.5 * y * y * math.sin(4 * rad(l0)) - 1.25 * e * e * math.sin(2 * rad(m)))
    minutes_utc = dt_utc.hour * 60 + dt_utc.minute + dt_utc.second / 60.0
    tst = (minutes_utc + eot + 4.0 * lon) % 1440.0
    ha = tst / 4.0 - 180.0
    lat_r = rad(lat)
    cos_zen = (math.sin(lat_r) * math.sin(decl)
               + math.cos(lat_r) * math.cos(decl) * math.cos(rad(ha)))
    cos_zen = max(-1.0, min(1.0, cos_zen))
    zen = math.acos(cos_zen)
    el = 90.0 - math.degrees(zen)
    sin_zen = math.sin(zen)
    if sin_zen < 1e-9:
        return 180.0, el
    cos_az = (math.sin(lat_r) * cos_zen - math.sin(decl)) / (math.cos(lat_r) * sin_zen)
    az = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
    az = (az + 180.0) % 360.0 if ha > 0 else (540.0 - az) % 360.0
    return az, el


def clear_sky(elevation_deg: float) -> tuple[float, float, float]:
    """(dni, dhi, ghi) W/m² for a clear day at the given sun elevation.
    Meinel direct-beam with Kasten-Young air mass; diffuse is ~10% of the beam."""
    if elevation_deg <= 0.0:
        return 0.0, 0.0, 0.0
    zen = 90.0 - elevation_deg
    cos_z = math.cos(math.radians(zen))
    am = 1.0 / (cos_z + 0.50572 * (96.07995 - zen) ** -1.6364)
    h_km = 0.1
    dni = 1353.0 * ((1 - 0.14 * h_km) * 0.7 ** (am ** 0.678) + 0.14 * h_km)
    dhi = 0.1 * dni
    return dni, dhi, dni * cos_z + dhi


def incidence_cos(sun_az: float, sun_el: float, face_az: float, tilt: float) -> float:
    """Cosine of the angle between the sun's rays and the panel normal."""
    el, t = math.radians(sun_el), math.radians(tilt)
    return (math.sin(el) * math.cos(t)
            + math.cos(el) * math.sin(t) * math.cos(math.radians(sun_az - face_az)))


def _poa(dni: float, dhi: float, ghi: float, sun_az: float, sun_el: float,
         face_az: float, tilt: float) -> float:
    """Plane-of-array irradiance W/m²: beam + isotropic sky + ground reflection."""
    if sun_el <= 0.0:
        return 0.0
    t = math.radians(tilt)
    beam = dni * max(0.0, incidence_cos(sun_az, sun_el, face_az, tilt))
    return beam + dhi * (1 + math.cos(t)) / 2 + ghi * _ALBEDO * (1 - math.cos(t)) / 2


def array_kw(dt_utc: datetime, lat: float, lon: float, faces: list[Face],
             scale_kw: float) -> tuple[float, list[float]]:
    """(total kW, per-face kW). ``scale_kw`` = array output at 1000 W/m² POA."""
    az, el = sun_position(dt_utc, lat, lon)
    dni, dhi, ghi = clear_sky(el)
    per = [scale_kw * f.weight * _poa(dni, dhi, ghi, az, el, f.azimuth, f.tilt) / 1000.0
           for f in faces]
    return sum(per), per


def sun_series(day: date, lat: float, lon: float, tz: str, step_min: int):
    """[(local_minute, az, el, dni, dhi, ghi)] across a local calendar day."""
    zone = ZoneInfo(tz)
    out = []
    for minute in range(0, 24 * 60, step_min):
        local = datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=zone)
        az, el = sun_position(local.astimezone(timezone.utc), lat, lon)
        dni, dhi, ghi = clear_sky(el)
        out.append((minute, az, el, dni, dhi, ghi))
    return out


def day_curve(day: date, lat: float, lon: float, faces: list[Face], scale_kw: float,
              tz: str | None = None, step_min: int = 5) -> dict:
    """Clear-sky day: per-step sun position and kW, plus sunrise/noon/sunset."""
    tz = tz or default_tz()
    series = sun_series(day, lat, lon, tz, step_min)
    points = []
    for minute, az, el, dni, dhi, ghi in series:
        per = [scale_kw * f.weight * _poa(dni, dhi, ghi, az, el, f.azimuth, f.tilt) / 1000.0
               for f in faces]
        points.append({"min": minute, "az": round(az, 2), "el": round(el, 2),
                       "kw": round(sum(per), 4), "faces": [round(p, 4) for p in per]})
    sunrise = sunset = None
    for prev, cur in zip(series, series[1:]):
        if sunrise is None and prev[2] <= _SUNRISE_EL < cur[2]:
            sunrise = prev[0] + step_min * (_SUNRISE_EL - prev[2]) / (cur[2] - prev[2])
        if prev[2] > _SUNRISE_EL >= cur[2]:
            sunset = prev[0] + step_min * (prev[2] - _SUNRISE_EL) / (prev[2] - cur[2])
    peak = max(series, key=lambda s: s[2])
    return {
        "date": day.isoformat(), "tz": tz, "step_min": step_min, "points": points,
        "sunrise_min": None if sunrise is None else round(sunrise, 1),
        "sunset_min": None if sunset is None else round(sunset, 1),
        "solar_noon_min": peak[0], "noon_el": round(peak[2], 2),
        "utc_offset_min": utc_offset_min(day, tz),
        "model_kwh": round(sum(p["kw"] for p in points) * step_min / 60.0, 3),
    }


def default_faces(se_weight: float = 0.8, sw_az: float = _SW_AZ, tilt: float = 20.0) -> list[Face]:
    return [Face(_SE_AZ, tilt, se_weight), Face(sw_az, tilt, 1.0 - se_weight)]


def fit_faces(store, lat: float, lon: float, tilt: float = 20.0,
              tz: str | None = None, lookback_days: int = 200) -> dict | None:
    """Estimate the SE/SW panel split (and array scale) from clear-day history.

    Picks days whose output is close to the best nearby day relative to the
    clear-sky model, then grid-searches SE weight and SW azimuth, solving the
    array scale in closed form. Returns None when there isn't enough data."""
    tz = tz or default_tz()
    since = (datetime.now() - timedelta(days=lookback_days)).isoformat()
    rows = store._conn.execute(
        "SELECT timestamp, solar_kw FROM readings WHERE timestamp >= ? ORDER BY timestamp",
        (since,)).fetchall()
    step = 5
    by_day: dict[date, dict[int, list[float]]] = {}
    for ts, kw in rows:
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if kw is None or kw != kw:
            continue
        slot = int(round((dt.hour * 60 + dt.minute) / step)) % (24 * 60 // step)
        by_day.setdefault(dt.date(), {}).setdefault(slot, []).append(float(kw))
    days = {d: {s: sum(v) / len(v) for s, v in slots.items()} for d, slots in by_day.items()}
    days = {d: s for d, s in days.items() if sum(1 for v in s.values() if v > 0.05) >= 100}
    if not days:
        return None

    sw_options = [180.0, 190.0, 200.0, 210.0, 220.0, 230.0, 240.0, 250.0]
    unit: dict[date, dict[float, list[float]]] = {}   # day -> face az -> POA/1000 per slot
    for d in days:
        series = sun_series(d, lat, lon, tz, step)
        unit[d] = {az: [_poa(s[3], s[4], s[5], s[1], s[2], az, tilt) / 1000.0 for s in series]
                   for az in [_SE_AZ] + sw_options}

    # clear-day selection: output / nominal-model ratio vs the best nearby day
    nominal = {d: [0.8 * a + 0.2 * b for a, b in zip(unit[d][_SE_AZ], unit[d][220.0])]
               for d in days}
    ratio = {}
    for d, slots in days.items():
        model = sum(nominal[d])
        if model > 0:
            ratio[d] = sum(slots.values()) / model
    if not ratio:
        return None
    ordered = sorted(ratio.values())
    p90 = ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))]
    chosen = []
    for d, r in ratio.items():
        window = [ratio[o] for o in ratio if abs((o - d).days) <= 7]
        if r >= 0.92 * max(window) and r >= 0.75 * p90:
            chosen.append(d)
    if not chosen:
        return None

    best = None
    for se_w in [0.5 + 0.05 * i for i in range(11)]:
        for sw_az in (sw_options if se_w < 0.999 else [220.0]):
            num = den = yy = 0.0
            n = 0
            for d in chosen:
                se, sw = unit[d][_SE_AZ], unit[d][sw_az]
                for slot, y in days[d].items():
                    f = se_w * se[slot] + (1 - se_w) * sw[slot]
                    if f <= 0.0:
                        continue
                    num += y * f
                    den += f * f
                    yy += y * y
                    n += 1
            if den <= 0 or n == 0:
                continue
            scale = num / den
            sse = yy - 2 * scale * num + scale * scale * den
            if best is None or sse < best[0]:
                best = (sse, se_w, sw_az, scale, n)
    if best is None:
        return None
    sse, se_w, sw_az, scale, n = best
    return {"se_weight": round(se_w, 3), "sw_azimuth": sw_az, "se_azimuth": _SE_AZ,
            "tilt": tilt, "scale_kw": round(scale, 3),
            "rmse_kw": round(math.sqrt(max(sse, 0.0) / n), 4),
            "days_used": len(chosen), "fitted": date.today().isoformat()}
