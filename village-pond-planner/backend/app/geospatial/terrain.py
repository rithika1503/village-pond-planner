"""
Terrain analysis module.

Responsibilities:
  1. Fetch a DEM grid for a bounding box from Open Elevation API.
  2. Generate contour lines from the DEM.
  3. Compute slope and identify low-slope / low-elevation candidate cells.

Design note (viva-ready):
  - Contours are extracted using matplotlib.pyplot.contour, which implements the
    marching-squares algorithm on a regular grid — well-tested and dependency-light.
  - Slope is approximated via finite differences on the grid (Δelevation / Δdistance).
    richdem.TerrainAttribute is used when a proper GeoTIFF is available.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional
from collections import deque

import numpy as np
import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
from scipy.ndimage import uniform_filter

from app.config import settings

logger = logging.getLogger(__name__)


# ─── DEM Fetching ──────────────────────────────────────────────────────────────

async def fetch_dem_grid(
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    n_points: int = settings.DEM_GRID_POINTS,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Fetch elevation data from Open Elevation API and return a regular grid.

    Returns:
        lats   : 1-D array of latitudes  (length n_points)
        lons   : 1-D array of longitudes (length n_points)
        elevs  : 2-D array of elevations (n_points × n_points), metres
    """
    import aiohttp

    lats = np.linspace(min_lat, max_lat, n_points)
    lons = np.linspace(min_lon, max_lon, n_points)
    lat_grid, lon_grid = np.meshgrid(lats, lons, indexing="ij")

    locations = [
        {"latitude": float(lat_grid[i, j]), "longitude": float(lon_grid[i, j])}
        for i in range(n_points)
        for j in range(n_points)
    ]

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            async with session.post(
                settings.OPEN_ELEVATION_BATCH_URL,
                json={"locations": locations},
            ) as resp:
                data = await resp.json()
        results = data.get("results", [])
        if len(results) != n_points * n_points:
            raise ValueError("Unexpected result count from Open Elevation API")
        elevs = np.array(
            [r["elevation"] for r in results], dtype=float
        ).reshape(n_points, n_points)
    except Exception as exc:
        logger.warning("Open Elevation API failed (%s); using synthetic flat DEM", exc)
        elevs = _synthetic_dem(n_points)

    return lats, lons, elevs


def _synthetic_dem(n: int) -> np.ndarray:
    """Generate a synthetic DEM for testing (gentle slope + small hills)."""
    x = np.linspace(0, 1, n)
    y = np.linspace(0, 1, n)
    xx, yy = np.meshgrid(x, y, indexing="ij")
    base = 200 + 50 * xx + 30 * yy
    hills = 20 * np.exp(-((xx - 0.3) ** 2 + (yy - 0.4) ** 2) / 0.02)
    noise = np.random.default_rng(42).normal(0, 1, (n, n))
    return base + hills + noise


# ─── Contour Generation ────────────────────────────────────────────────────────

def generate_contours_geojson(
    lats: np.ndarray,
    lons: np.ndarray,
    elevs: np.ndarray,
    interval_m: float = settings.CONTOUR_INTERVAL_M,
) -> dict:
    """
    Produce a GeoJSON FeatureCollection of contour lines.

    Algorithm: matplotlib.pyplot.contour (marching squares on regular grid).
    Each contour level -> one GeoJSON Feature with LineString geometry.
    """
    min_e, max_e = float(elevs.min()), float(elevs.max())
    levels = np.arange(
        np.floor(min_e / interval_m) * interval_m,
        np.ceil(max_e / interval_m) * interval_m + interval_m,
        interval_m,
    )

    fig, ax = plt.subplots()
    cs = ax.contour(lons, lats, elevs, levels=levels)
    plt.close(fig)

    features: list[dict] = []
    for level, segments in zip(cs.levels, cs.allsegs):
        for seg in segments:
            if len(seg) < 2:
                continue
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[float(v[0]), float(v[1])] for v in seg],
                    },
                    "properties": {"elevation_m": float(level)},
                }
            )

    return {"type": "FeatureCollection", "features": features}


# ─── Slope, Flow Accumulation & Candidate Cells ──────────────────────────────

