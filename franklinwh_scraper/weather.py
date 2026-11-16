"""Solar irradiance + cloud cover forecast via Open-Meteo (free, no API key)."""

import json
import logging
import math
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import requests

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL  = "https://geocoding-api.open-meteo.com/v1/search"

_TEMP_COEFF     = -0.0035  # crystalline silicon: -0.35%/°C above 25°C STC
_MIN_EFFICIENCY =  0.70    # floor at extreme temps (~100°C panel); below this derating is unphysical


@dataclass
class GeoLocation:
    name: str
    lat: float
    lon: float
    country: str


def geocode(city: str, timeout: int = 10) -> GeoLocation | None:
    """Look up lat/lon for a city name. Returns None if not found or on error —
    consistent with fetch_nws_storm_alerts/fetch_solar_outlook in this same
    module, which both catch broadly instead of propagating network errors."""
    try:
        resp = requests.get(GEOCODING_URL, params={
            "name": city, "count": 1, "language": "en", "format": "json",
        }, timeout=timeout)
        resp.raise_for_status()
        results = resp.json().get("results")
        if not results:
            return None
        r = results[0]
        return GeoLocation(
            name=r.get("name", city),
            lat=r["latitude"],
            lon=r["longitude"],
            country=r.get("country", ""),
        )
    except Exception as e:
        # Was two try blocks — the lat/lon KeyError access sat outside the
        # guarded one, so a malformed 200 response (missing those keys)
        # crashed the setup wizard instead of returning None like this
        # function's own docstring promises.
        logger.debug("Geocode lookup failed for %r: %s", city, e)
        return None


@dataclass
class HourlyForecast:
    time: datetime
    direct_radiation_wm2: float   # direct + diffuse = global horizontal irradiance
    diffuse_radiation_wm2: float
    cloud_cover_pct: float
    temp_c: float = 0.0           # ambient air temperature °C
    wind_speed_ms: float = 0.0    # wind speed at 10m (m/s)

    @property
    def ghi_wm2(self) -> float:
        return self.direct_radiation_wm2 + self.diffuse_radiation_wm2

    @property
    def panel_temp_c(self) -> float:
        """NOCT model: panel temp proportional to irradiance, reduced by wind cooling.

        Formula: T_cell = T_ambient + (NOCT-20) × GHI/800 × wind_factor
        NOCT=45°C for crystalline Si → coefficient = 25/800.
        Wind factor: each m/s at panel height (~0.75 × 10m) reduces heating ~4%.
        """
        noct_rise   = 25.0 * self.ghi_wm2 / 800.0
        panel_wind  = self.wind_speed_ms * 0.75   # 10m → rooftop height
        wind_factor = max(0.2, 1.0 - 0.04 * panel_wind)
        return self.temp_c + noct_rise * wind_factor

    @property
    def efficiency(self) -> float:
        """Panel efficiency vs. 25C STC from the cell temperature (hot panels
        make less). Floored so an extreme reading can't zero the hour."""
        return max(_MIN_EFFICIENCY, 1.0 + _TEMP_COEFF * (self.panel_temp_c - 25.0))

    @property
    def effective_ghi_wm2(self) -> float:
        """GHI after temperature derating: what the panels can actually use.
        Modelled kW = effective_ghi/1000 x system peak kW — the single
        definition shared by calibration, nowcast and prediction."""
        return self.ghi_wm2 * self.efficiency


