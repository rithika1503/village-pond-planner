"""
Suitability scoring module.

Formula (from plan §4.7):
  S = w1·T + w2·C + w3·R + w4·L

  T = terrain suitability score ∈ [0, 1]
  C = catchment suitability score ∈ [0, 1]
  R = rainfall/runoff potential score ∈ [0, 1]
  L = land availability score ∈ [0, 1]
  w1..w4 = configurable weights (default 0.30, 0.30, 0.20, 0.20)

Design note (viva-ready):
  This is an *explainable weighted decision-support model*, not a trained ML model.
  Weights are stored in `config.py` and can be tuned live during the demo.
  Each sub-score is normalised to [0, 1] with a clear physical interpretation.
  The model is intentionally transparent so that civil engineers can critique and
  adjust it without understanding machine learning.
"""

from __future__ import annotations

from app.config import settings


# ─── Sub-score Normalisation ───────────────────────────────────────────────────

def normalise_rainfall_score(annual_rainfall_mm: float) -> float:
    """
    R score: maps annual rainfall to [0, 1].

    Reference values (rough Indian context):
      < 400 mm  → arid (score ~ 0)
      400–600   → semi-arid (score ~ 0.3)
      600–1000  → sub-humid (score ~ 0.6)
      1000–1500 → humid (score ~ 0.8)
      > 1500    → very humid → pond fills quickly (score → 1.0)
    """
    r = float(annual_rainfall_mm)
    if r <= 0:
        return 0.0
    # Sigmoid-like ramp between 300 mm (0) and 1500 mm (1)
    score = min(1.0, max(0.0, (r - 300.0) / 1200.0))
    return round(score, 4)


def normalise_catchment_score(area_m2: float) -> float:
    """
    C score: larger catchment → more runoff → better site.

    Reference:
      < 10 ha  (100,000 m²) → score < 0.3
      ~50 ha                → score ~ 0.5
      > 200 ha              → score → 1.0
    """
    if area_m2 <= 0:
        return 0.0
    area_ha = area_m2 / 10_000.0
    score = min(1.0, area_ha / 200.0)
    return round(score, 4)


# ─── Total Score ──────────────────────────────────────────────────────────────

def compute_suitability_score(
    terrain_score: float,
    catchment_area_m2: float,
    annual_rainfall_mm: float,
    land_score: float,
    *,
    w_terrain: float = settings.WEIGHT_TERRAIN,
    w_catchment: float = settings.WEIGHT_CATCHMENT,
    w_rainfall: float = settings.WEIGHT_RAINFALL,
    w_land: float = settings.WEIGHT_LAND,
) -> dict:
    """
    Compute the overall suitability score S ∈ [0, 1].

    Returns dict with individual sub-scores, weights, and total score.
    """
    # Validate weights
    total_w = w_terrain + w_catchment + w_rainfall + w_land
    if not (0.99 < total_w < 1.01):
        # Normalise if caller passed non-unit weights
        w_terrain /= total_w
        w_catchment /= total_w
        w_rainfall /= total_w
        w_land /= total_w

    # Clamp inputs
    T = max(0.0, min(1.0, terrain_score))
    C = normalise_catchment_score(catchment_area_m2)
    R = normalise_rainfall_score(annual_rainfall_mm)
    L = max(0.0, min(1.0, land_score))

    S = w_terrain * T + w_catchment * C + w_rainfall * R + w_land * L

    return {
        "terrain_score": round(T, 4),
        "catchment_score": round(C, 4),
        "rainfall_score": round(R, 4),
        "land_score": round(L, 4),
        "total_score": round(S, 4),
        "weights": {
            "terrain": round(w_terrain, 3),
            "catchment": round(w_catchment, 3),
            "rainfall": round(w_rainfall, 3),
            "land": round(w_land, 3),
        },
    }
