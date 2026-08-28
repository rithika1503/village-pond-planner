"""
Catchment delineation module.

Algorithm (from plan §4.3):
  DEM → fill sinks → flow direction (D8) → flow accumulation
      → outlet point → trace upstream cells → catchment raster
      → polygonize → compute area

Uses pysheds for all hydrology steps — well-tested library, allows explaining
every step clearly in the viva without maintaining custom D8 code.

Design note:
  The DEM is passed as a 2-D NumPy array (lat × lon grid).
  All geometry is returned as GeoJSON for direct serialisation over the API.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

try:
    from pysheds.grid import Grid
    from pysheds.view import Raster
    _PYSHEDS_AVAILABLE = True
except ImportError:
    _PYSHEDS_AVAILABLE = False
    logger.warning("pysheds not available — catchment delineation will use fallback approximation")


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
    if _PYSHEDS_AVAILABLE:
        return _delineate_with_pysheds(lats, lons, elevs, outlet_lat, outlet_lon)
    return _fallback_circular_catchment(lats, lons, elevs, outlet_lat, outlet_lon)


# ─── pysheds Implementation ────────────────────────────────────────────────────

def _delineate_with_pysheds(
    lats: np.ndarray,
    lons: np.ndarray,
    elevs: np.ndarray,
    outlet_lat: float,
    outlet_lon: float,
) -> dict:
    """Full D8 catchment delineation using pysheds."""
    import tempfile, os
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.features import shapes as rasterio_shapes
    from shapely.geometry import shape, mapping
    from shapely.ops import unary_union
    import pyproj

    n_lat, n_lon = elevs.shape
    transform = from_bounds(
        lons.min(), lats.min(), lons.max(), lats.max(), n_lon, n_lat
    )

    # Write DEM to a temp GeoTIFF so pysheds can read it
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        with rasterio.open(
            tmp_path, "w",
            driver="GTiff",
            height=n_lat, width=n_lon,
            count=1, dtype=elevs.dtype,
            crs="EPSG:4326",
            transform=transform,
        ) as dst:
            dst.write(elevs, 1)

        grid = Grid.from_raster(tmp_path)
        dem = grid.read_raster(tmp_path)

        # Step 1: Fill sinks
        pit_filled = grid.fill_pits(dem)
        flooded = grid.fill_depressions(pit_filled)

        # Step 2: Flow direction (D8)
        inflated = grid.resolve_flats(flooded)
        fdir = grid.flowdir(inflated)

        # Step 3: Flow accumulation
        acc = grid.accumulation(fdir)

        # Step 4: Snap outlet to highest-accumulation nearby cell
        x_snap, y_snap = grid.snap_to_mask(acc > 10, (outlet_lon, outlet_lat))

        # Step 5: Delineate catchment
        catch = grid.catchment(x=x_snap, y=y_snap, fdir=fdir, xytype="coordinate")

        # Step 6: Polygonize
        catch_view = grid.view(catch, dtype=np.uint8)
        polys = [
            shape(geom)
            for geom, val in rasterio_shapes(catch_view, transform=grid.affine)
            if val == 1
        ]
        if not polys:
            raise ValueError("Empty catchment — snapping may have missed outlet")

        catchment_geom = unary_union(polys)
        area_m2 = _geodetic_area_m2(catchment_geom)

    finally:
        os.unlink(tmp_path)

    # Elevation stats within catchment bounding box
    avg_elev = float(np.mean(elevs))  # simplified; full mask would need rasterization
    slope_arr = _simple_slope(lats, lons, elevs)
    slope_summary = (
        f"mean {float(slope_arr.mean()):.1f}°, "
        f"max {float(slope_arr.max()):.1f}°, "
        f"min {float(slope_arr.min()):.1f}°"
    )

    return {
        "geometry_geojson": mapping(catchment_geom),
        "area_m2": round(area_m2, 1),
        "area_ha": round(area_m2 / 10_000, 2),
        "avg_elevation_m": round(avg_elev, 1),
        "slope_summary": slope_summary,
    }


# ─── Fallback (no pysheds) ────────────────────────────────────────────────────

def _fallback_circular_catchment(
    lats: np.ndarray,
    lons: np.ndarray,
    elevs: np.ndarray,
    outlet_lat: float,
    outlet_lon: float,
) -> dict:
    """
    Approximate catchment as a circle with radius ~1 km when pysheds is unavailable.
    This is a placeholder for environments without C extensions.
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
        from functools import partial
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
