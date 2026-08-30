import asyncio
import sys
import os
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.geospatial.kml_parser import parse_kml_bytes, contours_to_dem
from app.geospatial.terrain import identify_candidate_cells, is_stream_channel
from app.geospatial.hydrology_engine import HydrologyEngine
from app.geospatial.land_suitability import rank_candidates
from app.geospatial.catchment import delineate_catchment
from app.geospatial.hydrology import estimate_runoff_volume, size_pond
from app.config import settings
import numpy as np
import math

def get_contour_color(norm: float) -> str:
    """Return a hex colour from blue (low) to green (mid) to red (high)."""
    if norm < 0.5:
        # Blue to Green
        r = 0
        g = int(255 * (norm * 2))
        b = int(255 * (1 - norm * 2))
    else:
        # Green to Red
        r = int(255 * ((norm - 0.5) * 2))
        g = int(255 * (1 - (norm - 0.5) * 2))
        b = 0
    return f"#{r:02x}{g:02x}{b:02x}"

async def main():
    kml_path = "../contours_1m.kml"
    out_geojson_path = "../top3_visualization.geojson"
    
    if not os.path.exists(kml_path):
        print(f"Error: {kml_path} not found.")
        return

    print("Parsing KML and generating DEM...")
    with open(kml_path, "rb") as f:
        raw = f.read()
    
    contour_lines = parse_kml_bytes(raw)
    lats, lons, elevs = contours_to_dem(contour_lines, n_points=150)  # Increased resolution for smoother polygons
    
    print("Finding candidates...")
    raw_candidates = identify_candidate_cells(lats, lons, elevs)
    
    if not raw_candidates:
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

    ranked = rank_candidates(raw_candidates, max_sites=10)
    candidates_to_process = ranked[:10] if ranked else raw_candidates[:10]
    
    features = []
    
    # ── Add Contour Lines to GeoJSON ──
    print("Adding contour lines to output...")
    elevations = [elev for elev, _ in contour_lines]
    min_elev, max_elev = min(elevations), max(elevations)
    
    for elev, coords in contour_lines:
        norm = (elev - min_elev) / (max_elev - min_elev) if max_elev > min_elev else 0.5
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coords
            },
            "properties": {
                "elevation": elev,
                "stroke": get_contour_color(norm),
                "stroke-width": 1.5,
                "stroke-opacity": 0.6
            }
        })
    
    # 10-color palette for candidates
    palette = [
        {"fill": "#3388ff", "pond": "#d32f2f"},  # 1: Blue / Red
        {"fill": "#ff8833", "pond": "#f57c00"},  # 2: Orange
        {"fill": "#33ff88", "pond": "#388e3c"},  # 3: Green
        {"fill": "#9933ff", "pond": "#7b1fa2"},  # 4: Purple
        {"fill": "#ff33a8", "pond": "#c2185b"},  # 5: Pink
        {"fill": "#33d6ff", "pond": "#0097a7"},  # 6: Cyan
        {"fill": "#ffd700", "pond": "#fbc02d"},  # 7: Yellow/Gold
        {"fill": "#8b4513", "pond": "#5d4037"},  # 8: Brown
        {"fill": "#00fa9a", "pond": "#00796b"},  # 9: Teal
        {"fill": "#ff4500", "pond": "#e64a19"},  # 10: Deep Orange
    ]

    print(f"Delineating catchments for top {len(candidates_to_process)} candidates...")
    for idx, cand in enumerate(candidates_to_process):
        rank = idx + 1
        colors = palette[idx % len(palette)]
        print(f"  Processing Candidate {rank}...")
        try:
            catchment = delineate_catchment(lats, lons, elevs, cand["lat"], cand["lon"])
            
            # Catchment Polygon Feature
            features.append({
                "type": "Feature",
                "geometry": catchment["geometry_geojson"],
                "properties": {
                    "name": f"Candidate {rank} Catchment",
                    "rank": rank,
                    "area_ha": catchment["area_ha"],
                    "fill": colors["fill"],
                    "fill-opacity": 0.35,
                    "stroke": "#1a2b3c",
                    "stroke-width": 3
                }
            })
            
        except Exception as e:
            print(f"  Failed to delineate catchment for Candidate {rank}: {e}")
            continue
            
        # Calculate Pond Size
        runoff = estimate_runoff_volume(catchment["area_m2"], 800.0, "default")
        pond = size_pond(runoff["runoff_volume_m3"], 3.0)
        area_m2 = pond["pond_surface_area_m2"]
        
        # Draw SQUARE border for the pond (plot of land)
        side_m = math.sqrt(area_m2)
        half_side_m = side_m / 2.0
        
        # Convert meters to degrees
        dy_deg = half_side_m / 111320.0
        dx_deg = half_side_m / (111320.0 * math.cos(math.radians(cand["lat"])))
        
        square_coords = [
            [cand["lon"] - dx_deg, cand["lat"] + dy_deg], # Top-Left
            [cand["lon"] + dx_deg, cand["lat"] + dy_deg], # Top-Right
            [cand["lon"] + dx_deg, cand["lat"] - dy_deg], # Bottom-Right
            [cand["lon"] - dx_deg, cand["lat"] - dy_deg], # Bottom-Left
            [cand["lon"] - dx_deg, cand["lat"] + dy_deg]  # Close ring
        ]
        
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [square_coords]
            },
            "properties": {
                "name": f"Candidate {rank} Excavation Border ({area_m2:.1f} m²)",
                "rank": rank,
                "elevation_m": cand.get("elevation_m"),
                "fill": colors["pond"],
                "fill-opacity": 0.8,
                "stroke": "#000000",
                "stroke-width": 3
            }
        })
        
    geojson = {
        "type": "FeatureCollection",
        "features": features
    }
    
    with open(out_geojson_path, "w") as f:
        json.dump(geojson, f, indent=2)

    with open("../top10_visualization.geojson", "w") as f:
        json.dump(geojson, f, indent=2)
        
    print(f"\nSuccess! Visualisation saved to {out_geojson_path} and ../top10_visualization.geojson")
    print("You can open this file in QGIS or paste its contents into https://geojson.io")

if __name__ == "__main__":
    asyncio.run(main())
