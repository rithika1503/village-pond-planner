import json
from pathlib import Path

with open('analysis_result.json') as f:
    data = json.load(f)

pond = data['pond_location']
catchment_geom = data['catchment_geometry']
bbox = data['bounding_box']
elev_range = data['elevation_range_m']

html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Village Pond & Catchment Visualization</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    body {{
      margin: 0;
      padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
      display: flex;
      height: 100vh;
      background: #0f172a;
      color: #f8fafc;
    }}
    #map {{
      flex: 1;
      height: 100%;
    }}
    #sidebar {{
      width: 400px;
      padding: 24px;
      box-sizing: border-box;
      background: rgba(15, 23, 42, 0.96);
      backdrop-filter: blur(10px);
      box-shadow: -4px 0 24px rgba(0,0,0,0.5);
      display: flex;
      flex-direction: column;
      gap: 16px;
      overflow-y: auto;
      z-index: 1000;
    }}
    h1 {{
      font-size: 20px;
      margin: 0;
      color: #38bdf8;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .subtitle {{
      font-size: 13px;
      color: #94a3b8;
      margin-top: 4px;
    }}
    .card {{
      background: #1e293b;
      border-radius: 12px;
      padding: 16px;
      border: 1px solid #334155;
    }}
    .card-title {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #94a3b8;
      margin-bottom: 12px;
      font-weight: 600;
    }}
    .stat-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }}
    .stat-item {{
      display: flex;
      flex-direction: column;
    }}
    .stat-label {{
      font-size: 11px;
      color: #64748b;
      margin-bottom: 2px;
    }}
    .stat-value {{
      font-size: 15px;
      font-weight: 600;
      color: #f1f5f9;
    }}
    .badge {{
      display: inline-block;
      padding: 4px 10px;
      border-radius: 9999px;
      font-size: 12px;
      font-weight: 600;
      background: rgba(16, 185, 129, 0.2);
      color: #34d399;
      border: 1px solid rgba(16, 185, 129, 0.4);
    }}
    .legend-item {{
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 13px;
      margin-bottom: 8px;
    }}
    .legend-color {{
      width: 18px;
      height: 18px;
      border-radius: 4px;
      flex-shrink: 0;
    }}
  </style>
</head>
<body>
  <div id="map"></div>
  <div id="sidebar">
    <div>
      <h1>💧 Pond & Catchment Map</h1>
      <div class="subtitle">Computed directly from <code>contours_1m.kml</code></div>
    </div>

    <div class="card">
      <div class="card-title">Suitability Assessment</div>
      <div style="display: flex; align-items: center; justify-content: space-between;">
        <div>
          <div class="stat-label">Overall Suitability Score</div>
          <div class="stat-value" style="font-size: 24px; color: #38bdf8;">{data['suitability_score']:.2f} / 1.0</div>
        </div>
        <span class="badge">Recommended</span>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Optimal Pond Location</div>
      <div class="stat-grid">
        <div class="stat-item">
          <span class="stat-label">Latitude</span>
          <span class="stat-value">{pond['lat']:.5f}°</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">Longitude</span>
          <span class="stat-value">{pond['lon']:.5f}°</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">Elevation</span>
          <span class="stat-value">{pond['elevation_m']:.1f} m</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">Slope</span>
          <span class="stat-value">{pond['slope_deg']:.1f}° (Flat)</span>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Catchment & Hydrology Stats</div>
      <div class="stat-grid">
        <div class="stat-item">
          <span class="stat-label">Catchment Area</span>
          <span class="stat-value">{data['catchment_area_ha']:.2f} ha</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">Catchment (m²)</span>
          <span class="stat-value">{data['catchment_area_m2']:,.0f} m²</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">Est. Runoff Yield</span>
          <span class="stat-value">{data['runoff_volume_m3']:,.0f} m³</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">Pond Surface Area</span>
          <span class="stat-value">{data['pond_surface_area_m2']:,.0f} m²</span>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Map Layers</div>
      <div class="legend-item">
        <div class="legend-color" style="background: #0284c7; border: 2px solid #38bdf8;"></div>
        <span>Delineated Catchment Basin</span>
      </div>
      <div class="legend-item">
        <div class="legend-color" style="background: #ef4444; border-radius: 50%;"></div>
        <span>Selected Pond Excavation Site</span>
      </div>
      <div class="legend-item">
        <div class="legend-color" style="background: rgba(234, 179, 8, 0.2); border: 2px dashed #eab308;"></div>
        <span>KML Survey Extent</span>
      </div>
    </div>
  </div>

  <script>
    const map = L.map('map', {{
      center: [{pond['lat']}, {pond['lon']}],
      zoom: 15
    }});

    // Satellite base layer
    const satellite = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
      attribution: 'Esri World Imagery',
      maxZoom: 19
    }}).addTo(map);

    // OSM street map
    const osm = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      attribution: '© OpenStreetMap contributors',
      maxZoom: 19
    }});

    L.control.layers({{
      'Satellite': satellite,
      'OpenStreetMap': osm
    }}, null, {{ position: 'topleft' }}).addTo(map);

    // Catchment Polygon
    const catchmentGeojson = {json.dumps(catchment_geom)};
    const catchmentLayer = L.geoJSON(catchmentGeojson, {{
      style: {{
        color: '#38bdf8',
        weight: 3,
        opacity: 0.9,
        fillColor: '#0284c7',
        fillOpacity: 0.45
      }}
    }}).addTo(map);

    // Survey Bounding Box
    const bboxBounds = [
      [{bbox['min_lat']}, {bbox['min_lon']}],
      [{bbox['max_lat']}, {bbox['max_lon']}]
    ];
    L.rectangle(bboxBounds, {{
      color: '#eab308',
      weight: 2,
      dashArray: '6, 6',
      fill: false
    }}).addTo(map);

    // Pond Circle (sized to estimated excavation footprint)
    const pondCircle = L.circle([{pond['lat']}, {pond['lon']}], {{
      radius: Math.max(15, Math.sqrt({data['pond_surface_area_m2']} / Math.PI)),
      color: '#ef4444',
      weight: 2,
      fillColor: '#f87171',
      fillOpacity: 0.75
    }}).addTo(map);

    // Pond Marker & Popup
    const pondMarker = L.marker([{pond['lat']}, {pond['lon']}]).addTo(map);
    pondMarker.bindPopup(`
      <div style="color: #0f172a; font-family: sans-serif;">
        <h3 style="margin: 0 0 6px 0; color: #0284c7;">Optimal Pond Location</h3>
        <b>Latitude:</b> {pond['lat']:.5f}°<br/>
        <b>Longitude:</b> {pond['lon']:.5f}°<br/>
        <b>Elevation:</b> {pond['elevation_m']:.1f} m<br/>
        <b>Slope:</b> {pond['slope_deg']:.1f}°<br/>
        <b>Catchment Area:</b> {data['catchment_area_ha']:.2f} ha ({data['catchment_area_m2']:,.0f} m²)<br/>
        <b>Suitability Score:</b> {data['suitability_score']:.2f} / 1.0
      </div>
    `).openPopup();

    map.fitBounds(catchmentLayer.getBounds(), {{ padding: [50, 50] }});
  </script>
</body>
</html>"""

with open('catchment_map.html', 'w') as f:
    f.write(html_content)

print('catchment_map.html successfully generated!')