@dataclass
class SolarOutlook:
    hours: list[HourlyForecast]
    utc_offset_seconds: int = 0  # panel location's UTC offset, from Open-Meteo
    # One per forecast day (~2 days), local to the panel's timezone (same
    # timezone=auto convention as `hours`). Used to target the "Without EV"
    # prediction / morning-preview window at real sunrise instead of a
    # fixed clock hour — DST-proof by construction (2026-08-17).
    sunrise_times: list[datetime] = field(default_factory=list)
    # Same shape, for sunset: lets /sundown mean the real sunset instead of "an hour
    # after the last forecast hour with sun".
    sunset_times: list[datetime] = field(default_factory=list)
    # Where this forecast came from: "open-meteo" normally, "nws" when the fallback
    # (National Weather Service sky cover turned into irradiance) had to stand in.
    source: str = "open-meteo"

    def sunset_on(self, d) -> datetime | None:
        """Sunset datetime for date `d`, or None if outside the forecast window."""
        target = d.date() if isinstance(d, datetime) else d
        for t in self.sunset_times:
            if t.date() == target:
                return t
        return None

    def sunrise_on(self, d) -> datetime | None:
        """Sunrise datetime for date `d` (a date or datetime), or None if
        outside the ~2-day forecast window this outlook covers."""
        target = d.date() if isinstance(d, datetime) else d
        for t in self.sunrise_times:
            if t.date() == target:
                return t
        return None

    def _local_now(self) -> datetime:
        """'Now' in the panel location's timezone, not the machine's.

        Open-Meteo returns hourly.time as naive-but-local-to-the-lat/lon
        (timezone=auto). Comparing that against a machine-local datetime.now()
        is only correct when the advisor runs in the same timezone as the
        panels — true for the personal single-box deployment, false for a
        commercial (gridwise) install running in e.g. a UTC container. Using
        UTC now + the location's offset keeps every window/boundary check
        below correct regardless of what timezone the process itself runs in.
        """
        from datetime import timezone
        return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=self.utc_offset_seconds)

    def avg_ghi(self, next_hours: int) -> float:
        """Average global horizontal irradiance over the next N hours."""
        now = self._local_now()
        window = [
            h for h in self.hours
            if 0 <= (h.time - now).total_seconds() / 3600 <= next_hours
        ]
        if not window:
            return 0.0
        return sum(h.ghi_wm2 for h in window) / len(window)

    def avg_cloud_cover(self, next_hours: int) -> float:
        now = self._local_now()
        window = [
            h for h in self.hours
            if 0 <= (h.time - now).total_seconds() / 3600 <= next_hours
        ]
        if not window:
            return 100.0
        return sum(h.cloud_cover_pct for h in window) / len(window)

    def peak_ghi_today(self) -> float:
        now = self._local_now()
        today = [
            h for h in self.hours
            if h.time.date() == now.date()
        ]
        return max((h.ghi_wm2 for h in today), default=0.0)

    def today_generation_kwh(
        self, system_peak_kw: float, perf_ratio: float = 1.0,
        hourly_bias: dict[int, float] | None = None,
    ) -> float:
        """Estimate today's total solar generation in kWh with temperature derating.

        hourly_bias: per-clock-hour learned correction (actual/predicted) that
        captures systematic within-day shape errors — morning shade, inverter
        clipping near solar noon — that a single flat perf_ratio can't.
        """
        now = self._local_now()
        today_hours = [h for h in self.hours if h.time.date() == now.date()]
        total_kwh = 0.0
        for h in today_hours:
            eff = h.efficiency
            if hourly_bias:
                eff *= hourly_bias.get(h.time.hour, 1.0)
            total_kwh += h.ghi_wm2 / 1000.0 * system_peak_kw * eff
        return round(total_kwh * perf_ratio, 1)

    def remaining_today_generation_kwh(
        self, system_peak_kw: float, perf_ratio: float = 1.0,
        hourly_bias: dict[int, float] | None = None,
    ) -> float:
        """Same as today_generation_kwh but only future hours (h.time > now)
        — for a mid-day "how much solar is left today" estimate, where
        summing the whole day would double-count generation already
        reflected in the current battery SoC/history."""
        now = self._local_now()
        remaining = [h for h in self.hours if h.time.date() == now.date() and h.time > now]
        total_kwh = 0.0
        for h in remaining:
            eff = h.efficiency
            if hourly_bias:
                eff *= hourly_bias.get(h.time.hour, 1.0)
            total_kwh += h.ghi_wm2 / 1000.0 * system_peak_kw * eff
        return round(total_kwh * perf_ratio, 1)

    def tomorrow_avg_ghi(self) -> float:
        """Average GHI (W/m²) during solar hours (6 am–7 pm) tomorrow."""
        now = self._local_now()
        tomorrow = (now + timedelta(days=1)).date()
        window = [
            h for h in self.hours
            if h.time.date() == tomorrow and 6 <= h.time.hour <= 19
        ]
        if not window:
            return 0.0
        return sum(h.ghi_wm2 for h in window) / len(window)

    def tomorrow_avg_cloud_cover(self) -> float:
        """Average cloud cover (%) during solar hours (6 am–7 pm) tomorrow."""
        now = self._local_now()
        tomorrow = (now + timedelta(days=1)).date()
        window = [
            h for h in self.hours
            if h.time.date() == tomorrow and 6 <= h.time.hour <= 19
        ]
        if not window:
            return 100.0
        return sum(h.cloud_cover_pct for h in window) / len(window)

    # Observed live 2026-09-20: Open-Meteo returned a near-clear-sky GHI
    # curve (peak 780 W/m², avg 412 W/m² over 12h — above _GHI_CLOUDY_THRESHOLD)
    # while reporting cloud_cover=100% for nearly every hour of the same
    # forecast — a genuine upstream model inconsistency (thin/burning-off
    # marine layer the irradiance component didn't attenuate for). Every
    # GHI-only "is it cloudy" check in the app sailed right past it and
    # predicted a near-normal 20+ kWh day; actual came in ~19% lower.
    # cloud_cover this high overrides an optimistic GHI reading even though
    # GHI alone would call the day sunny.
    _CLOUD_COVER_OVERRIDE_PCT = 85.0

    def is_cloudy(self, next_hours: int, ghi_threshold: float) -> bool:
        """True if avg GHI over the next N hours is below threshold, or the
        model's own cloud_cover disagrees with an optimistic GHI reading."""
        if self.avg_ghi(next_hours) < ghi_threshold:
            return True
        return self.avg_cloud_cover(next_hours) >= self._CLOUD_COVER_OVERRIDE_PCT

    def is_cloudy_tomorrow(self, ghi_threshold: float) -> bool:
        """Same as is_cloudy but for tomorrow's 6 am–7 pm solar window."""
        if self.tomorrow_avg_ghi() < ghi_threshold:
            return True
        return self.tomorrow_avg_cloud_cover() >= self._CLOUD_COVER_OVERRIDE_PCT

    def ghi_at(self, dt: datetime) -> float:
        """Return GHI (W/m²) for the hour containing dt, 0 if not in forecast.

        `dt` is machine-local (predictor.py's only caller passes
        `datetime.now() + timedelta(hours=h)`) — every sibling method on
        this class was fixed to route through _local_now() so a process
        running in a different timezone from the panels (e.g. a commercial
        install in a UTC container) doesn't silently mismatch; this one was
        missed. Converting via "how far ahead is dt" rather than an absolute
        wall-clock comparison keeps it correct without changing the caller.
        """
        return self._ghi_for_local(self._local_now() + (dt - datetime.now()))

    def effective_ghi_at(self, dt: datetime) -> float:
        """ghi_at after temperature derating — see HourlyForecast.effective_ghi_wm2."""
        h = self._hour_for_local(self._local_now() + (dt - datetime.now()))
        return h.effective_ghi_wm2 if h else 0.0

    def current_ghi(self) -> float:
        """GHI (W/m²) for the hour we are in right now — the figure to pair
        with a live production reading when calibrating. (avg_ghi(1) is the
        window AHEAD of now, i.e. the next hour, which is not what a reading
        taken at 12:20 was produced under.)"""
        return self._ghi_for_local(self._local_now())

    def current_effective_ghi(self) -> float:
        h = self._hour_for_local(self._local_now())
        return h.effective_ghi_wm2 if h else 0.0

    def _hour_for_local(self, local_dt: datetime) -> "HourlyForecast | None":
        for h in self.hours:
            if (h.time.year == local_dt.year and h.time.month == local_dt.month
                    and h.time.day == local_dt.day and h.time.hour == local_dt.hour):
                return h
        return None

    def _ghi_for_local(self, local_dt: datetime) -> float:
        h = self._hour_for_local(local_dt)
        return h.ghi_wm2 if h else 0.0

    def avg_temp_c(self, next_hours: int) -> float:
        """Average forecast air temperature (°C) over the next N hours."""
        now = self._local_now()
        window = [
            h for h in self.hours
            if 0 <= (h.time - now).total_seconds() / 3600 <= next_hours
        ]
        if not window:
            return 22.0
        return sum(h.temp_c for h in window) / len(window)

    def tomorrow_generation_kwh(
        self, system_peak_kw: float, perf_ratio: float = 1.0,
        hourly_bias: dict[int, float] | None = None,
    ) -> float:
        """Estimate tomorrow's total solar generation in kWh with temperature derating.

        hourly_bias: per-clock-hour learned correction (actual/predicted) that
        captures systematic within-day shape errors — morning shade, inverter
        clipping near solar noon — that a single flat perf_ratio can't.
        """
        now = self._local_now()
        tomorrow = (now + timedelta(days=1)).date()
        total_kwh = 0.0
        for h in self.hours:
            if h.time.date() != tomorrow:
                continue
            eff = h.efficiency
            if hourly_bias:
                eff *= hourly_bias.get(h.time.hour, 1.0)
            total_kwh += h.ghi_wm2 / 1000.0 * system_peak_kw * eff
        return round(total_kwh * perf_ratio, 1)


