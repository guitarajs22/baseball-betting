"""
OpenWeatherMap integration — fetches current or forecast weather for a ballpark.

Free tier: https://openweathermap.org/api (sign up, use "Current Weather" API)
Add your key to .env as:  WEATHER_API_KEY=your_key_here

All units returned in imperial (°F, mph, inHg, %).
"""
import requests
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

OWM_CURRENT_URL  = "https://api.openweathermap.org/data/2.5/weather"
OWM_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"

# Backward-compat alias
OWM_URL = OWM_CURRENT_URL


def _degrees_to_compass(deg: float) -> str:
    """Convert wind direction in degrees to a 16-point compass label."""
    directions = [
        "N", "NNE", "NE", "ENE",
        "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW",
        "W", "WNW", "NW", "NNW",
    ]
    idx = round(deg / 22.5) % 16
    return directions[idx]


def _hpa_to_inhg(hpa: float) -> float:
    """Convert hectopascals to inches of mercury."""
    return round(hpa * 0.02953, 2)


def _parse_slot(slot: dict) -> dict:
    """Parse one OWM forecast/current slot into our standard weather dict."""
    wind    = slot.get("wind", {})
    main    = slot.get("main", {})
    weather = slot.get("weather", [{}])[0]
    wind_deg = wind.get("deg", 0)
    return {
        "temperature_f":  round(main.get("temp", 72), 1),
        "wind_speed_mph": round(wind.get("speed", 0), 1),
        "wind_compass":   _degrees_to_compass(wind_deg),
        "wind_deg":       int(wind_deg),
        "humidity_pct":   round(main.get("humidity", 50), 1),
        "pressure_inhg":  _hpa_to_inhg(main.get("pressure", 1013)),
        "description":    weather.get("description", "").title(),
    }


def fetch_weather(lat: float, lon: float, api_key: str) -> dict:
    """
    Fetch current weather for a lat/lon from OpenWeatherMap.

    Returns a dict with keys:
        temperature_f   float   Current temperature in °F
        wind_speed_mph  float   Wind speed in mph
        wind_compass    str     Wind coming FROM this direction, e.g. "SW"
        wind_deg        int     Wind direction in degrees (0=N, 90=E, 180=S, 270=W)
        humidity_pct    float   Relative humidity 0–100
        pressure_inhg   float   Barometric pressure in inches of mercury
        description     str     Short text, e.g. "partly cloudy"

    Raises ValueError if the API call fails or key is missing.
    """
    if not api_key:
        raise ValueError("No WEATHER_API_KEY set. Add it to your .env file.")

    try:
        resp = requests.get(OWM_CURRENT_URL, params={
            "lat": lat,
            "lon": lon,
            "appid": api_key,
            "units": "imperial",
        }, timeout=8)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise ValueError(f"Weather API request failed: {e}")

    return _parse_slot(resp.json())


def fetch_weather_forecast(lat: float, lon: float, api_key: str,
                            target_utc: datetime) -> dict:
    """
    Fetch the forecast closest to `target_utc` for a lat/lon.

    Uses OWM's free 5-day/3-hour forecast endpoint.
    `target_utc` should be a timezone-aware or naive UTC datetime.

    Returns the same dict format as fetch_weather().
    Raises ValueError if the API call fails or no forecast slots are returned.
    """
    if not api_key:
        raise ValueError("No WEATHER_API_KEY set. Add it to your .env file.")

    try:
        resp = requests.get(OWM_FORECAST_URL, params={
            "lat": lat,
            "lon": lon,
            "appid": api_key,
            "units": "imperial",
            "cnt": 16,   # 16 slots × 3 hours = 48 hours ahead — covers tomorrow fully
        }, timeout=8)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise ValueError(f"Forecast API request failed: {e}")

    slots = resp.json().get("list", [])
    if not slots:
        raise ValueError("No forecast data returned from OWM")

    # Make target naive UTC for comparison
    if target_utc.tzinfo is not None:
        target_ts = target_utc.replace(tzinfo=None)
    else:
        target_ts = target_utc

    # Find the slot whose dt_txt is closest to target_utc
    best_slot = min(
        slots,
        key=lambda s: abs(
            (datetime.strptime(s["dt_txt"], "%Y-%m-%d %H:%M:%S") - target_ts).total_seconds()
        ),
    )

    result = _parse_slot(best_slot)
    result["forecast_time"] = best_slot["dt_txt"]   # helpful for debugging
    return result
