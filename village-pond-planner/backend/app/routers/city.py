"""
City-based pond recommendation endpoints.

GET  /api/city/search           — geocode a city name via Nominatim (OSM)
POST /api/city/recommendations  — top-N pond sites within a city's bbox

Pipeline for recommendations:
  1. Geocode city → bbox + boundary polygon (Nominatim)
  2. Fetch DEM grid (Open Elevation, 50×50, capped)
  3. Identify + rank candidate cells (terrain + land suitability)
  4. Fetch rainfall once for city centroid (Open-Meteo)
  5. For each top-N candidate (parallel):
       a. Delineate catchment (D8 flow routing)
       b. Estimate runoff volume (V = P × A × C)
       c. Size pond (surface area = volume / depth)
       d. Compute suitability score
  6. Re-rank by suitability score (best first)
  7. Return ranked list with full geometry

Design notes (viva-ready):
  * Nominatim is used for geocoding — free, no key, powered by OSM.
    We pass a User-Agent as required by Nominatim's ToS.
  * Rainfall is fetched once for the city centroid and shared across
    candidates.  Within a city (≤ 50 km radius) ERA5 is the same grid cell.
  * catchment delineation is CPU-bound but fast at 50×50 grid (~0.1 s each).
  * asyncio.gather is used for parallel catchment processing.
  * Total timeout: 90 s (city bbox can be larger than a single polygon).
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Optional

import aiohttp
import numpy as np
from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.geospatial.catchment import delineate_catchment, _geodetic_area_m2
from app.geospatial.hydrology import estimate_runoff_volume, size_pond
from app.geospatial.land_suitability import rank_candidates
from app.geospatial.scoring import compute_suitability_score
from app.geospatial.terrain import identify_candidate_cells, fetch_dem_grid
from app.schemas.city import (
    CitySearchResult,
    CityRecommendationsRequest,
    CityRecommendationsResponse,
    SiteRecommendation,
)
from app.services.rainfall_service import fetch_rainfall

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/city", tags=["city-analysis"])

# Nominatim User-Agent (required by ToS)
_UA = "VillagePondPlanner-CSD/1.0 (student; rithika1503@gmail.com)"


# ─── GET /api/city/search ──────────────────────────────────────────────────────

@router.get("/search", response_model=CitySearchResult)
async def city_search(
    q: str = Query(..., description="City or village name"),
    state: Optional[str] = Query(default=None, description="State for disambiguation"),
):
    """
    Geocode a city/village name and return its bounding box + outline polygon.

    Uses Nominatim (OpenStreetMap) — no API key required.
    Adds ', India' as default country context. Pass `state` to disambiguate.
    """
    result = await _nominatim_geocode(q, state)
    if result is None:
        suggestion = f"{q}, {state}" if state else q
        raise HTTPException(
            status_code=404,
            detail=(
                f"Could not find '{suggestion}'. "
                "Try adding a state name (e.g. state=Chhattisgarh) "
                "or check the spelling."
            ),
        )
    return result


# ─── POST /api/city/recommendations ───────────────────────────────────────────

@router.post("/recommendations", response_model=CityRecommendationsResponse)
async def city_recommendations(req: CityRecommendationsRequest):
    """
    Find the top-N pond sites within a city's geographic bounds.

    Steps: geocode → DEM → candidates → rainfall → catchment × N → size × N → rank
    """
    async with asyncio.timeout(90):
        return await _run_city_analysis(req)


# ─── Pipeline ─────────────────────────────────────────────────────────────────

async def _run_city_analysis(req: CityRecommendationsRequest) -> CityRecommendationsResponse:

    # 1. Geocode
    geo = await _nominatim_geocode(req.city, req.state)
    if geo is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Could not find '{req.city}'. "
                "Try adding the state parameter (e.g. state='Chhattisgarh')."
            ),
        )

    bbox = geo.bbox  # {min_lat, max_lat, min_lon, max_lon}
    min_lat = bbox["min_lat"]
    max_lat = bbox["max_lat"]
    min_lon = bbox["min_lon"]
    max_lon = bbox["max_lon"]
    centroid_lat = geo.lat
    centroid_lon = geo.lon

    # Guard: reject bounding boxes that are too large (> 1° × 1° ≈ 12 000 km²)
    if (max_lat - min_lat) > 1.0 or (max_lon - min_lon) > 1.0:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{req.city}' covers too large an area for this analysis "
                "(> ~12 000 km²). Please be more specific."
            ),
        )

    # 2. DEM
    try:
        lats, lons, elevs = await fetch_dem_grid(
            min_lat, max_lat, min_lon, max_lon,
            n_points=settings.DEM_GRID_POINTS,
        )
    except Exception as exc:
        logger.error("DEM fetch failed for %s: %s", req.city, exc)
        raise HTTPException(
            status_code=503,
            detail="Could not fetch elevation data. Please try again shortly.",
        )

    # 3. Candidates
    try:
        from app.geospatial.osm_client import fetch_waterways
        osm_waterways = await fetch_waterways(min_lat, max_lat, min_lon, max_lon)
    except Exception:
        osm_waterways = None

    raw_candidates = identify_candidate_cells(lats, lons, elevs, osm_waterways=osm_waterways)
    ranked = rank_candidates(raw_candidates, max_sites=20)

    if not ranked:
        raise HTTPException(
            status_code=422,
            detail=(
                f"No suitable pond candidates found within '{req.city}'. "
                "The terrain may be too flat or too urban."
            ),
        )

    # 4. Rainfall (once, at city centroid — shared across all candidates)
    rainfall_data = await fetch_rainfall(centroid_lat, centroid_lon)
    annual_rainfall_mm = rainfall_data["annual_mean_mm"]

    # 5. Process top candidates in parallel (up to n_sites × 2 for buffer)
    top_pool = ranked[: req.n_sites * 2]
    tasks = [
        _process_candidate(
            rank_idx=i,
            cand=cand,
            lats=lats, lons=lons, elevs=elevs,
            annual_rainfall_mm=annual_rainfall_mm,
            land_cover=req.land_cover,
            desired_depth_m=req.desired_depth_m,
        )
        for i, cand in enumerate(top_pool)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Filter out failures
    valid: list[SiteRecommendation] = [
        r for r in results if isinstance(r, SiteRecommendation)
    ]

    if not valid:
        raise HTTPException(
            status_code=422,
            detail=(
                "Catchment delineation failed for all candidates. "
                "The terrain may lack sufficient relief for flow routing."
            ),
        )

    # 6. Re-rank by suitability score and take top n_sites
    valid.sort(key=lambda s: s.suitability_score, reverse=True)
    final = valid[: req.n_sites]
    for i, site in enumerate(final, 1):
        site.rank = i

    return CityRecommendationsResponse(
        city_name=req.city,
        display_name=geo.display_name,
        bbox=bbox,
        city_boundary=geo.boundary_geojson,
        recommendations=final,
        annual_rainfall_mm=round(annual_rainfall_mm, 1),
        rainfall_source=rainfall_data["source"],
        rainfall_years=rainfall_data["years"],
    )


async def _process_candidate(
    rank_idx: int,
    cand: dict,
    lats: np.ndarray,
    lons: np.ndarray,
    elevs: np.ndarray,
    annual_rainfall_mm: float,
    land_cover: str,
    desired_depth_m: float,
) -> SiteRecommendation:
    """
    Full pipeline for a single candidate: catchment → runoff → sizing → score.
    Runs in an executor thread for CPU-bound D8 work.
    """
    pond_lat = cand["lat"]
    pond_lon = cand["lon"]
    terrain_score = cand.get("terrain_score", 0.5)
    land_score = cand.get("land_score", 0.5)

    loop = asyncio.get_event_loop()

    # Catchment delineation is CPU-bound — run in thread pool
    catchment = await loop.run_in_executor(
        None,
        delineate_catchment, lats, lons, elevs, pond_lat, pond_lon,
    )

    area_m2 = catchment["area_m2"]
    catchment_geojson = catchment["geometry_geojson"]

    # Runoff
    runoff = estimate_runoff_volume(area_m2, annual_rainfall_mm, land_cover)
    if runoff["runoff_volume_m3"] <= 0:
        raise ValueError("zero runoff")

    # Pond sizing
    pond = size_pond(runoff["runoff_volume_m3"], desired_depth_m)

    # Score
    score = compute_suitability_score(terrain_score, area_m2, land_score)

    # Pond footprint
    pond_geom = _circle_geojson(pond_lat, pond_lon, pond["pond_surface_area_m2"])

    return SiteRecommendation(
        rank=rank_idx + 1,          # will be re-ranked after gather
        lat=round(pond_lat, 7),
        lon=round(pond_lon, 7),
        elevation_m=round(cand.get("elevation_m", 0.0), 2),
        slope_deg=round(cand.get("slope_deg", 0.0), 3),
        land_status=cand.get("land_status", "unknown"),
        terrain_score=round(terrain_score, 3),
        catchment_geometry=catchment_geojson,
        catchment_area_ha=round(area_m2 / 10_000, 2),
        annual_rainfall_mm=round(annual_rainfall_mm, 1),
        runoff_volume_m3=runoff["runoff_volume_m3"],
        runoff_coefficient=runoff["runoff_coefficient"],
        pond_storage_capacity_m3=pond["pond_storage_capacity_m3"],
        pond_surface_area_m2=pond["pond_surface_area_m2"],
        pond_depth_m=pond["pond_depth_m"],
        pond_geometry=pond_geom,
        suitability_score=round(score["total_score"], 3),
    )


# ─── Geocoding (multi-provider with fallbacks) ───────────────────────────────

_GEO_CACHE: Dict[str, CitySearchResult] = {}

async def _nominatim_geocode(city: str, state: Optional[str]) -> Optional[CitySearchResult]:
    """
    Geocode city with fallback chain:
      1. Nominatim (openstreetmap.org)
      2. photon.komoot.io  (OSM-based, no key, usually unblocked)
    Returns None only if all providers fail to find the city.
    """
    cache_key = f"{city}_{state}".lower()
    if cache_key in _GEO_CACHE:
        return _GEO_CACHE[cache_key]

    for attempt in (_try_nominatim, _try_photon):
        try:
            result = await attempt(city, state)
            if result is not None:
                _GEO_CACHE[cache_key] = result
                return result
        except Exception as exc:
            logger.warning("Geocoder attempt failed: %s", exc)
    return None


async def _try_nominatim(city: str, state: Optional[str]) -> Optional[CitySearchResult]:
    """Nominatim (OSM). Requires User-Agent header."""
    query_parts = [city]
    if state:
        query_parts.append(state)
    query_parts.append("India")
    query = ", ".join(query_parts)

    params = {
        "q": query, "format": "json", "limit": 5,
        "polygon_geojson": 1, "addressdetails": 1,
    }
    headers = {"User-Agent": _UA}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
        async with session.get(
            "https://nominatim.openstreetmap.org/search",
            params=params, headers=headers,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

    return _parse_nominatim_results(data, city, state)


async def _try_photon(city: str, state: Optional[str]) -> Optional[CitySearchResult]:
    """
    Photon (komoot.io) — OSM-based geocoder, returns GeoJSON FeatureCollection.

    We bias results toward India's centroid (lat=20, lon=77) so that
    ambiguous names like "Hyderabad" (exists in both India and Pakistan)
    resolve to the Indian city.
    """
    q = f"{city}, {state}" if state else city
    params = {
        "q": q,
        "limit": 8,
        "lang": "en",
        "lat": 20.0,    # India centroid bias
        "lon": 77.0,
    }
    headers = {"User-Agent": _UA}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
        async with session.get(
            "https://photon.komoot.io/api/",
            params=params, headers=headers,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

    features = data.get("features", [])
    if not features:
        return None

    def _is_india(f: dict) -> bool:
        """Check country field OR coordinate bounds for India."""
        props = f.get("properties", {})
        country = props.get("country", "")
        if country:
            return "India" in country
        # Fallback: rough bounding box for India
        coords = f.get("geometry", {}).get("coordinates", [0, 0])
        lon_f, lat_f = float(coords[0]), float(coords[1])
        return 6.5 <= lat_f <= 37.0 and 68.0 <= lon_f <= 97.5

    PREFERRED_TYPES = {
        "city", "town", "village", "municipality", "district",
        "administrative", "suburb", "hamlet", "county", "state_district",
    }

    # Priority 1: Indian city/town with matching type
    best = None
    for f in features:
        if _is_india(f) and f.get("properties", {}).get("osm_value", "") in PREFERRED_TYPES:
            best = f
            break

    # Priority 2: Any Indian result
    if best is None:
        for f in features:
            if _is_india(f):
                best = f
                break

    # Priority 3: First result (shouldn't reach here)
    if best is None:
        best = features[0]

    props = best.get("properties", {})
    coords = best["geometry"]["coordinates"]   # [lon, lat]
    lon_c, lat_c = float(coords[0]), float(coords[1])

    # Wider bbox for large cities (city ~15 km, town ~8 km, default ~5 km)
    osm_val = props.get("osm_value", "")
    if osm_val == "city":
        offset = 0.15
    elif osm_val in ("town", "municipality", "district"):
        offset = 0.08
    else:
        offset = 0.05

    bbox = {
        "min_lat": lat_c - offset, "max_lat": lat_c + offset,
        "min_lon": lon_c - offset, "max_lon": lon_c + offset,
    }

    display = ", ".join(filter(None, [
        props.get("name", city),
        props.get("state", state or ""),
        props.get("country", "India"),
    ]))

    return CitySearchResult(
        display_name=display,
        city=city,

        state=props.get("state", state),
        lat=lat_c,
        lon=lon_c,
        bbox=bbox,
        boundary_geojson=None,   # photon doesn't return polygons
    )


def _parse_nominatim_results(data: list, city: str, state: Optional[str]) -> Optional[CitySearchResult]:
    """Parse a Nominatim JSON response into CitySearchResult."""
    if not data:
        return None

    best = None
    for item in data:
        cls = item.get("class", "")
        typ = item.get("type", "")
        if cls in ("boundary", "place") and typ in (
            "administrative", "city", "town", "village",
            "municipality", "district",
        ):
            best = item
            break
    if best is None:
        best = data[0]

    bb = best.get("boundingbox", [])
    if len(bb) == 4:
        bbox = {
            "min_lat": float(bb[0]), "max_lat": float(bb[1]),
            "min_lon": float(bb[2]), "max_lon": float(bb[3]),
        }
    else:
        lat = float(best["lat"]); lon = float(best["lon"])
        bbox = {"min_lat": lat-.05, "max_lat": lat+.05, "min_lon": lon-.05, "max_lon": lon+.05}

    addr = best.get("address", {})
    return CitySearchResult(
        display_name=best.get("display_name", city),
        city=city,
        state=addr.get("state", state or ""),
        lat=float(best["lat"]),
        lon=float(best["lon"]),
        bbox=bbox,
        boundary_geojson=best.get("geojson"),
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _circle_geojson(lat: float, lon: float, area_m2: float, n_pts: int = 33) -> dict:
    """GeoJSON Polygon circle approximating a pond footprint."""
    radius_m = math.sqrt(area_m2 / math.pi)
    rad_lat = radius_m / 111_320.0
    rad_lon = radius_m / (111_320.0 * math.cos(math.radians(lat)))
    coords = []
    for i in range(n_pts):
        angle = math.pi * 2 * i / (n_pts - 1)
        coords.append([lon + rad_lon * math.cos(angle), lat + rad_lat * math.sin(angle)])
    return {"type": "Polygon", "coordinates": [coords]}