_STORM_KEYWORDS = (
    "storm", "wind", "flood", "rain", "tornado", "hurricane",
    "blizzard", "winter", "tropical", "fire weather", "red flag",
)


def fetch_nws_storm_alerts(lat: float, lon: float, timeout: int = 10) -> list[str]:
    """Active NWS alert event names for this point that imply outage/backup risk.

    Returns a list of event strings (e.g. ['High Wind Warning']). Empty on error.
    """
    try:
        r = requests.get(
            "https://api.weather.gov/alerts/active",
            params={"point": f"{lat},{lon}"},
            headers={"User-Agent": "franklinwh-advisor (github.com/echang793)"},
            timeout=timeout,
        )
        r.raise_for_status()
        features = r.json().get("features", [])
    except Exception as e:
        logger.debug("NWS alert fetch failed: %s", e)
        return []
    events: list[str] = []
    for f in features:
        event = (f.get("properties", {}).get("event") or "").strip()
        if event and any(kw in event.lower() for kw in _STORM_KEYWORDS):
            events.append(event)
    return events


# Two forecast models, averaged. Open-Meteo's default ("best_match") is the one
# that called a marine-layer day at 14.6 kWh when 6.6 arrived (ECMWF said 6.1);
# averaging it with ECMWF cut replayed daily error 15%, hourly error 13% and
# cloudy-day error 50% over 167 days of real readings. Multi-model responses name
# every column <variable>_<model>.
_FORECAST_MODELS = ("best_match", "ecmwf_ifs025")