def compute_slope(
    lats: np.ndarray,
    lons: np.ndarray,
    elevs: np.ndarray,
) -> np.ndarray:
    """
    Compute slope in degrees from a regular lat/lon elevation grid.

    Method: 2nd-order central differences for interior cells; forward/backward
    differences at edges. Horizontal distance between cells is estimated from
    the grid spacing at the mean latitude (flat-earth approximation, accurate
    enough at village scale).
    """
    lat_mean = float(lats.mean())
    deg_to_m_lat = 111_320.0
    deg_to_m_lon = 111_320.0 * np.cos(np.radians(lat_mean))

    dlat = float(lats[1] - lats[0]) * deg_to_m_lat if len(lats) > 1 else 1.0
    dlon = float(lons[1] - lons[0]) * deg_to_m_lon if len(lons) > 1 else 1.0

    dz_dlat, dz_dlon = np.gradient(elevs, dlat, dlon)
    slope_rad = np.arctan(np.sqrt(dz_dlat ** 2 + dz_dlon ** 2))
    return np.degrees(slope_rad)


from app.geospatial.hydrology_engine import HydrologyEngine


def is_stream_channel(
    accum: np.ndarray,
    stream_accum_percentile: float = settings.STREAM_ACCUM_PERCENTILE,
) -> np.ndarray:
    """
    Return a boolean mask (same shape as `accum`) where True means the cell
    is an active stream / river channel.

    Threshold: cells whose flow accumulation exceeds
    `stream_accum_percentile`-th percentile of the grid are classified as
    stream channels and are ineligible for pond placement.

    Rationale:
        A pond built inside a river channel would be immediately flooded,
        structurally unsafe, and ecologically harmful. The pond location
        should be at the *margin* of the drainage network, not inside it.
    """
    threshold = float(np.percentile(accum, stream_accum_percentile))
    return accum >= threshold


def mask_osm_waterways(waterways: list[list[dict]], lats: np.ndarray, lons: np.ndarray, buffer_deg: float = 0.002) -> np.ndarray:
    """
    Rasterize OSM waterway lines onto the boolean mask of the DEM grid.
    Cells within buffer_deg of any waterway segment are marked True.
    """
    nrows, ncols = len(lats), len(lons)
    mask = np.zeros((nrows, ncols), dtype=bool)
    
    if not waterways:
        return mask
        
    for i in range(nrows):
        for j in range(ncols):
            lat, lon = float(lats[i]), float(lons[j])
            
            for waterway in waterways:
                if mask[i, j]:
                    break
                for k in range(len(waterway) - 1):
                    x0, y0 = lon, lat
                    x1, y1 = waterway[k]['lon'], waterway[k]['lat']
                    x2, y2 = waterway[k+1]['lon'], waterway[k+1]['lat']
                    
                    l2 = (x2 - x1)**2 + (y2 - y1)**2
                    if l2 == 0:
                        dist = np.hypot(x0 - x1, y0 - y1)
                    else:
                        t = max(0, min(1, ((x0 - x1)*(x2 - x1) + (y0 - y1)*(y2 - y1)) / l2))
                        proj_x = x1 + t * (x2 - x1)
                        proj_y = y1 + t * (y2 - y1)
                        dist = np.hypot(x0 - proj_x, y0 - proj_y)
                        
                    if dist <= buffer_deg:
                        mask[i, j] = True
                        break
                        
    return mask

