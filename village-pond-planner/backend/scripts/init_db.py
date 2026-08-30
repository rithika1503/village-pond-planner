"""
DB initialisation and demo village seeding script.

Run once after PostGIS is up:
    python -m scripts.init_db

This script:
  1. Creates all tables (including PostGIS extensions).
  2. Seeds a demo village (Ralegan Siddhi, Maharashtra) for the demo fallback.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import os

# Allow `python -m scripts.init_db` from the backend/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.config import settings
from app.database import Base
from app.models.orm import Village  # ensure models are imported before create_all

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Demo village: Ralegan Siddhi, Ahmednagar district, Maharashtra
_DEMO_VILLAGE = {
    "name": "Ralegan Siddhi",
    "district": "Ahmednagar",
    "state": "Maharashtra",
    "population": 2500,
    "centroid_lat": 19.1603,
    "centroid_lon": 74.4678,
    # Approximate 0.1° bounding box
    "bbox_minx": 74.4178,
    "bbox_miny": 19.1103,
    "bbox_maxx": 74.5178,
    "bbox_maxy": 19.2103,
}


async def init_db():
    engine = create_async_engine(settings.DATABASE_URL, echo=True)
    async with engine.begin() as conn:
        # Enable PostGIS extension
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        # Create all tables
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Tables created.")

    # Seed demo village if not present
    SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as session:
        from sqlalchemy import select
        existing = await session.execute(
            select(Village).where(Village.name == _DEMO_VILLAGE["name"])
        )
        if existing.scalar_one_or_none() is None:
            v = Village(
                name=_DEMO_VILLAGE["name"],
                district=_DEMO_VILLAGE["district"],
                state=_DEMO_VILLAGE["state"],
                population=_DEMO_VILLAGE["population"],
                bbox_minx=_DEMO_VILLAGE["bbox_minx"],
                bbox_miny=_DEMO_VILLAGE["bbox_miny"],
                bbox_maxx=_DEMO_VILLAGE["bbox_maxx"],
                bbox_maxy=_DEMO_VILLAGE["bbox_maxy"],
            )
            session.add(v)
            await session.commit()
            logger.info("Demo village '%s' seeded.", _DEMO_VILLAGE["name"])
        else:
            logger.info("Demo village already exists — skipping seed.")

    await engine.dispose()
    logger.info("DB initialisation complete.")


if __name__ == "__main__":
    asyncio.run(init_db())
