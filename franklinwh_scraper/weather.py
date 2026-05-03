"""Solar irradiance + cloud cover forecast via Open-Meteo (free, no API key)."""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL  = "https://geocoding-api.open-meteo.com/v1/search"


@dataclass
class GeoLocation:
    name: str
    lat: float
    lon: float
    country: str


def geocode(city: str, timeout: int = 10) -> Optional[GeoLocation]:
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

    @property
    def ghi_wm2(self) -> float:
        return self.direct_radiation_wm2 + self.diffuse_radiation_wm2


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

    def today_generation_kwh(self, system_peak_kw: float) -> float:
        """
        Estimate today's total solar generation in kWh.

        Integrates today's hourly GHI forecast (Wh/m²) and scales by the
        system's peak output relative to standard test conditions (1000 W/m²).
        Each hourly GHI value represents the average W/m² for that hour, so
        GHI (W/m²) × 1 hour / 1000 = kWh/m², then × system_peak_kw gives kWh.
        """
        now = datetime.now()
        today_hours = [h for h in self.hours if h.time.date() == now.date()]
        total_kwh = sum(h.ghi_wm2 / 1000.0 * system_peak_kw for h in today_hours)
        return round(total_kwh, 1)

    def ghi_at(self, dt: datetime) -> float:
        """Return GHI (W/m²) for the hour containing dt, 0 if not in forecast."""
        for h in self.hours:
            if (h.time.year == dt.year and h.time.month == dt.month
                    and h.time.day == dt.day and h.time.hour == dt.hour):
                return h.ghi_wm2
        return 0.0


def fetch_solar_outlook(lat: float, lon: float, timeout: int = 10) -> SolarOutlook:
    """Fetch 48-hour hourly solar irradiance forecast for a location."""
    resp = requests.get(OPEN_METEO_URL, params={
        "latitude": lat,
        "longitude": lon,
        "hourly": "direct_radiation,diffuse_radiation,cloud_cover",
        "forecast_days": 2,
        "timezone": "auto",
    }, timeout=timeout)
    resp.raise_for_status()
    js = resp.json()

    hourly = js["hourly"]
    hours: list[HourlyForecast] = []
    for i, t in enumerate(hourly["time"]):
        dt = datetime.fromisoformat(t)
        hours.append(HourlyForecast(
            time=dt,
            direct_radiation_wm2=hourly["direct_radiation"][i] or 0.0,
            diffuse_radiation_wm2=hourly["diffuse_radiation"][i] or 0.0,
            cloud_cover_pct=hourly["cloud_cover"][i] or 0.0,
        ))

    logger.debug("Fetched %d hourly forecasts for %.4f, %.4f", len(hours), lat, lon)
    return SolarOutlook(hours=hours)
