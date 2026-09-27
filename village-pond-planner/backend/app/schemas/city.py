"""
Pydantic schemas for city-based pond recommendation endpoints.
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class CitySearchResult(BaseModel):
    """Geocoded city metadata returned by GET /api/city/search."""
    display_name: str
    city: str
    state: Optional[str] = None
    lat: float
    lon: float
    bbox: Dict[str, float]          # {min_lat, max_lat, min_lon, max_lon}
    boundary_geojson: Optional[Any] = None   # GeoJSON Polygon/MultiPolygon


class CityRecommendationsRequest(BaseModel):
    """
    Request body for POST /api/city/recommendations.

    - city          : City or village name (e.g. "Bhilai").
    - state         : Helps disambiguate common names (e.g. "Chhattisgarh").
    - land_cover    : Rational-method land cover → runoff coefficient C.
    - desired_depth_m: Target pond depth, clamped to [1, 6] m.
    - n_sites       : Number of top sites to return (1–5, default 3).
    """
    city: str = Field(..., description="City or village name")
    state: Optional[str] = Field(default=None, description="State for disambiguation")
    land_cover: str = Field(default="agricultural")
    desired_depth_m: float = Field(default=3.0, ge=0.5, le=10.0)
    n_sites: int = Field(default=3, ge=1, le=5)


class SiteRecommendation(BaseModel):
    """Full details for one recommended pond site."""
    rank: int
    lat: float
    lon: float
    elevation_m: float
    slope_deg: float
    land_status: str
    terrain_score: float

    # Catchment
    catchment_geometry: Any
    catchment_area_ha: float

    # Hydrology
    annual_rainfall_mm: float
    runoff_volume_m3: float
    runoff_coefficient: float

    # Pond sizing
    pond_storage_capacity_m3: float
    pond_surface_area_m2: float
    pond_depth_m: float
    pond_geometry: Any          # GeoJSON circle footprint

    # Composite score
    suitability_score: float


class CityRecommendationsResponse(BaseModel):
    """
    Response for POST /api/city/recommendations.

    Includes city metadata, boundary geometry, and the ranked list of
    recommended pond sites — each with full catchment, hydrology, and sizing.
    """
    city_name: str
    display_name: str
    bbox: Dict[str, float]
    city_boundary: Optional[Any] = None     # GeoJSON for city outline
    recommendations: List[SiteRecommendation]
    annual_rainfall_mm: float               # city-centroid rainfall (shared)
    rainfall_source: str
    rainfall_years: int
    note: str = (
        "Top sites ranked by composite suitability score "
        "(terrain 40% + catchment 40% + land 20%). "
        "Preliminary estimates — engineering survey required."
    )
