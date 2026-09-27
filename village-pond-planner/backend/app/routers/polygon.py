"""
POST /api/analyze-polygon
=========================

Accepts a GeoJSON Polygon (drawn by the user on the map) and returns:
  - Suggested pond location (lat/lon + footprint polygon)
  - Catchment area (user-drawn polygon OR auto D8-delineated)
  - Expected water volume (Rational Method: V = P × A × C)

All sourced from existing geospatial modules — no new math, just a new
entry-point that accepts map geometry instead of a KML file.

Design notes (viva-ready):
  * Stateless — no DB writes.
  * DEM is fetched from Open Elevation (batch ≤ 512 pts; 50×50 grid).
  * Rainfall comes from Open-Meteo archive (no API key).
  * If the user draws a catchment polygon, D8 delineation is skipped
    (faster + gives the user full control).
  * Grid capped at 50×50 = 2500 pts to bound latency.
  * asyncio.timeout(45) applied to the whole handler for resilience.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Optional

import numpy as np
from fastapi import APIRouter, HTTPException

from app.schemas.polygon import PolygonAnalysisRequest, PolygonAnalysisResponse
from app.services.rainfall_service import fetch_rainfall
from app.geospatial.catchment import delineate_catchment, _geodetic_area_m2
from app.geospatial.hydrology import estimate_runoff_volume, size_pond
from app.geospatial.land_suitability import rank_candidates
from app.geospatial.scoring import compute_suitability_score
from app.geospatial.terrain import identify_candidate_cells, fetch_dem_grid
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["polygon-analysis"])


# ─── Main endpoint ────────────────────────────────────────────────────────────

@router.post("/analyze-polygon", response_model=PolygonAnalysisResponse)
async def analyze_polygon(req: PolygonAnalysisRequest):
    """
    Full pond-planning analysis driven by a user-drawn map polygon.

    Pipeline:
      1. Validate + parse GeoJSON land polygon → bbox
      2. Fetch DEM grid (Open Elevation, 50×50)
      3. Identify + rank candidate pond cells inside polygon
      4. Catchment: use user polygon if provided, else D8-delineate
      5. Fetch rainfall (Open-Meteo, centroid lat/lon)
      6. Estimate runoff volume V = P × A × C
      7. Size pond (Surface Area = Volume / Depth)
      8. Compute suitability score
      9. Build + return response
    """
    async with asyncio.timeout(45):
        return await _run_analysis(req)


# ─── Pipeline ─────────────────────────────────────────────────────────────────

async def _run_analysis(req: PolygonAnalysisRequest) -> PolygonAnalysisResponse:
    # ── 1. Parse land polygon ────────────────────────────────────────────────
    try:
        land_geom = _parse_polygon(req.land_polygon)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid land_polygon: {exc}")

    bbox = _geom_bbox(land_geom)           # (min_lat, max_lat, min_lon, max_lon)
    centroid = land_geom.centroid
    centroid_lat, centroid_lon = centroid.y, centroid.x

    # Validate size — cap extremely large polygons to avoid timeouts
    _check_polygon_area(land_geom)

    # ── 2. Fetch DEM ─────────────────────────────────────────────────────────
    try:
        lats, lons, elevs = await fetch_dem_grid(*bbox, n_points=settings.DEM_GRID_POINTS)
    except Exception as exc:
        logger.error("DEM fetch failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Could not fetch elevation data. Please try again in a moment.",
        )

    # ── 3. Candidate identification ──────────────────────────────────────────
    try:
        from app.geospatial.osm_client import fetch_waterways
        osm_waterways = await fetch_waterways(*bbox)
    except Exception:
        osm_waterways = None  # OSM is optional; proceed without it

    raw_candidates = identify_candidate_cells(lats, lons, elevs, osm_waterways=osm_waterways)

    # Filter to candidates inside the drawn land polygon
    inside = [c for c in raw_candidates if land_geom.contains(_point(c["lat"], c["lon"]))]
    if not inside:
        # Fallback: use all candidates (polygon may be tiny)
        inside = raw_candidates

    ranked = rank_candidates(inside, max_sites=settings.MAX_CANDIDATE_SITES)

    if not ranked and not inside:
        raise HTTPException(
            status_code=422,
            detail=(
                "No suitable pond candidates found within the selected area. "
                "Try a larger polygon or a different location."
            ),
        )

    best = ranked[0] if ranked else inside[0]
    pond_lat = best["lat"]
    pond_lon = best["lon"]
    terrain_score = best.get("terrain_score", 0.5)
    land_score = best.get("land_score", 0.5)

    # ── 4. Catchment ─────────────────────────────────────────────────────────
    catchment_source = "d8-delineated"
    if req.catchment_polygon:
        # User supplied a custom catchment — skip D8
        try:
            catch_geom = _parse_polygon(req.catchment_polygon)
            area_m2 = _geodetic_area_m2(catch_geom)
            area_ha = area_m2 / 10_000.0
            from shapely.geometry import mapping
            catchment_geojson = mapping(catch_geom)
            catchment_source = "user-drawn"
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid catchment_polygon: {exc}",
            )
    else:
        try:
            catchment_result = delineate_catchment(lats, lons, elevs, pond_lat, pond_lon)
            area_m2 = catchment_result["area_m2"]
            area_ha = catchment_result["area_ha"]
            catchment_geojson = catchment_result["geometry_geojson"]
        except Exception as exc:
            logger.error("Catchment delineation failed: %s", exc)
            raise HTTPException(
                status_code=422,
                detail=(
                    "Catchment delineation failed — the terrain may be too flat. "
                    "Try drawing a manual catchment polygon instead."
                ),
            )

    # ── 5. Rainfall ──────────────────────────────────────────────────────────
    rainfall_data = await fetch_rainfall(pond_lat, pond_lon)
    annual_rainfall_mm = rainfall_data["annual_mean_mm"]

    # ── 6. Runoff ─────────────────────────────────────────────────────────────
    runoff = estimate_runoff_volume(area_m2, annual_rainfall_mm, req.land_cover)

    # ── 7. Pond sizing ────────────────────────────────────────────────────────
    if runoff["runoff_volume_m3"] <= 0:
        raise HTTPException(
            status_code=422,
            detail="Runoff volume is zero — check catchment area and rainfall.",
        )
    pond = size_pond(runoff["runoff_volume_m3"], req.desired_depth_m)

    # ── 8. Suitability ────────────────────────────────────────────────────────
    score = compute_suitability_score(
        terrain_score=terrain_score,
        catchment_area_m2=area_m2,
        land_score=land_score,
    )

    # ── 9. Build pond footprint polygon ──────────────────────────────────────
    pond_geometry = _circle_geojson(pond_lat, pond_lon, pond["pond_surface_area_m2"])

    return PolygonAnalysisResponse(
        pond_location={
            "lat": round(pond_lat, 7),
            "lon": round(pond_lon, 7),
            "elevation_m": round(best.get("elevation_m", 0.0), 2),
            "slope_deg": round(best.get("slope_deg", 0.0), 3),
            "land_status": best.get("land_status", "unknown"),
        },
        pond_geometry=pond_geometry,
        catchment_geometry=catchment_geojson,
        catchment_area_m2=round(area_m2, 1),
        catchment_area_ha=round(area_ha, 2),
        annual_rainfall_mm=round(annual_rainfall_mm, 1),
        rainfall_years=rainfall_data["years"],
        rainfall_source=rainfall_data["source"],
        runoff_volume_m3=runoff["runoff_volume_m3"],
        runoff_coefficient=runoff["runoff_coefficient"],
        pond_depth_m=pond["pond_depth_m"],
        pond_surface_area_m2=pond["pond_surface_area_m2"],
        pond_storage_capacity_m3=pond["pond_storage_capacity_m3"],
        suitability_score=round(score["total_score"], 3),
        catchment_source=catchment_source,
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_polygon(geojson: dict):
    """Parse a GeoJSON Polygon dict → Shapely geometry."""
    from shapely.geometry import shape
    geom = shape(geojson)
    if geom.is_empty:
        raise ValueError("Polygon is empty")
    return geom


def _geom_bbox(geom) -> tuple[float, float, float, float]:
    """Return (min_lat, max_lat, min_lon, max_lon) for a Shapely geometry."""
    minx, miny, maxx, maxy = geom.bounds  # minx=minlon, miny=minlat
    return (miny, maxy, minx, maxx)


def _point(lat: float, lon: float):
    from shapely.geometry import Point
    return Point(lon, lat)


def _check_polygon_area(geom) -> None:
    """Reject polygons covering more than ~10 000 km² to prevent timeouts."""
    from app.geospatial.catchment import _geodetic_area_m2
    area = _geodetic_area_m2(geom)
    if area > 10_000 * 1_000_000:  # 10 000 km² in m²
        raise HTTPException(
            status_code=422,
            detail="Selected area is too large (> 10 000 km²). Please draw a smaller polygon.",
        )


def _circle_geojson(lat: float, lon: float, area_m2: float, n_pts: int = 33) -> dict:
    """Build a GeoJSON Polygon circle approximating a pond footprint."""
    radius_m = math.sqrt(area_m2 / math.pi)
    rad_lat = radius_m / 111_320.0
    rad_lon = radius_m / (111_320.0 * math.cos(math.radians(lat)))
    coords = []
    for i in range(n_pts):
        angle = math.pi * 2 * i / (n_pts - 1)
        coords.append([lon + rad_lon * math.cos(angle), lat + rad_lat * math.sin(angle)])
    return {"type": "Polygon", "coordinates": [coords]}
