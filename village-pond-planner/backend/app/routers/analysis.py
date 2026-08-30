"""
Analysis router — catchment, rainfall, runoff, recommendation, and full orchestration.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Village, CandidateSite, AnalysisResult
from app.schemas.analysis import (
    CatchmentRequest, CatchmentResponse,
    RunoffRequest, RunoffResponse,
    RecommendationRequest, RecommendationResponse,
    FullAnalysisRequest, FullAnalysisResponse, SiteScore,
)
from app.services.dem_service import get_dem_for_village
from app.services.rainfall_service import fetch_rainfall
from app.geospatial.catchment import delineate_catchment
from app.geospatial.hydrology import estimate_runoff_volume, size_pond
from app.geospatial.scoring import compute_suitability_score
from app.geospatial.terrain import identify_candidate_cells
from app.geospatial.land_suitability import rank_candidates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analysis", tags=["analysis"])


# ─── POST /analysis/catchment ─────────────────────────────────────────────────

@router.post("/catchment", response_model=CatchmentResponse)
async def analysis_catchment(
    req: CatchmentRequest,
    db: AsyncSession = Depends(get_db),
):
    """Delineate the catchment polygon for a chosen outlet point."""
    village = await _get_village_or_404(req.village_id, db)
    bbox = _village_bbox(village)

    try:
        lats, lons, elevs = await get_dem_for_village(req.village_id, *bbox)
        result = delineate_catchment(lats, lons, elevs, req.lat, req.lon)
    except Exception as exc:
        logger.error("Catchment delineation failed: %s", exc)
        raise HTTPException(
            status_code=422,
            detail="Unable to calculate catchment for this point. Try another candidate site.",
        )

    return CatchmentResponse(
        site_id=req.site_id,
        area_m2=result["area_m2"],
        area_ha=result["area_ha"],
        geometry_geojson=result["geometry_geojson"],
        avg_elevation_m=result.get("avg_elevation_m"),
        slope_summary=result.get("slope_summary"),
    )


# ─── GET /rainfall ─────────────────────────────────────────────────────────────

@router.get("/rainfall")
async def analysis_rainfall(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
    start_year: int = Query(2015),
    end_year: int = Query(2023),
):
    """Fetch historical rainfall statistics for a location."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(status_code=400, detail="Invalid coordinates")
    data = await fetch_rainfall(lat, lon, start_year, end_year)
    return data


# ─── POST /analysis/runoff ─────────────────────────────────────────────────────

