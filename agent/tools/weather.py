"""
Weather Tool - Get weather information using wttr.in API.

Free, no API key required. Supports Chinese city names.
"""

from __future__ import annotations

import logging

import requests

from agent.tools.base import Tool, ToolResult

logger = logging.getLogger("agent.tools.weather")


class WeatherTool(Tool):
    """
    Get current weather and forecast for a location.

    Uses wttr.in API (free, no key needed).
    Supports Chinese and English city names.
    """

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "curl/7.68.0"})

    @property
    def name(self) -> str:
        return "get_weather"

    @property
    def description(self) -> str:
        return (
            "Get weather information for a location. Returns current weather "
            "and 3-day forecast including temperature, humidity, wind, and conditions. "
            "Supports Chinese city names (e.g. '深圳', '北京', '肇庆德庆')."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City name in Chinese or English (e.g. '肇庆', 'Shenzhen')",
                },
            },
            "required": ["location"],
        }

    def execute(self, location: str, **kwargs) -> ToolResult:
        """Get weather data from wttr.in."""
        if not location.strip():
            return ToolResult(success=False, error="Location cannot be empty")

        try:
            resp = self._session.get(
                f"https://wttr.in/{location}",
                params={"format": "j1"},
                timeout=15,
            )

            if resp.status_code != 200:
                return ToolResult(
                    success=False,
                    error=f"Weather API returned status {resp.status_code}",
                )

            data = resp.json()
            current = data.get("current_condition", [{}])[0]
            weather_days = data.get("weather", [])

            # Build current weather
            current_info = {
                "temp_c": current.get("temp_C", ""),
                "feels_like_c": current.get("FeelsLikeC", ""),
                "humidity": current.get("humidity", ""),
                "wind_speed_kmph": current.get("windspeedKmph", ""),
                "wind_dir": current.get("winddir16Point", ""),
                "visibility_km": current.get("visibility", ""),
                "uv_index": current.get("uvIndex", ""),
            }

            # Get Chinese description
            lang_zh = current.get("lang_zh", [])
            if lang_zh:
                current_info["description"] = lang_zh[0].get("value", "")
            else:
                desc_list = current.get("weatherDesc", [])
                if desc_list:
                    current_info["description"] = desc_list[0].get("value", "")

            # Build forecast
            forecast = []
            for day in weather_days[:3]:
                date = day.get("date", "")
                max_temp = day.get("maxtempC", "")
                min_temp = day.get("mintempC", "")

                # Get hourly details for the day
                hourly = day.get("hourly", [])
                desc = ""
                rain_chance = ""
                if hourly:
                    # Use midday (12:00) as representative
                    midday = hourly[4] if len(hourly) > 4 else hourly[0]
                    desc_list = midday.get("weatherDesc", [])
                    if desc_list:
                        desc = desc_list[0].get("value", "")
                    rain_chance = midday.get("chanceofrain", "")

                forecast.append({
                    "date": date,
                    "temp_range": f"{min_temp}~{max_temp}°C",
                    "description": desc,
                    "rain_chance": f"{rain_chance}%" if rain_chance else "",
                })

            output = {
                "location": location,
                "current": current_info,
                "forecast": forecast,
            }

            return ToolResult(
                success=True,
                output=output,
                metadata={"source": "wttr.in"},
            )

        except requests.exceptions.Timeout:
            return ToolResult(success=False, error="Weather API request timed out")
        except Exception as e:
            logger.exception("Weather tool failed")
            return ToolResult(success=False, error=f"Weather query failed: {e}")
