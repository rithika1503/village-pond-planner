"""
Contour Upload Router
=====================

POST /analyzeContour  — accepts a KML or KMZ contour map file and returns
                        comprehensive catchment and pond-planning information.

POST /findCatchment   — alias for /analyzeContour (same handler).

Pipeline (entirely file-driven, no hard-coded coordinates):
  1. Parse KML/KMZ → contour lines
  2. Interpolate contour lines → DEM grid (scipy griddata)
  3. Identify candidate pond locations (slope + elevation filter)
  4. Rank candidates (terrain + land suitability scores)
  5. Delineate catchment via D8 flow direction (pysheds)
  6. Fetch rainfall for centroid via Open-Meteo (or use provided value)
  7. Estimate runoff volume (Rational Method: V = P × A × C)
  8. Size the pond (surface area = volume / depth)
  9. Compute overall suitability score
 10. Return structured JSON

Design notes (viva-ready)
--------------------------
* The endpoint is fully STATELESS — no database writes, no village lookup.
  All inputs come from the uploaded file + optional query parameters.
* The KML parser (geospatial/kml_parser.py) is format-agnostic: it reads
  contour elevations from the <name> element and coordinates from any
  <LineString> or <Polygon> geometry.
* All downstream geospatial modules are reused without modification.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.geospatial.kml_parser import (
    contours_to_dem,
    get_bounding_box,
    get_elevation_stats,
    parse_kml_bytes,
)
from app.geospatial.catchment import delineate_catchment
from app.geospatial.hydrology import estimate_runoff_volume, size_pond
from app.geospatial.land_suitability import rank_candidates
from app.geospatial.scoring import compute_suitability_score
from app.geospatial.terrain import identify_candidate_cells
from app.schemas.analysis import ContourUploadResponse
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["contour"])

# ─── Shared handler ────────────────────────────────────────────────────────────

async def _analyze_contour(
    contour_map: UploadFile,
    land_cover: str,
    desired_depth_m: float,
    annual_rainfall_mm: Optional[float],
) -> ContourUploadResponse:
    """
    Core pipeline: parse KML/KMZ → derive terrain → catchment → pond sizing.
    """
    # ── 1. Read file ──────────────────────────────────────────────────────────
    raw = await contour_map.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # ── 2. Parse contour lines ────────────────────────────────────────────────
    try:
        contour_lines = parse_kml_bytes(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if len(contour_lines) < 3:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Only {len(contour_lines)} contour line(s) found. "
                "At least 3 are required to interpolate a DEM."
            ),
        )

    # ── 3. Interpolate contour lines → DEM grid ───────────────────────────────
    try:
        lats, lons, elevs = contours_to_dem(contour_lines, n_points=settings.DEM_GRID_POINTS)
    except Exception as exc:
        logger.exception("DEM interpolation failed")
        raise HTTPException(status_code=500, detail=f"DEM interpolation failed: {exc}")

    # ── 4. Terrain metadata ───────────────────────────────────────────────────
    bbox = get_bounding_box(contour_lines)
    elev_stats = get_elevation_stats(contour_lines)
    centroid_lat = (bbox["min_lat"] + bbox["max_lat"]) / 2
    centroid_lon = (bbox["min_lon"] + bbox["max_lon"]) / 2

    # ── 5. Candidate Identification ───────────────────────────────────────────
    try:
        from app.geospatial.osm_client import fetch_waterways
        osm_waterways = await fetch_waterways(bbox["min_lat"], bbox["max_lat"], bbox["min_lon"], bbox["max_lon"])
        
        raw_candidates = identify_candidate_cells(lats, lons, elevs, osm_waterways=osm_waterways)
        if not raw_candidates:
            # Relax slope/elevation thresholds but still honour the river-exclusion rule.
            import numpy as np
            from app.geospatial.terrain import compute_flow_accumulation, is_stream_channel

            accum = compute_flow_accumulation(elevs)
            stream_mask = is_stream_channel(accum, settings.STREAM_ACCUM_PERCENTILE)
            
            # Reapply OSM mask to relaxed candidates as well!
            if osm_waterways:
                from app.geospatial.terrain import mask_osm_waterways
                osm_mask = mask_osm_waterways(osm_waterways, lats, lons)
                stream_mask = stream_mask | osm_mask

            elev_p40 = float(np.percentile(elevs, 40))

            raw_candidates = [
                {
                    "lat": float(lats[i]),
                    "lon": float(lons[j]),
                    "elevation_m": float(elevs[i, j]),
                    "slope_deg": 0.0,
                    "flow_accum": int(accum[i, j]),
                    "is_stream": bool(stream_mask[i, j]),
                }
                for i in range(len(lats))
                for j in range(len(lons))
                # low-elevation AND not inside a river channel
                if elevs[i, j] <= elev_p40 and not stream_mask[i, j]
            ]

            if not raw_candidates:
                # Absolute last resort: pick the single lowest-elevation non-stream cell
                raw_candidates = [
                    {
                        "lat": float(lats[i]),
                        "lon": float(lons[j]),
                        "elevation_m": float(elevs[i, j]),
                        "slope_deg": 0.0,
                        "flow_accum": int(accum[i, j]),
                        "is_stream": bool(stream_mask[i, j]),
                    }
                    for i in range(len(lats))
                    for j in range(len(lons))
                    if not stream_mask[i, j]
                ]
                if raw_candidates:
                    raw_candidates.sort(key=lambda c: c["elevation_m"])
                    raw_candidates = [raw_candidates[0]]

    except Exception as exc:
        logger.exception("Terrain analysis failed")
        raise HTTPException(status_code=500, detail=f"Terrain analysis failed: {exc}")


    ranked = rank_candidates(raw_candidates, max_sites=10)

    # Pick the best candidate
    if ranked:
        best = ranked[0]
    elif raw_candidates:
        best = raw_candidates[0]
        best.setdefault("terrain_score", 0.5)
        best.setdefault("land_score", 0.5)
        best.setdefault("land_status", "unknown")
    else:
        raise HTTPException(
            status_code=422,
            detail=(
                "No candidate pond sites found. The terrain may be too flat, too steep, "
                "or all low-elevation cells are inside an active river channel."
            ),
        )

    pond_lat = best["lat"]
    pond_lon = best["lon"]
    terrain_score = best.get("terrain_score", 0.5)
    land_score = best.get("land_score", 0.5)

    # ── 6. Rainfall (moved up to be reused for all candidates) ────────────────
    rainfall_source = "provided"
    if annual_rainfall_mm is None:
        try:
            from app.services.rainfall_service import fetch_rainfall
            rainfall_data = await fetch_rainfall(centroid_lat, centroid_lon)
            annual_rainfall_mm = rainfall_data["annual_mean_mm"]
            rainfall_source = f"Open-Meteo ({rainfall_data.get('years', '?')} yr avg)"
        except Exception as exc:
            logger.warning("Rainfall fetch failed (%s); using default 800 mm", exc)
            annual_rainfall_mm = 800.0
            rainfall_source = "default (800 mm — rainfall API unavailable)"

    # ── 7. Top Candidates details ──────────────────────────────────────────────
    top_candidates_list = []
    candidates_to_process = ranked[:10] if ranked else raw_candidates[:10]

    for rank_idx, cand in enumerate(candidates_to_process):
        cand_lat = cand["lat"]
        cand_lon = cand["lon"]
        t_score = cand.get("terrain_score", 0.5)
        l_score = cand.get("land_score", 0.5)

        try:
            cand_catchment = delineate_catchment(lats, lons, elevs, cand_lat, cand_lon)
            cand_runoff = estimate_runoff_volume(cand_catchment["area_m2"], annual_rainfall_mm, land_cover)
            cand_pond = size_pond(cand_runoff["runoff_volume_m3"], desired_depth_m)
            cand_score = compute_suitability_score(t_score, cand_catchment["area_m2"], l_score)
            
            # Generate Pond Border GeoJSON (circle)
            cand_area = cand_pond["pond_surface_area_m2"]
            cand_radius = math.sqrt(cand_area / math.pi)
            cand_rad_lat = cand_radius / 111320.0
            cand_rad_lon = cand_radius / (111320.0 * math.cos(math.radians(cand_lat)))
            cand_coords = []
            for i in range(33):
                angle = math.pi * 2 * i / 32
                cand_coords.append([
                    cand_lon + cand_rad_lon * math.cos(angle),
                    cand_lat + cand_rad_lat * math.sin(angle)
                ])
            cand_geom = {"type": "Polygon", "coordinates": [cand_coords]}

            top_candidates_list.append({
                "rank": rank_idx + 1,
                "lat": round(cand_lat, 7),
                "lon": round(cand_lon, 7),
                "elevation_m": round(cand.get("elevation_m", 0.0), 2),
                "slope_deg": round(cand.get("slope_deg", 0.0), 3),
                "land_status": cand.get("land_status", "unknown"),
                "terrain_score": round(t_score, 3),
                "land_score": round(l_score, 3),
                "flow_accum": cand.get("flow_accum"),
                "pond_surface_area_m2": round(cand_area, 2),
                "suitability_score": round(cand_score["total_score"], 3),
                "pond_geometry": cand_geom,
            })
        except Exception as e:
            logger.warning("Could not compute details for candidate %d: %s", rank_idx + 1, e)
            top_candidates_list.append({
                "rank": rank_idx + 1,
                "lat": round(cand_lat, 7),
                "lon": round(cand_lon, 7),
                "elevation_m": round(cand.get("elevation_m", 0.0), 2),
                "slope_deg": round(cand.get("slope_deg", 0.0), 3),
                "land_status": cand.get("land_status", "unknown"),
                "terrain_score": round(t_score, 3),
                "land_score": round(l_score, 3),
                "flow_accum": cand.get("flow_accum"),
            })

    # For backward compatibility and root fields, use the best candidate's details
    try:
        catchment = delineate_catchment(lats, lons, elevs, pond_lat, pond_lon)
    except Exception as exc:
        logger.error("Catchment delineation failed: %s", exc)
        raise HTTPException(
            status_code=422,
            detail=(
                "Catchment delineation failed. The DEM may lack sufficient relief. "
                f"Detail: {exc}"
            ),
        )

    # ── 8. Runoff estimation ──────────────────────────────────────────────────
    runoff = estimate_runoff_volume(
        catchment["area_m2"], annual_rainfall_mm, land_cover
    )

    # ── 9. Pond sizing ────────────────────────────────────────────────────────
    if runoff["runoff_volume_m3"] <= 0:
        raise HTTPException(
            status_code=422,
            detail="Runoff volume is zero — check catchment area and rainfall inputs.",
        )
    pond = size_pond(runoff["runoff_volume_m3"], desired_depth_m)

    # ── 10. Suitability score ─────────────────────────────────────────────────
    score = compute_suitability_score(
        terrain_score=terrain_score,
        catchment_area_m2=catchment["area_m2"],
        land_score=land_score,
    )

    # ── 11. Build response ────────────────────────────────────────────────────
    method_notes = (
        "Terrain reconstructed from KML contour lines via scipy.griddata (Delaunay linear interpolation). "
        "Catchment delineated using the D8 flow-direction algorithm (pysheds). "
        f"Runoff estimated with the Rational Method (V = P × A × C, C = {runoff['runoff_coefficient']}). "
        f"Rainfall: {rainfall_source}."
    )
    
    # Primary Pond Border
    main_area = pond["pond_surface_area_m2"]
    main_radius = math.sqrt(main_area / math.pi)
    main_rad_lat = main_radius / 111320.0
    main_rad_lon = main_radius / (111320.0 * math.cos(math.radians(pond_lat)))
    main_coords = []
    for i in range(33):
        angle = math.pi * 2 * i / 32
        main_coords.append([
            pond_lon + main_rad_lon * math.cos(angle),
            pond_lat + main_rad_lat * math.sin(angle)
        ])
    main_geom = {"type": "Polygon", "coordinates": [main_coords]}

    return ContourUploadResponse(
        # Terrain overview
        elevation_range_m=elev_stats,
        contour_count=len(contour_lines),
        bounding_box=bbox,
        # Best pond site
        pond_location={
            "lat": round(pond_lat, 7),
            "lon": round(pond_lon, 7),
            "elevation_m": round(best.get("elevation_m", 0.0), 2),
            "slope_deg": round(best.get("slope_deg", 0.0), 3),
            "land_status": best.get("land_status", "unknown"),
        },
        pond_geometry=main_geom,
        top_candidates=top_candidates_list,
        # Catchment
        catchment_area_m2=catchment["area_m2"],
        catchment_area_ha=catchment["area_ha"],
        catchment_geometry=catchment["geometry_geojson"],
        # Hydrology
        annual_rainfall_mm=round(annual_rainfall_mm, 2),
        runoff_volume_m3=runoff["runoff_volume_m3"],
        runoff_coefficient=runoff["runoff_coefficient"],
        # Pond sizing
        pond_depth_m=pond["pond_depth_m"],
        pond_surface_area_m2=pond["pond_surface_area_m2"],
        pond_storage_capacity_m3=pond["pond_storage_capacity_m3"],
        # Scoring
        suitability_score=score["total_score"],
        score_breakdown=score,
        # Metadata
        slope_summary=catchment.get("slope_summary", "N/A"),
        avg_elevation_m=catchment.get("avg_elevation_m", 0.0),
        method_notes=method_notes,
    )


# ─── Route: POST /analyzeContour ──────────────────────────────────────────────

@router.post(
    "/analyzeContour",
    response_model=ContourUploadResponse,
    summary="Analyze Contour Map and Identify Pond Candidates",
)
async def analyze_contour(
    contour_map: UploadFile = File(
        ...,
        description="KML or KMZ contour map file containing elevation polyline data",
    ),
    land_cover: str = Form(
        "agricultural",
        description="Type of land cover for runoff estimation: 'vegetated', 'agricultural', 'built_up'",
    ),
    desired_depth_m: float = Form(
        default=3.0,
        description="Desired pond depth in metres (1–6 m range enforced)",
        ge=0.5,
        le=10.0,
    ),
    annual_rainfall_mm: Optional[float] = Form(
        default=None,
        description=(
            "Override annual rainfall in mm. "
            "If omitted, fetched from Open-Meteo for the map centroid."
        ),
        ge=0,
        le=10000,
    ),
):
    return await _analyze_contour(contour_map, land_cover, desired_depth_m, annual_rainfall_mm)


# ─── Route: POST /findCatchment (alias) ───────────────────────────────────────

@router.post(
    "/findCatchment",
    response_model=ContourUploadResponse,
    summary="[Alias] Find Catchment and Identify Pond Candidates",
)
async def find_catchment(
    contour_map: UploadFile = File(..., description="KML or KMZ contour map file"),
    land_cover: str = Form("agricultural"),
    desired_depth_m: float = Form(3.0),
    annual_rainfall_mm: Optional[float] = Form(None),
):
    return await _analyze_contour(
        contour_map=contour_map,
        land_cover=land_cover,
        desired_depth_m=desired_depth_m,
        annual_rainfall_mm=annual_rainfall_mm,
    )
