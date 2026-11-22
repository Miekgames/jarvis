"""
Weather via Open-Meteo.

Chosen deliberately: no API key, no account, generous free use -- so this is a
feature that genuinely works the moment JARVIS starts, rather than one more
"connect an account" placeholder. Geocoding uses the same provider.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("jarvis.weather")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "slight snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail",
}


class WeatherService:
    def __init__(self, settings, settings_store=None):
        self.settings = settings
        self.settings_store = settings_store
        self._client = None
        self._location_cache: Dict[str, Dict[str, Any]] = {}

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    # ------------------------------------------------------------------
    async def geocode(self, place: str) -> Optional[Dict[str, Any]]:
        key = place.lower().strip()
        if key in self._location_cache:
            return self._location_cache[key]
        try:
            response = await self._http().get(
                GEOCODE_URL, params={"name": place, "count": 1, "language": "en", "format": "json"}
            )
            response.raise_for_status()
            results = response.json().get("results") or []
        except Exception as exc:
            logger.warning("geocoding failed for '%s': %s", place, exc)
            return None
        if not results:
            return None
        first = results[0]
        location = {
            "name": first["name"],
            "country": first.get("country", ""),
            "admin": first.get("admin1", ""),
            "latitude": first["latitude"],
            "longitude": first["longitude"],
            "timezone": first.get("timezone", "auto"),
        }
        self._location_cache[key] = location
        return location

    async def _resolve_location(self, place: Optional[str]) -> Optional[Dict[str, Any]]:
        if place:
            return await self.geocode(place)
        if self.settings_store:
            stored = await self.settings_store.get("weather_location", None)
            if stored:
                return await self.geocode(stored) if isinstance(stored, str) else stored
        if self.settings.WEATHER_LOCATION:
            return await self.geocode(self.settings.WEATHER_LOCATION)
        return None

    # ------------------------------------------------------------------
    async def current(self, place: Optional[str] = None) -> Dict[str, Any]:
        location = await self._resolve_location(place)
        if location is None:
            return {
                "ok": False,
                "message": "No weather location is set. Tell me your city, or set "
                           "JARVIS_WEATHER_LOCATION in .env.",
            }

        try:
            response = await self._http().get(
                FORECAST_URL,
                params={
                    "latitude": location["latitude"],
                    "longitude": location["longitude"],
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                               "precipitation,weather_code,wind_speed_10m,is_day",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                             "precipitation_probability_max,sunrise,sunset",
                    "timezone": "auto",
                    "forecast_days": 5,
                },
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            return {"ok": False, "message": f"Weather service unreachable: {exc}"}

        current = data.get("current", {})
        units = data.get("current_units", {})
        code = int(current.get("weather_code", 0))
        description = WMO_CODES.get(code, "unknown conditions")
        temperature = current.get("temperature_2m")
        feels = current.get("apparent_temperature")
        unit = units.get("temperature_2m", "°C")

        daily = data.get("daily", {})
        forecast: List[Dict[str, Any]] = []
        for index, date in enumerate(daily.get("time", [])[:5]):
            forecast.append(
                {
                    "date": date,
                    "conditions": WMO_CODES.get(int(daily["weather_code"][index]), "unknown"),
                    "high": daily["temperature_2m_max"][index],
                    "low": daily["temperature_2m_min"][index],
                    "precipitation_chance": daily.get("precipitation_probability_max", [None] * 5)[index],
                }
            )

        place_name = f"{location['name']}, {location['country']}".strip(", ")
        summary = (
            f"{place_name}: {description}, {temperature}{unit}"
            + (f" (feels like {feels}{unit})" if feels is not None else "")
            + f", wind {current.get('wind_speed_10m')} {units.get('wind_speed_10m', 'km/h')}"
        )

        return {
            "ok": True,
            "location": place_name,
            "summary": summary,
            "conditions": description,
            "temperature": temperature,
            "feels_like": feels,
            "humidity": current.get("relative_humidity_2m"),
            "wind_speed": current.get("wind_speed_10m"),
            "precipitation": current.get("precipitation"),
            "is_day": bool(current.get("is_day", 1)),
            "unit": unit,
            "forecast": forecast,
            "sunrise": (daily.get("sunrise") or [None])[0],
            "sunset": (daily.get("sunset") or [None])[0],
        }

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
