"""
Villages router — village search, boundary retrieval, DEM/contours, candidate sites.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Village, CandidateSite
from app.schemas.village import VillageSummary, VillageDetail
from app.services.dem_service import get_dem_for_village
from app.geospatial.terrain import generate_contours_geojson, identify_candidate_cells
from app.geospatial.land_suitability import rank_candidates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/villages", tags=["villages"])


# ─── GET /villages ─────────────────────────────────────────────────────────────

@router.get("/", response_model=list[VillageSummary])
async def list_villages(
    q: Optional[str] = Query(None, description="Search by village name (case-insensitive)"),
    state: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Search and list villages."""
    stmt = select(Village)
    if q:
        stmt = stmt.where(Village.name.ilike(f"%{q}%"))
    if state:
        stmt = stmt.where(Village.state.ilike(f"%{state}%"))
    stmt = stmt.offset(offset).limit(limit).order_by(Village.name)
    result = await db.execute(stmt)
    villages = result.scalars().all()
    return [_village_to_summary(v) for v in villages]


# ─── GET /villages/{id} ────────────────────────────────────────────────────────

@router.get("/{village_id}", response_model=VillageDetail)
async def get_village(village_id: int, db: AsyncSession = Depends(get_db)):
    """Return village metadata + boundary GeoJSON."""
    village = await _get_or_404(village_id, db)
    detail = VillageDetail(**_village_to_summary(village).__dict__)
    if village.boundary is not None:
        try:
            from geoalchemy2.shape import to_shape
            from shapely.geometry import mapping
            detail.boundary_geojson = mapping(to_shape(village.boundary))
        except Exception:
            detail.boundary_geojson = None
    return detail


# ─── GET /villages/{id}/elevation ─────────────────────────────────────────────

@router.get("/{village_id}/elevation")
async def get_elevation(
    village_id: int,
    contour_interval: float = Query(10.0, description="Contour interval in metres"),
    db: AsyncSession = Depends(get_db),
):
    """Return DEM grid stats and contour lines as GeoJSON."""
    village = await _get_or_404(village_id, db)
    bbox = _village_bbox(village)

    lats, lons, elevs = await get_dem_for_village(village_id, *bbox)
    contours = generate_contours_geojson(lats, lons, elevs, interval_m=contour_interval)

    return {
        "village_id": village_id,
        "elevation_stats": {
            "min_m": float(elevs.min()),
            "max_m": float(elevs.max()),
            "mean_m": float(elevs.mean()),
        },
        "contours": contours,
    }


# ─── GET /villages/{id}/candidate-sites ───────────────────────────────────────

@router.get("/{village_id}/candidate-sites")
async def get_candidate_sites(
    village_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return top candidate pond sites for the village."""
    village = await _get_or_404(village_id, db)
    bbox = _village_bbox(village)

    lats, lons, elevs = await get_dem_for_village(village_id, *bbox)
    raw_candidates = identify_candidate_cells(lats, lons, elevs)
    ranked = rank_candidates(raw_candidates)

    # Build GeoJSON FeatureCollection
    features = []
    for i, c in enumerate(ranked, start=1):
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [c["lon"], c["lat"]],
            },
            "properties": {
                "rank": i,
                "elevation_m": c["elevation_m"],
                "slope_deg": c["slope_deg"],
                "terrain_score": c["terrain_score"],
                "land_score": c["land_score"],
                "land_status": c["land_status"],
            },
        })

    return {
        "village_id": village_id,
        "candidate_count": len(ranked),
        "sites": {"type": "FeatureCollection", "features": features},
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _get_or_404(village_id: int, db: AsyncSession) -> Village:
    result = await db.execute(select(Village).where(Village.id == village_id))
    village = result.scalar_one_or_none()
    if village is None:
        raise HTTPException(status_code=404, detail=f"Village {village_id} not found")
    return village


def _village_bbox(village: Village) -> tuple[float, float, float, float]:
    """Return (min_lat, max_lat, min_lon, max_lon)."""
    if all(
        v is not None
        for v in [village.bbox_minx, village.bbox_miny, village.bbox_maxx, village.bbox_maxy]
    ):
        return (
            village.bbox_miny,
            village.bbox_maxy,
            village.bbox_minx,
            village.bbox_maxx,
        )
    # Fallback: 0.1° box around centroid
    lat = village.bbox_miny or 18.5
    lon = village.bbox_minx or 74.5
    return (lat - 0.05, lat + 0.05, lon - 0.05, lon + 0.05)


def _village_to_summary(v: Village) -> VillageSummary:
    centroid_lat, centroid_lon = None, None
    if v.centroid is not None:
        try:
            from geoalchemy2.shape import to_shape
            pt = to_shape(v.centroid)
            centroid_lon = pt.x
            centroid_lat = pt.y
        except Exception:
            pass

    bbox = None
    if all(x is not None for x in [v.bbox_minx, v.bbox_miny, v.bbox_maxx, v.bbox_maxy]):
        bbox = [v.bbox_minx, v.bbox_miny, v.bbox_maxx, v.bbox_maxy]

    return VillageSummary(
        id=v.id,
        name=v.name,
        district=v.district,
        state=v.state,
        population=v.population,
        centroid_lat=centroid_lat,
        centroid_lon=centroid_lon,
        bbox=bbox,
    )
