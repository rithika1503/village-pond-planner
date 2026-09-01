#!/usr/bin/env python3
import sys
import logging
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt

# Add backend to path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(backend_dir))

from app.geospatial.kml_parser import parse_kml_bytes, contours_to_dem
from app.geospatial.hydrology_engine import HydrologyEngine

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

SAMPLE_KML = backend_dir.parent / "contours_1m.kml"

def main():
    if not SAMPLE_KML.exists():
        logging.error(f"Could not find sample KML at {SAMPLE_KML}")
        sys.exit(1)

    logging.info("Parsing KML...")
    kml_bytes = SAMPLE_KML.read_bytes()
    contour_lines = parse_kml_bytes(kml_bytes)
    # Use 150 grid points — SAME as generate_top3_geojson.py so results are consistent
    lats, lons, elevs = contours_to_dem(contour_lines, n_points=150)

    logging.info("Running Hydrology Engine...")
    engine = HydrologyEngine(lats, lons, elevs)
    engine.process()

    accum = engine.accum
    filled = engine.filled

    from app.geospatial.terrain import identify_candidate_cells
    from app.geospatial.land_suitability import rank_candidates
    from app.geospatial.catchment import delineate_catchment

    # Call synchronous fetch directly (no asyncio.run wrapper needed in script context)
    logging.info("Fetching OSM rivers to exclude from candidate search...")
    from app.geospatial.osm_client import _fetch_waterways_sync
    osm_waterways = _fetch_waterways_sync(float(lats.min()), float(lats.max()), float(lons.min()), float(lons.max()))

    logging.info("Finding candidates (excluding OSM rivers)...")
    candidates = identify_candidate_cells(lats, lons, elevs, osm_waterways=osm_waterways)
    sorted_candidates = rank_candidates(candidates)
    top5 = sorted_candidates[:5]

    extent = [lons.min(), lons.max(), lats.min(), lats.max()]

    # Collect catchment masks
    catchment_masks = []
    for cand in top5:
        try:
            start_i = int(np.argmin(np.abs(lats - cand['lat'])))
            start_j = int(np.argmin(np.abs(lons - cand['lon'])))
            
            # Simple snap to stream
            stream_threshold = float(np.percentile(accum, 92.0))
            snap_i, snap_j = start_i, start_j
            for _ in range(50):
                ri, rj = engine.flow_to[snap_i, snap_j]
                if ri < 0: break
                if accum[ri, rj] >= stream_threshold:
                    snap_i, snap_j = ri, rj
                    break
                if accum[ri, rj] > accum[snap_i, snap_j] * 1.5 + 5:
                    snap_i, snap_j = ri, rj
                    break
                snap_i, snap_j = ri, rj
                
            cells = engine.trace_upstream(snap_i, snap_j)
            
            mask = np.zeros_like(accum, dtype=bool)
            for ci, cj in cells:
                mask[ci, cj] = True
            catchment_masks.append(mask)
        except Exception as e:
            logging.warning(f"Failed to mask catchment: {e}")

    def plot_catchments():
        for i, mask in enumerate(catchment_masks):
            rgba = np.zeros((*mask.shape, 4))
            if i == 0:
                rgba[mask] = [1, 0, 0, 0.4] # Red
            elif i == 1:
                rgba[mask] = [0, 1, 0, 0.4] # Green
            elif i == 2:
                rgba[mask] = [0, 0, 1, 0.4] # Blue
            elif i == 3:
                rgba[mask] = [0, 1, 1, 0.4] # Cyan
            elif i == 4:
                rgba[mask] = [1, 0, 1, 0.4] # Magenta
            plt.imshow(rgba, origin='lower', extent=extent)

    # 1. Visualize Flow Accumulation (Log scale)
    logging.info("Generating flow_accumulation.png...")
    plt.figure(figsize=(10, 8))
    plt.imshow(np.log1p(accum), cmap='viridis', origin='lower', interpolation='bicubic', extent=extent)
    
    plot_catchments()
    
    # Plot top 5 pins
    for i, cand in enumerate(top5):
        plt.plot(cand['lon'], cand['lat'], marker='*', color='red', markersize=15, markeredgecolor='black')
        plt.text(cand['lon'], cand['lat'], f" #{i+1}", color='white', fontsize=12, fontweight='bold')

    plt.colorbar(label='Log(Accumulation + 1)')
    plt.title('Flow Accumulation with Top 5 Catchments')
    plt.xlabel('Longitude')
    plt.ylabel('Latitude')
    plt.savefig(backend_dir.parent / "flow_accumulation.png", dpi=150)
    plt.close()

    # 2. Visualize Filled DEM
    logging.info("Generating filled_dem.png...")
    plt.figure(figsize=(10, 8))
    plt.imshow(filled, cmap='terrain', origin='lower', interpolation='bicubic', extent=extent)
    
    plot_catchments()
    
    # Plot top 5 pins
    for i, cand in enumerate(top5):
        plt.plot(cand['lon'], cand['lat'], marker='*', color='red', markersize=15, markeredgecolor='black')
        plt.text(cand['lon'], cand['lat'], f" #{i+1}", color='white', fontsize=12, fontweight='bold')

    plt.colorbar(label='Elevation (m)')
    plt.title('Filled DEM with Top 5 Catchments')
    plt.xlabel('Longitude')
    plt.ylabel('Latitude')
    plt.savefig(backend_dir.parent / "filled_dem.png", dpi=150)
    plt.close()

    logging.info("Done! Check flow_accumulation.png and filled_dem.png")

if __name__ == "__main__":
    main()
