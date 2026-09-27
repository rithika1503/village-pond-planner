/**
 * Village Pond Planner — Wizard frontend
 *
 * State machine:
 *   method → (area) → city → mapmode → top3 OR draw → draw-params → draw-results
 *          → (kml)  → kml upload → kml-viz + kml-results
 */

const API_BASE = "http://10.1.75.53:5293";


// ─── Map ──────────────────────────────────────────────────────────────────────
const map = L.map("map", { zoomControl: true }).setView([20.5, 78.9], 5);

const osmLayer = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "© OpenStreetMap contributors", maxZoom: 19,
});
const satelliteLayer = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  { attribution: "Tiles © Esri — Maxar, Earthstar Geographics", maxZoom: 19 }
);
osmLayer.addTo(map);
let satelliteActive = false;

// ─── Layer groups ─────────────────────────────────────────────────────────────
const landLayer      = new L.FeatureGroup().addTo(map);
const catchmentLayer = new L.FeatureGroup().addTo(map);
const cityLayer      = new L.FeatureGroup().addTo(map);
const resultLayer    = new L.FeatureGroup().addTo(map);

// ─── Draw controls ────────────────────────────────────────────────────────────
const landDrawCtrl = new L.Draw.Polygon(map, {
  shapeOptions: { color:"#6c9eb7", weight:2, dashArray:"6 4", fillColor:"#a8d5e2", fillOpacity:0.15 },
  showArea: true, metric: true,
});
const catchDrawCtrl = new L.Draw.Polygon(map, {
  shapeOptions: { color:"#7aafc4", weight:2, fillColor:"#a8d5e2", fillOpacity:0.25 },
  showArea: true, metric: true,
});

// ─── State ────────────────────────────────────────────────────────────────────
let appMode          = null;    // "area" | "kml"
let mapMode          = "top3";  // "top3" | "draw"
let cityBbox         = null;
let landGeoJSON      = null;
let catchmentGeoJSON = null;
let drawMode         = null;
let top3Recs         = [];      // store for card click → fly-to

// ─── DOM helpers ──────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const show = id => { $(id).classList.remove("hidden"); $(id).style.display = ""; };
const hide = id => { $(id).classList.add("hidden"); $(id).style.display = "none"; };

const ALL_STEPS = [
  "step-method","step-city","step-mapmode","step-top3",
  "step-draw","step-draw-params","step-draw-results",
  "step-kml","step-kml-viz","step-kml-results",
];

function showOnly(...ids) {
  ALL_STEPS.forEach(id => hide(id));
  ids.forEach(id => show(id));
}

function setStatus(state) {
  const tag = $("status-tag");
  tag.className = "status-tag " + state;
  const labels = {
    idle:"Ready", drawing:"Drawing…",
    loading:'<span class="spinner"></span>Analyzing…',
    done:"Done", error:"Error",
  };
  tag.innerHTML = labels[state] || "Ready";
}

// ─── STEP 0 — Method picker ────────────────────────────────────────────────
$("btn-method-area").addEventListener("click", () => {
  appMode = "area";
  showOnly("step-city");
  $("header-subtitle").textContent = "Map mode — search a city or draw your area";
});

$("btn-method-kml").addEventListener("click", () => {
  appMode = "kml";
  showOnly("step-kml");
  $("header-subtitle").textContent = "Contour map mode — upload a KML/KMZ file";
});

// ─── STEP 1A — City search ────────────────────────────────────────────────
$("btn-city-search").addEventListener("click", doCitySearch);
$("inp-city").addEventListener("keydown", e => { if (e.key === "Enter") doCitySearch(); });
$("inp-state").addEventListener("keydown", e => { if (e.key === "Enter") doCitySearch(); });

async function doCitySearch() {
  const city  = $("inp-city").value.trim();
  const state = $("inp-state").value.trim();
  if (!city) { showCityError("Enter a city or village name."); return; }

  hideCityError();
  setStatus("loading");
  $("btn-city-search").disabled = true;

  try {
    const params = new URLSearchParams({ q: city });
    if (state) params.set("state", state);
    const resp = await fetch(`${API_BASE}/api/city/search?${params}`);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const data = await resp.json();
    onCityFound(data);
    setStatus("idle");
  } catch (err) {
    showCityError("City not found: " + err.message);
    setStatus("error");
  } finally {
    $("btn-city-search").disabled = false;
  }
}

