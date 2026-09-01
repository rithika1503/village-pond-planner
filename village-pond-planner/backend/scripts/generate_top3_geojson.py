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
    from app.geospatial.osm_client import fetch_waterways
    osm_waterways = await fetch_waterways(lats.min(), lats.max(), lons.min(), lons.max())
    
    raw_candidates = identify_candidate_cells(lats, lons, elevs, osm_waterways=osm_waterways)
    
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
    sorted_candidates = ranked if ranked else raw_candidates
    
    # ── Add Contour Lines to GeoJSON ──
    print("Adding contour lines to output...")
    elevations = [elev for elev, _ in contour_lines]
    min_elev, max_elev = min(elevations), max(elevations)
    
    contour_features = []
    for elev, coords in contour_lines:
        norm = (elev - min_elev) / (max_elev - min_elev) if max_elev > min_elev else 0.5
        contour_features.append({
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

    def generate_geojson(top_n: int, out_path: str):
        candidates_to_process = sorted_candidates[:top_n]
        features = list(contour_features) # start with contours
        
        print(f"\nDelineating catchments for top {len(candidates_to_process)} candidates to {out_path}...")
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
                
                print(f"    -> Catchment Area: {catchment['area_ha']} ha")
                
            except Exception as e:
                print(f"  Failed to delineate catchment for Candidate {rank}: {e}")
                continue
                
            # Calculate Pond Size
            runoff = estimate_runoff_volume(catchment["area_m2"], 800.0, "default")
            pond = size_pond(runoff["runoff_volume_m3"], 3.0)
            area_m2 = pond["pond_surface_area_m2"]
            
            # (Removed physical pond square borders to reduce visual noise on the map)

            # 2. Pond Pin Point
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [cand["lon"], cand["lat"]]
                },
                "properties": {
                    "name": f"Candidate {rank} Pin",
                    "rank": rank,
                    "elevation_m": cand.get("elevation_m"),
                    "marker-color": colors["pond"],
                    "marker-size": "large",
                    "marker-symbol": "water"
                }
            })
            
        geojson = {
            "type": "FeatureCollection",
            "features": features
        }
        
        with open(out_path, "w") as f:
            json.dump(geojson, f, indent=2)
            
        print(f"  -> Saved to {out_path}")

    # Generate both files
    generate_geojson(3, "../top3_visualization.geojson")
    generate_geojson(10, "../top10_visualization.geojson")
    
    print("\nSuccess! You can open these files in QGIS or paste their contents into https://geojson.io")

if __name__ == "__main__":
    asyncio.run(main())
