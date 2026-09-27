"""
Pydantic schemas for POST /api/analyze-polygon.
"""
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class PolygonAnalysisRequest(BaseModel):
    """
    Request body for POST /api/analyze-polygon.

    - land_polygon     : GeoJSON Polygon the user drew on the map (required).
                         The analysis bbox is derived from this.
    - catchment_polygon: Optional GeoJSON Polygon drawn by the user to override
                         automatic D8 catchment delineation. When provided,
                         the catchment area is computed from the polygon geometry
                         directly (no DEM traversal needed).
    - land_cover       : Rational-method land cover type — controls runoff
                         coefficient C in V = P × A × C.
    - desired_depth_m  : Target pond depth. Clamped to [1, 6] m in the engine.
    """
    land_polygon: Dict[str, Any] = Field(
        ...,
        description="GeoJSON Polygon of the land area selected on the map",
    )
    catchment_polygon: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional user-drawn GeoJSON Polygon for the catchment area. "
            "If omitted, catchment is delineated automatically via D8 flow routing."
        ),
    )
    land_cover: str = Field(
        default="agricultural",
        description="Land cover type: 'vegetated' | 'agricultural' | 'built_up' | 'default'",
    )
    desired_depth_m: float = Field(
        default=3.0,
        ge=0.5,
        le=10.0,
        description="Desired pond depth in metres",
    )


class PolygonAnalysisResponse(BaseModel):
    """
    Response for POST /api/analyze-polygon.

    All geometry is returned as GeoJSON so the frontend can add it directly
    to Leaflet as L.geoJSON(...).
    """
    # ── Pond location ───────────────────────────────────────────────────
    pond_location: Dict[str, Any] = Field(
        description="Best pond candidate: {lat, lon, elevation_m, slope_deg, land_status}"
    )
    pond_geometry: Any = Field(
        description="GeoJSON Polygon — physical footprint of the sized pond"
    )

    # ── Catchment ───────────────────────────────────────────────────────
    catchment_geometry: Any = Field(
        description="GeoJSON Polygon of the upstream catchment boundary"
    )
    catchment_area_m2: float
    catchment_area_ha: float

    # ── Hydrology ───────────────────────────────────────────────────────
    annual_rainfall_mm: float = Field(
        description="Mean annual rainfall at the pond centroid (mm), from Open-Meteo"
    )
    rainfall_years: int = Field(
        description="Number of years of rainfall data averaged"
    )
    rainfall_source: str = Field(
        description="API source used: 'open-meteo' | 'nasa-power' | 'unavailable'"
    )
    runoff_volume_m3: float = Field(
        description="Estimated annual runoff volume (m³) — V = P × A × C"
    )
    runoff_coefficient: float

    # ── Pond sizing ──────────────────────────────────────────────────────
    pond_depth_m: float
    pond_surface_area_m2: float
    pond_storage_capacity_m3: float

    # ── Scoring ──────────────────────────────────────────────────────────
    suitability_score: float = Field(
        description="Overall suitability S ∈ [0, 1]"
    )

    # ── Meta ──────────────────────────────────────────────────────────────
    catchment_source: str = Field(
        description="'user-drawn' | 'd8-delineated'"
    )
    note: str = Field(
        default=(
            "Preliminary estimate. Evaporation, infiltration, seepage, and "
            "sedimentation are excluded — a detailed engineering survey is "
            "required before construction."
        )
    )