function onCityFound(data) {
  cityBbox = data.bbox;
  $("city-found-name").textContent = data.display_name.split(",").slice(0, 3).join(",");
  show("city-found");

  cityLayer.clearLayers();
  if (data.boundary_geojson) {
    L.geoJSON(data.boundary_geojson, {
      style: { color:"#aaa", weight:1.5, fillOpacity:0.04, dashArray:"4 3" },
    }).addTo(cityLayer);
  } else {
    const bb = data.bbox;
    L.rectangle([[bb.min_lat,bb.min_lon],[bb.max_lat,bb.max_lon]], {
      color:"#aaa", weight:1.5, fillOpacity:0.04, dashArray:"4 3",
    }).addTo(cityLayer);
  }
  const bb = data.bbox;
  map.fitBounds([[bb.min_lat,bb.min_lon],[bb.max_lat,bb.max_lon]], { padding:[20,20] });

  // Reveal mode selector
  showOnly("step-city","step-mapmode");
  show("step-city");
  show("step-mapmode");
  setMapMode("top3");
}

$("btn-city-clear").addEventListener("click", () => {
  cityBbox = null;
  hide("city-found");
  cityLayer.clearLayers();
  resultLayer.clearLayers();
  $("top3-cards").innerHTML = "";
  hideCityError();
  showOnly("step-city");
});

$("btn-skip-city").addEventListener("click", () => {
  cityBbox = null;
  showOnly("step-city","step-mapmode");
  setMapMode("draw"); // force draw mode when no city
  hide("step-top3");
  show("step-draw");
});

// ─── STEP 2A — Map mode toggle ────────────────────────────────────────────
$("btn-mode-top3").addEventListener("click", () => setMapMode("top3"));
$("btn-mode-draw").addEventListener("click", () => setMapMode("draw"));

function setMapMode(mode) {
  mapMode = mode;
  $("btn-mode-top3").classList.toggle("active", mode === "top3");
  $("btn-mode-draw").classList.toggle("active", mode === "draw");

  if (mode === "top3") {
    $("mode-hint").innerHTML = "Auto-recommend the 3 best pond sites inside the city bounds.";
    // Show top3 panel, hide draw panels
    show("step-top3");
    hide("step-draw");
    hide("step-draw-params");
    hide("step-draw-results");
  } else {
    $("mode-hint").innerHTML = "Draw a land area on the map to analyse it.";
    hide("step-top3");
    show("step-draw");
    // params + results only appear after drawing
  }
}

// ─── STEP 3A-i — Top-3 analysis ────────────────────────────────────────────
$("btn-top3-run").addEventListener("click", doTop3Analysis);

