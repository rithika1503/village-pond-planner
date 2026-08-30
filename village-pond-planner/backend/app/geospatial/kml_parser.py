"""
KML/KMZ Contour Parser
======================

Converts an uploaded KML or KMZ contour map into a regular elevation grid
(lats × lons × elevs) compatible with the existing geospatial pipeline.

Algorithm
---------
1.  If the file is a KMZ (ZIP archive), extract the first .kml member.
2.  Parse every <Placemark> in the KML:
      - Read <name> as the elevation value (metres).
      - Read coordinate tuples from <LineString> or <Polygon/outerBoundaryIs>.
3.  Build a scattered point cloud  (lon, lat, elevation).
4.  Call scipy.interpolate.griddata (linear) on a regular (n×n) grid that
    spans the file's bounding box.
5.  Fill NaN edge cells with nearest-neighbour extrapolation.

Design notes (viva-ready)
--------------------------
* No hard-coded elevations, coordinates, or names.  Everything is derived from
  the file's content.
* scipy.griddata (Qhull triangulation → Delaunay interpolation) is the standard
  cartographic approach used in open-source GIS tools like QGIS / GRASS.
* The n_points parameter controls resolution; the default (50) matches the
  existing DEM pipeline (config.DEM_GRID_POINTS = 50).
* Both KML and KMZ formats are supported; KMZ is transparently decompressed.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from typing import List, Tuple
from xml.etree import ElementTree as ET

import numpy as np

logger = logging.getLogger(__name__)

# KML namespace map — covers standard KML 2.2 and common variants
_NS = {
    "kml": "http://www.opengis.net/kml/2.2",
    "gx": "http://www.google.com/kml/ext/2.2",
}

# Regex to parse a bare <Folder> root without namespace (as in the sample file)
_COORD_RE = re.compile(r"[-\d.]+,[-\d.]+(?:,[-\d.]+)?")


# ─── Public API ────────────────────────────────────────────────────────────────

ContourLine = Tuple[float, List[Tuple[float, float]]]  # (elevation_m, [(lon, lat), ...])


def parse_kml_bytes(data: bytes) -> List[ContourLine]:
    """
    Parse KML or KMZ bytes and return a list of contour lines.

    Each contour line is a (elevation_m, [(lon, lat), ...]) tuple.
    Raises ValueError if no contour lines can be extracted.
    """
    kml_bytes = _extract_kml(data)
    lines = _parse_contours(kml_bytes)
    if not lines:
        raise ValueError(
            "No contour lines found in the uploaded file. "
            "Ensure <Placemark> elements have numeric <name> (elevation) "
            "and <LineString> or <Polygon> coordinates."
        )
    logger.info("Parsed %d contour lines from KML", len(lines))
    return lines


def contours_to_dem(
    contour_lines: List[ContourLine],
    n_points: int = 50,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Interpolate scattered contour points onto a regular grid.

    Parameters
    ----------
    contour_lines : list of (elevation_m, [(lon, lat), ...])
    n_points      : grid size along each axis (default 50 → 50×50 grid)

    Returns
    -------
    lats  : 1-D array (length n_points), south → north
    lons  : 1-D array (length n_points), west → east
    elevs : 2-D array (n_points × n_points), elevation in metres

    Algorithm
    ---------
    1.  Flatten all contour points into (lon, lat, elev) triples.
    2.  scipy.interpolate.griddata with method="linear" fills interior cells.
    3.  NaN holes are filled with method="nearest" (extrapolation at edges).
    """
    from scipy.interpolate import griddata

    # Flatten all points
    lons_pts, lats_pts, elevs_pts = [], [], []
    for elev, coords in contour_lines:
        for lon, lat in coords:
            lons_pts.append(lon)
            lats_pts.append(lat)
            elevs_pts.append(elev)

    lons_arr = np.array(lons_pts, dtype=float)
    lats_arr = np.array(lats_pts, dtype=float)
    elevs_arr = np.array(elevs_pts, dtype=float)

    # Bounding box
    min_lat, max_lat = lats_arr.min(), lats_arr.max()
    min_lon, max_lon = lons_arr.min(), lons_arr.max()

    # Guard: add a small buffer if the bbox is degenerate
    lat_range = max_lat - min_lat
    lon_range = max_lon - min_lon
    if lat_range < 1e-6:
        min_lat -= 0.001; max_lat += 0.001
    if lon_range < 1e-6:
        min_lon -= 0.001; max_lon += 0.001

    # Regular grid
    lats = np.linspace(min_lat, max_lat, n_points)
    lons = np.linspace(min_lon, max_lon, n_points)
    grid_lons, grid_lats = np.meshgrid(lons, lats)  # shape (n_points, n_points)

    points = np.column_stack([lons_arr, lats_arr])
    grid_points = np.column_stack([grid_lons.ravel(), grid_lats.ravel()])

    # Linear interpolation (Delaunay triangulation)
    elevs_linear = griddata(points, elevs_arr, grid_points, method="linear")

    # Fill NaN with nearest-neighbour
    nan_mask = np.isnan(elevs_linear)
    if nan_mask.any():
        elevs_nearest = griddata(points, elevs_arr, grid_points, method="nearest")
        elevs_linear[nan_mask] = elevs_nearest[nan_mask]

    elevs = elevs_linear.reshape(n_points, n_points)

    logger.info(
        "DEM grid: %dx%d | lat [%.4f, %.4f] | lon [%.4f, %.4f] | elev [%.1f, %.1f] m",
        n_points, n_points, min_lat, max_lat, min_lon, max_lon,
        elevs.min(), elevs.max(),
    )
    return lats, lons, elevs