def _blend_models(hourly: dict, name: str, n: int) -> list:
    """Per-timestamp mean of `name` across the models that have a value there
    (None where none do). Falls back to the plain, un-suffixed column that a
    single-model response uses."""
    cols = [hourly[f"{name}_{m}"] for m in _FORECAST_MODELS if f"{name}_{m}" in hourly]
    if not cols and name in hourly:
        cols = [hourly[name]]
    out: list = []
    for i in range(n):
        vals = [c[i] for c in cols if i < len(c) and c[i] is not None]
        out.append(sum(vals) / len(vals) if vals else None)
    return out


def fetch_solar_outlook(lat: float, lon: float, timeout: int = 10, retries: int = 3) -> SolarOutlook:
    """Fetch ~72-hour hourly solar irradiance + temperature forecast for a location."""
    import time as _time
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(OPEN_METEO_URL, params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "direct_radiation,diffuse_radiation,cloud_cover,temperature_2m,wind_speed_10m",
                "daily": "sunrise,sunset",
                "forecast_days": 3,  # today + tomorrow + day after (multi-day alerts)
                "timezone": "auto",
                "models": ",".join(_FORECAST_MODELS),
            }, timeout=timeout)
            resp.raise_for_status()
            break
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                _time.sleep(min(60, 5 * 2 ** attempt) * (0.5 + random.random()))
    else:
        raise last_exc  # type: ignore[misc]
    js = resp.json()

    hourly  = js["hourly"]
    n       = len(hourly["time"])
    temps   = _blend_models(hourly, "temperature_2m", n)
    winds   = _blend_models(hourly, "wind_speed_10m", n)
    clouds  = _blend_models(hourly, "cloud_cover", n)
    hours: list[HourlyForecast] = []
    direct  = _blend_models(hourly, "direct_radiation", n)
    diffuse = _blend_models(hourly, "diffuse_radiation", n)
    for i, t in enumerate(hourly["time"]):
        dt = datetime.fromisoformat(t)
        # Open-Meteo radiation is the average of the PRECEDING hour, labelled
        # with the hour's END (the 07:00 value covers 06:00-07:00). Each entry
        # here stands for the hour that STARTS at its time, so it takes the
        # next label's value; the final entry has no following label and
        # reads 0 rather than a guess. Temperature, wind and cloud cover are
        # instantaneous and stay on their own labels.
        nxt = i + 1
        hours.append(HourlyForecast(
            time=dt,
            direct_radiation_wm2=(direct[nxt] if nxt < len(direct) else 0.0) or 0.0,
            diffuse_radiation_wm2=(diffuse[nxt] if nxt < len(diffuse) else 0.0) or 0.0,
            cloud_cover_pct=clouds[i] or 0.0,
            temp_c=temps[i] if temps[i] is not None else 0.0,
            wind_speed_ms=winds[i] if winds[i] is not None else 0.0,
        ))

    daily = js.get("daily", {})
    sunrise_raw = daily.get("sunrise") or next(
        (daily[f"sunrise_{m}"] for m in _FORECAST_MODELS if daily.get(f"sunrise_{m}")), [])
    sunrise_times = [datetime.fromisoformat(s) for s in sunrise_raw if s]
    sunset_raw = daily.get("sunset") or next(
        (daily[f"sunset_{m}"] for m in _FORECAST_MODELS if daily.get(f"sunset_{m}")), [])
    sunset_times = [datetime.fromisoformat(s) for s in sunset_raw if s]

    logger.debug("Fetched %d hourly forecasts for %.4f, %.4f", len(hours), lat, lon)
    return SolarOutlook(
        hours=hours, utc_offset_seconds=js.get("utc_offset_seconds", 0),
        sunrise_times=sunrise_times, sunset_times=sunset_times,
    )