async function doTop3Analysis() {
  if (!cityBbox) { showCityError("Search for a city first."); return; }

  setStatus("loading");
  $("btn-top3-run").disabled = true;
  $("top3-cards").innerHTML = `<p class="loading-note"><span class="spinner"></span> Analyzing terrain…</p>`;
  resultLayer.clearLayers();

  try {
    const resp = await fetch(`${API_BASE}/api/city/recommendations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        city:           $("inp-city").value.trim(),
        state:          $("inp-state").value.trim() || null,
        land_cover:     $("sel-lc-city").value,
        desired_depth_m: parseFloat($("inp-depth-city").value) || 3.0,
        n_sites: 3,
      }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const data = await resp.json();
    renderTop3(data);
    setStatus("done");
  } catch (err) {
    $("top3-cards").innerHTML = `<p class="error-box visible">Failed: ${err.message}</p>`;
    setStatus("error");
  } finally {
    $("btn-top3-run").disabled = false;
  }
}

const SITE_COLORS = ["#7fb89a","#7aafc4","#b5a8d5","#c4a87a","#c47a7a"];

function renderTop3(data) {
  resultLayer.clearLayers();
  const container = $("top3-cards");
  container.innerHTML = "";
  top3Recs = data.recommendations || [];

  if (!top3Recs.length) {
    container.innerHTML = `<p class="loading-note">No sites found.</p>`;
    return;
  }

  // Rainfall header
  const hdr = document.createElement("div");
  hdr.className = "result-note";
  hdr.style.marginBottom = "6px";
  hdr.innerHTML = `Rainfall: <strong>${fmt(data.annual_rainfall_mm,0)} mm/yr</strong> (${data.rainfall_years} yrs)`;
  container.appendChild(hdr);

  top3Recs.forEach((site, idx) => {
    const color = SITE_COLORS[idx] || "#888";

    // Card
    const card = document.createElement("div");
    card.className = "site-card";
    card.dataset.idx = idx;
    card.innerHTML = `
      <div class="site-card-header" style="border-left-color:${color}">
        <span class="site-rank">#${idx+1}</span>
        <span class="site-score">${pct(site.suitability_score)}</span>
      </div>
      <div class="site-card-body">
        <div class="result-row"><span class="result-label">Location</span><span class="result-value">${site.lat.toFixed(4)}, ${site.lon.toFixed(4)}</span></div>
        <div class="result-row"><span class="result-label">Catchment</span><span class="result-value highlight">${fmt(site.catchment_area_ha,1)} ha</span></div>
        <div class="result-row"><span class="result-label">Expected water vol.</span><span class="result-value highlight">${fmtBig(site.pond_storage_capacity_m3)} m³</span></div>
        <div class="result-row"><span class="result-label">Est. annual runoff</span><span class="result-value">${fmtBig(site.runoff_volume_m3)} m³</span></div>
        <div class="result-row"><span class="result-label">Pond area</span><span class="result-value">${fmtBig(site.pond_surface_area_m2)} m²</span></div>
        <div class="result-row"><span class="result-label">Elevation · Slope</span><span class="result-value">${site.elevation_m} m · ${site.slope_deg}°</span></div>
        <div class="result-row"><span class="result-label">Land status</span><span class="result-value">${site.land_status || "—"}</span></div>
      </div>`;
    card.addEventListener("click", () => highlightSite(idx));
    container.appendChild(card);

    // Map overlays
    if (site.catchment_geometry) {
      L.geoJSON(site.catchment_geometry, {
        style:{ color, weight:1.5, fillColor:color, fillOpacity:0.15, dashArray:"4 3" },
      }).bindTooltip(`Site #${idx+1} Catchment: ${fmt(site.catchment_area_ha,1)} ha`).addTo(resultLayer);
    }
    if (site.pond_geometry) {
      L.geoJSON(site.pond_geometry, {
        style:{ color, weight:2, fillColor:color, fillOpacity:0.45 },
      }).bindTooltip(`Site #${idx+1} Pond`).addTo(resultLayer);
    }
    const icon = L.divIcon({
      className: "",
      html: `<div style="background:${color};color:#fff;border:2px solid #fff;
        border-radius:50%;width:22px;height:22px;display:flex;align-items:center;
        justify-content:center;font-size:11px;font-weight:700;
        box-shadow:0 1px 4px rgba(0,0,0,.3)">${idx+1}</div>`,
      iconSize:[22,22], iconAnchor:[11,11],
    });
    L.marker([site.lat, site.lon], { icon })
      .bindPopup(`<b>Site #${idx+1} — ${pct(site.suitability_score)}</b><br>
        <b>Expected Water Volume: ${fmtBig(site.pond_storage_capacity_m3)} m³</b><br>
        Est. Runoff: ${fmtBig(site.runoff_volume_m3)} m³<br>
        Catchment: ${fmt(site.catchment_area_ha,1)} ha<br>
        Elevation: ${site.elevation_m} m · Slope: ${site.slope_deg}°<br>
        Land: ${site.land_status || "—"}`)
      .addTo(resultLayer);
  });

  try {
    const b = resultLayer.getBounds();
    if (b.isValid()) map.fitBounds(b, { padding:[30,30] });
  } catch (_) {}
}

function highlightSite(idx) {
  document.querySelectorAll(".site-card").forEach((c,i) => c.classList.toggle("active", i === idx));
  const site = top3Recs[idx];
  if (site) map.flyTo([site.lat, site.lon], 14, { duration:0.8 });
}

// ─── STEP 3A-ii — Draw area ───────────────────────────────────────────────
map.on(L.Draw.Event.CREATED, (e) => {
  const geoJSON = e.layer.toGeoJSON().geometry;
  if (drawMode === "land") {
    landLayer.clearLayers();
    landLayer.addLayer(e.layer);
    landGeoJSON = geoJSON;
    $("btn-draw-land").classList.add("active");
    $("btn-draw-land").textContent = "✓ Land area drawn";
    show("step-draw-params");
    updateAnalyzeBtn();
  } else if (drawMode === "catchment") {
    catchmentLayer.clearLayers();
    catchmentLayer.addLayer(e.layer);
    catchmentGeoJSON = geoJSON;
    $("btn-draw-catchment").classList.add("active");
    $("btn-draw-catchment").textContent = "✓ Catchment drawn";
    updateAnalyzeBtn();
  }
  drawMode = null;
  hideBadge();
  setStatus("idle");
});

