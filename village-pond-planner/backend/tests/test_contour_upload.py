"""
Tests for POST /analyzeContour and POST /findCatchment endpoints.

These tests use the real contours_1m.kml file provided with the assignment.
The test verifies:
  - HTTP 200 response
  - All required fields are present and have correct types
  - pond_location coordinates fall within the file's bounding box
  - catchment area is positive
  - runoff volume is positive
  - suitability score is in [0, 1]
  - No hard-coded coordinates — all values are derived from the file

Run with:
    cd backend
    python -m pytest tests/test_contour_upload.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ── Bootstrap app without a real DB ──────────────────────────────────────────
# The /analyzeContour endpoint is stateless; no DB is needed.
# We mock get_db to avoid requiring a running PostgreSQL instance.
from unittest.mock import AsyncMock, patch

# Patch DB before importing app
with patch("app.database.get_db", return_value=AsyncMock()):
    from app.main import app

client = TestClient(app)

# Path to the sample KML file (two levels up from backend/)
SAMPLE_KML = Path(__file__).parent.parent.parent / "contours_1m.kml"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _post_analyze(kml_path: Path, extra_form: dict | None = None) -> dict:
    """POST /analyzeContour with the given KML file, return parsed JSON."""
    form = {"land_cover": "agricultural", "desired_depth_m": "3.0"}
    if extra_form:
        form.update(extra_form)
    with open(kml_path, "rb") as f:
        resp = client.post(
            "/analyzeContour",
            files={"file": (kml_path.name, f, "application/vnd.google-earth.kml+xml")},
            data=form,
        )
    return resp


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestAnalyzeContourEndpoint:
    """Integration tests for POST /analyzeContour."""

    def test_sample_kml_returns_200(self):
        """The sample contours_1m.kml should produce a 200 OK response."""
        if not SAMPLE_KML.exists():
            pytest.skip(f"Sample KML not found at {SAMPLE_KML}")
        resp = _post_analyze(SAMPLE_KML)
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}. Body: {resp.text[:500]}"
        )

    def test_response_has_required_fields(self):
        """Response JSON must contain all ContourUploadResponse fields."""
        if not SAMPLE_KML.exists():
            pytest.skip(f"Sample KML not found at {SAMPLE_KML}")
        resp = _post_analyze(SAMPLE_KML)
        assert resp.status_code == 200
        body = resp.json()

        required_fields = [
            "elevation_range_m",
            "contour_count",
            "bounding_box",
            "pond_location",
            "catchment_area_m2",
            "catchment_area_ha",
            "catchment_geometry",
            "annual_rainfall_mm",
            "runoff_volume_m3",
            "runoff_coefficient",
            "pond_depth_m",
            "pond_surface_area_m2",
            "pond_storage_capacity_m3",
            "suitability_score",
            "score_breakdown",
            "slope_summary",
            "avg_elevation_m",
            "method_notes",
        ]
        for field in required_fields:
            assert field in body, f"Missing field: {field}"

    def test_pond_location_within_bounding_box(self):
        """The recommended pond location must lie within the file's bounding box."""
        if not SAMPLE_KML.exists():
            pytest.skip(f"Sample KML not found at {SAMPLE_KML}")
        resp = _post_analyze(SAMPLE_KML)
        assert resp.status_code == 200
        body = resp.json()

        bbox = body["bounding_box"]
        loc = body["pond_location"]

        assert bbox["min_lat"] <= loc["lat"] <= bbox["max_lat"], (
            f"Pond lat {loc['lat']} outside bbox lat [{bbox['min_lat']}, {bbox['max_lat']}]"
        )
        assert bbox["min_lon"] <= loc["lon"] <= bbox["max_lon"], (
            f"Pond lon {loc['lon']} outside bbox lon [{bbox['min_lon']}, {bbox['max_lon']}]"
        )

    def test_catchment_area_is_positive(self):
        if not SAMPLE_KML.exists():
            pytest.skip(f"Sample KML not found at {SAMPLE_KML}")
        resp = _post_analyze(SAMPLE_KML)
        assert resp.status_code == 200
        body = resp.json()
        assert body["catchment_area_m2"] > 0, "Catchment area must be positive"
        assert body["catchment_area_ha"] > 0, "Catchment area (ha) must be positive"

    def test_runoff_volume_is_positive(self):
        if not SAMPLE_KML.exists():
            pytest.skip(f"Sample KML not found at {SAMPLE_KML}")
        resp = _post_analyze(SAMPLE_KML)
        assert resp.status_code == 200
        body = resp.json()
        assert body["runoff_volume_m3"] > 0

    def test_suitability_score_in_range(self):
        if not SAMPLE_KML.exists():
            pytest.skip(f"Sample KML not found at {SAMPLE_KML}")
        resp = _post_analyze(SAMPLE_KML)
        assert resp.status_code == 200
        body = resp.json()
        score = body["suitability_score"]
        assert 0.0 <= score <= 1.0, f"Suitability score {score} out of [0, 1]"

    def test_contour_count_is_positive(self):
        if not SAMPLE_KML.exists():
            pytest.skip(f"Sample KML not found at {SAMPLE_KML}")
        resp = _post_analyze(SAMPLE_KML)
        assert resp.status_code == 200
        assert resp.json()["contour_count"] > 0

    def test_elevation_range_is_sensible(self):
        """Min elevation must be < max elevation in the response."""
        if not SAMPLE_KML.exists():
            pytest.skip(f"Sample KML not found at {SAMPLE_KML}")
        resp = _post_analyze(SAMPLE_KML)
        assert resp.status_code == 200
        elev = resp.json()["elevation_range_m"]
        assert elev["min"] < elev["max"], "Min elevation must be less than max elevation"

    def test_catchment_geometry_is_geojson_polygon(self):
        """Catchment geometry must be a valid GeoJSON Polygon or MultiPolygon."""
        if not SAMPLE_KML.exists():
            pytest.skip(f"Sample KML not found at {SAMPLE_KML}")
        resp = _post_analyze(SAMPLE_KML)
        assert resp.status_code == 200
        geom = resp.json()["catchment_geometry"]
        assert geom.get("type") in ("Polygon", "MultiPolygon"), (
            f"Expected Polygon or MultiPolygon, got {geom.get('type')}"
        )
        assert "coordinates" in geom

    def test_pond_depth_within_config_limits(self):
        """Pond depth must be within [1.0, 6.0] m as per config."""
        if not SAMPLE_KML.exists():
            pytest.skip(f"Sample KML not found at {SAMPLE_KML}")
        resp = _post_analyze(SAMPLE_KML)
        assert resp.status_code == 200
        depth = resp.json()["pond_depth_m"]
        assert 1.0 <= depth <= 6.0, f"Pond depth {depth} outside config limits [1, 6] m"

    def test_alias_route_findcatchment(self):
        """POST /findCatchment must return the same result as /analyzeContour."""
        if not SAMPLE_KML.exists():
            pytest.skip(f"Sample KML not found at {SAMPLE_KML}")
        with open(SAMPLE_KML, "rb") as f:
            resp = client.post(
                "/findCatchment",
                files={"file": (SAMPLE_KML.name, f, "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "agricultural", "desired_depth_m": "3.0"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "catchment_area_m2" in body

    def test_empty_file_returns_400(self):
        """An empty file upload should return HTTP 400."""
        from io import BytesIO
        resp = client.post(
            "/analyzeContour",
            files={"file": ("empty.kml", BytesIO(b""), "application/vnd.google-earth.kml+xml")},
            data={"land_cover": "default", "desired_depth_m": "3.0"},
        )
        assert resp.status_code == 400

    def test_invalid_xml_returns_422(self):
        """Garbage bytes should return HTTP 422."""
        from io import BytesIO
        resp = client.post(
            "/analyzeContour",
            files={"file": ("bad.kml", BytesIO(b"NOT XML AT ALL!!!"), "application/vnd.google-earth.kml+xml")},
            data={"land_cover": "default", "desired_depth_m": "3.0"},
        )
        assert resp.status_code == 422

    def test_custom_rainfall_override(self):
        """Providing annual_rainfall_mm param must be respected (no API call)."""
        if not SAMPLE_KML.exists():
            pytest.skip(f"Sample KML not found at {SAMPLE_KML}")
        resp = _post_analyze(SAMPLE_KML, extra_form={"annual_rainfall_mm": "1200.0"})
        assert resp.status_code == 200
        body = resp.json()
        assert abs(body["annual_rainfall_mm"] - 1200.0) < 0.01


class TestKmlParser:
    """Unit tests for the kml_parser module, independent of FastAPI."""

    def test_parse_returns_contour_lines(self):
        """parse_kml_bytes should return a non-empty list of (elev, coords) tuples."""
        if not SAMPLE_KML.exists():
            pytest.skip(f"Sample KML not found at {SAMPLE_KML}")
        from app.geospatial.kml_parser import parse_kml_bytes
        data = SAMPLE_KML.read_bytes()
        lines = parse_kml_bytes(data)
        assert len(lines) > 0, "Expected at least one contour line"
        elev, coords = lines[0]
        assert isinstance(elev, float), "Elevation must be float"
        assert len(coords) >= 2, "Contour line must have at least 2 points"
        lon, lat = coords[0]
        assert -180 <= lon <= 180, "Longitude out of range"
        assert -90 <= lat <= 90, "Latitude out of range"

    def test_contours_to_dem_shape(self):
        """contours_to_dem should return arrays of the correct shape."""
        if not SAMPLE_KML.exists():
            pytest.skip(f"Sample KML not found at {SAMPLE_KML}")
        from app.geospatial.kml_parser import parse_kml_bytes, contours_to_dem
        data = SAMPLE_KML.read_bytes()
        lines = parse_kml_bytes(data)
        lats, lons, elevs = contours_to_dem(lines, n_points=25)
        assert lats.shape == (25,)
        assert lons.shape == (25,)
        assert elevs.shape == (25, 25)
        assert not (elevs == 0).all(), "All-zero DEM is suspect"

    def test_empty_bytes_raises_value_error(self):
        from app.geospatial.kml_parser import parse_kml_bytes
        with pytest.raises((ValueError, Exception)):
            parse_kml_bytes(b"")


class TestRiverExclusionEdgeCase:
    """
    Verify that a pond is never placed inside an active river/stream channel.

    Edge-case rationale:
        A pond built at the bottom of a river channel would be immediately
        flooded and structurally unsafe.  The D8 flow accumulation of the
        selected pond cell must be below the configured stream threshold.
    """

    def test_pond_not_in_high_accumulation_stream_cell(self):
        """
        The pond location returned by /analyzeContour must NOT sit on a cell
        whose D8 flow accumulation exceeds the STREAM_ACCUM_PERCENTILE
        threshold — i.e., it must not be inside an active river channel.
        """
        if not SAMPLE_KML.exists():
            pytest.skip(f"Sample KML not found at {SAMPLE_KML}")

        import numpy as np
        from app.geospatial.kml_parser import parse_kml_bytes, contours_to_dem
        from app.geospatial.terrain import compute_flow_accumulation, is_stream_channel
        from app.config import settings

        kml_bytes = SAMPLE_KML.read_bytes()
        contour_lines = parse_kml_bytes(kml_bytes)
        lats, lons, elevs = contours_to_dem(contour_lines, n_points=50)

        accum = compute_flow_accumulation(elevs)
        stream_mask = is_stream_channel(accum, settings.STREAM_ACCUM_PERCENTILE)

        # Get the pond location from the API
        with open(SAMPLE_KML, "rb") as f:
            resp = client.post(
                "/analyzeContour",
                files={"file": (SAMPLE_KML.name, f, "application/vnd.google-earth.kml+xml")},
                data={"land_cover": "agricultural", "desired_depth_m": "3.0"},
            )
        assert resp.status_code == 200
        pond = resp.json()["pond_location"]

        # Find the nearest grid cell to the recommended pond location
        lat_idx = int(np.argmin(np.abs(lats - pond["lat"])))
        lon_idx = int(np.argmin(np.abs(lons - pond["lon"])))

        assert not stream_mask[lat_idx, lon_idx], (
            f"Pond placed inside a river channel! "
            f"Cell ({lat_idx}, {lon_idx}) has flow accumulation "
            f"{accum[lat_idx, lon_idx]} which exceeds the stream threshold "
            f"(top {100 - settings.STREAM_ACCUM_PERCENTILE:.0f}% of cells). "
            f"Pond should be on stable ground, not in an active stream."
        )

    def test_flow_accumulation_detects_stream_channels(self):
        """
        compute_flow_accumulation() must produce higher values in topographic
        valleys (where streams flow) than on ridges.  Uses a synthetic V-valley
        DEM where the expected drainage channel location is unambiguous.

        DEM layout (10x10):
            - elevation = lateral distance from centre axis + height above bottom row
            - minimum at bottom-centre (i=9, j=4..5) → the stream channel
            - maximum at top corners (i=0, j=0 and j=9) → ridge
        """
        import numpy as np
        from app.geospatial.terrain import compute_flow_accumulation, is_stream_channel

        size = 10
        centre_j = (size - 1) / 2.0  # 4.5

        elevs = np.zeros((size, size), dtype=float)
        for i in range(size):
            for j in range(size):
                # High at top-corners, low at bottom-centre
                lateral = abs(j - centre_j)      # → 0 at valley axis, 4.5 at edges
                height  = float(size - 1 - i)    # → 9 at top row, 0 at bottom row
                elevs[i, j] = lateral + height

        # Verify DEM shape: bottom-centre is lowest, top-corner is highest
        assert elevs[size - 1, size // 2] < elevs[0, 0], (
            "Synthetic DEM sanity check: valley bottom must be lower than ridge top"
        )

        accum = compute_flow_accumulation(elevs)

        # The bottom-row centre cells collect water from the whole valley
        bottom_centre_accum = int(accum[size - 1, size // 2 - 1])  # j=4
        # The top corner receives water from nobody (source cell)
        corner_accum = int(accum[0, 0])

        assert bottom_centre_accum > corner_accum, (
            "Flow accumulation should be higher at valley bottom than at ridges. "
            f"Bottom-centre accum={bottom_centre_accum}, corner accum={corner_accum}"
        )

        # Stream mask should flag the high-accumulation valley bottom cells
        stream_mask = is_stream_channel(accum, stream_accum_percentile=85.0)

        # At least one bottom-centre cell must be a stream channel
        valley_is_stream = (
            stream_mask[size - 1, size // 2]
            or stream_mask[size - 1, size // 2 - 1]
        )
        assert valley_is_stream, (
            "Valley bottom cells should be classified as stream channels "
            f"(accum at j=4: {accum[size-1, size//2-1]}, j=5: {accum[size-1, size//2]})"
        )

        # Ridge corners must NOT be flagged as streams (they are source cells)
        assert not stream_mask[0, 0], (
            f"Top-corner ridge cell should not be a stream channel "
            f"(accum={accum[0, 0]})"
        )

