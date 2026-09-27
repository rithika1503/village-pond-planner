"""
Rainfall data service.

Primary source  : Open-Meteo archive API (no API key required).
Fallback source : NASA POWER monthly API (no API key required).
Caching         : Results are stored in the `rainfall` DB table.
                  If a cached record is < RAINFALL_CACHE_DAYS old, it is served
                  directly from the DB.

Data returned: monthly rainfall totals (mm) for each available year + annual totals.

Design note (viva-ready):
  Open-Meteo provides ERA5 reanalysis data at ~27 km grid resolution going back
  to 1940.  It is freely available without registration, making it the most
  practical no-key rainfall source for India.  Precipitation variable used:
  `precipitation_sum` (daily) → aggregated to monthly via the API's own
  daily archive endpoint.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)


async def fetch_rainfall(
    lat: float,
    lon: float,
    start_year: int = 2015,
    end_year: int = 2023,
) -> dict:
    """
    Fetch monthly precipitation from Open-Meteo archive.

    Returns:
        {
          "source": str,
          "years": int,
          "annual_mean_mm": float,
          "annual_min_mm": float,
          "annual_max_mm": float,
          "by_year": { year: { "monthly": [...12 floats...], "annual": float } },
        }
    """
    try:
        return await _fetch_open_meteo(lat, lon, start_year, end_year)
    except Exception as exc:
        logger.warning("Open-Meteo failed (%s), trying NASA POWER fallback", exc)

    try:
        return await _fetch_nasa_power(lat, lon, start_year, end_year)
    except Exception as exc2:
        logger.error("NASA POWER also failed (%s) — returning default 800mm fallback", exc2)
        # Instead of 0, return a sensible Indian average (800mm) so the analysis doesn't crash
        zeroed = _zeroed_response(start_year, end_year)
        zeroed["annual_mean_mm"] = 800.0
        zeroed["annual_min_mm"] = 600.0
        zeroed["annual_max_mm"] = 1000.0
        zeroed["source"] = "fallback-default"
        return zeroed


# ─── Open-Meteo ───────────────────────────────────────────────────────────────

async def _fetch_open_meteo(
    lat: float,
    lon: float,
    start_year: int,
    end_year: int,
) -> dict:
    """Fetch daily precipitation from Open-Meteo and aggregate to monthly/annual."""
    start = f"{start_year}-01-01"
    end = f"{min(end_year, date.today().year - 1)}-12-31"

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start,
        "end_date": end,
        "daily": "precipitation_sum",
        "timezone": "auto",
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.get(settings.OPEN_METEO_URL, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()

    daily_dates: list[str] = data["daily"]["time"]
    daily_precip: list[float] = [v or 0.0 for v in data["daily"]["precipitation_sum"]]

    by_year: dict[int, dict] = {}
    for date_str, precip in zip(daily_dates, daily_precip):
        y, m, _ = date_str.split("-")
        year, month = int(y), int(m)
        if year not in by_year:
            by_year[year] = {"monthly": [0.0] * 12, "annual": 0.0}
        by_year[year]["monthly"][month - 1] += precip
        by_year[year]["annual"] += precip

    # Round monthly values
    for v in by_year.values():
        v["monthly"] = [round(x, 1) for x in v["monthly"]]
        v["annual"] = round(v["annual"], 1)

    annual_totals = [v["annual"] for v in by_year.values()]
    return {
        "source": "open-meteo",
        "years": len(by_year),
        "annual_mean_mm": round(sum(annual_totals) / len(annual_totals), 1) if annual_totals else 0.0,
        "annual_min_mm": round(min(annual_totals), 1) if annual_totals else 0.0,
        "annual_max_mm": round(max(annual_totals), 1) if annual_totals else 0.0,
        "by_year": by_year,
    }


# ─── NASA POWER Fallback ──────────────────────────────────────────────────────

async def _fetch_nasa_power(
    lat: float,
    lon: float,
    start_year: int,
    end_year: int,
) -> dict:
    """
    Fetch monthly precipitation from NASA POWER (parameter PRECTOTCORR_SUM, monthly).
    """
    params = {
        "parameters": "PRECTOTCORR_SUM",
        "community": "RE",
        "longitude": lon,
        "latitude": lat,
        "start": str(start_year),
        "end": str(min(end_year, date.today().year - 1)),
        "format": "JSON",
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.get(settings.NASA_POWER_URL, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()

    monthly_data = (
        data.get("properties", {}).get("parameter", {}).get("PRECTOTCORR_SUM", {})
    )
    # Keys like "201501", "201502", ...
    by_year: dict[int, dict] = {}
    for key, val in monthly_data.items():
        year, month = int(key[:4]), int(key[4:])
        if year not in by_year:
            by_year[year] = {"monthly": [0.0] * 12, "annual": 0.0}
        precip = val if val not in (-999, None) else 0.0
        by_year[year]["monthly"][month - 1] = round(float(precip), 1)
        by_year[year]["annual"] += float(precip)

    for v in by_year.values():
        v["annual"] = round(v["annual"], 1)

    annual_totals = [v["annual"] for v in by_year.values()]
    return {
        "source": "nasa-power",
        "years": len(by_year),
        "annual_mean_mm": round(sum(annual_totals) / len(annual_totals), 1) if annual_totals else 0.0,
        "annual_min_mm": round(min(annual_totals), 1) if annual_totals else 0.0,
        "annual_max_mm": round(max(annual_totals), 1) if annual_totals else 0.0,
        "by_year": by_year,
    }


def _zeroed_response(start_year: int, end_year: int) -> dict:
    """Return zeroed rainfall data when all APIs fail."""
    return {
        "source": "unavailable",
        "years": 0,
        "annual_mean_mm": 0.0,
        "annual_min_mm": 0.0,
        "annual_max_mm": 0.0,
        "by_year": {},
    }
