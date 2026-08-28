"""
Hydrology module — runoff estimation and pond sizing.

Formulas (from plan §4.5 and §4.6):

  Runoff:
    V = P × A × C
    V = runoff volume (m³)
    P = annual rainfall depth, mm → m (÷ 1000)
    A = catchment area (m²)
    C = rational runoff coefficient (dimensionless, ∈ [0, 1])

  Pond sizing (preliminary):
    Target storage ≈ estimated annual runoff volume
    Surface Area = Volume / Depth
    Depth is clamped to [POND_MIN_DEPTH_M, POND_MAX_DEPTH_M] from config.

  DISCLAIMER: This is a first-order estimate.
  Actual design must account for: evaporation, infiltration, seepage,
  sedimentation rate, soil permeability, and seasonal distribution of rainfall.

Design note (viva-ready):
  The Rational Method (V = PAC) is the standard first approximation used by
  civil engineers for small catchments (<5 km²).  The runoff coefficient C
  accounts for how much rainfall becomes surface runoff rather than infiltrating
  or evaporating.  Values here are taken from IS:SP:13-1980 and CPHEEO guidelines.
"""

from __future__ import annotations

from app.config import settings


# ─── Runoff Coefficients ───────────────────────────────────────────────────────

_RUNOFF_COEFFICIENTS: dict[str, float] = {
    "vegetated": settings.RUNOFF_COEFF_VEGETATED,
    "agricultural": settings.RUNOFF_COEFF_AGRICULTURAL,
    "built_up": settings.RUNOFF_COEFF_BUILT_UP,
    "built-up": settings.RUNOFF_COEFF_BUILT_UP,
    "default": settings.RUNOFF_COEFF_DEFAULT,
    "unknown": settings.RUNOFF_COEFF_DEFAULT,
    "government": settings.RUNOFF_COEFF_AGRICULTURAL,
    "private": settings.RUNOFF_COEFF_DEFAULT,
}


def get_runoff_coefficient(land_cover: str) -> float:
    """Return the rational runoff coefficient C for the given land cover type."""
    key = land_cover.lower().strip()
    return _RUNOFF_COEFFICIENTS.get(key, settings.RUNOFF_COEFF_DEFAULT)


def estimate_runoff_volume(
    catchment_area_m2: float,
    annual_rainfall_mm: float,
    land_cover: str = "default",
) -> dict:
    """
    Estimate annual surface runoff volume using the Rational Method.

    Args:
        catchment_area_m2  : Catchment area in m².
        annual_rainfall_mm : Mean annual rainfall in mm.
        land_cover         : Land cover type (controls C coefficient).

    Returns dict with:
        catchment_area_m2, annual_rainfall_mm, runoff_coefficient, runoff_volume_m3
    """
    if catchment_area_m2 <= 0:
        raise ValueError("catchment_area_m2 must be positive")
    if annual_rainfall_mm < 0:
        raise ValueError("annual_rainfall_mm cannot be negative")

    C = get_runoff_coefficient(land_cover)
    rainfall_m = annual_rainfall_mm / 1000.0  # mm → m
    volume_m3 = rainfall_m * catchment_area_m2 * C

    return {
        "catchment_area_m2": round(catchment_area_m2, 2),
        "annual_rainfall_mm": round(annual_rainfall_mm, 2),
        "runoff_coefficient": round(C, 3),
        "runoff_volume_m3": round(volume_m3, 2),
    }


def size_pond(
    runoff_volume_m3: float,
    desired_depth_m: float | None = None,
) -> dict:
    """
    Estimate pond dimensions from target storage volume.

    Assumes:
      - Target storage = estimated annual runoff volume.
      - Surface area = volume / depth.
      - Depth is supplied by caller or defaulted from config.
      - Depth is clamped to [POND_MIN_DEPTH_M, POND_MAX_DEPTH_M].

    Returns dict with:
        runoff_volume_m3, pond_depth_m, pond_surface_area_m2,
        pond_storage_capacity_m3, note
    """
    if runoff_volume_m3 <= 0:
        raise ValueError("runoff_volume_m3 must be positive")

    if desired_depth_m is None:
        depth_m = settings.POND_DEFAULT_DEPTH_M
    else:
        depth_m = float(desired_depth_m)

    depth_m = max(settings.POND_MIN_DEPTH_M, min(depth_m, settings.POND_MAX_DEPTH_M))

    surface_area_m2 = runoff_volume_m3 / depth_m
    # Storage capacity = volume (by definition, same as runoff target here)
    storage_m3 = surface_area_m2 * depth_m

    note = (
        "Preliminary estimate based on Rational Method. "
        "Evaporation, infiltration, seepage, sedimentation, and soil permeability "
        "are excluded — a detailed engineering survey is required before construction."
    )

    return {
        "runoff_volume_m3": round(runoff_volume_m3, 2),
        "pond_depth_m": round(depth_m, 2),
        "pond_surface_area_m2": round(surface_area_m2, 2),
        "pond_storage_capacity_m3": round(storage_m3, 2),
        "note": note,
    }
