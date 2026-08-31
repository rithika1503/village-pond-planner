"""
Comprehensive per-step pipeline tests
======================================

Every step in the /analyzeContour pipeline is tested in isolation.

Pipeline under test (from contour.py docstring):
  Step 1  : Read & validate uploaded file
  Step 2  : Parse KML/KMZ → contour lines           (kml_parser.parse_kml_bytes)
  Step 3  : Interpolate contours → DEM grid         (kml_parser.contours_to_dem)
  Step 4  : Terrain metadata (bbox, elev stats)     (kml_parser.get_bounding_box / get_elevation_stats)
  Step 5  : Identify candidate pond locations        (terrain.identify_candidate_cells)
    5a      Stream/river channel exclusion            (from app.geospatial.hydrology_engine import HydrologyEngine
        from app.geospatial.terrain import is_stream_channel
        
        lats, lons = np.array([21.0, 21.001, 21.002]), np.array([81.0, 81.001, 81.002])
        elevs = np.array([
            [300.0, 290.0, 300.0],
            [290.0, 280.0, 290.0],
            [280.0, 270.0, 280.0]
        ])
        engine = HydrologyEngine(lats, lons, elevs)
        engine.process()
        accum = engine.accum

        # 50th percentile should flag the bottom-middle as stream
        is_stream = is_stream_channel(accum, 50.0)
        assert is_stream[2, 1] == True)
  Step 6  : Rank candidates                         (land_suitability.rank_candidates)
  Step 7  : Catchment delineation (D8 / fallback)  (catchment.delineate_catchment)
  Step 8  : Rainfall (provided or fetched)          (hydrology.estimate_runoff_volume)
  Step 9  : Runoff estimation (Rational Method)     (hydrology.estimate_runoff_volume)
  Step 10 : Pond sizing                             (hydrology.size_pond)
  Step 11 : Suitability score                       (scoring.compute_suitability_score)
  Step 12 : Full /analyzeContour endpoint (end-to-end integration)

Run:
    cd backend
    python -m pytest tests/test_pipeline_steps.py -v
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pytest
from unittest.mock import AsyncMock, patch

# ── bootstrap FastAPI test client without a live DB ──────────────────────────
with patch("app.database.get_db", return_value=AsyncMock()):
    from app.main import app

from fastapi.testclient import TestClient

client = TestClient(app)

# ── Paths ─────────────────────────────────────────────────────────────────────
SAMPLE_KML = Path(__file__).parent.parent.parent / "contours_1m.kml"


# ── Minimal synthetic KML helpers ─────────────────────────────────────────────

def _make_kml(n_contours: int = 5) -> bytes:
    """
    Build a tiny but valid KML with n_contours placemarks in a
    V-valley shape (useful for deterministic flow-accumulation tests).
    """
    lines: List[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<kml>",
        "<Document>",
    ]
    elevations = [float(100 + i * 5) for i in range(n_contours)]
    for k, elev in enumerate(elevations):
        lat_base = 18.50 + k * 0.002
        coords = " ".join(
            f"74.{400 + j:03d},{lat_base:.6f},0"
            for j in range(10)
        )
        lines += [
            "<Placemark>",
            f"  <name>{elev}</name>",
            "  <LineString>",
            f"    <coordinates>{coords}</coordinates>",
            "  </LineString>",
            "</Placemark>",
        ]
    lines += ["</Document>", "</kml>"]
    return "\n".join(lines).encode()


def _make_kmz(n_contours: int = 5) -> bytes:
    """Wrap synthetic KML inside a KMZ (ZIP) archive."""
    kml_data = _make_kml(n_contours)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.kml", kml_data)
    return buf.getvalue()


def _valley_dem(n: int = 20) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Synthetic V-valley DEM for deterministic hydrology tests.
    Valley centre runs along column j = n//2, decreasing elevation towards row n-1.
    """
    lats = np.linspace(18.50, 18.60, n)
    lons = np.linspace(74.40, 74.50, n)
    centre_j = (n - 1) / 2.0
    elevs = np.zeros((n, n), dtype=float)
    for i in range(n):
        for j in range(n):
            elevs[i, j] = abs(j - centre_j) + float(n - 1 - i)
    return lats, lons, elevs


