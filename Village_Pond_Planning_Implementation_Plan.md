# AI-based Village Pond Planning System — Complete Implementation Plan

**Based on:** Assignment 1 brief + your submitted HLD
**Today:** Aug 27, 2026 · **Final submission/demo:** Sep 5, 2026 (≈9 days)

Your HLD architecture is sound and matches what the assignment wants — this plan turns it into a day-by-day build order, not a redesign.

---

## 1. Scope lock (MVP definition)

One flow, built end-to-end first, polished after:

> Select village → satellite map → DEM/contours → candidate land → catchment → rainfall → runoff → pond sizing → recommendation → report

This covers all 8 functional requirements in the brief.

---

## 2. Architecture (from your HLD — unchanged)

```
React + Leaflet (frontend)
        │  REST/JSON, GeoJSON
        ▼
FastAPI (orchestration only — no heavy math here)
        │
   ┌────┼─────────────┐
   ▼    ▼             ▼
  DEM  Rainfall API  Land data
   │    │             │
   └────┼─────────────┘
        ▼
 Geospatial Engine (GeoPandas/Rasterio/Shapely/PyProj/NumPy)
   terrain → land suitability → catchment → runoff → pond sizing → scoring
        ▼
 PostgreSQL + PostGIS
        ▼
 Interactive map + results panel + report
```

**Principle:** FastAPI validates and orchestrates; all geospatial math lives in a separate `geospatial/` module. This is also what makes the algorithm easy to explain in the viva.

### Stack (per your HLD)
- Frontend: React + Leaflet
- Backend: FastAPI
- DB: PostgreSQL + PostGIS
- Geospatial: GeoPandas, Rasterio, Shapely, PyProj, NumPy, richdem/pysheds (flow direction/accumulation), matplotlib or gdal_contour (contours)
- Elevation: Open Elevation API (or SRTM tile fallback)
- Rainfall: Open-Meteo / NASA POWER (public, no key hassle — good backup to IMD)
- Land data: Bhunaksha / best available public source
- Deployment: Docker

Drop OpenCV unless you actually need raw image preprocessing — GeoPandas/Rasterio cover the terrain math.

---

## 3. Database schema

| Table | Key fields |
|---|---|
| **villages** | id, name, district, state, boundary (geom), centroid, bbox |
| **candidate_sites** | id, village_id, lat, lon, elevation, slope, land_status, terrain_score, catchment_score, rainfall_score, land_score, total_score |
| **catchments** | id, site_id, geometry, area, avg_elevation, slope_summary |
| **rainfall** | location, year, monthly rainfall, annual total, source, fetched_at |
| **analysis_results** | site_id, rainfall, catchment_area, runoff, depth, surface_area, storage_capacity, suitability_score, generated_at |

---

## 4. Core algorithms (from your HLD §6 — use these formulas directly)