# ── Cached fetch (shared by every caller — alerts, cli, webapi, chatbot) ────
#
# Lives here rather than in each caller so a chatbot query and the alert
# loop hit the same 30-min-TTL cache instead of each keeping (and paying
# for) its own copy of the same Open-Meteo forecast.
#
# The advisor and the dashboard are separate processes, so the last good
# forecast is also kept on disk (set_cache_dir): the dashboard reads what the
# advisor fetched instead of calling Open-Meteo itself, and a restart during an
# outage still has a forecast. A failed fetch is remembered for 5 minutes, so a
# slow or down Open-Meteo costs one timeout per 5 minutes instead of one per
# request (the dashboard's panels used to wait ~50 s on every load). When the
# last good forecast is over 3 hours old, the NWS fallback stands in.

OUTLOOK_CACHE_FILE = ".outlook_cache.json"
_FRESH_S = 1800              # refetch after 30 min
_DISK_FRESH_S = 2400         # another process's copy is good for 40 min (the advisor refreshes at 30)
_NEGATIVE_S = 300            # after a failure, don't try again for 5 min
_FALLBACK_AFTER_S = 3 * 3600 # stale beyond this: ask NWS instead

_outlook_cache: dict = {}
_cache_dir: list = [None]
_fetch_policy = {"timeout": 10, "retries": 3}


def _clock() -> float:
    return time.time()


def set_cache_dir(path) -> None:
    """Where the shared on-disk copy lives (the advisor's output dir). None turns it off."""
    from pathlib import Path
    _cache_dir[0] = Path(path) if path is not None else None


def set_fetch_policy(timeout: int = 10, retries: int = 3) -> None:
    """The dashboard asks once with a short timeout; the advisor can afford retries."""
    _fetch_policy["timeout"] = timeout
    _fetch_policy["retries"] = retries


def _num(x) -> float:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def outlook_to_dict(o: SolarOutlook) -> dict:
    """JSON-safe form (no NaN/Infinity: those become 0.0)."""
    return {
        "source": o.source,
        "utc_offset_seconds": int(o.utc_offset_seconds or 0),
        "sunrise_times": [t.isoformat() for t in o.sunrise_times],
        "sunset_times": [t.isoformat() for t in o.sunset_times],
        "hours": [[h.time.isoformat(), _num(h.direct_radiation_wm2), _num(h.diffuse_radiation_wm2),
                   _num(h.cloud_cover_pct), _num(h.temp_c), _num(h.wind_speed_ms)] for h in o.hours],
    }


