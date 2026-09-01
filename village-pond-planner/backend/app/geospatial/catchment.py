"""
Catchment delineation module.

Algorithm:
  DEM -> HydrologyEngine (fill sinks, D8 flow direction, flow accumulation)
      -> Snap outlet to convergence point -> trace upstream cells -> catchment raster
      -> polygonize -> compute area

This implementation uses the unified HydrologyEngine to ensure consistency
with terrain analysis.
"""

from __future__ import annotations

import logging
from typing import Optional
import numpy as np

from app.geospatial.hydrology_engine import HydrologyEngine
from app.config import settings

logger = logging.getLogger(__name__)


# ─── Public API ────────────────────────────────────────────────────────────────

def delineate_catchment(
    lats: np.ndarray,
    lons: np.ndarray,
    elevs: np.ndarray,
    outlet_lat: float,
    outlet_lon: float,
) -> dict:
    """
    Delineate the catchment upstream of an outlet point.

    Returns a dict with:
        geometry_geojson  : GeoJSON Polygon
        area_m2           : float
        area_ha           : float
        avg_elevation_m   : float
        slope_summary     : str
    """
    logger.info("Delineating catchment using consistent custom NumPy hydrological engine")
    return _dem_based_catchment(lats, lons, elevs, outlet_lat, outlet_lon)


# ─── NumPy DEM-based Catchment (consistent D8 watershed delineation) ───────────

def _dem_based_catchment(
    lats: np.ndarray,
    lons: np.ndarray,
    elevs: np.ndarray,
    outlet_lat: float,
    outlet_lon: float,
) -> dict:
    """
    Upstream catchment delineation using the unified HydrologyEngine.
    """
    from shapely.geometry import box as shapely_box, mapping
    from shapely.ops import unary_union

    nrows, ncols = elevs.shape

    # 1. Process DEM through unified engine
    engine = HydrologyEngine(lats, lons, elevs)
    engine.process()
    
    accum = engine.accum
    flow_to = engine.flow_to

    # 2. Outlet snapping: walk downstream to a hydrological convergence
    # We want to snap to a major stream, not just the exclusion boundary, so use 98th percentile.
    stream_threshold = float(np.percentile(accum, 98.0))
    max_accum = int(np.max(accum))
    logger.info(f"Stream threshold (p98.0): {stream_threshold:.1f}. Max accumulation: {max_accum}")
    
    start_i = int(np.argmin(np.abs(lats - outlet_lat)))
    start_j = int(np.argmin(np.abs(lons - outlet_lon)))
    
    snap_i, snap_j = start_i, start_j
    while True:
        ri, rj = flow_to[snap_i, snap_j]
        if ri < 0:
            break  # Local minimum, nowhere to flow
            
        # Criterion 1: Move ONTO the cell crossing the main stream threshold, then stop
        if accum[ri, rj] >= stream_threshold:
            snap_i, snap_j = ri, rj
            break
            
        # Criterion 2: Stop at a major confluence (jump of > 50% and at least 50 cells)
        if accum[ri, rj] > accum[snap_i, snap_j] * 1.5 + 50:
            snap_i, snap_j = ri, rj
            break
            
        snap_i, snap_j = ri, rj

    logger.info(
        f"Snapped outlet from ({start_i}, {start_j}) to ({snap_i}, {snap_j}). "
        f"Accumulation: {accum[start_i, start_j]} -> {accum[snap_i, snap_j]}"
    )

    # 3. BFS upstream trace from snapped outlet cell
    catchment_cells = engine.trace_upstream(snap_i, snap_j)

    if len(catchment_cells) < 3:
        # Include immediate neighbor cells if very small
        neighbours = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]
        for di, dj in neighbours:
            ni, nj = snap_i + di, snap_j + dj
            if 0 <= ni < nrows and 0 <= nj < ncols:
                catchment_cells.add((ni, nj))

    logger.info(f"Traced catchment contains {len(catchment_cells)} upstream cells.")

    # 4. Rasterized Geometry & Smoothing
    half_dy = (lats[1] - lats[0]) / 2.0 if nrows > 1 else 0.001
    half_dx = (lons[1] - lons[0]) / 2.0 if ncols > 1 else 0.001

    boxes = [
        shapely_box(
            float(lons[cj]) - half_dx,
            float(lats[ci]) - half_dy,
            float(lons[cj]) + half_dx,
            float(lats[ci]) + half_dy,
        )
        for ci, cj in catchment_cells
    ]
    catchment_geom = unary_union(boxes)
    catchment_geom = catchment_geom.buffer(0.0001).buffer(-0.0001).simplify(0.0002)

    # Filter out any tiny detached "noisy" cells/polygons
    if catchment_geom.geom_type == 'MultiPolygon':
        # Keep only the largest contiguous polygon
        catchment_geom = max(catchment_geom.geoms, key=lambda p: p.area)

    area_m2 = _geodetic_area_m2(catchment_geom)
    area_ha = area_m2 / 10_000.0
    logger.info(f"Final delineated catchment area: {area_ha:.2f} ha ({area_m2:.1f} m²)")

    # 5. Slope & Elevation Stats
    cell_elevs = [float(elevs[ci, cj]) for ci, cj in catchment_cells]
    avg_elev = float(np.mean(cell_elevs))
    slope_arr = _simple_slope(lats, lons, elevs)
    cell_slopes = [float(slope_arr[ci, cj]) for ci, cj in catchment_cells]

    slope_summary = (
        f"mean {float(np.mean(cell_slopes)):.1f}°, "
        f"max {float(np.max(cell_slopes)):.1f}° "
        f"({len(catchment_cells)} upstream watershed cells)"
    )

    return {
        "geometry_geojson": mapping(catchment_geom),
        "area_m2": round(area_m2, 1),
        "area_ha": round(area_m2 / 10_000, 2),
        "avg_elevation_m": round(avg_elev, 1),
        "slope_summary": slope_summary,
    }


