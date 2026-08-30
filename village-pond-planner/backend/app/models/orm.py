"""
SQLAlchemy ORM models — all 5 tables from the implementation plan.
Uses GeoAlchemy2 for PostGIS geometry columns.
"""
from datetime import datetime
from typing import Optional

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey,
    Integer, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Village(Base):
    __tablename__ = "villages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    district: Mapped[Optional[str]] = mapped_column(String(200))
    state: Mapped[Optional[str]] = mapped_column(String(200))
    # PostGIS geometry columns
    boundary: Mapped[Optional[object]] = mapped_column(
        Geometry("MULTIPOLYGON", srid=4326), nullable=True
    )
    centroid: Mapped[Optional[object]] = mapped_column(
        Geometry("POINT", srid=4326), nullable=True
    )
    bbox_minx: Mapped[Optional[float]] = mapped_column(Float)
    bbox_miny: Mapped[Optional[float]] = mapped_column(Float)
    bbox_maxx: Mapped[Optional[float]] = mapped_column(Float)
    bbox_maxy: Mapped[Optional[float]] = mapped_column(Float)
    population: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    candidate_sites: Mapped[list["CandidateSite"]] = relationship(
        back_populates="village", cascade="all, delete-orphan"
    )


class CandidateSite(Base):
    __tablename__ = "candidate_sites"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    village_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("villages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    elevation_m: Mapped[Optional[float]] = mapped_column(Float)
    slope_deg: Mapped[Optional[float]] = mapped_column(Float)
    land_status: Mapped[Optional[str]] = mapped_column(
        String(50)
    )  # government / private / unknown / water-body / built-up
    terrain_score: Mapped[Optional[float]] = mapped_column(Float)
    catchment_score: Mapped[Optional[float]] = mapped_column(Float)
    rainfall_score: Mapped[Optional[float]] = mapped_column(Float)
    land_score: Mapped[Optional[float]] = mapped_column(Float)
    total_score: Mapped[Optional[float]] = mapped_column(Float)
    is_eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    village: Mapped["Village"] = relationship(back_populates="candidate_sites")
    catchments: Mapped[list["Catchment"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    analysis_results: Mapped[list["AnalysisResult"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )


class Catchment(Base):
    __tablename__ = "catchments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("candidate_sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    geometry: Mapped[Optional[object]] = mapped_column(
        Geometry("POLYGON", srid=4326), nullable=True
    )
    area_m2: Mapped[Optional[float]] = mapped_column(Float)
    avg_elevation_m: Mapped[Optional[float]] = mapped_column(Float)
    slope_summary: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    site: Mapped["CandidateSite"] = relationship(back_populates="catchments")


class RainfallRecord(Base):
    __tablename__ = "rainfall"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    jan_mm: Mapped[Optional[float]] = mapped_column(Float)
    feb_mm: Mapped[Optional[float]] = mapped_column(Float)
    mar_mm: Mapped[Optional[float]] = mapped_column(Float)
    apr_mm: Mapped[Optional[float]] = mapped_column(Float)
    may_mm: Mapped[Optional[float]] = mapped_column(Float)
    jun_mm: Mapped[Optional[float]] = mapped_column(Float)
    jul_mm: Mapped[Optional[float]] = mapped_column(Float)
    aug_mm: Mapped[Optional[float]] = mapped_column(Float)
    sep_mm: Mapped[Optional[float]] = mapped_column(Float)
    oct_mm: Mapped[Optional[float]] = mapped_column(Float)
    nov_mm: Mapped[Optional[float]] = mapped_column(Float)
    dec_mm: Mapped[Optional[float]] = mapped_column(Float)
    annual_total_mm: Mapped[Optional[float]] = mapped_column(Float)
    source: Mapped[Optional[str]] = mapped_column(String(50))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AnalysisResult(Base):
    __tablename__ = "analysis_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("candidate_sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rainfall_annual_mm: Mapped[Optional[float]] = mapped_column(Float)
    catchment_area_m2: Mapped[Optional[float]] = mapped_column(Float)
    runoff_volume_m3: Mapped[Optional[float]] = mapped_column(Float)
    runoff_coeff: Mapped[Optional[float]] = mapped_column(Float)
    pond_depth_m: Mapped[Optional[float]] = mapped_column(Float)
    pond_surface_area_m2: Mapped[Optional[float]] = mapped_column(Float)
    pond_storage_capacity_m3: Mapped[Optional[float]] = mapped_column(Float)
    suitability_score: Mapped[Optional[float]] = mapped_column(Float)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    site: Mapped["CandidateSite"] = relationship(back_populates="analysis_results")
