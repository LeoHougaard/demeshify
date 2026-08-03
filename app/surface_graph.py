from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from shapely.geometry import Point, Polygon

from .mesh import MeshData


@dataclass(slots=True)
class PlanarPatch:
    """One maximal connected planar surface and its local sketch frame."""

    patch_id: str
    face_indices: np.ndarray
    origin: np.ndarray
    normal: np.ndarray
    x_direction: np.ndarray
    y_direction: np.ndarray
    area: float
    boundary_loops: list[np.ndarray]
    polygon: Polygon | None

    def project(self, point: np.ndarray) -> np.ndarray:
        relative = np.asarray(point, dtype=float) - self.origin
        return np.asarray(
            [relative @ self.x_direction, relative @ self.y_direction],
            dtype=float,
        )

    def contains_projected(self, point: np.ndarray, tolerance: float) -> bool:
        return bool(
            self.polygon is None
            or self.polygon.buffer(tolerance).covers(Point(self.project(point)))
        )


@dataclass(slots=True)
class SurfaceGraph:
    planar_patches: list[PlanarPatch]


def _plane_basis(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    reference = (
        np.asarray([1.0, 0.0, 0.0])
        if abs(float(normal[0])) < 0.8
        else np.asarray([0.0, 1.0, 0.0])
    )
    x_direction = np.cross(normal, reference)
    x_direction /= np.linalg.norm(x_direction)
    return x_direction, np.cross(normal, x_direction)


def _boundary_loops(mesh: object, face_indices: np.ndarray) -> list[np.ndarray]:
    faces = np.asarray(mesh.faces[face_indices], dtype=np.int64)
    counts: dict[tuple[int, int], int] = {}
    directed: dict[tuple[int, int], tuple[int, int]] = {}
    for face in faces:
        for first, second in zip(face, np.roll(face, -1), strict=True):
            key = tuple(sorted((int(first), int(second))))
            counts[key] = counts.get(key, 0) + 1
            directed[key] = (int(first), int(second))
    edges = [directed[key] for key, count in counts.items() if count == 1]
    adjacency: dict[int, list[int]] = {}
    for first, second in edges:
        adjacency.setdefault(first, []).append(second)
        adjacency.setdefault(second, []).append(first)
    remaining = {tuple(sorted(edge)) for edge in edges}
    loops: list[np.ndarray] = []
    while remaining:
        first_edge = next(iter(remaining))
        start, current = first_edge
        vertices = [start, current]
        remaining.remove(first_edge)
        previous = start
        while current != start:
            options = [
                neighbor
                for neighbor in adjacency.get(current, [])
                if neighbor != previous
                and tuple(sorted((current, neighbor))) in remaining
            ]
            if not options:
                break
            following = options[0]
            remaining.remove(tuple(sorted((current, following))))
            vertices.append(following)
            previous, current = current, following
        if len(vertices) >= 4 and vertices[-1] == vertices[0]:
            loops.append(np.asarray(mesh.vertices[vertices], dtype=float))
    return loops


def detect_surface_graph(data: MeshData) -> SurfaceGraph:
    """Region-grow arbitrary planar mesh faces into reusable sketch supports."""

    mesh = data.mesh
    if not hasattr(mesh, "face_adjacency"):
        # Some programmatic callers supply measured analytic features without
        # retaining the source triangles.  Surface references are optional in
        # that compatibility path; geometry recovery must still proceed.
        return SurfaceGraph(planar_patches=[])
    adjacency = np.asarray(mesh.face_adjacency, dtype=np.int64)
    if len(adjacency) == 0:
        return SurfaceGraph(planar_patches=[])
    normals = np.asarray(mesh.face_normals, dtype=float)
    centers = np.asarray(mesh.triangles_center, dtype=float)
    angular_tolerance = math.radians(1.0)
    cosine_tolerance = math.cos(angular_tolerance)
    distance_tolerance = max(data.diagonal * 0.00008, 0.001)
    first = adjacency[:, 0]
    second = adjacency[:, 1]
    normal_match = np.einsum("ij,ij->i", normals[first], normals[second])
    center_delta = centers[second] - centers[first]
    plane_distance = np.abs(np.einsum("ij,ij->i", center_delta, normals[first]))
    same_plane = (normal_match >= cosine_tolerance) & (
        plane_distance <= distance_tolerance
    )
    edges = adjacency[same_plane]
    rows = np.concatenate((edges[:, 0], edges[:, 1]))
    columns = np.concatenate((edges[:, 1], edges[:, 0]))
    graph = coo_matrix(
        (np.ones(len(rows), dtype=np.uint8), (rows, columns)),
        shape=(len(mesh.faces), len(mesh.faces)),
    )
    component_count, labels = connected_components(graph, directed=False)
    minimum_area = max(float(mesh.area) * 0.0002, data.diagonal**2 * 1e-7)
    raw: list[tuple[float, np.ndarray, np.ndarray, np.ndarray]] = []
    for label in range(component_count):
        face_indices = np.flatnonzero(labels == label)
        area = float(np.sum(mesh.area_faces[face_indices]))
        if area < minimum_area:
            continue
        weights = np.asarray(mesh.area_faces[face_indices], dtype=float)
        normal = np.average(normals[face_indices], axis=0, weights=weights)
        normal /= max(float(np.linalg.norm(normal)), 1e-12)
        residual = np.abs((centers[face_indices] - centers[face_indices[0]]) @ normal)
        if float(np.percentile(residual, 95)) > distance_tolerance:
            continue
        origin = np.average(centers[face_indices], axis=0, weights=weights)
        raw.append((area, face_indices, origin, normal))

    patches: list[PlanarPatch] = []
    for index, (area, face_indices, origin, normal) in enumerate(
        sorted(raw, key=lambda item: -item[0]),
        start=1,
    ):
        x_direction, y_direction = _plane_basis(normal)
        loops_3d = _boundary_loops(mesh, face_indices)
        loops_2d = [
            np.column_stack(
                (
                    (loop - origin) @ x_direction,
                    (loop - origin) @ y_direction,
                )
            )
            for loop in loops_3d
        ]
        polygons = [Polygon(loop) for loop in loops_2d if len(loop) >= 4]
        polygons = [polygon.buffer(0) for polygon in polygons if polygon.area > 0]
        outer_index = (
            max(range(len(polygons)), key=lambda item: polygons[item].area)
            if polygons
            else None
        )
        polygon = polygons[outer_index] if outer_index is not None else None
        if polygon is not None and outer_index is not None:
            for candidate_index, candidate in enumerate(polygons):
                # `polygon` is replaced after every difference, so object
                # identity cannot reliably identify the original outer loop.
                if candidate_index == outer_index:
                    continue
                if polygon.contains(candidate.representative_point()):
                    polygon = polygon.difference(candidate)
            if not isinstance(polygon, Polygon):
                polygon = max(polygon.geoms, key=lambda item: item.area)
        patches.append(
            PlanarPatch(
                patch_id=f"plane-{index:03d}",
                face_indices=face_indices,
                origin=origin,
                normal=normal,
                x_direction=x_direction,
                y_direction=y_direction,
                area=area,
                boundary_loops=loops_2d,
                polygon=polygon,
            )
        )
    return SurfaceGraph(planar_patches=patches)


def cylinder_support_patches(
    graph: SurfaceGraph,
    origin: np.ndarray,
    direction: np.ndarray,
    depth: float,
    tolerance: float,
    radius: float = 0.0,
) -> tuple[str | None, str | None]:
    """Find planar entry/termination references for an arbitrary cylinder."""

    direction = np.asarray(direction, dtype=float)
    direction /= max(float(np.linalg.norm(direction)), 1e-12)
    # A fitted cylindrical wall starts at the most extreme rim vertex.  On an
    # oblique entry face that is not the point where the cylinder axis crosses
    # the plane, so allow one radius of axial overhang at either end.  The axis
    # also passes through the opening (outside the planar patch polygon); the
    # surrounding face is a valid support when its boundary is within a radius.
    endpoint_margin = max(tolerance, radius * 1.5)
    matches: list[tuple[float, PlanarPatch, np.ndarray]] = []
    for patch in graph.planar_patches:
        denominator = float(patch.normal @ direction)
        if abs(denominator) <= 0.08:
            continue
        distance = float(patch.normal @ (patch.origin - origin) / denominator)
        point = origin + direction * distance
        projected = Point(patch.project(point))
        touches_patch = bool(
            patch.polygon is None
            or patch.polygon.buffer(tolerance).covers(projected)
            or patch.polygon.distance(projected) <= radius + tolerance
        )
        if -endpoint_margin <= distance <= depth + endpoint_margin and touches_patch:
            matches.append((distance, patch, point))
    if not matches:
        return None, None
    support = min(matches, key=lambda item: abs(item[0]))
    termination = min(matches, key=lambda item: abs(item[0] - depth))
    return support[1].patch_id, termination[1].patch_id