**4.1 Terrain processing**
- Sink fill the DEM (corrects spurious pits) before any flow analysis.
- Contours: fetch DEM for village bbox → matplotlib or `gdal_contour` at a *configurable* interval (don't hardcode 5m/10m for every terrain).

**4.2 Candidate land identification**
- Slope from DEM (richdem `TerrainAttribute`).
- Flag low-slope + relatively-low-elevation cells as candidates.
- Classify by land status: government / private / unknown / water-body / built-up. Only government (or explicitly eligible) + unknown-with-lower-confidence should be marked eligible — reject water/built-up/protected.
- **Framing caution:** call this "land availability/suitability indication," not "legally verified government land," unless your data source actually proves ownership.

**4.3 Catchment delineation** (20/100 marks — your biggest single line item)
```
DEM → fill sinks → flow direction (D8) → flow accumulation
    → pond outlet point → trace upstream cells → catchment raster
    → polygonize → compute area
```
Use `pysheds` or `richdem` rather than writing D8 from scratch — well-tested, and you can still explain every step.

**4.4 Rainfall aggregation**
Catchment centroid → rainfall API → monthly data → annual totals → average/min/max over available years. Cache aggressively (rate limits).

**4.5 Runoff estimation**
```
V = P × A × C
V = runoff volume (m³)
P = rainfall depth, convert mm → m (divide by 1000)
A = catchment area (m²)
C = runoff coefficient (by land type: vegetated=low, agricultural=medium, built-up=high)
```

**4.6 Pond sizing**
```
Target storage ≈ estimated annual runoff
Depth = Storage Volume / Surface Area
```
Label this explicitly as a **preliminary estimate** — note evaporation, infiltration, seepage, sedimentation, and soil permeability are excluded, since a real design would need them.

**4.7 Suitability scoring** ("AI" component)
```
S = w1·T + w2·C + w3·R + w4·L
T = terrain suitability, C = catchment suitability
R = rainfall/runoff potential, L = land availability
```
Suggested starting weights: T 30%, C 30%, R 20%, L 20% — put them in config, not hardcoded, so you can justify/tune them live in the demo. Frame this as an **explainable weighted decision-support model**, not "we trained an ML model" — the brief doesn't require a trained model, and this is much easier to defend in the viva.

---

## 5. API design (per your HLD §5)

```
GET  /villages                       — search/list
GET  /villages/{id}                  — boundary + metadata
GET  /villages/{id}/imagery          — satellite layer
GET  /villages/{id}/elevation        — DEM grid / contours
GET  /villages/{id}/candidate-sites  — suggested low/vacant land
POST /analysis/catchment             — catchment for a chosen point
GET  /rainfall                       — historical stats for a location
POST /analysis/runoff                — runoff volume
POST /analysis/recommendation        — depth + storage
POST /analysis/full                  — orchestrates the entire chain, one call
GET  /health
```
`POST /analysis/full` is what your frontend should call for the main demo — it chains DEM → terrain → candidates → catchment → rainfall → runoff → pond sizing → score → result. All geometry crosses the API boundary as **GeoJSON**, never raw raster/GeoDataFrame objects.

---

## 6. Build order (vertical slices, not layer-by-layer)

Build backend→frontend→visible-result for each slice before moving on. This guarantees you always have a working app, even if time runs out.

1. **Skeleton** — FastAPI `/health`, DB connection, frontend can talk to backend.
2. **Village selection** — search → boundary → map zoom/display.
3. **Satellite + base map** — Leaflet layers with a toggle control (satellite / boundary / contours / candidates / catchment).
4. **DEM acquisition + caching** — fetch once per village, cache locally/DB; never re-fetch per click.
5. **Contour generation** — DEM → contour lines → GeoJSON → Leaflet.
6. **Terrain analysis** — slope + relative elevation → low-slope/low-elevation candidate regions.
7. **Land suitability** — classify candidate regions by land status; combine with terrain to shortlist top ~5 sites (not hundreds of raw points).
8. **Suitability scoring** — implement `S = w1T + w2C + w3R + w4L`, rank candidates.
9. **Catchment delineation** — D8 flow direction/accumulation → catchment polygon per selected site.
10. **Rainfall integration** — API call, cache, annual stats + optional small chart.
11. **Runoff calculation** — `V = P × A × C`, unit conversion handled carefully.
12. **Pond sizing** — depth + storage capacity, clearly labeled as preliminary.
13. **Results panel + map overlay** — everything from FR #8 rendered together.
14. **Caching layer** (DEM / rainfall / analysis results), async processing + loading indicators.
15. **Error handling & validation** (village not found, DEM unavailable, invalid coords, catchment failure fallback message).
16. **Docker Compose** (frontend, backend, postgis).
17. **Testing** (unit, geospatial, API, end-to-end demo script).
18. **Report generation + API docs (FastAPI auto-docs) + README/installation guide.**

### 🔴 Must-have vs 🟡 should-have vs 🟢 nice-to-have
- **Must:** items 1–13 above (covers System functionality 35 + Terrain/catchment 20 marks).
- **Should:** layer controls, caching, loading states, docs, tests, Docker, report.
- **Nice:** advanced ML, fancy charts, auth, multi-pond optimization, historical comparison — skip these if time is tight.

---

## 7. Suggested schedule (Aug 27 → Sep 5)

| Date | Target |
|---|---|
| Aug 27 | Project setup: PostGIS + FastAPI + React/Leaflet skeleton, `/health` working |
| Aug 28 | Village DB + search + satellite map + boundary display |
| Aug 29 | DEM fetch/cache + contour generation |
| Aug 30 | Slope + land suitability + candidate site generation |
| Aug 31 | Flow direction/accumulation + catchment delineation |
| Sep 1 | Rainfall API integration + historical stats |
| Sep 2 | Runoff calc + pond sizing + suitability scoring |
| Sep 3 | Full frontend integration (`/analysis/full`) + caching + error handling |
| Sep 4 | Testing + Docker + docs + **prepare one cached demo village as fallback** |
| Sep 5 | Final demo/submission |

**Critical safety net:** pick one real village and pre-cache its boundary, DEM, contours, land data, and rainfall now. Your live demo runs off this cached village so an API outage on demo day doesn't sink you — you can still show live calls separately as a bonus.

---

## 8. Testing checklist

- **Unit:** rainfall unit conversion, runoff formula, area calc, pond sizing, suitability score.
- **Geospatial:** known small test DEM → verify slope, flow direction, flow accumulation, catchment shape.
- **API:** every endpoint above, including failure paths.
- **End-to-end:** the full 13-step flow in Section 6, run start to finish — make this your primary demo script.

---

## 9. Validation & error handling (needed for a credible demo)

- Village not found → clear message, not a stack trace.
- DEM unavailable → "elevation data temporarily unavailable," fall back to cache.
- Rainfall API down → serve cached data.
- Catchment fails → "unable to calculate catchment for this point, try another candidate."
- Reject invalid coordinates, negative areas, runoff coefficient outside [0,1], non-positive depth/storage.

---

## 10. Report & documentation deliverables (matches assignment deliverables list)

- Source code (Git history with meaningful incremental commits, not one final dump).
- Installation guide (README: setup, env vars, Docker run instructions).
- API documentation (FastAPI's auto-generated docs, filled in with descriptions/examples per endpoint).
- Final technical report per site: village info, coordinates, terrain, catchment, rainfall stats, runoff, pond recommendation, suitability score, **assumptions & limitations** (this last section matters — it's where you show engineering judgement).
- **Short design note per algorithm**: what it does, why you chose it, inputs/outputs, assumptions, limitations. This directly serves the assignment's LLM policy requirement that you can explain every component in the viva.

---

## 11. Mapping to the evaluation rubric

| Criterion | Marks | Where it's covered above |
|---|---|---|
| System functionality | 35 | Sections 6 (steps 1–13), 5 |
| Terrain & catchment analysis | 20 | Sections 4.1–4.3, step 9 |
| Frontend & visualization | 5 | Sections 2, 6 (steps 2–3, 13) |
| Software design & code quality | 15 | Section 2 (orchestration-only FastAPI), Section 3 |
| System design & management | 15 | Sections 6–7 (build order, schedule, caching, Docker) |
| Documentation & report | 10 | Section 10 |

Prioritizing in this order means the highest-weighted criteria (functionality + terrain/catchment = 55 marks) are locked in first, before polish.
