import asyncio
import sys
import os
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.geospatial.kml_parser import parse_kml_bytes, contours_to_dem, get_bounding_box, get_elevation_stats
from app.geospatial.terrain import identify_candidate_cells, is_stream_channel
from app.geospatial.hydrology_engine import HydrologyEngine
from app.geospatial.land_suitability import rank_candidates
from app.geospatial.catchment import delineate_catchment
from app.geospatial.hydrology import estimate_runoff_volume, size_pond
from app.geospatial.scoring import compute_suitability_score
from app.config import settings

async def main():
    kml_path = "../contours_1m.kml"
    if not os.path.exists(kml_path):
        print(f"Error: {kml_path} not found.")
        return

    print("=" * 60)
    print(" STEP 1: Parse KML and generate DEM ")
    print("=" * 60)
    with open(kml_path, "rb") as f:
        raw = f.read()
    
    contour_lines = parse_kml_bytes(raw)
    print(f"-> Parsed {len(contour_lines)} contour lines.")
    
    bbox = get_bounding_box(contour_lines)
    elev_stats = get_elevation_stats(contour_lines)
    print(f"-> Bounding Box: {bbox}")
    print(f"-> Elevation Stats: min={elev_stats.get('min', 0)}m, max={elev_stats.get('max', 0)}m, mean={elev_stats.get('mean', 0):.2f}m")
    
    lats, lons, elevs = contours_to_dem(contour_lines, n_points=settings.DEM_GRID_POINTS)
    print(f"-> Generated DEM grid of shape {elevs.shape}")
    print("\n")

    print("=" * 60)
    print(" STEP 2: Terrain Analysis & Pond Candidates ")
    print("=" * 60)
    raw_candidates = identify_candidate_cells(lats, lons, elevs)
    
    if not raw_candidates:
        import numpy as np
        engine = HydrologyEngine(lats, lons, elevs)
        engine.process()
        accum = engine.accum
        stream_mask = is_stream_channel(accum, settings.STREAM_ACCUM_PERCENTILE)
        elev_p40 = float(np.percentile(elevs, 40))
        raw_candidates = [
            {
                "lat": float(lats[i]), "lon": float(lons[j]), "elevation_m": float(elevs[i, j]),
                "slope_deg": 0.0, "flow_accum": int(accum[i, j]), "is_stream": bool(stream_mask[i, j])
            }
            for i in range(len(lats)) for j in range(len(lons))
            if elevs[i, j] <= elev_p40 and not stream_mask[i, j]
        ]
        if not raw_candidates:
             raw_candidates = [
                {
                    "lat": float(lats[i]), "lon": float(lons[j]), "elevation_m": float(elevs[i, j]),
                    "slope_deg": 0.0, "flow_accum": int(accum[i, j]), "is_stream": bool(stream_mask[i, j])
                }
                for i in range(len(lats)) for j in range(len(lons))
                if not stream_mask[i, j]
            ]
             if raw_candidates:
                 raw_candidates = [min(raw_candidates, key=lambda c: c["elevation_m"])]

    print(f"-> Identified {len(raw_candidates)} raw candidate cells.")
    
    ranked = rank_candidates(raw_candidates, max_sites=10)
    print(f"-> Ranked top candidates (showing top 10):")
    candidates_to_process = ranked[:10] if ranked else raw_candidates[:10]
    for idx, cand in enumerate(candidates_to_process):
        print(f"   [{idx+1:2d}] Lat: {cand['lat']:.6f}, Lon: {cand['lon']:.6f}, Elev: {cand.get('elevation_m',0):.2f}m, Slope: {cand.get('slope_deg',0):.2f}°, FlowAccum: {cand.get('flow_accum',0)}, Terrain Score: {cand.get('terrain_score', 0):.2f}")
    
    best = candidates_to_process[0]
    pond_lat = best["lat"]
    pond_lon = best["lon"]
    print("\n")

    print("=" * 60)
    print(" STEP 3: Catchment Delineation (for best candidate) ")
    print("=" * 60)
    catchment = delineate_catchment(lats, lons, elevs, pond_lat, pond_lon)
    print(f"-> Catchment Area: {catchment['area_m2']:.2f} m² ({catchment['area_ha']:.2f} ha)")
    print(f"-> Average Elevation in catchment: {catchment.get('avg_elevation_m', 0):.2f}m")
    print(f"-> Slope summary: {catchment.get('slope_summary', 'N/A')}")
    print("\n")

    print("=" * 60)
    print(" STEP 4: Rainfall ")
    print("=" * 60)
    centroid_lat = (bbox["min_lat"] + bbox["max_lat"]) / 2
    centroid_lon = (bbox["min_lon"] + bbox["max_lon"]) / 2
    try:
        from app.services.rainfall_service import fetch_rainfall
        rainfall_data = await fetch_rainfall(centroid_lat, centroid_lon)
        annual_rainfall_mm = rainfall_data["annual_mean_mm"]
        print(f"-> Fetched rainfall from Open-Meteo: {annual_rainfall_mm:.2f} mm/year")
    except Exception as e:
        annual_rainfall_mm = 800.0
        print(f"-> Fetch failed. Using default rainfall: 800.0 mm/year")
    print("\n")

    print("=" * 60)
    print(" STEP 5: Runoff Estimation & Pond Sizing ")
    print("=" * 60)
    land_cover = "default"
    runoff = estimate_runoff_volume(catchment["area_m2"], annual_rainfall_mm, land_cover)
    print(f"-> Runoff Coefficient (C): {runoff['runoff_coefficient']}")
    print(f"-> Estimated Annual Runoff Volume: {runoff['runoff_volume_m3']:.2f} m³")
    
    desired_depth_m = 3.0
    pond = size_pond(runoff["runoff_volume_m3"], desired_depth_m)
    print(f"-> Recommended Pond Depth: {pond['pond_depth_m']} m")
    print(f"-> Recommended Surface Area: {pond['pond_surface_area_m2']:.2f} m²")
    print(f"-> Total Storage Capacity: {pond['pond_storage_capacity_m3']:.2f} m³")
    print("\n")

    print("=" * 60)
    print(" STEP 6: Suitability Scoring ")
    print("=" * 60)
    score = compute_suitability_score(
        terrain_score=best.get("terrain_score", 0.5),
        catchment_area_m2=catchment["area_m2"],
        land_score=best.get("land_score", 0.5),
    )
    print(f"-> Total Suitability Score: {score['total_score']:.3f} (out of 1.0)")
    print(f"-> Breakdown:")
    print(f"     Terrain Score: {score['terrain_score']:.3f} (weight {score['weights']['terrain']})")
    print(f"     Catchment Score: {score['catchment_score']:.3f} (weight {score['weights']['catchment']})")
    print(f"     Land Availability: {score['land_score']:.3f} (weight {score['weights']['land']})")
    print("\n")

    print("Pipeline check complete!")


if __name__ == "__main__":
    asyncio.run(main())
