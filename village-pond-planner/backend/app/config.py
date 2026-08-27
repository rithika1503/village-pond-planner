"""
Application configuration — all tunable parameters in one place.
Edit this file (or set env vars) to change behaviour without touching business logic.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Application ───────────────────────────────────────────────
    APP_NAME: str = "Village Pond Planning System"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # ─── Database ──────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://pond_user:pond_pass@localhost:5432/village_pond"
    DATABASE_SYNC_URL: str = "postgresql+psycopg2://pond_user:pond_pass@localhost:5432/village_pond"

    # ─── External APIs ─────────────────────────────────────────────
    # Open Elevation API (no key required)
    OPEN_ELEVATION_URL: str = "https://api.open-elevation.com/api/v1/lookup"
    OPEN_ELEVATION_BATCH_URL: str = "https://api.open-elevation.com/api/v1/lookup"

    # Open-Meteo (no key required)
    OPEN_METEO_URL: str = "https://archive-api.open-meteo.com/v1/archive"

    # NASA POWER (fallback)
    NASA_POWER_URL: str = "https://power.larc.nasa.gov/api/temporal/monthly/point"

    # ─── Caching ───────────────────────────────────────────────────
    # Local directory to cache DEM GeoTIFFs
    DEM_CACHE_DIR: str = "./dem_cache"
    # How many days before re-fetching rainfall data
    RAINFALL_CACHE_DAYS: int = 7

    # ─── DEM / Terrain ─────────────────────────────────────────────
    # DEM resolution in degrees (~90m for SRTM-3, ~30m for SRTM-1)
    DEM_RESOLUTION_DEG: float = 0.0008333  # ≈ 90 m
    # Number of grid points along each axis when sampling elevation
    DEM_GRID_POINTS: int = 50
    # Contour interval in metres
    CONTOUR_INTERVAL_M: float = 10.0
    # Slope threshold below which land is "low slope" (degrees)
    SLOPE_THRESHOLD_DEG: float = 5.0
    # Relative-elevation percentile threshold for "low elevation"
    ELEVATION_LOW_PERCENTILE: float = 30.0

    # ─── Land Suitability ──────────────────────────────────────────
    # Max candidate sites returned per village
    MAX_CANDIDATE_SITES: int = 5

    # ─── Runoff / Hydrology ────────────────────────────────────────
    # Rational-method runoff coefficients by land cover type
    RUNOFF_COEFF_VEGETATED: float = 0.30
    RUNOFF_COEFF_AGRICULTURAL: float = 0.55
    RUNOFF_COEFF_BUILT_UP: float = 0.75
    RUNOFF_COEFF_DEFAULT: float = 0.45

    # ─── Suitability Scoring Weights ───────────────────────────────
    # S = w_terrain·T + w_catchment·C + w_rainfall·R + w_land·L
    # Must sum to 1.0
    WEIGHT_TERRAIN: float = 0.30
    WEIGHT_CATCHMENT: float = 0.30
    WEIGHT_RAINFALL: float = 0.20
    WEIGHT_LAND: float = 0.20

    # ─── Pond Sizing Limits ────────────────────────────────────────
    POND_MIN_DEPTH_M: float = 1.0
    POND_MAX_DEPTH_M: float = 6.0
    POND_DEFAULT_DEPTH_M: float = 3.0  # used when storage / area calc fails

    # ─── CORS ──────────────────────────────────────────────────────
    CORS_ORIGINS: list[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
    ]


# Singleton — import this everywhere
settings = Settings()
