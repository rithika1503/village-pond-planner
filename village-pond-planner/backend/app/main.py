"""
FastAPI application entry point.

Architecture:
  - FastAPI validates and orchestrates; all geospatial math lives in `geospatial/`.
  - All geometry crosses the API boundary as GeoJSON (never raw rasters).
  - Async throughout for non-blocking I/O with the DB and external APIs.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.routers import villages, analysis, contour

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    logger.info("Starting %s v%s", settings.APP_NAME, settings.APP_VERSION)
    yield
    logger.info("Shutting down")


# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "AI-based Village Pond Planning System — "
        "terrain analysis, catchment delineation, rainfall integration, "
        "runoff estimation, pond sizing, and suitability scoring."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(villages.router)
app.include_router(analysis.router)
app.include_router(contour.router)


# ─── Health check ─────────────────────────────────────────────────────────────

@app.get("/health", tags=["system"])
async def health():
    """Liveness probe."""
    return {"status": "ok", "version": settings.APP_VERSION}


# ─── Global error handler ─────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred. Please try again."},
    )