def _fallback_circular_catchment(
    lats: np.ndarray,
    lons: np.ndarray,
    elevs: np.ndarray,
    outlet_lat: float,
    outlet_lon: float,
) -> dict:
    """
    Legacy circular approximation — kept for reference.
    The DEM-based upstream tracing (_dem_based_catchment) is now used instead.
    """
    radius_deg = 0.009  # ≈ 1 km
    n_points = 36
    angles = np.linspace(0, 2 * np.pi, n_points, endpoint=False)
    coords = [
        [outlet_lon + radius_deg * np.cos(a), outlet_lat + radius_deg * np.sin(a)]
        for a in angles
    ]
    coords.append(coords[0])  # close ring

    area_m2 = np.pi * (radius_deg * 111_320) ** 2  # rough

    avg_elev = float(np.mean(elevs))
    slope_arr = _simple_slope(lats, lons, elevs)
    slope_summary = (
        f"mean {float(slope_arr.mean()):.1f}° (approx — pysheds unavailable)"
    )

    return {
        "geometry_geojson": {
            "type": "Polygon",
            "coordinates": [coords],
        },
        "area_m2": round(area_m2, 1),
        "area_ha": round(area_m2 / 10_000, 2),
        "avg_elevation_m": round(avg_elev, 1),
        "slope_summary": slope_summary,
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _simple_slope(lats: np.ndarray, lons: np.ndarray, elevs: np.ndarray) -> np.ndarray:
    dlat = abs(float(lats[1] - lats[0])) * 111_320.0 if len(lats) > 1 else 1.0
    dlon = (
        abs(float(lons[1] - lons[0])) * 111_320.0 * np.cos(np.radians(float(lats.mean())))
        if len(lons) > 1
        else 1.0
    )
    dz_dlat, dz_dlon = np.gradient(elevs, dlat, dlon)
    return np.degrees(np.arctan(np.sqrt(dz_dlat ** 2 + dz_dlon ** 2)))


def _geodetic_area_m2(geom) -> float:
    """Approximate area of a WGS-84 polygon in m² using pyproj."""
    try:
        import pyproj
        from shapely.ops import transform

        geod = pyproj.Geod(ellps="WGS84")
        area, _ = geod.geometry_area_perimeter(geom)
        return abs(area)
    except Exception:
        # Fallback: naive planar area in degrees² → m²
        bounds = geom.bounds  # minx, miny, maxx, maxy
        w = (bounds[2] - bounds[0]) * 111_320.0
        h = (bounds[3] - bounds[1]) * 111_320.0
        return w * h
