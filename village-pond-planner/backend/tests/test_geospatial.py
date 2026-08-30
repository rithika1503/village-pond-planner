"""
Unit tests for core geospatial algorithms.

Tests cover:
  - Runoff estimation formula (V = P × A × C)
  - Pond sizing
  - Suitability scoring
  - Rainfall normalisation
  - Terrain slope computation
  - Contour generation (smoke test)
"""

import pytest
import numpy as np


# ─── Hydrology ────────────────────────────────────────────────────────────────

class TestRunoff:
    def test_basic_runoff(self):
        from app.geospatial.hydrology import estimate_runoff_volume
        result = estimate_runoff_volume(
            catchment_area_m2=100_000,  # 10 ha
            annual_rainfall_mm=800,
            land_cover="agricultural",
        )
        # C = 0.55; P = 0.8 m; A = 100,000 m²
        expected = 0.8 * 100_000 * 0.55
        assert abs(result["runoff_volume_m3"] - expected) < 1.0
        assert result["runoff_coefficient"] == pytest.approx(0.55)

    def test_vegetated_lower_than_builtup(self):
        from app.geospatial.hydrology import estimate_runoff_volume
        veg = estimate_runoff_volume(50_000, 600, "vegetated")
        built = estimate_runoff_volume(50_000, 600, "built_up")
        assert veg["runoff_volume_m3"] < built["runoff_volume_m3"]

    def test_zero_rainfall(self):
        from app.geospatial.hydrology import estimate_runoff_volume
        result = estimate_runoff_volume(50_000, 0, "default")
        assert result["runoff_volume_m3"] == 0.0

    def test_invalid_area(self):
        from app.geospatial.hydrology import estimate_runoff_volume
        with pytest.raises(ValueError):
            estimate_runoff_volume(-1, 800, "default")

    def test_rainfall_unit_conversion(self):
        """1000 mm rainfall = 1 m; sanity check."""
        from app.geospatial.hydrology import estimate_runoff_volume
        result = estimate_runoff_volume(1_000_000, 1000, "default")
        C = 0.45
        expected = 1.0 * 1_000_000 * C
        assert abs(result["runoff_volume_m3"] - expected) < 1.0


class TestPondSizing:
    def test_basic_sizing(self):
        from app.geospatial.hydrology import size_pond
        result = size_pond(runoff_volume_m3=9000, desired_depth_m=3.0)
        assert result["pond_depth_m"] == pytest.approx(3.0)
        assert result["pond_surface_area_m2"] == pytest.approx(3000.0)
        assert result["pond_storage_capacity_m3"] == pytest.approx(9000.0)

    def test_depth_clamped_minimum(self):
        from app.geospatial.hydrology import size_pond
        result = size_pond(1000, desired_depth_m=0.1)  # below min
        assert result["pond_depth_m"] >= 1.0  # clamped to POND_MIN_DEPTH_M

    def test_depth_clamped_maximum(self):
        from app.geospatial.hydrology import size_pond
        result = size_pond(1000, desired_depth_m=100.0)  # above max
        assert result["pond_depth_m"] <= 6.0  # clamped to POND_MAX_DEPTH_M

    def test_invalid_volume(self):
        from app.geospatial.hydrology import size_pond
        with pytest.raises(ValueError):
            size_pond(0)


# ─── Scoring ──────────────────────────────────────────────────────────────────

class TestScoring:
    def test_perfect_conditions(self):
        from app.geospatial.scoring import compute_suitability_score
        result = compute_suitability_score(
            terrain_score=1.0,
            catchment_area_m2=2_000_000,  # 200 ha → C score = 1.0
            annual_rainfall_mm=1500,       # → R score = 1.0
            land_score=1.0,
        )
        assert result["total_score"] == pytest.approx(1.0, abs=0.01)

    def test_zero_conditions(self):
        from app.geospatial.scoring import compute_suitability_score
        result = compute_suitability_score(
            terrain_score=0.0,
            catchment_area_m2=0.0,
            annual_rainfall_mm=0.0,
            land_score=0.0,
        )
        assert result["total_score"] == pytest.approx(0.0)

    def test_weights_sum_one(self):
        from app.geospatial.scoring import compute_suitability_score
        result = compute_suitability_score(0.5, 100_000, 800, 0.7)
        w = result["weights"]
        total_w = w["terrain"] + w["catchment"] + w["rainfall"] + w["land"]
        assert abs(total_w - 1.0) < 0.01

    def test_rainfall_normalisation(self):
        from app.geospatial.scoring import normalise_rainfall_score
        assert normalise_rainfall_score(0) == 0.0
        assert normalise_rainfall_score(300) == pytest.approx(0.0)
        assert 0 < normalise_rainfall_score(800) < 1.0
        assert normalise_rainfall_score(1800) == pytest.approx(1.0)

    def test_catchment_normalisation(self):
        from app.geospatial.scoring import normalise_catchment_score
        assert normalise_catchment_score(0) == 0.0
        assert normalise_catchment_score(2_000_000) == pytest.approx(1.0)
        assert 0 < normalise_catchment_score(500_000) < 1.0


# ─── Terrain ──────────────────────────────────────────────────────────────────

class TestTerrain:
    def _sample_dem(self, n=20):
        lats = np.linspace(18.5, 18.6, n)
        lons = np.linspace(74.4, 74.5, n)
        xx, yy = np.meshgrid(np.linspace(0, 1, n), np.linspace(0, 1, n), indexing="ij")
        elevs = 200 + 50 * xx + 30 * yy
        return lats, lons, elevs

    def test_slope_flat_is_zero(self):
        from app.geospatial.terrain import compute_slope
        lats = np.linspace(18.5, 18.6, 10)
        lons = np.linspace(74.4, 74.5, 10)
        elevs = np.ones((10, 10)) * 300.0  # perfectly flat
        slope = compute_slope(lats, lons, elevs)
        assert np.allclose(slope, 0.0, atol=1e-6)

    def test_candidate_cells_within_bbox(self):
        from app.geospatial.terrain import identify_candidate_cells
        lats, lons, elevs = self._sample_dem()
        candidates = identify_candidate_cells(lats, lons, elevs)
        for c in candidates:
            assert lats.min() <= c["lat"] <= lats.max()
            assert lons.min() <= c["lon"] <= lons.max()

    def test_contour_geojson_structure(self):
        from app.geospatial.terrain import generate_contours_geojson
        lats, lons, elevs = self._sample_dem()
        geojson = generate_contours_geojson(lats, lons, elevs, interval_m=10.0)
        assert geojson["type"] == "FeatureCollection"
        assert isinstance(geojson["features"], list)
        for feat in geojson["features"]:
            assert feat["type"] == "Feature"
            assert "elevation_m" in feat["properties"]
            assert feat["geometry"]["type"] == "LineString"
