"""Solar irradiance + cloud cover forecast via Open-Meteo (free, no API key)."""

import logging
from dataclasses import dataclass
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
    """Look up lat/lon for a city name. Returns None if not found."""
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


@dataclass
class SolarOutlook:
    hours: list[HourlyForecast]

    def avg_ghi(self, next_hours: int) -> float:
        """Average global horizontal irradiance over the next N hours."""
        now = datetime.now()
        window = [
            h for h in self.hours
            if 0 <= (h.time - now).total_seconds() / 3600 <= next_hours
        ]
        if not window:
            return 0.0
        return sum(h.ghi_wm2 for h in window) / len(window)

    def avg_cloud_cover(self, next_hours: int) -> float:
        now = datetime.now()
        window = [
            h for h in self.hours
            if 0 <= (h.time - now).total_seconds() / 3600 <= next_hours
        ]
        if not window:
            return 100.0
        return sum(h.cloud_cover_pct for h in window) / len(window)

    def peak_ghi_today(self) -> float:
        now = datetime.now()
        today = [
            h for h in self.hours
            if h.time.date() == now.date()
        ]
        return max((h.ghi_wm2 for h in today), default=0.0)

    def today_generation_kwh(self, system_peak_kw: float, perf_ratio: float = 1.0) -> float:
        """Estimate today's total solar generation in kWh with temperature derating."""
        now = datetime.now()
        today_hours = [h for h in self.hours if h.time.date() == now.date()]
        total_kwh = 0.0
        for h in today_hours:
            eff = max(_MIN_EFFICIENCY, 1.0 + _TEMP_COEFF * (h.panel_temp_c - 25.0))
            total_kwh += h.ghi_wm2 / 1000.0 * system_peak_kw * eff
        return round(total_kwh * perf_ratio, 1)

    def tomorrow_avg_ghi(self) -> float:
        """Average GHI (W/m²) during solar hours (6 am–7 pm) tomorrow."""
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).date()
        window = [
            h for h in self.hours
            if h.time.date() == tomorrow and 6 <= h.time.hour <= 19
        ]
        if not window:
            return 0.0
        return sum(h.ghi_wm2 for h in window) / len(window)

    def ghi_at(self, dt: datetime) -> float:
        """Return GHI (W/m²) for the hour containing dt, 0 if not in forecast."""
        for h in self.hours:
            if (h.time.year == dt.year and h.time.month == dt.month
                    and h.time.day == dt.day and h.time.hour == dt.hour):
                return h.ghi_wm2
        return 0.0

    def avg_temp_c(self, next_hours: int) -> float:
        """Average forecast air temperature (°C) over the next N hours."""
        now = datetime.now()
        window = [
            h for h in self.hours
            if 0 <= (h.time - now).total_seconds() / 3600 <= next_hours
        ]
        if not window:
            return 22.0
        return sum(h.temp_c for h in window) / len(window)

    def tomorrow_generation_kwh(self, system_peak_kw: float, perf_ratio: float = 1.0) -> float:
        """Estimate tomorrow's total solar generation in kWh with temperature derating."""
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).date()
        total_kwh = 0.0
        for h in self.hours:
            if h.time.date() != tomorrow:
                continue
            eff = max(_MIN_EFFICIENCY, 1.0 + _TEMP_COEFF * (h.panel_temp_c - 25.0))
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


def fetch_solar_outlook(lat: float, lon: float, timeout: int = 10, retries: int = 3) -> SolarOutlook:
    """Fetch 48-hour hourly solar irradiance + temperature forecast for a location."""
    import time as _time
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(OPEN_METEO_URL, params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "direct_radiation,diffuse_radiation,cloud_cover,temperature_2m,wind_speed_10m",
                "forecast_days": 2,
                "timezone": "auto",
            }, timeout=timeout)
            resp.raise_for_status()
            break
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                _time.sleep(min(60, 5 * 2 ** attempt))
    else:
        raise last_exc  # type: ignore[misc]
    js = resp.json()

    hourly  = js["hourly"]
    temps   = hourly.get("temperature_2m", [])
    winds   = hourly.get("wind_speed_10m", [])
    hours: list[HourlyForecast] = []
    for i, t in enumerate(hourly["time"]):
        dt = datetime.fromisoformat(t)
        hours.append(HourlyForecast(
            time=dt,
            direct_radiation_wm2=hourly["direct_radiation"][i] or 0.0,
            diffuse_radiation_wm2=hourly["diffuse_radiation"][i] or 0.0,
            cloud_cover_pct=hourly["cloud_cover"][i] or 0.0,
            temp_c=temps[i] if i < len(temps) and temps[i] is not None else 0.0,
            wind_speed_ms=winds[i] if i < len(winds) and winds[i] is not None else 0.0,
        ))

    logger.debug("Fetched %d hourly forecasts for %.4f, %.4f", len(hours), lat, lon)
    return SolarOutlook(hours=hours)
