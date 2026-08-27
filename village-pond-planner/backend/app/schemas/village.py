"""
Pydantic schemas for Village endpoints.
"""
from typing import Optional, Any
from pydantic import BaseModel, Field


class VillageBase(BaseModel):
    name: str
    district: Optional[str] = None
    state: Optional[str] = None
    population: Optional[int] = None


class VillageCreate(VillageBase):
    centroid_lat: float
    centroid_lon: float


class VillageSummary(VillageBase):
    id: int
    centroid_lat: Optional[float] = None
    centroid_lon: Optional[float] = None
    bbox: Optional[list[float]] = Field(
        None, description="[minx, miny, maxx, maxy] in WGS-84"
    )

    model_config = {"from_attributes": True}


class VillageDetail(VillageSummary):
    boundary_geojson: Optional[Any] = None  # raw GeoJSON geometry
