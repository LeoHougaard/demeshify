"""NumPy/SciPy subset of point-cloud-utils for the Pyodide worker."""

import numpy as np
from scipy.spatial import cKDTree

_PAIR_BUDGET = 32768


def _xyz(values):
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 3 or not np.isfinite(values).all():
        raise ValueError("Expected finite XYZ rows")
    return values


def _triangles(vertices, faces):
    vertices = _xyz(vertices)
    faces = np.asarray(faces)
    if (
        faces.ndim != 2
        or faces.shape[1] != 3
        or faces.dtype.kind not in "iu"
        or (faces.size and (faces.min() < 0 or faces.max() >= len(vertices)))
    ):
        raise ValueError("Invalid triangle indices")
    return vertices[faces]


def _dot(a, b):
    return np.einsum("...i,...i->...", a, b)


def _ratio(a, b):
    return np.divide(a, b, out=np.zeros_like(a), where=b != 0)


def _project(points, triangles):
    """Test plane interiors and all edge segments, including collapsed edges."""
    a, b, c = triangles.transpose(1, 0, 2)
    ab, ac, ap = b - a, c - a, points - a
    normal = np.cross(ab, ac)
    area2 = _dot(normal, normal)
    u = _ratio(_dot(np.cross(ap, ac), normal), area2)
    v = _ratio(_dot(np.cross(ab, ap), normal), area2)
    weights = np.column_stack((1 - u - v, u, v))
    delta = ap - u[:, None] * ab - v[:, None] * ac
    best = np.where((area2 > 0) & (weights.min(axis=1) >= 0), _dot(delta, delta), np.inf)
    for start, end in ((0, 1), (1, 2), (2, 0)):
        edge = triangles[:, end] - triangles[:, start]
        offset = points - triangles[:, start]
        t = np.clip(_ratio(_dot(offset, edge), _dot(edge, edge)), 0, 1)
        delta = offset - t[:, None] * edge
        distance = _dot(delta, delta)
        better = distance < best
        weights[better] = 0
        weights[better, start] = 1 - t[better]
        weights[better, end] = t[better]
        best[better] = distance[better]
    return np.sqrt(best), weights


def closest_points_on_mesh(p, v, f):
    points = _xyz(p)
    triangles = _triangles(v, f)
    if not len(triangles):
        raise ValueError("Closest point requires a nonempty mesh")
    centers = triangles.mean(axis=1)
    radii = np.maximum.reduce([np.linalg.norm(triangles[:, k] - centers, axis=1) for k in range(3)])
    # Seed each query with a real triangle. A face capable of improving
    # its distance has its center within best_distance + face_radius.
    tree = cKDTree(centers)
    faces = tree.query(points)[1]
    distances = np.empty(len(points))
    bary = np.empty_like(points)
    for start in range(0, len(points), _PAIR_BUDGET):
        rows = slice(start, start + _PAIR_BUDGET)
        distances[rows], bary[rows] = _project(points[rows], triangles[faces[rows]])

    # Separate scales so one large face cannot inflate every search sphere.
    exponents = np.frexp(radii)[1]
    order = np.argsort(exponents, kind="stable")
    groups = np.split(order, np.flatnonzero(np.diff(exponents[order])) + 1)
    for group in groups:
        tree = cKDTree(centers[group])
        radius = radii[group].max()
        for start in range(0, len(points), 1024):
            stop = min(start + 1024, len(points))
            pblock = points[start:stop]
            bounds = (distances[start:stop] + radius) * (1 + 8 * np.finfo(float).eps)
            counts = tree.query_ball_point(pblock, bounds, return_length=True)
            totals = np.r_[0, counts.cumsum()]
            i = 0
            while i < len(counts):
                # A single probe may overlap every face. Its index list
                # costs O(faces), but projection temporaries stay bounded.
                j = max(i + 1, totals.searchsorted(totals[i] + _PAIR_BUDGET, side="right") - 1)
                if totals[j] > totals[i]:
                    hits = tree.query_ball_point(pblock[i:j], bounds[i:j], return_sorted=False)
                    ids = group[np.concatenate(hits).astype(np.intp)]
                    rows = np.repeat(np.arange(start + i, start + j), counts[i:j])
                    for k in range(0, len(ids), _PAIR_BUDGET):
                        cand = ids[k : k + _PAIR_BUDGET]
                        q = rows[k : k + _PAIR_BUDGET]
                        d, w = _project(points[q], triangles[cand])
                        # Reduce repeated query indices before assigning.
                        first = np.r_[0, np.flatnonzero(np.diff(q)) + 1]
                        minimum = np.minimum.reduceat(d, first)
                        winning = np.flatnonzero(
                            d == np.repeat(minimum, np.diff(np.r_[first, len(q)]))
                        )
                        winning = winning[np.r_[True, np.diff(q[winning]) != 0]]
                        win = winning[d[winning] < distances[q[winning]]]
                        distances[q[win]], faces[q[win]], bary[q[win]] = d[win], cand[win], w[win]
                i = j
    return distances, faces, bary


def triangle_soup_fast_winding_number(v, f, p):
    """Signed solid-angle sum in bounded tiles (exact, quadratic worst case)."""
    triangles = _triangles(v, f)
    points = _xyz(p)
    result = np.zeros(len(points))
    for i in range(0, len(points), 32):
        probes = points[i : i + 32, None, :]
        width = _PAIR_BUDGET // len(probes)
        for j in range(0, len(triangles), width):
            tile = triangles[j : j + width]
            a, b, c = (tile[:, k] - probes for k in range(3))
            la, lb, lc = (np.linalg.norm(x, axis=2) for x in (a, b, c))
            numerator = _dot(a, np.cross(b, c))
            denominator = la * lb * lc + _dot(a, b) * lc + _dot(b, c) * la + _dot(c, a) * lb
            result[i : i + len(probes)] += np.arctan2(numerator, denominator).sum(axis=1)
    return result / (2 * np.pi)


def decimate_triangle_mesh(v, f, max_faces):
    raise NotImplementedError("Mesh decimation is unavailable in the browser")