def outlook_from_dict(d: dict) -> SolarOutlook:
    return SolarOutlook(
        hours=[HourlyForecast(time=datetime.fromisoformat(r[0]), direct_radiation_wm2=_num(r[1]),
                              diffuse_radiation_wm2=_num(r[2]), cloud_cover_pct=_num(r[3]),
                              temp_c=_num(r[4]), wind_speed_ms=_num(r[5])) for r in d["hours"]],
        utc_offset_seconds=int(d.get("utc_offset_seconds", 0)),
        sunrise_times=[datetime.fromisoformat(t) for t in d.get("sunrise_times", [])],
        sunset_times=[datetime.fromisoformat(t) for t in d.get("sunset_times", [])],
        source=str(d.get("source", "open-meteo")),
    )


def _loc_key(lat: float, lon: float) -> list:
    return [round(float(lat), 3), round(float(lon), 3)]


def _read_disk(key: list):
    d = _cache_dir[0]
    if d is None:
        return None
    try:
        js = json.loads((d / OUTLOOK_CACHE_FILE).read_text())
        if js.get("key") != key:
            return None
        return float(js["fetched_at"]), outlook_from_dict(js["outlook"])
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def _write_disk(key: list, fetched_at: float, o: SolarOutlook) -> None:
    d = _cache_dir[0]
    if d is None:
        return
    try:
        target = d / OUTLOOK_CACHE_FILE
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"key": key, "fetched_at": fetched_at, "outlook": outlook_to_dict(o)},
                                  allow_nan=False))
        os.replace(tmp, target)
    except (OSError, ValueError, TypeError):
        logger.debug("Couldn't write the outlook cache", exc_info=True)


def fetch_solar_outlook_cached(lat: float, lon: float, timeout: int | None = None,
                               retries: int | None = None):
    """A SolarOutlook (or None when nothing is available). Never raises.

    Order: fresh memory copy -> fresh disk copy (another process fetched it) -> stale copy
    while a recent failure is still being backed off -> Open-Meteo -> stale copy if under 3 h
    old -> NWS fallback -> whatever stale copy there is. `fetched_at` only ever moves on a
    real Open-Meteo success, so the weather-stale alert keeps measuring the right thing."""
    now = _clock()
    c = _outlook_cache
    key = _loc_key(lat, lon)
    if c.get("key") not in (None, key):
        c.clear()
    c["key"] = key

    o, fa = c.get("outlook"), c.get("fetched_at", 0.0)
    if o is not None and o.source != "nws" and now - fa < _FRESH_S:
        return o
    disk = _read_disk(key)
    if disk is not None and disk[0] > fa:
        c["outlook"], c["fetched_at"] = disk[1], disk[0]
        o, fa = disk[1], disk[0]
        if now - fa < _DISK_FRESH_S:
            return o
    if o is not None and o.source == "nws" and now - c.get("nws_at", 0.0) < _FRESH_S \
            and now - c.get("failed_at", -1e18) < _NEGATIVE_S:
        return o
    if now - c.get("failed_at", -1e18) < _NEGATIVE_S:
        return o                                     # backing off: serve what we have

    try:
        fresh = fetch_solar_outlook(lat, lon,
                                    timeout=timeout if timeout is not None else _fetch_policy["timeout"],
                                    retries=retries if retries is not None else _fetch_policy["retries"])
        c["outlook"], c["fetched_at"] = fresh, now
        c.pop("failed_at", None)
        _write_disk(key, now, fresh)
        return fresh
    except Exception as e:  # noqa: BLE001
        logger.warning("Weather forecast fetch failed: %s", e)
        c["failed_at"] = now

    if o is not None:
        if o.source == "nws" and now - c.get("nws_at", 0.0) < _FRESH_S:
            return o
        if o.source != "nws" and now - fa < _FALLBACK_AFTER_S:
            return o
    try:
        nws = fetch_nws_outlook(lat, lon)
        c["outlook"], c["nws_at"] = nws, now
        logger.warning("Using the NWS fallback forecast (Open-Meteo unavailable)")
        return nws
    except Exception as e:  # noqa: BLE001
        logger.warning("NWS fallback forecast failed too: %s", e)
        return o


# ── NWS fallback ─────────────────────────────────────────────────────────────
#
# api.weather.gov has no irradiance, only sky cover. Irradiance is estimated as
# clear-sky GHI (sunpath.clear_sky at mid-hour sun elevation) scaled by the
# Kasten-Czeplak cloud factor 1 - 0.75 * cover^3.4. Rough, but far better than
# a blank forecast: it only stands in when Open-Meteo has been down 3+ hours.