map.on(L.Draw.Event.DRAWSTOP, () => { drawMode = null; hideBadge(); });

$("btn-draw-land").addEventListener("click", () => {
  stopDrawing();
  drawMode = "land";
  landDrawCtrl.enable();
  setStatus("drawing");
  showBadge("Click vertices to outline the land area. Double-click to close.");
});

$("btn-draw-catchment").addEventListener("click", () => {
  stopDrawing();
  drawMode = "catchment";
  catchDrawCtrl.enable();
  setStatus("drawing");
  showBadge("Click vertices to outline the catchment. Double-click to close.");
});

$("btn-analyze").addEventListener("click", runDrawAnalysis);

async function runDrawAnalysis() {
  if (!landGeoJSON) return;
  if ((landGeoJSON.coordinates?.[0] ?? []).length > 502) {
    showError("Too many polygon vertices — please simplify the shape.");
    return;
  }

  setStatus("loading");
  hideError();
  $("btn-analyze").disabled = true;
  resultLayer.clearLayers();

  try {
    const resp = await fetch(`${API_BASE}/api/analyze-polygon`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        land_polygon:     landGeoJSON,
        catchment_polygon: catchmentGeoJSON || null,
        land_cover:       $("sel-land-cover").value,
        desired_depth_m:  parseFloat($("inp-depth").value) || 3.0,
      }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const d = await resp.json();
    renderDrawResults(d);
    renderDrawOverlay(d);
    show("step-draw-results");
    setStatus("done");
  } catch (err) {
    showError("Analysis failed: " + err.message);
    setStatus("error");
  } finally {
    $("btn-analyze").disabled = false;
    updateAnalyzeBtn();
  }
}

function renderDrawResults(d) {
  $("r-catch-area").textContent = fmt(d.catchment_area_ha,1) + " ha";
  $("r-rainfall").textContent   = fmt(d.annual_rainfall_mm,0) + " mm/yr ×" + d.rainfall_years + " yrs";
  $("r-storage").textContent    = fmtBig(d.pond_storage_capacity_m3) + " m³";
  $("r-runoff").textContent     = fmtBig(d.runoff_volume_m3) + " m³";
  $("r-pond-area").textContent  = fmtBig(d.pond_surface_area_m2) + " m²";
  $("r-pond-depth").textContent = fmt(d.pond_depth_m,1) + " m";
  $("r-suitability").textContent= pct(d.suitability_score);
  $("r-source").textContent     = d.rainfall_source;
  $("r-catch-src").textContent  = d.catchment_source === "user-drawn" ? "User polygon" : "D8 auto";
  $("score-bar").style.width    = pct(d.suitability_score);
}

function renderDrawOverlay(d) {
  resultLayer.clearLayers();
  if (d.catchment_geometry) {
    L.geoJSON(d.catchment_geometry, {
      style:{ color:"#7aafc4", weight:1.5, fillColor:"#a8d5e2", fillOpacity:0.30, dashArray:"4 3" },
    }).bindTooltip("Catchment: " + fmt(d.catchment_area_ha,1) + " ha").addTo(resultLayer);
  }
  if (d.pond_geometry) {
    L.geoJSON(d.pond_geometry, {
      style:{ color:"#7fb89a", weight:2, fillColor:"#b5d5c5", fillOpacity:0.50 },
    }).bindTooltip("Pond: " + fmtBig(d.pond_surface_area_m2) + " m²").addTo(resultLayer);
  }
  if (d.pond_location) {
    const { lat, lon } = d.pond_location;
    L.circleMarker([lat,lon], {
      radius:7, color:"#fff", weight:2, fillColor:"#7fb89a", fillOpacity:1,
    }).bindPopup(
      `<b>Suggested Pond Location</b><br>
       <b>Expected Water Volume: ${fmtBig(d.pond_storage_capacity_m3)} m³</b><br>
       Est. Annual Runoff: ${fmtBig(d.runoff_volume_m3)} m³<br>
       Catchment: ${fmt(d.catchment_area_ha,1)} ha<br>
       Rainfall: ${fmt(d.annual_rainfall_mm,0)} mm/yr<br>
       Land status: ${d.pond_location.land_status || "—"}`
    ).addTo(resultLayer);
    try {
      const b = resultLayer.getBounds();
      if (b.isValid()) map.fitBounds(b, { padding:[30,30] });
    } catch (_) {}
  }
}

