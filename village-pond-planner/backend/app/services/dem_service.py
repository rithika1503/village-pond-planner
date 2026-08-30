"""
DEM (Digital Elevation Model) service.

Responsibilities:
  - Fetch elevation data from the Open Elevation API.
  - Cache raw results per village in the DB or local file system.
  - Provide the DEM grid to geospatial modules.
"""

from __future__ import annotations

import logging
import os
import json
from pathlib import Path

import numpy as np

from app.config import settings
from app.geospatial.terrain import fetch_dem_grid

logger = logging.getLogger(__name__)

_cache_dir = Path(settings.DEM_CACHE_DIR)
_cache_dir.mkdir(parents=True, exist_ok=True)


def _cache_path(village_id: int) -> Path:
    return _cache_dir / f"village_{village_id}_dem.npz"


async def get_dem_for_village(
    village_id: int,
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    force_refresh: bool = False,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Return (lats, lons, elevs) for a village, fetching from API only if not cached.

    Cache format: NumPy .npz file on disk — simple, fast, no DB dependency.
    """
    cache = _cache_path(village_id)

    if not force_refresh and cache.exists():
        logger.info("DEM cache hit for village %d", village_id)
        data = np.load(cache)
        return data["lats"], data["lons"], data["elevs"]

    logger.info("Fetching DEM for village %d from API", village_id)
    lats, lons, elevs = await fetch_dem_grid(min_lat, max_lat, min_lon, max_lon)

    # Persist to disk
    np.savez(cache, lats=lats, lons=lons, elevs=elevs)
    logger.info("DEM cached at %s", cache)

    return lats, lons, elevs