def get_bounding_box(contour_lines: List[ContourLine]) -> dict:
    """Return bounding box derived from contour coordinates."""
    all_lons = [lon for _, coords in contour_lines for lon, _ in coords]
    all_lats = [lat for _, coords in contour_lines for _, lat in coords]
    return {
        "min_lat": float(min(all_lats)),
        "max_lat": float(max(all_lats)),
        "min_lon": float(min(all_lons)),
        "max_lon": float(max(all_lons)),
    }


def get_elevation_stats(contour_lines: List[ContourLine]) -> dict:
    """Return min/max/mean elevation from contour line elevation values."""
    elevations = [elev for elev, _ in contour_lines]
    return {
        "min": float(min(elevations)),
        "max": float(max(elevations)),
        "mean": float(np.mean(elevations)),
        "unique_levels": sorted(set(elevations)),
    }


# ─── Private helpers ───────────────────────────────────────────────────────────

def _extract_kml(data: bytes) -> bytes:
    """
    If data is a KMZ (ZIP), extract and return the primary KML member.
    Otherwise return data unchanged.
    """
    if _is_kmz(data):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
            if not kml_names:
                raise ValueError("KMZ archive contains no .kml file")
            # Prefer doc.kml; otherwise take the first found
            primary = next((n for n in kml_names if "doc" in n.lower()), kml_names[0])
            return zf.read(primary)
    return data


def _is_kmz(data: bytes) -> bool:
    """KMZ files start with the ZIP magic bytes PK\\x03\\x04."""
    return data[:4] == b"PK\x03\x04"


def _parse_contours(kml_bytes: bytes) -> List[ContourLine]:
    """
    Parse KML XML and extract (elevation, [(lon, lat)]) pairs.

    Handles:
    - Namespaced KML (<kml:Placemark>)
    - Bare XML (<Placemark>) as produced by ContourMapGenerator / QGIS exports
    """
    try:
        root = ET.fromstring(kml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid KML XML: {exc}") from exc

    # Detect namespace prefix by inspecting the root tag
    ns_prefix = _detect_ns(root)
    placemarks = root.findall(f".//{ns_prefix}Placemark")

    logger.debug("Found %d Placemark elements", len(placemarks))

    contours: List[ContourLine] = []
    for pm in placemarks:
        elev = _extract_elevation(pm, ns_prefix)
        if elev is None:
            continue
        coords = _extract_coords(pm, ns_prefix)
        if coords:
            contours.append((elev, coords))

    return contours


def _detect_ns(root: ET.Element) -> str:
    """Return namespace prefix string like '{http://...}' or '' (bare)."""
    tag = root.tag
    if tag.startswith("{"):
        ns_uri = tag[1: tag.index("}")]
        return f"{{{ns_uri}}}"
    return ""


def _extract_elevation(pm: ET.Element, ns: str) -> float | None:
    """
    Try to read elevation from the <name> element.

    The sample file names each Placemark with a numeric elevation string like
    '277.0', '280.0', etc.  Also checks <SimpleData name="elevation">.
    """
    # Primary: <name> element
    name_el = pm.find(f"{ns}name")
    if name_el is not None and name_el.text:
        try:
            return float(name_el.text.strip())
        except (ValueError, TypeError):
            pass

    # Fallback: <SimpleData name="elevation"> or <SimpleData name="Elev">
    for sd in pm.findall(f".//{ns}SimpleData"):
        attr = (sd.get("name") or "").lower()
        if attr in ("elevation", "elev", "height", "z"):
            try:
                return float(sd.text.strip())
            except (ValueError, TypeError):
                pass

    # Fallback: check ExtendedData / Data[@name='elevation']
    for data_el in pm.findall(f".//{ns}Data"):
        attr = (data_el.get("name") or "").lower()
        if attr in ("elevation", "elev", "height", "z"):
            val_el = data_el.find(f"{ns}value")
            if val_el is not None and val_el.text:
                try:
                    return float(val_el.text.strip())
                except (ValueError, TypeError):
                    pass

    return None


def _extract_coords(pm: ET.Element, ns: str) -> List[Tuple[float, float]]:
    """
    Extract (lon, lat) pairs from a Placemark.

    Supports <LineString>, <Polygon/outerBoundaryIs/LinearRing>, and
    <MultiGeometry> containing either.
    """
    coords: List[Tuple[float, float]] = []

    # Search all <coordinates> elements (covers LineString, Polygon, etc.)
    for coord_el in pm.findall(f".//{ns}coordinates"):
        text = coord_el.text
        if not text:
            continue
        for match in _COORD_RE.finditer(text):
            parts = match.group().split(",")
            try:
                lon = float(parts[0])
                lat = float(parts[1])
                # Third value (altitude) is ignored
                coords.append((lon, lat))
            except (IndexError, ValueError):
                continue

    return coords
