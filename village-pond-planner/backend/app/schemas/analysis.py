"""
Pydantic schemas for analysis endpoints.
"""
from typing import Optional, Any, Dict, List
from pydantic import BaseModel, Field


# ─── Catchment ────────────────────────────────────────────────────────────────

class CatchmentRequest(BaseModel):
    site_id: int = Field(..., description="ID of the candidate site")
    lat: float = Field(..., description="Outlet point latitude")
    lon: float = Field(..., description="Outlet point longitude")
    village_id: int = Field(..., description="Village ID (for DEM lookup)")


class CatchmentResponse(BaseModel):
    site_id: int
    area_m2: float
    area_ha: float
    geometry_geojson: Any  # GeoJSON polygon
    avg_elevation_m: Optional[float] = None
    slope_summary: Optional[str] = None


# ─── Runoff ───────────────────────────────────────────────────────────────────

class RunoffRequest(BaseModel):
    catchment_area_m2: float
    annual_rainfall_mm: float
    land_cover: Optional[str] = "default"  # vegetated / agricultural / built_up / default


class RunoffResponse(BaseModel):
    catchment_area_m2: float
    annual_rainfall_mm: float
    runoff_coefficient: float
    runoff_volume_m3: float


# ─── Recommendation ───────────────────────────────────────────────────────────

class RecommendationRequest(BaseModel):
    runoff_volume_m3: float
    desired_depth_m: Optional[float] = None  # if None → use config default


class RecommendationResponse(BaseModel):
    runoff_volume_m3: float
    pond_depth_m: float
    pond_surface_area_m2: float
    pond_storage_capacity_m3: float
    note: str = (
        "Preliminary estimate. Evaporation, infiltration, seepage, and sedimentation "
        "are excluded — a detailed engineering survey is required before construction."
    )


# ─── Full Analysis ────────────────────────────────────────────────────────────

class FullAnalysisRequest(BaseModel):
    village_id: int
    site_lat: float
    site_lon: float
    land_cover: Optional[str] = "default"


class SiteScore(BaseModel):
    terrain_score: float
    catchment_score: float
    rainfall_score: float
    land_score: float
    total_score: float
    weights: dict[str, float]


class FullAnalysisResponse(BaseModel):
    village_id: int
    site_lat: float
    site_lon: float
    # Sub-results
    catchment: CatchmentResponse
    rainfall_annual_mm: float
    rainfall_years: int
    runoff: RunoffResponse
    recommendation: RecommendationResponse
    score: SiteScore
    # Raw candidate site id if persisted
    site_id: Optional[int] = None


# ─── Contour Upload ──────────────────────────────────────────────────────────

class CandidateSiteDetail(BaseModel):
    """
    Details for a single candidate pond site (used in top_candidates list).
    """
    rank: int = Field(description="1 = best candidate")
    lat: float
    lon: float
    elevation_m: float
    slope_deg: float
    land_status: str
    terrain_score: float = Field(description="Terrain suitability T ∈ [0,1]")
    land_score: float = Field(description="Land availability L ∈ [0,1]")
    flow_accum: Optional[int] = Field(
        default=None,
        description="D8 flow accumulation (higher = closer to a stream channel)",
    )
    # Per-candidate pond sizing (based on same catchment + rainfall as primary)
    pond_surface_area_m2: Optional[float] = Field(
        default=None,
        description="Estimated pond surface area if this site were chosen (m²)",
    )
    pond_geometry: Any = Field(
        default=None,
        description="GeoJSON Polygon representing the physical extent (border) of this candidate pond.",
    )
    suitability_score: Optional[float] = Field(
        default=None,
        description="Overall suitability score S ∈ [0,1] for this candidate",
    )


class ContourUploadResponse(BaseModel):
    """
    Response for POST /analyzeContour and POST /findCatchment.

    All values are derived from the uploaded KML/KMZ file; nothing is
    hard-coded.  The geometry_geojson field contains a GeoJSON Polygon
    representing the delineated upstream catchment.
    """

    # ── Terrain overview ───────────────────────────────────────────────────
    elevation_range_m: Dict[str, Any] = Field(
        description="Min / max / mean elevation and list of unique contour levels (metres)"
    )
    contour_count: int = Field(description="Number of contour lines parsed from the file")
    bounding_box: Dict[str, float] = Field(
        description="Geographic extent of the uploaded map {min_lat, max_lat, min_lon, max_lon}"
    )

    # ── Top-3 candidate pond locations ────────────────────────────────────
    pond_location: Dict[str, Any] = Field(
        description=(
            "Best-ranked candidate pond site derived from terrain analysis. "
            "Fields: lat, lon, elevation_m, slope_deg, land_status, and geometry_geojson."
        )
    )
    pond_geometry: Any = Field(
        default=None,
        description="GeoJSON Polygon representing the physical extent (border) of the primary pond based on its calculated surface area."
    )
    top_candidates: List[CandidateSiteDetail] = Field(
        default_factory=list,
        description=(
            "Top-3 candidate pond sites ranked by terrain score (descending). "
            "Each entry includes coordinates, scores, and a per-site pond estimate."
        ),
    )

    # ── Catchment ──────────────────────────────────────────────────────────
    catchment_area_m2: float = Field(description="Delineated catchment area in square metres")
    catchment_area_ha: float = Field(description="Delineated catchment area in hectares")
    catchment_geometry: Any = Field(
        description="GeoJSON Polygon of the upstream catchment boundary"
    )

    # ── Hydrology ──────────────────────────────────────────────────────────
    annual_rainfall_mm: float = Field(
        description="Annual rainfall used for runoff estimation (mm)"
    )
    runoff_volume_m3: float = Field(
        description="Estimated annual surface runoff volume (m³) — Rational Method V=PAC"
    )
    runoff_coefficient: float = Field(
        description="Rational Method runoff coefficient C ∈ [0, 1] for the given land cover"
    )

    # ── Pond sizing ────────────────────────────────────────────────────────
    pond_depth_m: float = Field(description="Recommended pond depth (metres)")
    pond_surface_area_m2: float = Field(description="Required pond surface area (m²)")
    pond_storage_capacity_m3: float = Field(
        description="Pond storage capacity (m³) = surface area × depth"
    )

    # ── Suitability scoring ────────────────────────────────────────────────
    suitability_score: float = Field(
        description="Overall suitability score S ∈ [0, 1] (weighted terrain+catchment+rainfall+land)"
    )
    score_breakdown: Dict[str, Any] = Field(
        description="Individual sub-scores and weights used in S = w₁T + w₂C + w₃R + w₄L"
    )

    # ── Metadata ───────────────────────────────────────────────────────────
    slope_summary: str = Field(description="Slope statistics within the catchment")
    avg_elevation_m: float = Field(description="Mean elevation within the catchment (metres)")
    method_notes: str = Field(
        description="Plain-English description of the algorithms and data sources used"
    )
