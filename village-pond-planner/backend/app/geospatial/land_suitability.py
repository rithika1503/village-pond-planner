"""
Land suitability module.

Responsibilities:
  - Classify candidate cells by simulated land status (government / private /
    unknown / water-body / built-up).
  - Filter ineligible land (water-body, built-up, protected).
  - Score and rank candidates; return top-N sites.

Design note (viva-ready):
  In a production system, land status would come from Bhunaksha shapefiles or a
  land-records API.  Here we use a deterministic heuristic (low elevation +
  proximity to drainage lines → likely government / low-value land) with an
  explicit note that this is an *indication*, not a legal verification.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

# Land statuses that are NOT eligible for pond construction
_INELIGIBLE_STATUSES = {"water-body", "built-up", "protected"}

# Scoring weights for land score component (within this module)
_LAND_STATUS_SCORE: dict[str, float] = {
    "government": 1.0,
    "unknown": 0.6,
    "private": 0.3,
    "built-up": 0.0,
    "water-body": 0.0,
    "protected": 0.0,
}


def classify_land_status(
    lat: float,
    lon: float,
    elevation_m: float,
    slope_deg: float,
    *,
    flow_accum: int = 0,
    max_flow_accum: int = 1,
) -> str:
    """
    Fully deterministic terrain-based land-status classification.
    No randomness — identical inputs always produce identical output.

    Rule 1  slope > 15° → 'built-up'
        Rocky / too steep for excavation.

    Rule 2  slope < 0.5° AND elev < 200 m → 'unknown'
        Very flat lowland: may be a seasonal drainage plain or existing
        water body — needs field verification.

    Rule 3  flow accumulation ratio ≥ 0.70 → 'government'
        Strong drainage convergence zone (top 30% of candidate set).
        In Indian land-records practice, nala beds and low-lying drainage
        depressions are overwhelmingly government revenue/wasteland —
        the primary targets of MGNREGA watershed and PMKSY programs.

    Rule 4  flow accumulation ratio 0.30–0.70 → 'unknown'
        Moderate convergence; ownership indeterminate without records.

    Rule 5  low accumulation AND slope > 3° → 'private'
        Upland cell with little convergence — typically private farmland.

    Rule 6  all other cases → 'unknown'

    Production note:
        Replace with a Bhunaksha shapefile query or DILRMP REST API call
        to retrieve actual survey-number-level ownership data.
    """
    # Rule 1: steep = rocky / not excavatable
    if slope_deg > 15:
        return "built-up"

    # Rule 2: very flat + low = drainage plain / potential water body
    if slope_deg < 0.5 and elevation_m < 200:
        return "unknown"

    # Rules 3–6: driven by flow accumulation ratio
    accum_ratio = flow_accum / max(1, max_flow_accum)

    if accum_ratio >= 0.70:
        return "government"   # strong convergence → drainage land (govt)
    if accum_ratio >= 0.30:
        return "unknown"      # moderate convergence → mixed ownership
    if slope_deg > 3.0:
        return "private"      # upland, low convergence → likely private
    return "unknown"



def score_terrain(
    slope_deg: float,
    elevation_m: float,
    elev_threshold: float,
    flow_accum: int = 0,
    max_accum: int = 1,
    min_accum: int = 0,
) -> float:
    """
    Terrain sub-score T ∈ [0, 1].

    Higher score for:
      - Lower slope (flatter land → easier excavation, less runoff loss).
      - Lower elevation relative to the study area (sits in a valley bottom).
      - Higher flow accumulation relative to non-stream cells (more upslope
        drainage converges here → more water collected into the pond).

    Weights:
      40% slope + 30% relative elevation + 30% flow accumulation
    """
    # Slope: 0° = perfect, slope_threshold = 0
    slope_score = max(0.0, 1.0 - slope_deg / settings.SLOPE_THRESHOLD_DEG)

    # Elevation: cells below the threshold score 1.0, linearly decaying above it
    elev_above = max(0.0, elevation_m - elev_threshold)
    elev_score = max(0.0, 1.0 - elev_above / 30.0)

    # Flow accumulation: cells that collect more water score higher
    accum_range = max(1, max_accum - min_accum)
    accum_score = float(flow_accum - min_accum) / accum_range

    return 0.40 * slope_score + 0.30 * elev_score + 0.30 * accum_score


def rank_candidates(
    raw_candidates: list[dict],
    *,
    max_sites: int = settings.MAX_CANDIDATE_SITES,
    elev_threshold: Optional[float] = None,
) -> list[dict]:
    """
    Take raw candidate cells (from terrain.identify_candidate_cells),
    classify land status, compute terrain + land scores (incorporating
    flow accumulation), filter ineligible land, and return the top-`max_sites`
    candidates ranked by terrain_score.

    The terrain score now uses flow accumulation as a positive signal:
    cells that collect more water from upslope are better pond sites,
    as long as they are not *inside* an active river channel (which the
    upstream identify_candidate_cells already filters out via stream_mask).
    """
    if not raw_candidates:
        return []

    # Use the full candidate elevation range for normalisation so the scoring
    # differentiates within the actual candidate set, not an arbitrary offset.
    elevs = [c["elevation_m"] for c in raw_candidates]
    if elev_threshold is None:
        elev_threshold = float(np.percentile(elevs, 30)) if elevs else 0.0

    # Normalise flow accumulation across the candidate set (not the stream cells)
    accum_vals = [c.get("flow_accum", 0) for c in raw_candidates]
    min_accum = int(min(accum_vals)) if accum_vals else 0
    max_accum = int(max(accum_vals)) if accum_vals else 1

    scored: list[dict] = []
    for c in raw_candidates:
        land_status = classify_land_status(
            c["lat"], c["lon"], c["elevation_m"], c["slope_deg"],
            flow_accum=c.get("flow_accum", 0),
            max_flow_accum=max_accum,
        )
        if land_status in _INELIGIBLE_STATUSES:
            continue  # filter out

        t_score = score_terrain(
            c["slope_deg"],
            c["elevation_m"],
            elev_threshold,
            flow_accum=c.get("flow_accum", 0),
            max_accum=max_accum,
            min_accum=min_accum,
        )
        l_score = _LAND_STATUS_SCORE.get(land_status, 0.5)

        scored.append(
            {
                **c,
                "land_status": land_status,
                "terrain_score": round(t_score, 4),
                "land_score": round(l_score, 4),
                "is_eligible": True,
            }
        )

    # Sort descending by terrain_score (which now captures slope + elevation +
    # flow accumulation), then land_score as tiebreaker
    scored.sort(key=lambda x: (x["terrain_score"], x["land_score"]), reverse=True)
    return scored[:max_sites]