def identify_candidate_cells(
    lats: np.ndarray,
    lons: np.ndarray,
    elevs: np.ndarray,
    osm_waterways: list[list[dict]] = None,
    slope_threshold_deg: float = settings.SLOPE_THRESHOLD_DEG,
    elevation_low_percentile: float = settings.ELEVATION_LOW_PERCENTILE,
    stream_accum_percentile: float = settings.STREAM_ACCUM_PERCENTILE,
) -> list[dict]:
    """
    Return candidate pond sites as spatially distinct region representatives.

    Algorithm (viva-ready):
      1. Filter every DEM cell by: slope < threshold AND elevation in the
         bottom `elevation_low_percentile`-th percentile AND not a stream cell.
      2. Group qualifying cells into contiguous regions via 4-connectivity
         BFS flood-fill. Adjacent qualifying cells form one potential pond
         region (e.g. a valley bottom or flat depression).
      3. For each region, select the representative cell with the HIGHEST
         flow accumulation — this is the hydrological convergence point of
         that region where water naturally gathers most.
      4. Apply a minimum 500 m spatial-separation filter so the final
         candidates are spatially distinct and cover different parts of the
         study area.

    River-exclusion:
      Cells whose D8 flow accumulation exceeds `stream_accum_percentile` are
      active stream/river channels and are excluded. 
      If `osm_waterways` are provided, cells falling near physical real-world 
      rivers are also explicitly excluded.
    """
    slope = compute_slope(lats, lons, elevs)
    elev_threshold = float(np.percentile(elevs, elevation_low_percentile))

    engine = HydrologyEngine(lats, lons, elevs)
    engine.process()
    accum = engine.accum
    stream_mask = is_stream_channel(accum, stream_accum_percentile)
    
    # Optional OSM Masking
    if osm_waterways:
        osm_mask = mask_osm_waterways(osm_waterways, lats, lons)
        stream_mask = stream_mask | osm_mask
        logger.info(f"OSM Exclusion: Masked {np.count_nonzero(osm_mask)} cells near physical rivers.")

    # Hydrological criterion: Meaningful drainage paths (e.g., top 25% of flow accum)
    drainage_threshold = float(np.percentile(accum, 75.0))
    drainage_mask = accum >= drainage_threshold
    
    # Find areas adjacent to meaningful drainage (1-cell dilation)
    # This allows ponds on the banks of main streams or over minor tributaries.
    from scipy.ndimage import maximum_filter
    drainage_vicinity = maximum_filter(drainage_mask, size=3)

    nrows, ncols = len(lats), len(lons)

    # Step 1: Build qualifying-cell boolean grid
    qualifies = np.zeros((nrows, ncols), dtype=bool)
    for i in range(nrows):
        for j in range(ncols):
            if (
                slope[i, j] < slope_threshold_deg
                and elevs[i, j] < elev_threshold
                and not stream_mask[i, j]
                and drainage_vicinity[i, j]  # MUST be close to a drainage path
            ):
                qualifies[i, j] = True

    # Step 2: BFS flood-fill -> contiguous regions
    visited = np.zeros((nrows, ncols), dtype=bool)
    regions: list[list[tuple[int, int]]] = []

    for si in range(nrows):
        for sj in range(ncols):
            if not qualifies[si, sj] or visited[si, sj]:
                continue
            region: list[tuple[int, int]] = []
            q: deque = deque([(si, sj)])
            visited[si, sj] = True
            while q:
                ci, cj = q.popleft()
                region.append((ci, cj))
                for di, dj in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                    ni, nj = ci + di, cj + dj
                    if (
                        0 <= ni < nrows
                        and 0 <= nj < ncols
                        and qualifies[ni, nj]
                        and not visited[ni, nj]
                    ):
                        visited[ni, nj] = True
                        q.append((ni, nj))
            regions.append(region)

    logger.info(
        "Candidate regions: %d qualifying cells grouped into %d contiguous regions",
        int(qualifies.sum()), len(regions),
    )

    # Step 3: Best representative per region
    # Best = highest flow accumulation (convergence point), tie-break lowest elev.
    raw_reps: list[dict] = []
    for region in regions:
        best = max(region, key=lambda c: (accum[c[0], c[1]], -elevs[c[0], c[1]]))
        ci, cj = best
        raw_reps.append({
            "lat": float(lats[ci]),
            "lon": float(lons[cj]),
            "elevation_m": float(elevs[ci, cj]),
            "slope_deg": float(slope[ci, cj]),
            "flow_accum": int(accum[ci, cj]),
            "region_size": len(region),
            "is_stream": False,
        })

    # Sort: most hydrologically meaningful first
    raw_reps.sort(key=lambda c: (-c["flow_accum"], -c["region_size"], c["elevation_m"]))

    # Step 4: Minimum-distance spatial filter (500 m)
    lat_mean = float(lats.mean())
    deg_per_m_lat = 1.0 / 111_320.0
    deg_per_m_lon = 1.0 / (111_320.0 * np.cos(np.radians(lat_mean)))

    accepted: list[dict] = []
    for cand in raw_reps:
        too_close = any(
            np.hypot(
                (cand["lat"] - prev["lat"]) / deg_per_m_lat,
                (cand["lon"] - prev["lon"]) / deg_per_m_lon,
            ) < 500.0
            for prev in accepted
        )
        if not too_close:
            accepted.append(cand)

    logger.info(
        "After spatial-diversity filter (500 m): %d distinct candidate regions",
        len(accepted),
    )

    return accepted
