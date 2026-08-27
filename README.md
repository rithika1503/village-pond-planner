# Village Pond Planner — AI-Based Site Selection System

AI-driven geospatial decision-support system for identifying optimal pond locations in Indian villages.  
Built for **CSD Assignment 1** (submission: Sep 5, 2026).

---

## Architecture

```
React + Leaflet (frontend)
        │  REST / GeoJSON
        ▼
FastAPI (orchestration only — no heavy math here)
        │
   ┌────┼──────────────────┐
   ▼    ▼                  ▼
  DEM  Rainfall API      Land data
        │
        ▼
Geospatial Engine (GeoPandas / Rasterio / Shapely / pysheds / NumPy)
  terrain → land suitability → catchment → runoff → pond sizing → scoring
        ▼
PostgreSQL + PostGIS
        ▼
Interactive map + results panel + report
```

## Algorithms

| Algorithm | Formula | Location |
|---|---|---|
| Terrain slope | Finite differences (Δelev / Δdist) | `geospatial/terrain.py` |
| Catchment | DEM → fill sinks → D8 → flow acc → trace | `geospatial/catchment.py` |
| Runoff (Rational) | V = P × A × C | `geospatial/hydrology.py` |
| Pond sizing | Surface area = Volume / Depth | `geospatial/hydrology.py` |
| Suitability score | S = 0.3T + 0.3C + 0.2R + 0.2L | `geospatial/scoring.py` |

All weights and thresholds are in `app/config.py` — **no magic numbers in business logic**.

---

## Quick Start (Local, without Docker)

### Prerequisites
- Python 3.11+, Node 20+
- PostgreSQL + PostGIS (or use the Docker path below)

### Backend
```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # edit DATABASE_URL
python -m scripts.init_db     # create tables + seed demo village
uvicorn app.main:app --reload
```
API docs: http://localhost:8000/docs

### Frontend
```bash
cd frontend
npm install
npm run dev
```
App: http://localhost:5173

---

## Docker Compose (Recommended)

```bash
docker compose up --build
```

Services:
- `db` — PostGIS on port 5432
- `backend` — FastAPI on port 8000
- `frontend` — React/nginx on port 5173

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness probe |
| GET | `/villages/` | Search/list villages |
| GET | `/villages/{id}` | Village boundary + metadata |
| GET | `/villages/{id}/elevation` | DEM stats + contour GeoJSON |
| GET | `/villages/{id}/candidate-sites` | Top-5 candidate pond sites |
| POST | `/analysis/catchment` | Catchment polygon (D8 method) |
| GET | `/analysis/rainfall` | Historical rainfall stats |
| POST | `/analysis/runoff` | Runoff volume (Rational Method) |
| POST | `/analysis/recommendation` | Pond sizing (depth + storage) |
| POST | `/analysis/full` | Full orchestration (chain of all above) |

All geometry is returned as **GeoJSON** — never raw raster objects.

---

## Testing

```bash
cd backend
pytest tests/ -v
```

Covers: runoff formula, unit conversions, pond sizing, scoring, slope computation, contour GeoJSON structure.

---

## Evaluation Rubric Mapping

| Criterion | Marks | Where covered |
|---|---|---|
| System functionality | 35 | All 10 API endpoints + full orchestration |
| Terrain & catchment analysis | 20 | `geospatial/terrain.py`, `geospatial/catchment.py` |
| Frontend & visualization | 5 | React + Leaflet with layer controls |
| Software design & code quality | 15 | FastAPI orchestration, `geospatial/` module separation |
| System design & management | 15 | Docker, caching, error handling, schedule |
| Documentation & report | 10 | This README + FastAPI auto-docs + algorithm docstrings |

---

## Assumptions & Limitations

- Land-status classification is heuristic (no live Bhunaksha integration) — labeled as an *indication*, not legal verification.
- Pond sizing is a preliminary Rational Method estimate; evaporation, infiltration, seepage, and sedimentation are excluded.
- DEM from Open Elevation (~90 m resolution) — adequate for village-scale planning, not for engineering-grade surveys.
- Suitability scoring is a transparent weighted model, not a trained ML model — weights are configurable and explainable.
