"""
Centralized hydrology engine using a consistent custom NumPy implementation.

This ensures that candidate selection and catchment delineation use exactly
the same DEM processing, D8 flow direction, and flow accumulation networks.
"""

from __future__ import annotations
import logging
import numpy as np
from collections import deque

logger = logging.getLogger(__name__)

class HydrologyEngine:
    def __init__(self, lats: np.ndarray, lons: np.ndarray, elevs: np.ndarray):
        self.lats = lats
        self.lons = lons
        self.elevs = elevs
        self.nrows, self.ncols = elevs.shape
        
        self.filled = None
        self.flow_to = None
        self.accum = None
        
    def process(self) -> None:
        """Run the full D8 flow routing sequence."""
        if self.accum is not None:
            return  # Already processed
            
        logger.info("Initializing consistent custom NumPy hydrological engine")
        self._fill_depressions()
        self._compute_flow_direction()
        self._compute_flow_accumulation()

    def _fill_depressions(self) -> None:
        """Fill pits / depressions to resolve micro-sinks."""
        self.filled = self.elevs.copy().astype(float)
        for _ in range(40):
            changed = False
            for i in range(1, self.nrows - 1):
                for j in range(1, self.ncols - 1):
                    min_nb = min(
                        self.filled[i - 1, j - 1], self.filled[i - 1, j], self.filled[i - 1, j + 1],
                        self.filled[i,     j - 1],                        self.filled[i,     j + 1],
                        self.filled[i + 1, j - 1], self.filled[i + 1, j], self.filled[i + 1, j + 1],
                    )
                    if self.filled[i, j] < min_nb:
                        self.filled[i, j] = min_nb + 1e-4
                        changed = True
            if not changed:
                break

    def _compute_flow_direction(self) -> None:
        """Compute D8 flow direction pointers using geographical distances."""
        lat_mean = float(self.lats.mean())
        dy = float(self.lats[1] - self.lats[0]) * 111_320.0 if self.nrows > 1 else 1.0
        dx = (float(self.lons[1] - self.lons[0]) * 111_320.0 * np.cos(np.radians(lat_mean))
              if self.ncols > 1 else 1.0)

        neighbours = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]
        dist = {
            (-1,-1): np.hypot(dy, dx), (-1,0): dy, (-1,1): np.hypot(dy, dx),
            (0,-1): dx,                            (0,1): dx,
            (1,-1): np.hypot(dy, dx),  (1,0): dy,  (1,1): np.hypot(dy, dx),
        }

        self.flow_to = np.full((self.nrows, self.ncols, 2), -1, dtype=int)
        for i in range(self.nrows):
            for j in range(self.ncols):
                best_slope = 0.0
                best_nb = None
                for di, dj in neighbours:
                    ni, nj = i + di, j + dj
                    if 0 <= ni < self.nrows and 0 <= nj < self.ncols:
                        dz = float(self.filled[i, j]) - float(self.filled[ni, nj])
                        slope = dz / dist[(di, dj)]
                        if slope > best_slope:
                            best_slope = slope
                            best_nb = (ni, nj)
                if best_nb is not None:
                    self.flow_to[i, j] = best_nb

    def _compute_flow_accumulation(self) -> None:
        """Compute D8 flow accumulation using highest-to-lowest topological sort."""
        self.accum = np.ones((self.nrows, self.ncols), dtype=int)
        order = np.argsort(-self.filled.ravel())
        for idx in order:
            i, j = divmod(int(idx), self.ncols)
            ri, rj = self.flow_to[i, j]
            if ri >= 0:
                self.accum[ri, rj] += self.accum[i, j]

    def get_upstream_contributors(self) -> dict[tuple[int, int], list[tuple[int, int]]]:
        """Build a graph of upstream contributing cells for BFS tracing."""
        if self.flow_to is None:
            self.process()
            
        contributors: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for i in range(self.nrows):
            for j in range(self.ncols):
                ri, rj = self.flow_to[i, j]
                if ri >= 0:
                    key = (int(ri), int(rj))
                    contributors.setdefault(key, []).append((i, j))
        return contributors

    def trace_upstream(self, start_i: int, start_j: int) -> set[tuple[int, int]]:
        """Trace all upstream cells starting from an outlet cell using BFS."""
        contributors = self.get_upstream_contributors()
        catchment_cells: set[tuple[int, int]] = set()
        queue: deque = deque([(start_i, start_j)])
        
        while queue:
            ci, cj = queue.popleft()
            catchment_cells.add((ci, cj))
            for upstream in contributors.get((ci, cj), []):
                if upstream not in catchment_cells:
                    queue.append(upstream)
                    
        return catchment_cells
