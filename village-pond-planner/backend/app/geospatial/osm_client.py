import logging
import asyncio
import requests
import xml.etree.ElementTree as ET

from app.config import settings

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "VillagePondPlanner/1.0 (academic research project)",
    "Accept": "application/xml",
}


def _fetch_waterways_sync(min_lat: float, max_lat: float, min_lon: float, max_lon: float) -> list[list[dict]]:
    """
    Synchronous fetch of OSM waterways via the official OpenStreetMap API using requests.
    The official API returns XML, so we manually parse the XML tree to extract ways tagged
    as waterways and look up their corresponding node coordinates.
    """
    url = f"https://api.openstreetmap.org/api/0.6/map?bbox={min_lon},{min_lat},{max_lon},{max_lat}"
    
    waterways = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        
        # Parse XML
        root = ET.fromstring(resp.content)
        
        # 1. Build a lookup of all nodes in the bounding box
        nodes = {}
        for node in root.findall('node'):
            node_id = node.get('id')
            lat = node.get('lat')
            lon = node.get('lon')
            if node_id and lat and lon:
                nodes[node_id] = {'lat': float(lat), 'lon': float(lon)}
                
        # 2. Find all ways tagged as waterways
        for way in root.findall('way'):
            is_waterway = False
            for tag in way.findall('tag'):
                if tag.get('k') == 'waterway':
                    # Accept any waterway type (river, stream, canal, drain, ditch, etc.)
                    is_waterway = True
                    break
            
            if is_waterway:
                # 3. Extract the sequence of node references for this way
                coords = []
                for nd in way.findall('nd'):
                    ref = nd.get('ref')
                    if ref in nodes:
                        coords.append(nodes[ref])
                
                if coords:
                    waterways.append(coords)

        logger.info("Official OSM API: fetched %d waterway(s) in bounding box.", len(waterways))

    except Exception as e:
        logger.warning("Official OSM API request failed: %s. Proceeding without OSM rivers.", e)

    return waterways


async def fetch_waterways(min_lat: float, max_lat: float, min_lon: float, max_lon: float) -> list[list[dict]]:
    """
    Async wrapper around the synchronous fetch so it can be called from FastAPI
    and from asyncio.run() in scripts without blocking the event loop.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_waterways_sync, min_lat, max_lat, min_lon, max_lon)