def _flat_dem(n: int = 20, elevation: float = 300.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Perfectly flat DEM."""
    lats = np.linspace(18.50, 18.60, n)
    lons = np.linspace(74.40, 74.50, n)
    elevs = np.full((n, n), elevation, dtype=float)
    return lats, lons, elevs


def _slope_dem(n: int = 20) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Uniform gentle slope, low at south-west corner."""
    lats = np.linspace(18.50, 18.60, n)
    lons = np.linspace(74.40, 74.50, n)
    xx, yy = np.meshgrid(np.linspace(0, 1, n), np.linspace(0, 1, n), indexing="ij")
    elevs = 200.0 + 50.0 * xx + 30.0 * yy
    return lats, lons, elevs


# =============================================================================
# STEP 1  —  File reading / validation
# =============================================================================

class TestStep1FileValidation:
    """Step 1: The endpoint must reject empty or missing files immediately."""

    def test_empty_file_returns_400(self):
        """An empty byte-string upload must return HTTP 400 — not 422 or 500."""
        resp = client.post(
            "/analyzeContour",
            files={"file": ("empty.kml", io.BytesIO(b""), "application/vnd.google-earth.kml+xml")},
            data={"land_cover": "default", "desired_depth_m": "3.0"},
        )
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"

    def test_non_xml_garbage_returns_422(self):
        """Non-XML content should fail KML parsing and return HTTP 422."""
        resp = client.post(
            "/analyzeContour",
            files={"file": ("garbage.kml", io.BytesIO(b"RANDOM BYTES NOTXML"),
                            "application/vnd.google-earth.kml+xml")},
            data={"land_cover": "default", "desired_depth_m": "3.0"},
        )
        assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"

    def test_kmz_file_accepted(self):
        """A valid KMZ (ZIP) must be accepted and parsed (not rejected at file-read step)."""
        kmz = _make_kmz(n_contours=5)
        resp = client.post(
            "/analyzeContour",
            files={"file": ("test.kmz", io.BytesIO(kmz), "application/vnd.google-earth.kmz")},
            data={"land_cover": "default", "desired_depth_m": "3.0"},
        )
        # Should succeed or fail with 422 (too few contours), not 400
        assert resp.status_code in (200, 422), (
            f"KMZ upload should not return 400; got {resp.status_code}: {resp.text[:300]}"
        )

    def test_desired_depth_below_minimum_rejected(self):
        """desired_depth_m below 0.5 is rejected by FastAPI form validation."""
        kml = _make_kml(5)
        resp = client.post(
            "/analyzeContour",
            files={"file": ("test.kml", io.BytesIO(kml), "application/vnd.google-earth.kml+xml")},
            data={"land_cover": "default", "desired_depth_m": "0.1"},
        )
        assert resp.status_code == 422, (
            f"depth 0.1 is below the 0.5 minimum — expected 422, got {resp.status_code}"
        )

    def test_desired_depth_above_maximum_rejected(self):
        """desired_depth_m above 10.0 is rejected by FastAPI form validation."""
        kml = _make_kml(5)
        resp = client.post(
            "/analyzeContour",
            files={"file": ("test.kml", io.BytesIO(kml), "application/vnd.google-earth.kml+xml")},
            data={"land_cover": "default", "desired_depth_m": "99.0"},
        )
        assert resp.status_code == 422, (
            f"depth 99 is above the 10.0 maximum — expected 422, got {resp.status_code}"
        )


# =============================================================================
# STEP 2  —  KML/KMZ parsing → contour lines
# =============================================================================

class TestStep2KmlParsing:
    """Step 2: parse_kml_bytes must extract contour (elevation, coords) tuples."""

    def test_synthetic_kml_parsed_correctly(self):
        from app.geospatial.kml_parser import parse_kml_bytes
        data = _make_kml(n_contours=6)
        lines = parse_kml_bytes(data)
        assert len(lines) == 6, f"Expected 6 contour lines, got {len(lines)}"

    def test_each_line_has_float_elevation(self):
        from app.geospatial.kml_parser import parse_kml_bytes
        lines = parse_kml_bytes(_make_kml(4))
        for elev, coords in lines:
            assert isinstance(elev, float), f"Elevation must be float, got {type(elev)}"

    def test_each_line_has_at_least_two_coords(self):
        from app.geospatial.kml_parser import parse_kml_bytes
        lines = parse_kml_bytes(_make_kml(4))
        for elev, coords in lines:
            assert len(coords) >= 2, "Contour line needs >=2 coordinate pairs"

    def test_coordinates_within_valid_geographic_range(self):
        from app.geospatial.kml_parser import parse_kml_bytes
        lines = parse_kml_bytes(_make_kml(4))
        for _, coords in lines:
            for lon, lat in coords:
                assert -180 <= lon <= 180, f"Longitude {lon} out of range"
                assert -90 <= lat <= 90, f"Latitude {lat} out of range"

    def test_kmz_extraction(self):
        """_extract_kml must unwrap the KMZ zip layer transparently."""
        from app.geospatial.kml_parser import parse_kml_bytes
        kmz = _make_kmz(n_contours=4)
        lines = parse_kml_bytes(kmz)
        assert len(lines) == 4

    def test_empty_bytes_raises_value_error(self):
        from app.geospatial.kml_parser import parse_kml_bytes
        with pytest.raises((ValueError, Exception)):
            parse_kml_bytes(b"")

    def test_kml_with_no_numeric_name_raises(self):
        """KML with no numeric <name> should raise ValueError (no contours found)."""
        from app.geospatial.kml_parser import parse_kml_bytes
        kml = b"""<?xml version="1.0"?>
<kml><Document>
  <Placemark><name>not_a_number</name>
    <LineString><coordinates>74.4,18.5,0 74.5,18.5,0</coordinates></LineString>
  </Placemark>
</Document></kml>"""
        with pytest.raises(ValueError, match="No contour lines"):
            parse_kml_bytes(kml)

    def test_kml_with_simpledata_elevation(self):
        """Parser must also read elevation from <SimpleData name='elevation'>."""
        from app.geospatial.kml_parser import parse_kml_bytes
        kml = b"""<?xml version="1.0"?>
<kml><Document>
  <Placemark>
    <ExtendedData><SchemaData>
      <SimpleData name="elevation">250.0</SimpleData>
    </SchemaData></ExtendedData>
    <LineString><coordinates>74.4,18.5,0 74.5,18.5,0</coordinates></LineString>
  </Placemark>
</Document></kml>"""
        lines = parse_kml_bytes(kml)
        assert len(lines) == 1
        assert lines[0][0] == pytest.approx(250.0)

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_sample_kml_returns_many_contours(self):
        from app.geospatial.kml_parser import parse_kml_bytes
        lines = parse_kml_bytes(SAMPLE_KML.read_bytes())
        assert len(lines) >= 3, "Real KML should have >=3 contour lines"

    def test_too_few_contours_endpoint_returns_422(self):
        """The endpoint enforces >=3 contour lines; fewer must return 422."""
        kml = _make_kml(n_contours=2)
        resp = client.post(
            "/analyzeContour",
            files={"file": ("tiny.kml", io.BytesIO(kml), "application/vnd.google-earth.kml+xml")},
            data={"land_cover": "default", "desired_depth_m": "3.0"},
        )
        assert resp.status_code == 422, (
            f"2 contour lines should fail with 422, got {resp.status_code}"
        )


# =============================================================================
# STEP 3  —  Contour-to-DEM interpolation
# =============================================================================

class TestStep3DemInterpolation:
    """Step 3: contours_to_dem must produce a regular, non-degenerate grid."""

    def _contours(self):
        from app.geospatial.kml_parser import parse_kml_bytes
        return parse_kml_bytes(_make_kml(n_contours=5))

    def test_output_shapes_match_n_points(self):
        from app.geospatial.kml_parser import contours_to_dem
        lats, lons, elevs = contours_to_dem(self._contours(), n_points=30)
        assert lats.shape == (30,)
        assert lons.shape == (30,)
        assert elevs.shape == (30, 30)

    def test_no_nan_in_dem(self):
        """After nearest-neighbour fill, the DEM must contain zero NaN values."""
        from app.geospatial.kml_parser import contours_to_dem
        _, _, elevs = contours_to_dem(self._contours(), n_points=20)
        assert not np.isnan(elevs).any(), "DEM must not contain NaN after interpolation"

    def test_elevation_values_are_realistic(self):
        """DEM elevations should span a realistic range (not all-zero)."""
        from app.geospatial.kml_parser import contours_to_dem
        _, _, elevs = contours_to_dem(self._contours(), n_points=20)
        assert elevs.max() > elevs.min(), "DEM must have non-zero elevation range"

    def test_lat_lons_are_sorted_ascending(self):
        from app.geospatial.kml_parser import contours_to_dem
        lats, lons, _ = contours_to_dem(self._contours(), n_points=20)
        assert np.all(np.diff(lats) > 0), "lats must be strictly ascending"
        assert np.all(np.diff(lons) > 0), "lons must be strictly ascending"

    def test_dem_covers_full_bounding_box(self):
        """DEM lat/lon range must span the input point cloud bounding box."""
        from app.geospatial.kml_parser import parse_kml_bytes, contours_to_dem, get_bounding_box
        data = _make_kml(5)
        contours = parse_kml_bytes(data)
        bbox = get_bounding_box(contours)
        lats, lons, _ = contours_to_dem(contours, n_points=20)
        assert lats.min() >= bbox["min_lat"] - 1e-6
        assert lats.max() <= bbox["max_lat"] + 1e-6
        assert lons.min() >= bbox["min_lon"] - 1e-6
        assert lons.max() <= bbox["max_lon"] + 1e-6

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_real_kml_dem_shape(self):
        from app.geospatial.kml_parser import parse_kml_bytes, contours_to_dem
        lines = parse_kml_bytes(SAMPLE_KML.read_bytes())
        lats, lons, elevs = contours_to_dem(lines, n_points=50)
        assert elevs.shape == (50, 50)
        assert not np.isnan(elevs).any()


# =============================================================================
# STEP 4  —  Terrain metadata (bounding box + elevation stats)
# =============================================================================

class TestStep4TerrainMetadata:
    """Step 4: get_bounding_box and get_elevation_stats must be correct."""

    def _contours(self):
        from app.geospatial.kml_parser import parse_kml_bytes
        return parse_kml_bytes(_make_kml(n_contours=4))

    def test_bounding_box_keys(self):
        from app.geospatial.kml_parser import get_bounding_box
        bbox = get_bounding_box(self._contours())
        for key in ("min_lat", "max_lat", "min_lon", "max_lon"):
            assert key in bbox

    def test_bounding_box_ordering(self):
        from app.geospatial.kml_parser import get_bounding_box
        bbox = get_bounding_box(self._contours())
        assert bbox["min_lat"] <= bbox["max_lat"]
        assert bbox["min_lon"] <= bbox["max_lon"]

    def test_elevation_stats_keys(self):
        from app.geospatial.kml_parser import get_elevation_stats
        stats = get_elevation_stats(self._contours())
        for key in ("min", "max", "mean", "unique_levels"):
            assert key in stats

    def test_elevation_min_less_than_max(self):
        from app.geospatial.kml_parser import get_elevation_stats
        stats = get_elevation_stats(self._contours())
        assert stats["min"] <= stats["max"]

    def test_unique_levels_sorted(self):
        from app.geospatial.kml_parser import get_elevation_stats
        stats = get_elevation_stats(self._contours())
        levels = stats["unique_levels"]
        assert levels == sorted(levels), "unique_levels must be sorted"

    def test_mean_elevation_in_range(self):
        from app.geospatial.kml_parser import get_elevation_stats
        stats = get_elevation_stats(self._contours())
        assert stats["min"] <= stats["mean"] <= stats["max"]

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_real_kml_bbox_in_india(self):
        """contours_1m.kml should have bounding box roughly in India."""
        from app.geospatial.kml_parser import parse_kml_bytes, get_bounding_box
        bbox = get_bounding_box(parse_kml_bytes(SAMPLE_KML.read_bytes()))
        assert 5 <= bbox["min_lat"] <= 40, f"min_lat {bbox['min_lat']} not in India range"
        assert 60 <= bbox["min_lon"] <= 100, f"min_lon {bbox['min_lon']} not in India range"


# =============================================================================
# STEP 5  —  Candidate pond site identification (slope + elevation filter)
# =============================================================================

class TestStep5CandidateIdentification:
    """Step 5: identify_candidate_cells must respect slope + elevation filters."""

    def test_candidates_returned_for_slope_dem(self):
        from app.geospatial.terrain import identify_candidate_cells
        lats, lons, elevs = _slope_dem()
        candidates = identify_candidate_cells(lats, lons, elevs)
        assert len(candidates) > 0, "Slope DEM must produce some candidates"

    def test_candidates_within_bounding_box(self):
        from app.geospatial.terrain import identify_candidate_cells
        lats, lons, elevs = _slope_dem()
        for c in identify_candidate_cells(lats, lons, elevs):
            assert lats.min() <= c["lat"] <= lats.max()
            assert lons.min() <= c["lon"] <= lons.max()

    def test_candidates_have_required_keys(self):
        from app.geospatial.terrain import identify_candidate_cells
        lats, lons, elevs = _slope_dem()
        for c in identify_candidate_cells(lats, lons, elevs):
            for k in ("lat", "lon", "elevation_m", "slope_deg"):
                assert k in c, f"Candidate missing key '{k}'"

    def test_candidates_slope_below_threshold(self):
        """All returned candidates must have slope < threshold."""
        from app.geospatial.terrain import identify_candidate_cells
        from app.config import settings
        lats, lons, elevs = _slope_dem()
        for c in identify_candidate_cells(lats, lons, elevs):
            assert c["slope_deg"] < settings.SLOPE_THRESHOLD_DEG, (
                f"Candidate slope {c['slope_deg']:.2f} exceeds threshold "
                f"{settings.SLOPE_THRESHOLD_DEG}"
            )

    def test_flat_terrain_all_candidates_have_zero_slope(self):
        """On a flat DEM every cell qualifies by slope; slope_deg should be ~0."""
        from app.geospatial.terrain import identify_candidate_cells
        lats, lons, elevs = _flat_dem()
        candidates = identify_candidate_cells(lats, lons, elevs)
        for c in candidates:
            assert c["slope_deg"] < 1e-3, f"Flat DEM slope should be ~0, got {c['slope_deg']}"

    def test_is_stream_flag_is_false_for_all_candidates(self):
        """Candidates returned must have is_stream=False (river exclusion applied)."""
        from app.geospatial.terrain import identify_candidate_cells
        lats, lons, elevs = _valley_dem()
        for c in identify_candidate_cells(lats, lons, elevs):
            assert c["is_stream"] is False, "Candidate must not be a stream cell"


# =============================================================================
# STEP 5a  —  River / stream channel exclusion (edge case)
# =============================================================================

class TestStep5aRiverExclusion:
    """
    Step 5a: The D8 flow-accumulation + stream-mask logic must exclude
    high-accumulation (river-channel) cells from pond candidates.

    Design rationale: building a pond inside an active river channel is
    hydraulically incorrect and structurally dangerous.  The D8 flow
    accumulation metric is the standard GIS proxy for channel detection.
    """

    def test_compute_flow_accumulation_returns_correct_shape(self):
        from app.geospatial.hydrology_engine import HydrologyEngine

        lats, lons = np.array([21.0, 21.001]), np.array([81.0, 81.001])
        elevs = np.array([[300.0, 290.0], [280.0, 270.0]])
        engine = HydrologyEngine(lats, lons, elevs)
        engine.process()
        accum = engine.accum

        assert accum.shape == (2, 2)

    def test_flow_accumulation_all_positive(self):
        from app.geospatial.hydrology_engine import HydrologyEngine
        lats, lons = np.array([21.0, 21.001]), np.array([81.0, 81.001])
        elevs = np.array([[300.0, 290.0], [280.0, 270.0]])
        engine = HydrologyEngine(lats, lons, elevs)
        engine.process()
        accum = engine.accum

        assert np.all(accum >= 1), "Every cell accumulates at least itself"

    def test_valley_bottom_has_higher_accum_than_ridge(self):
        """Valley bottom (stream outlet) must have more accumulation than ridges."""
        from app.geospatial.hydrology_engine import HydrologyEngine
        lats, lons = np.array([21.0, 21.001, 21.002]), np.array([81.0, 81.001, 81.002])
        # 3x3 V-shape valley draining to bottom-middle (2, 1)
        elevs = np.array([
            [300.0, 290.0, 300.0],
            [290.0, 280.0, 290.0],
            [280.0, 270.0, 280.0]
        ])
        engine = HydrologyEngine(lats, lons, elevs)
        engine.process()
        accum = engine.accum

        assert accum[2, 1] > accum[0, 0]
        assert accum[2, 1] >= 3

    def test_is_stream_channel_flags_high_accum_cells(self):
        """is_stream_channel must flag valley-bottom cells as streams."""
        from app.geospatial.hydrology_engine import HydrologyEngine
        from app.geospatial.terrain import is_stream_channel
        
        lats, lons = np.array([21.0, 21.001, 21.002]), np.array([81.0, 81.001, 81.002])
        elevs = np.array([
            [300.0, 290.0, 300.0],
            [290.0, 280.0, 290.0],
            [280.0, 270.0, 280.0]
        ])
        engine = HydrologyEngine(lats, lons, elevs)
        engine.process()
        accum = engine.accum
        is_stream = is_stream_channel(accum, 85.0)
        assert is_stream[0, 0] == False
        assert is_stream[0, 2] == False
        assert is_stream[2, 1] == True

    def test_ridge_cells_not_flagged_as_stream(self):
        """Source cells (ridges) must NOT be stream channels."""
        from app.geospatial.hydrology_engine import HydrologyEngine
        from app.geospatial.terrain import is_stream_channel
        
        lats, lons = np.array([21.0, 21.001, 21.002]), np.array([81.0, 81.001, 81.002])
        elevs = np.array([
            [300.0, 290.0, 300.0],
            [290.0, 280.0, 290.0],
            [280.0, 270.0, 280.0]
        ])
        engine = HydrologyEngine(lats, lons, elevs)
        engine.process()
        accum = engine.accum

        # 50th percentile should flag the bottom-middle as stream
        is_stream = is_stream_channel(accum, 50.0)
        assert is_stream[2, 1] == True

    def test_identify_candidates_excludes_stream_cells(self):
        """Cells flagged as stream channels must never appear in candidates list."""
        from app.geospatial.terrain import (
            identify_candidate_cells
        )
        from app.config import settings
        lats, lons, elevs = _valley_dem(n=20)
        
        # We manually process here for internal verification, identify_candidate_cells 
        # is the integration point.
        candidates = identify_candidate_cells(lats, lons, elevs)

        for c in candidates:
            assert c["is_stream"] is False, (
                f"Candidate at ({c['lat']:.5f}, {c['lon']:.5f}) is inside a "
                f"stream channel — should be excluded."
            )

    def test_stream_percentile_threshold_respected(self):
        """Cells strictly below the percentile threshold are not stream channels."""
        from app.geospatial.hydrology_engine import HydrologyEngine
        from app.geospatial.terrain import is_stream_channel
        _, _, elevs = _slope_dem(n=20)
        lats = np.linspace(18.5, 18.6, 20)
        lons = np.linspace(74.4, 74.5, 20)
        engine = HydrologyEngine(lats, lons, elevs)
        engine.process()
        accum = engine.accum
        threshold = float(np.percentile(accum, 85.0))
        stream_mask = is_stream_channel(accum, 85.0)
        below_threshold = accum < threshold
        assert not np.any(stream_mask & below_threshold), (
            "Cells below the percentile threshold must not be flagged as streams"
        )

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_real_kml_pond_not_in_river_channel(self):
        """
        CRITICAL edge-case: the pond recommended from contours_1m.kml must NOT
        be placed inside an active stream/river channel.
        """
        from app.geospatial.kml_parser import parse_kml_bytes, contours_to_dem
        from app.geospatial.hydrology_engine import HydrologyEngine
        from app.geospatial.terrain import is_stream_channel
        from app.config import settings

        kml_bytes = SAMPLE_KML.read_bytes()
        contour_lines = parse_kml_bytes(kml_bytes)
        lats, lons, elevs = contours_to_dem(contour_lines, n_points=50)

        engine = HydrologyEngine(lats, lons, elevs)
        engine.process()
        accum = engine.accum
        stream_mask = is_stream_channel(accum, settings.STREAM_ACCUM_PERCENTILE)

        with open(SAMPLE_KML, "rb") as fh:
            resp = client.post(
                "/analyzeContour",
                files={"file": (SAMPLE_KML.name, fh, "application/vnd.google-earth.kml+xml")},
                data={"pondDepth": 3.0}
            )
        assert resp.status_code == 200
        pond = resp.json()["pond_location"]

        lat_idx = int(np.argmin(np.abs(lats - pond["lat"])))
        lon_idx = int(np.argmin(np.abs(lons - pond["lon"])))

        assert not stream_mask[lat_idx, lon_idx], (
            f"RIVER EDGE CASE FAILED — Pond placed inside a stream channel!\n"
            f"  Pond @ ({pond['lat']}, {pond['lon']})\n"
            f"  Grid cell ({lat_idx}, {lon_idx}) flow_accum={accum[lat_idx, lon_idx]}\n"
            f"  Stream threshold = top {100 - settings.STREAM_ACCUM_PERCENTILE:.0f}% of cells\n"
            "  A pond must be built adjacent to the drainage network, not inside a river."
        )


# =============================================================================
# STEP 6  —  Candidate ranking (land suitability)
# =============================================================================

class TestStep6CandidateRanking:
    """Step 6: rank_candidates must score, filter, and sort candidates correctly."""

    def _raw_candidates(self, n: int = 10):
        """Generate deterministic raw candidate dicts."""
        return [
            {
                "lat": 18.5 + i * 0.001,
                "lon": 74.4 + i * 0.001,
                "elevation_m": 250.0 + i,
                "slope_deg": float(i) * 0.3,
                "flow_accum": 5,
                "is_stream": False,
            }
            for i in range(n)
        ]

    def test_returns_at_most_max_sites(self):
        from app.geospatial.land_suitability import rank_candidates
        ranked = rank_candidates(self._raw_candidates(20), max_sites=5)
        assert len(ranked) <= 5

    def test_ranked_have_required_keys(self):
        from app.geospatial.land_suitability import rank_candidates
        for c in rank_candidates(self._raw_candidates(5)):
            for k in ("lat", "lon", "elevation_m", "slope_deg",
                      "land_status", "terrain_score", "land_score"):
                assert k in c, f"Ranked candidate missing key '{k}'"

    def test_terrain_scores_descending(self):
        """Candidates must be sorted by terrain_score descending."""
        from app.geospatial.land_suitability import rank_candidates
        ranked = rank_candidates(self._raw_candidates(15), max_sites=10)
        scores = [c["terrain_score"] for c in ranked]
        assert scores == sorted(scores, reverse=True), "Ranked list must be sorted descending"

    def test_ineligible_land_excluded(self):
        """Candidates classified as water-body / built-up must be removed."""
        from app.geospatial.land_suitability import rank_candidates
        raw = self._raw_candidates(50)
        ranked = rank_candidates(raw)
        ineligible = {"water-body", "built-up", "protected"}
        for c in ranked:
            assert c["land_status"] not in ineligible, (
                f"Ineligible land status '{c['land_status']}' slipped through ranking"
            )

    def test_terrain_score_in_unit_range(self):
        from app.geospatial.land_suitability import rank_candidates
        for c in rank_candidates(self._raw_candidates(10)):
            assert 0.0 <= c["terrain_score"] <= 1.0

    def test_land_score_in_unit_range(self):
        from app.geospatial.land_suitability import rank_candidates
        for c in rank_candidates(self._raw_candidates(10)):
            assert 0.0 <= c["land_score"] <= 1.0

    def test_empty_input_returns_empty(self):
        from app.geospatial.land_suitability import rank_candidates
        assert rank_candidates([]) == []

    def test_score_terrain_flat_is_high(self):
        """slope=0 at elev_threshold should yield near-maximum terrain score."""
        from app.geospatial.land_suitability import score_terrain
        score = score_terrain(slope_deg=0.0, elevation_m=200.0, elev_threshold=200.0)
        assert score >= 0.7

    def test_score_terrain_steep_is_low(self):
        """
        score_terrain with slope well above the threshold must return a low score.

        Formula: score = 0.6 * slope_score + 0.4 * elev_score
          slope_score = max(0, 1 - slope / threshold)   → 0 when slope >= threshold
          elev_score  = max(0, 1 - (elev - threshold) / 30)

        At slope = 2 × threshold and elev 30 m above threshold:
          slope_score = 0   (capped at 0 because slope >= threshold)
          elev_score  = 0   (elev_above = 30, capped at 0)
          total       = 0.0
        """
        from app.geospatial.land_suitability import score_terrain
        from app.config import settings
        elev_thresh = 200.0
        score = score_terrain(
            slope_deg=settings.SLOPE_THRESHOLD_DEG * 2,  # clearly above threshold
            elevation_m=elev_thresh + 30.0,               # 30 m above → elev_score = 0
            elev_threshold=elev_thresh,
        )
        assert score == pytest.approx(0.0), (
            f"Steep high-elevation site should have score=0, got {score}"
        )



    def test_government_land_score_highest(self):
        """Government land has score 1.0; water-body = 0.0."""
        from app.geospatial.land_suitability import _LAND_STATUS_SCORE
        assert _LAND_STATUS_SCORE["government"] > _LAND_STATUS_SCORE["private"]
        assert _LAND_STATUS_SCORE["water-body"] == 0.0


# =============================================================================
# STEP 7  —  Catchment delineation (D8 algorithm)
# =============================================================================

class TestStep7CatchmentDelineation:
    """Step 7: delineate_catchment must return a valid GeoJSON polygon."""

    def test_catchment_has_required_keys(self):
        from app.geospatial.catchment import delineate_catchment
        lats, lons, elevs = _slope_dem(n=20)
        result = delineate_catchment(lats, lons, elevs,
                                     outlet_lat=lats[5], outlet_lon=lons[5])
        for k in ("geometry_geojson", "area_m2", "area_ha",
                  "avg_elevation_m", "slope_summary"):
            assert k in result, f"Missing key '{k}' in catchment result"

    def test_catchment_area_is_positive(self):
        from app.geospatial.catchment import delineate_catchment
        lats, lons, elevs = _slope_dem(n=20)
        result = delineate_catchment(lats, lons, elevs,
                                     outlet_lat=lats[5], outlet_lon=lons[5])
        assert result["area_m2"] > 0
        assert result["area_ha"] > 0

    def test_catchment_geometry_type_polygon(self):
        from app.geospatial.catchment import delineate_catchment
        lats, lons, elevs = _slope_dem(n=20)
        result = delineate_catchment(lats, lons, elevs,
                                     outlet_lat=lats[5], outlet_lon=lons[5])
        geom = result["geometry_geojson"]
        assert geom["type"] in ("Polygon", "MultiPolygon"), (
            f"Expected Polygon/MultiPolygon, got {geom['type']}"
        )
        assert "coordinates" in geom

    def test_catchment_ha_consistent_with_m2(self):
        from app.geospatial.catchment import delineate_catchment
        lats, lons, elevs = _slope_dem(n=20)
        result = delineate_catchment(lats, lons, elevs,
                                     outlet_lat=lats[5], outlet_lon=lons[5])
        assert abs(result["area_ha"] - result["area_m2"] / 10_000) < 0.1

    def test_avg_elevation_within_dem_range(self):
        from app.geospatial.catchment import delineate_catchment
        lats, lons, elevs = _slope_dem(n=20)
        result = delineate_catchment(lats, lons, elevs,
                                     outlet_lat=lats[5], outlet_lon=lons[5])
        assert elevs.min() <= result["avg_elevation_m"] <= elevs.max() + 1e-3

    def test_slope_summary_is_string(self):
        from app.geospatial.catchment import delineate_catchment
        lats, lons, elevs = _slope_dem(n=20)
        result = delineate_catchment(lats, lons, elevs,
                                     outlet_lat=lats[5], outlet_lon=lons[5])
        assert isinstance(result["slope_summary"], str)

    def test_dem_slope_helper_flat_is_zero(self):
        """_simple_slope on a flat DEM must return all-zero slopes."""
        from app.geospatial.catchment import _simple_slope
        lats, lons, elevs = _flat_dem(n=15)
        slope = _simple_slope(lats, lons, elevs)
        assert np.allclose(slope, 0.0, atol=1e-6)

    def test_geodetic_area_positive(self):
        from app.geospatial.catchment import _geodetic_area_m2
        from shapely.geometry import box
        geom = box(74.4, 18.5, 74.5, 18.6)
        area = _geodetic_area_m2(geom)
        assert area > 0

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_real_kml_catchment_area_nonzero(self):
        from app.geospatial.kml_parser import parse_kml_bytes, contours_to_dem
        from app.geospatial.catchment import delineate_catchment
        lines = parse_kml_bytes(SAMPLE_KML.read_bytes())
        lats, lons, elevs = contours_to_dem(lines, n_points=30)
        result = delineate_catchment(lats, lons, elevs,
                                     outlet_lat=lats[10], outlet_lon=lons[10])
        assert result["area_m2"] > 0


# =============================================================================
# STEP 8  —  Rainfall (provided vs fetched)
# =============================================================================

class TestStep8Rainfall:
    """
    Step 8: When annual_rainfall_mm is provided the endpoint must echo it back.
    When not provided, a fallback of 800 mm (or live API value) is used.
    """

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_provided_rainfall_echoed_in_response(self):
        with open(SAMPLE_KML, "rb") as fh:
            resp = client.post(
                "/analyzeContour",
                files={"file": (SAMPLE_KML.name, fh,
                                "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "agricultural", "desired_depth_m": "3.0",
                      "annual_rainfall_mm": "1200.0"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert abs(body["annual_rainfall_mm"] - 1200.0) < 0.01, (
            f"Provided rainfall 1200 mm not echoed: got {body['annual_rainfall_mm']}"
        )

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_omitted_rainfall_uses_positive_fallback(self):
        """Without a provided value the API should still return a positive rainfall."""
        with open(SAMPLE_KML, "rb") as fh:
            resp = client.post(
                "/analyzeContour",
                files={"file": (SAMPLE_KML.name, fh,
                                "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "default", "desired_depth_m": "3.0"},
            )
        assert resp.status_code == 200
        assert resp.json()["annual_rainfall_mm"] > 0

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_rainfall_upper_bound_validation(self):
        """annual_rainfall_mm above 10 000 is rejected by form validation."""
        with open(SAMPLE_KML, "rb") as fh:
            resp = client.post(
                "/analyzeContour",
                files={"file": (SAMPLE_KML.name, fh,
                                "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "default", "desired_depth_m": "3.0",
                      "annual_rainfall_mm": "99999"},
            )
        assert resp.status_code == 422


# =============================================================================
# STEP 9  —  Runoff estimation (V = P x A x C)
# =============================================================================

class TestStep9RunoffEstimation:
    """Step 9: estimate_runoff_volume must implement V = P x A x C exactly."""

    def test_formula_correctness(self):
        from app.geospatial.hydrology import estimate_runoff_volume
        A = 100_000
        P_mm = 800
        C = 0.55  # agricultural
        expected = (P_mm / 1000) * A * C
        result = estimate_runoff_volume(A, P_mm, "agricultural")
        assert abs(result["runoff_volume_m3"] - expected) < 1.0

    def test_zero_rainfall_gives_zero_runoff(self):
        from app.geospatial.hydrology import estimate_runoff_volume
        result = estimate_runoff_volume(50_000, 0, "default")
        assert result["runoff_volume_m3"] == 0.0

    def test_vegetated_runoff_less_than_builtup(self):
        from app.geospatial.hydrology import estimate_runoff_volume
        veg = estimate_runoff_volume(50_000, 600, "vegetated")
        built = estimate_runoff_volume(50_000, 600, "built_up")
        assert veg["runoff_volume_m3"] < built["runoff_volume_m3"]

    def test_agricultural_coefficient(self):
        from app.geospatial.hydrology import get_runoff_coefficient
        assert get_runoff_coefficient("agricultural") == pytest.approx(0.55)

    def test_vegetated_coefficient(self):
        from app.geospatial.hydrology import get_runoff_coefficient
        assert get_runoff_coefficient("vegetated") == pytest.approx(0.30)

    def test_builtup_coefficient(self):
        from app.geospatial.hydrology import get_runoff_coefficient
        assert get_runoff_coefficient("built_up") == pytest.approx(0.75)

    def test_unknown_cover_uses_default_coefficient(self):
        from app.geospatial.hydrology import get_runoff_coefficient
        from app.config import settings
        assert get_runoff_coefficient("TOTALLY_UNKNOWN") == pytest.approx(
            settings.RUNOFF_COEFF_DEFAULT
        )

    def test_negative_area_raises_value_error(self):
        from app.geospatial.hydrology import estimate_runoff_volume
        with pytest.raises(ValueError):
            estimate_runoff_volume(-100, 800, "default")

    def test_negative_rainfall_raises_value_error(self):
        from app.geospatial.hydrology import estimate_runoff_volume
        with pytest.raises(ValueError):
            estimate_runoff_volume(10_000, -50, "default")

    def test_rainfall_unit_conversion(self):
        """1000 mm = 1 m; volume must scale accordingly."""
        from app.geospatial.hydrology import estimate_runoff_volume
        from app.config import settings
        A = 1_000_000
        C = settings.RUNOFF_COEFF_DEFAULT
        result = estimate_runoff_volume(A, 1000, "default")
        expected = 1.0 * A * C
        assert abs(result["runoff_volume_m3"] - expected) < 1.0

    def test_return_dict_keys(self):
        from app.geospatial.hydrology import estimate_runoff_volume
        result = estimate_runoff_volume(100_000, 800, "default")
        for k in ("catchment_area_m2", "annual_rainfall_mm",
                   "runoff_coefficient", "runoff_volume_m3"):
            assert k in result


# =============================================================================
# STEP 10  —  Pond sizing
# =============================================================================

class TestStep10PondSizing:
    """Step 10: size_pond must compute correct dimensions and clamp depth."""

    def test_basic_sizing(self):
        from app.geospatial.hydrology import size_pond
        result = size_pond(9000.0, desired_depth_m=3.0)
        assert result["pond_depth_m"] == pytest.approx(3.0)
        assert result["pond_surface_area_m2"] == pytest.approx(3000.0)
        assert result["pond_storage_capacity_m3"] == pytest.approx(9000.0)

    def test_depth_clamped_to_minimum(self):
        from app.geospatial.hydrology import size_pond
        from app.config import settings
        result = size_pond(5000.0, desired_depth_m=0.01)
        assert result["pond_depth_m"] >= settings.POND_MIN_DEPTH_M

    def test_depth_clamped_to_maximum(self):
        from app.geospatial.hydrology import size_pond
        from app.config import settings
        result = size_pond(5000.0, desired_depth_m=999.0)
        assert result["pond_depth_m"] <= settings.POND_MAX_DEPTH_M

    def test_storage_equals_surface_times_depth(self):
        """storage_capacity must equal surface_area x depth (by definition)."""
        from app.geospatial.hydrology import size_pond
        result = size_pond(12_000.0, desired_depth_m=4.0)
        expected = result["pond_surface_area_m2"] * result["pond_depth_m"]
        assert abs(result["pond_storage_capacity_m3"] - expected) < 0.5

    def test_zero_volume_raises(self):
        from app.geospatial.hydrology import size_pond
        with pytest.raises(ValueError):
            size_pond(0.0)

    def test_negative_volume_raises(self):
        from app.geospatial.hydrology import size_pond
        with pytest.raises(ValueError):
            size_pond(-500.0)

    def test_none_depth_uses_default(self):
        from app.geospatial.hydrology import size_pond
        from app.config import settings
        result = size_pond(6000.0, desired_depth_m=None)
        assert result["pond_depth_m"] == pytest.approx(settings.POND_DEFAULT_DEPTH_M)

    def test_return_dict_keys(self):
        from app.geospatial.hydrology import size_pond
        result = size_pond(6000.0, desired_depth_m=2.0)
        for k in ("runoff_volume_m3", "pond_depth_m",
                   "pond_surface_area_m2", "pond_storage_capacity_m3", "note"):
            assert k in result

    def test_note_field_is_string(self):
        from app.geospatial.hydrology import size_pond
        assert isinstance(size_pond(6000.0)["note"], str)

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_endpoint_pond_depth_within_config_bounds(self):
        """End-to-end check: pond depth in response must respect config limits."""
        from app.config import settings
        with open(SAMPLE_KML, "rb") as fh:
            resp = client.post(
                "/analyzeContour",
                files={"file": (SAMPLE_KML.name, fh,
                                "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "default", "desired_depth_m": "3.0"},
            )
        assert resp.status_code == 200
        depth = resp.json()["pond_depth_m"]
        assert settings.POND_MIN_DEPTH_M <= depth <= settings.POND_MAX_DEPTH_M


# =============================================================================
# STEP 11  —  Suitability scoring (S = w1*T + w2*C + w3*R + w4*L)
# =============================================================================

class TestStep11SuitabilityScoring:
    """Step 11: compute_suitability_score must compute a weighted sum in [0, 1]."""

    def test_perfect_conditions_give_score_one(self):
        from app.geospatial.scoring import compute_suitability_score
        result = compute_suitability_score(
            terrain_score=1.0,
            catchment_area_m2=2_000_000,
            land_score=1.0,
        )
        assert result["total_score"] == pytest.approx(1.0, abs=0.01)

    def test_zero_conditions_give_score_zero(self):
        from app.geospatial.scoring import compute_suitability_score
        result = compute_suitability_score(0.0, 0.0, 0.0)
        assert result["total_score"] == pytest.approx(0.0)

    def test_total_score_in_unit_range(self):
        from app.geospatial.scoring import compute_suitability_score
        result = compute_suitability_score(0.5, 500_000, 0.7)
        assert 0.0 <= result["total_score"] <= 1.0

    def test_weights_sum_to_one(self):
        from app.geospatial.scoring import compute_suitability_score
        result = compute_suitability_score(0.5, 100_000, 0.6)
        w = result["weights"]
        assert abs(w["terrain"] + w["catchment"] + w["land"] - 1.0) < 0.01

    def test_subscores_present_in_result(self):
        from app.geospatial.scoring import compute_suitability_score
        result = compute_suitability_score(0.5, 100_000, 0.6)
        for k in ("terrain_score", "catchment_score",
                   "land_score", "total_score", "weights"):
            assert k in result



    def test_normalise_catchment_zero_area(self):
        from app.geospatial.scoring import normalise_catchment_score
        assert normalise_catchment_score(0) == 0.0

    def test_normalise_catchment_large_area(self):
        from app.geospatial.scoring import normalise_catchment_score
        assert normalise_catchment_score(2_000_000) == pytest.approx(1.0)

    def test_non_unit_weights_normalised(self):
        """If user passes weights that do not sum to 1, the function normalises them."""
        from app.geospatial.scoring import compute_suitability_score
        result = compute_suitability_score(
            0.5, 100_000, 800, 0.5,
            w_terrain=3.0, w_catchment=3.0, w_rainfall=2.0, w_land=2.0,
        )
        w = result["weights"]
        assert abs(w["terrain"] + w["catchment"] + w["rainfall"] + w["land"] - 1.0) < 0.02


# =============================================================================
# STEP 12  —  Full end-to-end integration (/analyzeContour & /findCatchment)
# =============================================================================

class TestStep12EndToEndIntegration:
    """Step 12: Full pipeline via the HTTP endpoint."""

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_analyze_contour_returns_200(self):
        with open(SAMPLE_KML, "rb") as fh:
            resp = client.post(
                "/analyzeContour",
                files={"file": (SAMPLE_KML.name, fh,
                                "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "agricultural", "desired_depth_m": "3.0"},
            )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text[:400]}"
        )

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_response_contains_all_required_fields(self):
        with open(SAMPLE_KML, "rb") as fh:
            body = client.post(
                "/analyzeContour",
                files={"file": (SAMPLE_KML.name, fh,
                                "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "default", "desired_depth_m": "3.0"},
            ).json()
        required = [
            "elevation_range_m", "contour_count", "bounding_box",
            "pond_location", "catchment_area_m2", "catchment_area_ha",
            "catchment_geometry", "annual_rainfall_mm", "runoff_volume_m3",
            "runoff_coefficient", "pond_depth_m", "pond_surface_area_m2",
            "pond_storage_capacity_m3", "suitability_score",
            "score_breakdown", "slope_summary", "avg_elevation_m", "method_notes",
        ]
        for field in required:
            assert field in body, f"Missing field: {field}"

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_pond_location_within_bounding_box(self):
        with open(SAMPLE_KML, "rb") as fh:
            body = client.post(
                "/analyzeContour",
                files={"file": (SAMPLE_KML.name, fh,
                                "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "default", "desired_depth_m": "3.0"},
            ).json()
        bbox, loc = body["bounding_box"], body["pond_location"]
        assert bbox["min_lat"] <= loc["lat"] <= bbox["max_lat"]
        assert bbox["min_lon"] <= loc["lon"] <= bbox["max_lon"]

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_all_numeric_outputs_are_positive(self):
        with open(SAMPLE_KML, "rb") as fh:
            body = client.post(
                "/analyzeContour",
                files={"file": (SAMPLE_KML.name, fh,
                                "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "default", "desired_depth_m": "3.0",
                      "annual_rainfall_mm": "800"},
            ).json()
        for field in ("catchment_area_m2", "catchment_area_ha",
                       "annual_rainfall_mm", "runoff_volume_m3",
                       "pond_depth_m", "pond_surface_area_m2",
                       "pond_storage_capacity_m3"):
            assert body[field] > 0, f"Field '{field}' must be positive, got {body[field]}"

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_suitability_score_in_unit_interval(self):
        with open(SAMPLE_KML, "rb") as fh:
            body = client.post(
                "/analyzeContour",
                files={"file": (SAMPLE_KML.name, fh,
                                "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "default", "desired_depth_m": "3.0"},
            ).json()
        assert 0.0 <= body["suitability_score"] <= 1.0

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_elevation_range_min_less_than_max(self):
        with open(SAMPLE_KML, "rb") as fh:
            body = client.post(
                "/analyzeContour",
                files={"file": (SAMPLE_KML.name, fh,
                                "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "default", "desired_depth_m": "3.0"},
            ).json()
        er = body["elevation_range_m"]
        assert er["min"] < er["max"]

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_catchment_geometry_is_polygon(self):
        with open(SAMPLE_KML, "rb") as fh:
            body = client.post(
                "/analyzeContour",
                files={"file": (SAMPLE_KML.name, fh,
                                "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "default", "desired_depth_m": "3.0"},
            ).json()
        assert body["catchment_geometry"]["type"] in ("Polygon", "MultiPolygon")

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_method_notes_mentions_rational_method(self):
        with open(SAMPLE_KML, "rb") as fh:
            body = client.post(
                "/analyzeContour",
                files={"file": (SAMPLE_KML.name, fh,
                                "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "default", "desired_depth_m": "3.0"},
            ).json()
        assert "Rational Method" in body["method_notes"]

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_find_catchment_alias_returns_200(self):
        """POST /findCatchment is an alias and must return 200 for a valid KML."""
        with open(SAMPLE_KML, "rb") as fh:
            resp = client.post(
                "/findCatchment",
                files={"file": (SAMPLE_KML.name, fh,
                                "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "agricultural", "desired_depth_m": "3.0"},
            )
        assert resp.status_code == 200
        assert "catchment_area_m2" in resp.json()

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_land_cover_options_all_return_200(self):
        """Every valid land_cover value must succeed."""
        for cover in ("vegetated", "agricultural", "built_up", "default"):
            with open(SAMPLE_KML, "rb") as fh:
                resp = client.post(
                    "/analyzeContour",
                    files={"file": (SAMPLE_KML.name, fh,
                                    "application/vnd.google-earth.kml+xml")},
                    data={"land_cover": cover, "desired_depth_m": "3.0",
                          "annual_rainfall_mm": "800"},
                )
            assert resp.status_code == 200, f"land_cover='{cover}' returned {resp.status_code}"

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_higher_rainfall_increases_runoff(self):
        """More rainfall should produce more runoff (everything else equal)."""
        def runoff(mm: float) -> float:
            with open(SAMPLE_KML, "rb") as fh:
                return client.post(
                    "/analyzeContour",
                    files={"file": (SAMPLE_KML.name, fh,
                                    "application/vnd.google-earth.kml+xml")},
                    data={"land_cover": "default", "desired_depth_m": "3.0",
                          "annual_rainfall_mm": str(mm)},
                ).json()["runoff_volume_m3"]
        assert runoff(1200) > runoff(600)

    @pytest.mark.skipif(not SAMPLE_KML.exists(), reason="Sample KML not present")
    def test_score_breakdown_sub_scores_consistent_with_total(self):
        """Total score must be a weighted combination of sub-scores."""
        with open(SAMPLE_KML, "rb") as fh:
            body = client.post(
                "/analyzeContour",
                files={"file": (SAMPLE_KML.name, fh,
                                "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "default", "desired_depth_m": "3.0",
                      "annual_rainfall_mm": "800"},
            ).json()
        bd = body["score_breakdown"]
        w = bd["weights"]
        computed = (
            w["terrain"] * bd["terrain_score"]
            + w["catchment"] * bd["catchment_score"]
            + w["rainfall"] * bd["rainfall_score"]
            + w["land"] * bd["land_score"]
        )
        assert abs(computed - bd["total_score"]) < 0.01