// ─── STEP 1B — KML upload ─────────────────────────────────────────────────
$("inp-kml").addEventListener("change", () => {
  $("btn-kml-run").disabled = !$("inp-kml").files?.length;
  $("kml-error").classList.remove("visible");
});

$("btn-kml-run").addEventListener("click", runKmlAnalysis);

async function runKmlAnalysis() {
  const file = $("inp-kml").files?.[0];
  if (!file) return;

  setStatus("loading");
  $("kml-error").classList.remove("visible");
  $("btn-kml-run").disabled = true;
  resultLayer.clearLayers();
  hide("step-kml-viz");
  hide("step-kml-results");

  const fd = new FormData();
  fd.append("contour_map", file);
  fd.append("land_cover", $("sel-lc-kml").value);
  fd.append("desired_depth_m", $("inp-depth-kml").value);

  try {
    const resp = await fetch(`${API_BASE}/analyzeContourWithViz`, {
      method: "POST",
      body: fd,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const d = await resp.json();
    renderKmlViz(d);
    renderKmlResults(d);
    renderKmlOverlay(d);
    show("step-kml-viz");
    show("step-kml-results");
    setStatus("done");
  } catch (err) {
    $("kml-error").textContent = "Analysis failed: " + err.message;
    $("kml-error").classList.add("visible");
    setStatus("error");
  } finally {
    $("btn-kml-run").disabled = false;
  }
}

function renderKmlViz(d) {
  if (d.dem_image_b64) {
    const demSrc = "data:image/png;base64," + d.dem_image_b64;
    $("img-dem").src = demSrc;
    if ($("link-dem")) $("link-dem").href = demSrc;
  } else {
    // fallback to static images served from frontend folder
    $("img-dem").src = "filled_dem.png";
    if ($("link-dem")) $("link-dem").href = "filled_dem.png";
  }
  if (d.flow_image_b64) {
    const flowSrc = "data:image/png;base64," + d.flow_image_b64;
    $("img-flow").src = flowSrc;
    if ($("link-flow")) $("link-flow").href = flowSrc;
  } else {
    $("img-flow").src = "flow_accumulation.png";
    if ($("link-flow")) $("link-flow").href = "flow_accumulation.png";
  }
}

function renderKmlResults(d) {
  const er = d.elevation_range_m || {};
  $("kr-contours").textContent  = (d.contour_count ?? "—") + " lines";
  $("kr-elev").textContent      = `${fmt(er.min_m,0)}–${fmt(er.max_m,0)} m (Δ ${fmt(er.range_m,0)} m)`;
  $("kr-catch").textContent     = fmt(d.catchment_area_ha,1) + " ha";
  $("kr-storage").textContent   = fmtBig(d.pond_storage_capacity_m3) + " m³";
  $("kr-runoff").textContent    = fmtBig(d.runoff_volume_m3) + " m³";
  $("kr-pond-area").textContent = fmtBig(d.pond_surface_area_m2) + " m²";
  $("kr-pond-depth").textContent= fmt(d.pond_depth_m,1) + " m";
  $("kr-score").textContent     = pct(d.suitability_score);
  $("kr-score-bar").style.width = pct(d.suitability_score);
}

function renderKmlOverlay(d) {
  resultLayer.clearLayers();
  if (d.catchment_geometry) {
    L.geoJSON(d.catchment_geometry, {
      style:{ color:"#7aafc4", weight:1.5, fillColor:"#a8d5e2", fillOpacity:0.30, dashArray:"4 3" },
    }).bindTooltip("Catchment: " + fmt(d.catchment_area_ha,1) + " ha").addTo(resultLayer);
  }
  if (d.pond_geometry) {
    L.geoJSON(d.pond_geometry, {
      style:{ color:"#7fb89a", weight:2, fillColor:"#b5d5c5", fillOpacity:0.50 },
    }).bindTooltip("Pond footprint").addTo(resultLayer);
  }
  if (d.pond_location) {
    const { lat, lon } = d.pond_location;
    L.circleMarker([lat,lon], {
      radius:8, color:"#fff", weight:2, fillColor:"#7fb89a", fillOpacity:1,
    }).bindPopup(
      `<b>Best Pond Site (KML)</b><br>
       <b>Expected Water Volume: ${fmtBig(d.pond_storage_capacity_m3)} m³</b><br>
       Est. Runoff: ${fmtBig(d.runoff_volume_m3)} m³<br>
       Rainfall: ${fmt(d.annual_rainfall_mm,0)} mm/yr`
    ).addTo(resultLayer);
    try {
      const b = resultLayer.getBounds();
      if (b.isValid()) map.fitBounds(b, { padding:[30,30] });
    } catch (_) {}
  }
}

// ─── Restart ──────────────────────────────────────────────────────────────
$("btn-restart").addEventListener("click", () => {
  stopDrawing();
  [landLayer, catchmentLayer, cityLayer, resultLayer].forEach(l => l.clearLayers());
  landGeoJSON = catchmentGeoJSON = cityBbox = null;
  drawMode = null;
  appMode = mapMode = null;
  top3Recs = [];

  // Reset UI
  $("inp-city").value = "";
  $("inp-state").value = "";
  hide("city-found");
  hideCityError();
  $("top3-cards").innerHTML = "";
  $("btn-draw-land").classList.remove("active");
  $("btn-draw-land").textContent = "Draw land area";
  $("btn-draw-catchment").classList.remove("active");
  $("btn-draw-catchment").textContent = "Draw catchment (optional)";
  $("btn-analyze").disabled = true;
  $("kml-error").classList.remove("visible");
  $("inp-kml").value = "";
  $("btn-kml-run").disabled = true;
  $("header-subtitle").textContent = "Select an input method to begin";

  showOnly("step-method");
  setStatus("idle");
  hideBadge();
  map.setView([20.5, 78.9], 5);
});

// ─── Satellite toggle ─────────────────────────────────────────────────────
$("btn-satellite").addEventListener("click", () => {
  satelliteActive = !satelliteActive;
  if (satelliteActive) {
    map.removeLayer(osmLayer); satelliteLayer.addTo(map); satelliteLayer.bringToBack();
    $("btn-satellite").classList.add("active");
    $("btn-satellite").textContent = "🗺 Map";
  } else {
    map.removeLayer(satelliteLayer); osmLayer.addTo(map); osmLayer.bringToBack();
    $("btn-satellite").classList.remove("active");
    $("btn-satellite").textContent = "🛰 Satellite";
  }
});

// ─── Legend ───────────────────────────────────────────────────────────────
const legend = L.control({ position:"bottomright" });
legend.onAdd = () => {
  const div = L.DomUtil.create("div","legend");
  div.innerHTML = `
    <div class="legend-item"><span class="legend-swatch" style="background:#a8d5e230;border-color:#6c9eb7;border-style:dashed"></span>Land area</div>
    <div class="legend-item"><span class="legend-swatch" style="background:#a8d5e24d;border-color:#7aafc4"></span>Catchment</div>
    <div class="legend-item"><span class="legend-swatch" style="background:#b5d5c580;border-color:#7fb89a"></span>Pond footprint</div>
    <div class="legend-item"><span class="legend-swatch" style="background:#7fb89a;border-color:#fff;border-radius:50%"></span>Pond site</div>
  `;
  return div;
};
legend.addTo(map);

// ─── Utility helpers ───────────────────────────────────────────────────────
function stopDrawing() { landDrawCtrl.disable(); catchDrawCtrl.disable(); }
function updateAnalyzeBtn() { $("btn-analyze").disabled = !landGeoJSON; }
function showBadge(msg) { $("draw-mode-badge").textContent = msg; $("draw-mode-badge").classList.add("visible"); }
function hideBadge()    { $("draw-mode-badge").classList.remove("visible"); }
function showCityError(msg) { $("city-error").textContent = msg; $("city-error").classList.add("visible"); }
function hideCityError()    { $("city-error").classList.remove("visible"); }
function showError(msg) { $("error-box").textContent = msg; $("error-box").classList.add("visible"); }
function hideError()    { $("error-box").classList.remove("visible"); }

function fmt(v, d) { return typeof v === "number" ? v.toFixed(d) : "—"; }
function fmtBig(v) {
  if (typeof v !== "number") return "—";
  if (v >= 1_000_000) return (v/1_000_000).toFixed(2)+"M";
  if (v >= 1_000)     return (v/1_000).toFixed(1)+"k";
  return v.toFixed(0);
}
function pct(v) { return typeof v === "number" ? Math.round(v*100)+"%" : "—"; }

// ─── Init ─────────────────────────────────────────────────────────────────
showOnly("step-method");
setStatus("idle");