_NWS_HEADERS = {"User-Agent": "franklinwh-advisor (github.com/echang793)", "Accept": "application/geo+json"}
_DURATION_RE = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


def _expand_hours(values: list, tz) -> dict:
    """{local naive hour: value} from NWS "validTime": "start/PnDTnH" entries."""
    out: dict = {}
    for v in values or []:
        try:
            start_s, dur_s = str(v["validTime"]).split("/")
            m = _DURATION_RE.match(dur_s)
            hours = (int(m.group(1) or 0) * 24 + int(m.group(2) or 0)) if m else 1
            start = datetime.fromisoformat(start_s)
            val = v.get("value")
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if val is None:
            continue
        for i in range(max(1, hours)):
            t = (start + timedelta(hours=i)).astimezone(tz).replace(tzinfo=None)
            out[t] = float(val)
    return out


def _sun_crossing(day, lat: float, lon: float, tz, lo_h: int, hi_h: int, rising: bool) -> datetime | None:
    from .sunpath import sun_position
    prev = None
    t = datetime(day.year, day.month, day.day, lo_h)
    end = datetime(day.year, day.month, day.day, hi_h)
    while t <= end:
        el = sun_position(t.replace(tzinfo=tz), lat, lon)[1]
        up = el > -0.833
        if prev is not None and up != prev and up == rising:
            return t
        prev = up
        t += timedelta(minutes=2)
    return None


def nws_outlook_from_grid(grid: dict, lat: float, lon: float, tz: str | None = None) -> SolarOutlook:
    """Turn a gridpoint payload into a SolarOutlook (hours in local time). Raises ValueError
    when there is no sky cover to work from."""
    from zoneinfo import ZoneInfo

    from .sunpath import clear_sky, default_tz, sun_position
    zone = ZoneInfo(tz or default_tz())
    props = (grid or {}).get("properties") or {}
    sky = _expand_hours((props.get("skyCover") or {}).get("values"), zone)
    if not sky:
        raise ValueError("NWS grid has no sky cover")
    temps = _expand_hours((props.get("temperature") or {}).get("values"), zone)
    winds = _expand_hours((props.get("windSpeed") or {}).get("values"), zone)   # km/h
    hours: list[HourlyForecast] = []
    for t in sorted(sky):
        cover = max(0.0, min(100.0, sky[t])) / 100.0
        el = sun_position((t + timedelta(minutes=30)).replace(tzinfo=zone), lat, lon)[1]
        ghi = clear_sky(el)[2] * (1.0 - 0.75 * cover ** 3.4)
        diffuse = ghi * min(1.0, 0.15 + 0.85 * cover)
        hours.append(HourlyForecast(
            time=t, direct_radiation_wm2=ghi - diffuse, diffuse_radiation_wm2=diffuse,
            cloud_cover_pct=cover * 100.0, temp_c=temps.get(t, 0.0),
            wind_speed_ms=winds.get(t, 0.0) / 3.6,
        ))
    days = sorted({h.time.date() for h in hours})
    sunrise = [x for d in days if (x := _sun_crossing(d, lat, lon, zone, 3, 10, True))]
    sunset = [x for d in days if (x := _sun_crossing(d, lat, lon, zone, 14, 21, False))]
    offset = int(hours[0].time.replace(tzinfo=zone).utcoffset().total_seconds())
    return SolarOutlook(hours=hours, utc_offset_seconds=offset, sunrise_times=sunrise,
                        sunset_times=sunset, source="nws")


def fetch_nws_outlook(lat: float, lon: float, timeout: int = 10) -> SolarOutlook:
    """NWS gridpoint forecast as a SolarOutlook. Raises on any failure (the caller falls back)."""
    r = requests.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", headers=_NWS_HEADERS, timeout=timeout)
    r.raise_for_status()
    props = r.json().get("properties") or {}
    url = props.get("forecastGridData")
    if not url:
        raise ValueError("NWS points lookup returned no grid URL")
    g = requests.get(url, headers=_NWS_HEADERS, timeout=timeout)
    g.raise_for_status()
    return nws_outlook_from_grid(g.json(), lat, lon, tz=props.get("timeZone"))
