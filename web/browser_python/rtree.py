"""Read-only subset of rtree used by trimesh in Pyodide."""

from types import SimpleNamespace

import numpy as np
from scipy.spatial import cKDTree


class Property:
    def __init__(self, dimension=2):
        self.dimension = dimension


class Index:
    """Bulk-loaded hierarchy of bounding boxes, packed in spatial order."""

    def __init__(self, stream, properties=None):
        self.dimension = properties.dimension if properties is not None else 2
        ids, boxes = [], []
        for identifier, bounds, _ in stream:
            ids.append(identifier)
            boxes.append(bounds)
        bounds = np.asarray(boxes, dtype=np.float64)
        if bounds.shape != (len(ids), 2 * self.dimension):
            raise ValueError("expected a nonempty stream of min/max bounds")
        lo, hi = np.hsplit(bounds, 2)
        # KD partitioning puts nearby centers together. All pruning uses the
        # actual box extents, including boxes that span multiple partitions.
        fanout = 16
        order = cKDTree(lo * 0.5 + hi * 0.5, leafsize=fanout).indices
        self._ids = np.asarray(ids, dtype=np.int64)[order]
        self._offsets = np.arange(fanout)
        lo, hi = lo[order], hi[order]
        self._levels = [(lo, hi)]
        while len(lo) > fanout:
            starts = np.arange(0, len(lo), fanout)
            lo = np.minimum.reduceat(lo, starts, axis=0)
            hi = np.maximum.reduceat(hi, starts, axis=0)
            self._levels.append((lo, hi))
        self.bounds = np.r_[lo.min(axis=0), hi.max(axis=0)].tolist()

    def intersection(self, coordinates):
        query = np.asarray(coordinates, dtype=np.float64)
        low = query[:self.dimension]
        high = low if query.size == self.dimension else query[self.dimension:]
        rows = np.arange(len(self._levels[-1][0]))
        for depth in range(len(self._levels) - 1, -1, -1):
            lo, hi = self._levels[depth]
            rows = rows[((lo[rows] <= high) & (hi[rows] >= low)).all(axis=1)]
            if not rows.size:
                return iter(())
            if depth:
                rows = (rows[:, None] * len(self._offsets) + self._offsets).ravel()
                rows = rows[rows < len(self._levels[depth - 1][0])]
        return iter(self._ids[rows].tolist())


index = SimpleNamespace(Property=Property, Index=Index)