@router.post("/runoff", response_model=RunoffResponse)
async def analysis_runoff(req: RunoffRequest):
    """Calculate annual runoff volume using the Rational Method (V = P × A × C)."""
    try:
        result = estimate_runoff_volume(
            req.catchment_area_m2,
            req.annual_rainfall_mm,
            req.land_cover or "default",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RunoffResponse(**result)


# ─── POST /analysis/recommendation ───────────────────────────────────────────

@router.post("/recommendation", response_model=RecommendationResponse)
async def analysis_recommendation(req: RecommendationRequest):
    """Estimate pond depth, surface area, and storage capacity."""
    try:
        result = size_pond(req.runoff_volume_m3, req.desired_depth_m)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RecommendationResponse(**result)


# ─── POST /analysis/full ──────────────────────────────────────────────────────

@router.post("/full", response_model=FullAnalysisResponse)
async def analysis_full(
    req: FullAnalysisRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Orchestrate the complete analysis chain:
      DEM → terrain → candidates → catchment → rainfall → runoff → sizing → score
    """
    village = await _get_village_or_404(req.village_id, db)
    bbox = _village_bbox(village)

    # 1. DEM
    lats, lons, elevs = await get_dem_for_village(req.village_id, *bbox)

    # 2. Terrain + land score at the chosen point
    raw_candidates = identify_candidate_cells(lats, lons, elevs)
    ranked = rank_candidates(raw_candidates, max_sites=20)
    # Find the closest candidate to requested lat/lon
    best = _closest_candidate(ranked, req.site_lat, req.site_lon)
    terrain_score = best.get("terrain_score", 0.5) if best else 0.5
    land_score = best.get("land_score", 0.5) if best else 0.5
    land_cover = req.land_cover or best.get("land_status", "default") if best else "default"

    # 3. Catchment
    try:
        catchment_result = delineate_catchment(
            lats, lons, elevs, req.site_lat, req.site_lon
        )
    except Exception as exc:
        logger.error("Catchment failed in /analysis/full: %s", exc)
        raise HTTPException(
            status_code=422,
            detail="Catchment delineation failed for this point. Try another location.",
        )

    # 4. Rainfall
    centroid_lat = req.site_lat
    centroid_lon = req.site_lon
    rainfall_data = await fetch_rainfall(centroid_lat, centroid_lon)
    annual_rainfall_mm = rainfall_data["annual_mean_mm"]
    rainfall_years = rainfall_data["years"]

    # 5. Runoff
    runoff_result = estimate_runoff_volume(
        catchment_result["area_m2"],
        annual_rainfall_mm,
        land_cover,
    )

    # 6. Pond sizing
    pond_result = size_pond(runoff_result["runoff_volume_m3"])

    # 7. Suitability score
    score_result = compute_suitability_score(
        terrain_score=terrain_score,
        catchment_area_m2=catchment_result["area_m2"],
        annual_rainfall_mm=annual_rainfall_mm,
        land_score=land_score,
    )

    # 8. Persist result
    site = CandidateSite(
        village_id=req.village_id,
        lat=req.site_lat,
        lon=req.site_lon,
        elevation_m=best.get("elevation_m") if best else None,
        slope_deg=best.get("slope_deg") if best else None,
        land_status=land_cover,
        terrain_score=terrain_score,
        catchment_score=score_result["catchment_score"],
        rainfall_score=score_result["rainfall_score"],
        land_score=land_score,
        total_score=score_result["total_score"],
    )
    db.add(site)
    await db.flush()

    analysis = AnalysisResult(
        site_id=site.id,
        rainfall_annual_mm=annual_rainfall_mm,
        catchment_area_m2=catchment_result["area_m2"],
        runoff_volume_m3=runoff_result["runoff_volume_m3"],
        runoff_coeff=runoff_result["runoff_coefficient"],
        pond_depth_m=pond_result["pond_depth_m"],
        pond_surface_area_m2=pond_result["pond_surface_area_m2"],
        pond_storage_capacity_m3=pond_result["pond_storage_capacity_m3"],
        suitability_score=score_result["total_score"],
    )
    db.add(analysis)
    await db.commit()

    return FullAnalysisResponse(
        village_id=req.village_id,
        site_lat=req.site_lat,
        site_lon=req.site_lon,
        catchment=CatchmentResponse(
            site_id=site.id,
            area_m2=catchment_result["area_m2"],
            area_ha=catchment_result["area_ha"],
            geometry_geojson=catchment_result["geometry_geojson"],
            avg_elevation_m=catchment_result.get("avg_elevation_m"),
            slope_summary=catchment_result.get("slope_summary"),
        ),
        rainfall_annual_mm=annual_rainfall_mm,
        rainfall_years=rainfall_years,
        runoff=RunoffResponse(**runoff_result),
        recommendation=RecommendationResponse(**pond_result),
        score=SiteScore(**score_result),
        site_id=site.id,
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _get_village_or_404(village_id: int, db: AsyncSession) -> Village:
    result = await db.execute(select(Village).where(Village.id == village_id))
    village = result.scalar_one_or_none()
    if village is None:
        raise HTTPException(status_code=404, detail=f"Village {village_id} not found")
    return village


def _village_bbox(village: Village) -> tuple[float, float, float, float]:
    if all(
        v is not None
        for v in [village.bbox_minx, village.bbox_miny, village.bbox_maxx, village.bbox_maxy]
    ):
        return (village.bbox_miny, village.bbox_maxy, village.bbox_minx, village.bbox_maxx)
    lat = village.bbox_miny or 18.5
    lon = village.bbox_minx or 74.5
    return (lat - 0.05, lat + 0.05, lon - 0.05, lon + 0.05)


def _closest_candidate(candidates: list[dict], lat: float, lon: float) -> dict | None:
    """Return the candidate closest to (lat, lon)."""
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda c: (c["lat"] - lat) ** 2 + (c["lon"] - lon) ** 2,
    )
