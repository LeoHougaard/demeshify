from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Literal, TypeAlias

import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import coo_matrix, csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra, minimum_spanning_tree
from scipy.spatial import cKDTree
from shapely.geometry import Point, Polygon

from .mesh import MeshData

SurfaceKind: TypeAlias = Literal[
    "plane",
    "cylinder",
    "cone",
    "sphere",
    "torus",
    "extrusion",
    "revolution",
    "freeform",
]


@dataclass(slots=True)
class PlanarPatch:
    """One maximal connected planar surface and its local sketch frame."""

    patch_id: str
    face_indices: np.ndarray
    vertex_indices: np.ndarray
    origin: np.ndarray
    normal: np.ndarray
    x_direction: np.ndarray
    y_direction: np.ndarray
    area: float
    boundary_loops: list[np.ndarray]
    polygon: Polygon | None
    boundary_loops_3d: list[np.ndarray] = field(default_factory=list)
    rms_error: float = 0.0
    max_error: float = 0.0
    normal_error_degrees: float = 0.0
    kind: Literal["plane"] = field(default="plane", init=False)

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
class CylindricalPatch:
    """A connected portion of an infinite analytic cylinder."""

    patch_id: str
    face_indices: np.ndarray
    vertex_indices: np.ndarray
    origin: np.ndarray
    axis: np.ndarray
    radius: float
    start: float
    end: float
    area: float
    boundary_loops: list[np.ndarray]
    rms_error: float
    max_error: float
    normal_error_degrees: float
    kind: Literal["cylinder"] = field(default="cylinder", init=False)


@dataclass(slots=True)
class ConicalPatch:
    """A connected portion of an analytic cone or conical frustum."""

    patch_id: str
    face_indices: np.ndarray
    vertex_indices: np.ndarray
    apex: np.ndarray
    axis: np.ndarray
    semi_angle: float
    start: float
    end: float
    area: float
    boundary_loops: list[np.ndarray]
    rms_error: float
    max_error: float
    normal_error_degrees: float
    kind: Literal["cone"] = field(default="cone", init=False)


@dataclass(slots=True)
class SphericalPatch:
    """A connected portion of an analytic sphere."""

    patch_id: str
    face_indices: np.ndarray
    vertex_indices: np.ndarray
    center: np.ndarray
    radius: float
    area: float
    boundary_loops: list[np.ndarray]
    rms_error: float
    max_error: float
    normal_error_degrees: float
    kind: Literal["sphere"] = field(default="sphere", init=False)


@dataclass(slots=True)
class ToroidalPatch:
    """A connected portion of an analytic torus."""

    patch_id: str
    face_indices: np.ndarray
    vertex_indices: np.ndarray
    center: np.ndarray
    axis: np.ndarray
    major_radius: float
    minor_radius: float
    area: float
    boundary_loops: list[np.ndarray]
    rms_error: float
    max_error: float
    normal_error_degrees: float
    kind: Literal["torus"] = field(default="torus", init=False)


@dataclass(slots=True)
class LinearExtrusionPatch:
    """A B-spline/profile curve translated along one constant direction."""

    patch_id: str
    face_indices: np.ndarray
    vertex_indices: np.ndarray
    direction: np.ndarray
    profile_points: np.ndarray
    start: float
    end: float
    area: float
    boundary_loops: list[np.ndarray]
    rms_error: float
    max_error: float
    normal_error_degrees: float
    kind: Literal["extrusion"] = field(default="extrusion", init=False)


@dataclass(slots=True)
class SurfaceOfRevolutionPatch:
    """A general B-spline/profile curve revolved around a fixed axis."""

    patch_id: str
    face_indices: np.ndarray
    vertex_indices: np.ndarray
    origin: np.ndarray
    axis: np.ndarray
    radial_direction: np.ndarray
    profile_points: np.ndarray
    area: float
    boundary_loops: list[np.ndarray]
    rms_error: float
    max_error: float
    normal_error_degrees: float
    kind: Literal["revolution"] = field(default="revolution", init=False)


@dataclass(slots=True)
class FreeformPatch:
    """A connected residual surface that is not one elementary analytic type.

    These faces are deliberately retained instead of being forced into a bad
    primitive.  A later B-spline/NURBS fitting stage can parameterize them.
    """

    patch_id: str
    face_indices: np.ndarray
    vertex_indices: np.ndarray
    area: float
    boundary_loops: list[np.ndarray]
    kind: Literal["freeform"] = field(default="freeform", init=False)


SurfacePatch: TypeAlias = (
    PlanarPatch
    | CylindricalPatch
    | ConicalPatch
    | SphericalPatch
    | ToroidalPatch
    | LinearExtrusionPatch
    | SurfaceOfRevolutionPatch
    | FreeformPatch
)


@dataclass(slots=True)
class SurfaceAdjacency:
    """Shared mesh boundary between two recognized surface patches."""

    first_patch_id: str
    second_patch_id: str
    boundary_curves: list[np.ndarray]


@dataclass(slots=True)
class SurfaceGraph:
    planar_patches: list[PlanarPatch] = field(default_factory=list)
    cylindrical_patches: list[CylindricalPatch] = field(default_factory=list)
    conical_patches: list[ConicalPatch] = field(default_factory=list)
    spherical_patches: list[SphericalPatch] = field(default_factory=list)
    toroidal_patches: list[ToroidalPatch] = field(default_factory=list)
    extrusion_patches: list[LinearExtrusionPatch] = field(default_factory=list)
    revolution_patches: list[SurfaceOfRevolutionPatch] = field(default_factory=list)
    freeform_patches: list[FreeformPatch] = field(default_factory=list)
    adjacency: list[SurfaceAdjacency] = field(default_factory=list)
    face_patch_ids: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=object)
    )

    @property
    def patches(self) -> list[SurfacePatch]:
        return [
            *self.planar_patches,
            *self.cylindrical_patches,
            *self.conical_patches,
            *self.spherical_patches,
            *self.toroidal_patches,
            *self.extrusion_patches,
            *self.revolution_patches,
            *self.freeform_patches,
        ]

    def patch(self, patch_id: str) -> SurfacePatch | None:
        return next(
            (patch for patch in self.patches if patch.patch_id == patch_id),
            None,
        )


@dataclass(slots=True)
class _AnalyticFit:
    kind: Literal["cylinder", "cone", "sphere", "torus"]
    parameters: dict[str, np.ndarray | float]
    rms_error: float
    max_error: float
    normal_error_degrees: float
    score: float


def _canonical_direction(direction: np.ndarray) -> np.ndarray:
    result = np.asarray(direction, dtype=float)
    result /= max(float(np.linalg.norm(result)), 1e-15)
    dominant = int(np.argmax(np.abs(result)))
    if result[dominant] < 0:
        result = -result
    return result


def _plane_basis(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    reference = (
        np.asarray([1.0, 0.0, 0.0])
        if abs(float(normal[0])) < 0.8
        else np.asarray([0.0, 1.0, 0.0])
    )
    x_direction = np.cross(normal, reference)
    x_direction /= np.linalg.norm(x_direction)
    return x_direction, np.cross(normal, x_direction)


def _edge_chains(edges: np.ndarray) -> list[np.ndarray]:
    """Turn unordered vertex-index edges into maximal open or closed chains."""

    if len(edges) == 0:
        return []
    adjacency: dict[int, list[int]] = {}
    remaining: set[tuple[int, int]] = set()
    for raw_first, raw_second in np.asarray(edges, dtype=np.int64):
        first, second = int(raw_first), int(raw_second)
        if first == second:
            continue
        key = tuple(sorted((first, second)))
        remaining.add(key)
        adjacency.setdefault(first, []).append(second)
        adjacency.setdefault(second, []).append(first)

    chains: list[np.ndarray] = []
    while remaining:
        degree_one = next(
            (
                vertex
                for edge in remaining
                for vertex in edge
                if sum(
                    tuple(sorted((vertex, neighbor))) in remaining
                    for neighbor in adjacency.get(vertex, [])
                )
                == 1
            ),
            None,
        )
        start = degree_one if degree_one is not None else next(iter(remaining))[0]
        chain = [start]
        previous: int | None = None
        current = start
        while True:
            choices = [
                neighbor
                for neighbor in adjacency.get(current, [])
                if neighbor != previous
                and tuple(sorted((current, neighbor))) in remaining
            ]
            if not choices:
                break
            following = choices[0]
            remaining.remove(tuple(sorted((current, following))))
            chain.append(following)
            previous, current = current, following
            if current == start:
                break
        if len(chain) >= 2:
            chains.append(np.asarray(chain, dtype=np.int64))
    simple_chains: list[np.ndarray] = []
    pending = chains
    while pending:
        chain = pending.pop()
        positions: dict[int, int] = {}
        split: tuple[int, int] | None = None
        for index, raw_vertex in enumerate(chain):
            vertex = int(raw_vertex)
            previous = positions.get(vertex)
            if previous is not None and not (
                previous == 0 and index == len(chain) - 1
            ):
                split = (previous, index)
                break
            positions[vertex] = index
        if split is None:
            simple_chains.append(chain)
            continue
        first, second = split
        cycle = chain[first : second + 1]
        remainder = np.r_[chain[: first + 1], chain[second + 1 :]]
        if len(cycle) >= 3:
            pending.append(cycle)
        if len(remainder) >= 2:
            pending.append(remainder)
    return simple_chains


def _boundary_loops(mesh: object, face_indices: np.ndarray) -> list[np.ndarray]:
    faces = np.asarray(mesh.faces[face_indices], dtype=np.int64)
    if len(faces) == 0:
        return []
    all_edges = np.sort(
        np.vstack((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]])),
        axis=1,
    )
    unique_edges, counts = np.unique(all_edges, axis=0, return_counts=True)
    boundary_edges = unique_edges[counts == 1]
    return [
        np.asarray(mesh.vertices[chain], dtype=float)
        for chain in _edge_chains(boundary_edges)
    ]


def _longest_cyclic_run(mask: np.ndarray) -> np.ndarray:
    """Return indices of the longest true run in a cyclic boundary mask."""

    mask = np.asarray(mask, dtype=bool)
    if not np.any(mask):
        return np.empty(0, dtype=np.int64)
    doubled = np.r_[mask, mask]
    best_start = 0
    best_length = 0
    run_start: int | None = None
    for index, selected in enumerate(doubled):
        if selected and run_start is None:
            run_start = index
        if run_start is None:
            continue
        if selected and index != len(doubled) - 1:
            continue
        run_end = index + 1 if selected else index
        run_length = min(run_end - run_start, len(mask))
        if run_start < len(mask) and run_length > best_length:
            best_start, best_length = run_start, run_length
        run_start = None
    return np.arange(best_start, best_start + best_length, dtype=np.int64) % len(mask)


def _point_to_polyline_distances(
    points: np.ndarray,
    polyline: np.ndarray,
) -> np.ndarray:
    """Return nearest-segment distances without assuming equal sampling."""

    distances = np.full(len(points), math.inf, dtype=float)
    for start, end in zip(polyline[:-1], polyline[1:], strict=False):
        vector = end - start
        length_squared = float(vector @ vector)
        if length_squared <= 1e-20:
            continue
        parameters = np.clip(((points - start) @ vector) / length_squared, 0.0, 1.0)
        projected = start + parameters[:, None] * vector
        distances = np.minimum(distances, np.linalg.norm(points - projected, axis=1))
    return distances


def _ordered_cloud_path(
    coordinates: np.ndarray,
    distance_tolerance: float,
) -> np.ndarray | None:
    """Order a dense 2-D sampling of one non-branching curve."""

    coordinates = np.asarray(coordinates, dtype=float)
    local_scale = float(np.linalg.norm(np.ptp(coordinates, axis=0)))
    cell = max(distance_tolerance * 0.4, local_scale * 1e-7, 1e-9)
    keys = np.round(coordinates / cell).astype(np.int64)
    _, unique_indices = np.unique(keys, axis=0, return_index=True)
    unique = coordinates[np.sort(unique_indices)]
    if len(unique) < 4:
        return None

    neighbor_count = min(9, len(unique))
    neighbor_distances, neighbor_indices = cKDTree(unique).query(
        unique,
        k=neighbor_count,
    )
    rows: list[int] = []
    columns: list[int] = []
    weights: list[float] = []
    for source in range(len(unique)):
        for distance, target in zip(
            np.atleast_1d(neighbor_distances[source])[1:],
            np.atleast_1d(neighbor_indices[source])[1:],
            strict=False,
        ):
            if not math.isfinite(float(distance)) or int(target) == source:
                continue
            rows.extend((source, int(target)))
            columns.extend((int(target), source))
            weights.extend((float(distance), float(distance)))
    graph = csr_matrix(
        (weights, (rows, columns)),
        shape=(len(unique), len(unique)),
    )
    tree = minimum_spanning_tree(graph)
    tree = tree + tree.T
    from_seed = np.asarray(dijkstra(tree, indices=0), dtype=float)
    if not np.any(np.isfinite(from_seed)):
        return None
    first_end = int(np.argmax(np.where(np.isfinite(from_seed), from_seed, -1.0)))
    from_end, predecessors = dijkstra(
        tree,
        indices=first_end,
        return_predecessors=True,
    )
    from_end = np.asarray(from_end, dtype=float)
    second_end = int(np.argmax(np.where(np.isfinite(from_end), from_end, -1.0)))
    path = [second_end]
    while path[-1] != first_end:
        predecessor = int(predecessors[path[-1]])
        if predecessor < 0:
            return None
        path.append(predecessor)
    return unique[np.asarray(path[::-1], dtype=np.int64)]


def _profile_from_reduced_cloud(
    reduced: np.ndarray,
    direction: np.ndarray,
    start: float,
    distance_tolerance: float,
) -> np.ndarray | None:
    """Order a trimmed extrusion profile from all collapsed surface nodes.

    Boolean intersections can remove both constant-parameter end curves from
    an extrusion. Collapsing every node along the sweep direction still leaves
    a dense one-dimensional cloud. The weighted diameter of its k-nearest-
    neighbor minimum spanning tree recovers the profile order; the caller then
    rejects shortcuts or branches with an explicit node-to-polyline residual.
    """

    first, second = _plane_basis(direction)
    coordinates = np.column_stack((reduced @ first, reduced @ second))
    ordered = _ordered_cloud_path(coordinates, distance_tolerance)
    if ordered is None:
        return None
    profile = (
        ordered[:, 0, None] * first
        + ordered[:, 1, None] * second
        + start * direction
    )
    return profile if len(profile) >= 4 else None


def _fit_linear_extrusion_patch(
    mesh: object,
    face_indices: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
    index: int,
) -> LinearExtrusionPatch | None:
    """Recognize a general profile translated along a constant direction."""

    normals = np.asarray(mesh.face_normals[face_indices], dtype=float)
    # Three quadrilateral strips (six triangles) are the smallest profile run
    # that supplies two consistent local circumcircles. Shorter runs remain
    # underdetermined and are left in their original representation.
    if len(normals) < 6:
        return None
    _, singular_values, directions = np.linalg.svd(normals, full_matrices=False)
    if singular_values[1] <= 1e-12:
        return None
    direction = _canonical_direction(directions[-1])
    normal_alignment = np.abs(normals @ direction)
    normal_errors = np.degrees(np.arcsin(np.clip(normal_alignment, 0.0, 1.0)))
    normal_error = float(np.percentile(normal_errors, 95))
    if normal_error > normal_tolerance_degrees:
        return None

    vertex_indices = np.unique(mesh.faces[face_indices])
    points = np.asarray(mesh.vertices[vertex_indices], dtype=float)
    axial = points @ direction
    start, end = float(np.min(axial)), float(np.max(axial))
    span = end - start
    if span <= distance_tolerance * 5:
        return None
    loops = _boundary_loops(mesh, face_indices)
    if not loops:
        return None

    threshold = max(distance_tolerance * 3, span * 0.002)
    profile_points: np.ndarray | None = None
    for loop in sorted(loops, key=len, reverse=True):
        loop_axial = loop @ direction
        indices = _longest_cyclic_run(loop_axial <= start + threshold)
        if len(indices) >= 4:
            profile_points = np.asarray(loop[indices], dtype=float)
            break

    # Every surface node should reduce to the same transverse profile after
    # removing its displacement along the extrusion direction.
    reduced = points - np.outer(axial - start, direction)
    if profile_points is None:
        profile_points = _profile_from_reduced_cloud(
            reduced,
            direction,
            start,
            distance_tolerance,
        )
    if profile_points is None:
        return None
    errors = _point_to_polyline_distances(reduced, profile_points)
    rms_error, max_error = _fit_statistics(errors)
    if rms_error > distance_tolerance * 2 or max_error > distance_tolerance * 5:
        cloud_profile = _profile_from_reduced_cloud(
            reduced,
            direction,
            start,
            distance_tolerance,
        )
        if cloud_profile is not None:
            cloud_errors = _point_to_polyline_distances(reduced, cloud_profile)
            cloud_rms, cloud_max = _fit_statistics(cloud_errors)
            if (cloud_rms, cloud_max) < (rms_error, max_error):
                profile_points = cloud_profile
                rms_error, max_error = cloud_rms, cloud_max
    if rms_error > distance_tolerance * 2 or max_error > distance_tolerance * 5:
        return None
    return LinearExtrusionPatch(
        patch_id=f"extrusion-{index:03d}",
        face_indices=np.asarray(face_indices, dtype=np.int64),
        vertex_indices=vertex_indices,
        direction=direction,
        profile_points=profile_points,
        start=start,
        end=end,
        area=float(np.sum(mesh.area_faces[face_indices])),
        boundary_loops=loops,
        rms_error=rms_error,
        max_error=max_error,
        normal_error_degrees=normal_error,
    )


def _fit_surface_of_revolution_patch(
    mesh: object,
    face_indices: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
    index: int,
) -> SurfaceOfRevolutionPatch | None:
    """Fit ``profile(v)`` rotated about one line from nodes and normals."""

    if len(face_indices) < 16:
        return None
    vertex_indices = np.unique(mesh.faces[face_indices])
    points = np.asarray(mesh.vertices[vertex_indices], dtype=float)
    sample_indices = np.linspace(
        0,
        len(face_indices) - 1,
        min(1200, len(face_indices)),
        dtype=int,
    )
    centers = np.asarray(mesh.triangles_center[face_indices[sample_indices]], dtype=float)
    normals = np.asarray(mesh.face_normals[face_indices[sample_indices]], dtype=float)
    center = np.mean(centers, axis=0)
    scale = float(np.linalg.norm(np.ptp(centers, axis=0)))
    if scale <= distance_tolerance * 5:
        return None

    candidates: list[np.ndarray] = []
    for values in (centers - center, normals):
        _, _, directions = np.linalg.svd(values, full_matrices=False)
        candidates.extend(directions)
    best: tuple[float, np.ndarray, np.ndarray, np.ndarray] | None = None
    lower = np.r_[[-1.5] * 3, center - scale * 2]
    upper = np.r_[[1.5] * 3, center + scale * 2]
    for raw_direction in candidates:
        initial_axis = _canonical_direction(raw_direction)
        plane_normals = np.cross(normals, np.broadcast_to(initial_axis, normals.shape))
        matrix = np.vstack((plane_normals, initial_axis))
        target = np.r_[
            np.einsum("ij,ij->i", plane_normals, centers),
            float(initial_axis @ center),
        ]
        initial_origin = np.linalg.lstsq(matrix, target, rcond=None)[0]
        initial_origin = np.clip(
            initial_origin,
            center - scale * 2,
            center + scale * 2,
        )

        def objective(parameters: np.ndarray) -> np.ndarray:
            raw_axis = parameters[:3]
            raw_length = max(float(np.linalg.norm(raw_axis)), 1e-12)
            axis = raw_axis / raw_length
            origin = parameters[3:]
            # An axis origin is invariant to translation along the axis. Pin
            # that null degree of freedom at the patch center for stable fits.
            origin = origin - axis * float((origin - center) @ axis)
            tangents = np.cross(np.broadcast_to(axis, centers.shape), centers - origin)
            radii = np.linalg.norm(tangents, axis=1)
            residuals = np.einsum("ij,ij->i", normals, tangents)
            residuals /= np.maximum(radii, scale * 1e-5)
            return np.r_[residuals, (raw_length - 1.0) * 0.1]

        try:
            fitted = least_squares(
                objective,
                np.r_[initial_axis, initial_origin],
                bounds=(lower, upper),
                max_nfev=300,
                loss="soft_l1",
                f_scale=max(math.sin(math.radians(normal_tolerance_degrees)) * 0.3, 0.01),
            )
        except Exception:
            continue
        raw_axis = fitted.x[:3]
        axis = _canonical_direction(raw_axis)
        origin = np.asarray(fitted.x[3:], dtype=float)
        origin -= axis * float((origin - center) @ axis)
        residuals = np.abs(objective(fitted.x)[:-1])
        normal_error = float(
            np.degrees(np.arcsin(np.clip(np.percentile(residuals, 95), 0.0, 1.0)))
        )
        if best is None or normal_error < best[0]:
            best = (normal_error, axis, origin, residuals)
    if best is None or best[0] > normal_tolerance_degrees:
        return None
    normal_error, axis, origin, _ = best

    relative = points - origin
    axial = relative @ axis
    radial_vectors = relative - np.outer(axial, axis)
    radial = np.linalg.norm(radial_vectors, axis=1)
    profile_coordinates = np.column_stack((axial, radial))
    ordered = _ordered_cloud_path(profile_coordinates, distance_tolerance)
    if ordered is None:
        return None
    profile_errors = _point_to_polyline_distances(profile_coordinates, ordered)
    rms_error, max_error = _fit_statistics(profile_errors)
    if rms_error > distance_tolerance * 2 or max_error > distance_tolerance * 5:
        return None
    radial_direction, _ = _plane_basis(axis)
    profile_points = (
        origin
        + ordered[:, 0, None] * axis
        + ordered[:, 1, None] * radial_direction
    )
    return SurfaceOfRevolutionPatch(
        patch_id=f"revolution-{index:03d}",
        face_indices=np.asarray(face_indices, dtype=np.int64),
        vertex_indices=vertex_indices,
        origin=origin,
        axis=axis,
        radial_direction=radial_direction,
        profile_points=profile_points,
        area=float(np.sum(mesh.area_faces[face_indices])),
        boundary_loops=_boundary_loops(mesh, face_indices),
        rms_error=rms_error,
        max_error=max_error,
        normal_error_degrees=normal_error,
    )


def _circular_extrusion_axis(
    patch: LinearExtrusionPatch,
    distance_tolerance: float,
) -> tuple[np.ndarray, np.ndarray, float] | None:
    """Recover the axis line when an extrusion profile is a circular arc."""

    points = np.asarray(patch.profile_points, dtype=float)
    if len(points) < 6:
        return None
    first, second = _plane_basis(patch.direction)
    x = points @ first
    y = points @ second
    matrix = np.column_stack((2 * x, 2 * y, np.ones(len(points))))
    center_x, center_y, constant = np.linalg.lstsq(
        matrix,
        x**2 + y**2,
        rcond=None,
    )[0]
    radius_squared = float(constant + center_x**2 + center_y**2)
    if radius_squared <= distance_tolerance**2:
        return None
    radius = math.sqrt(radius_squared)
    errors = np.abs(np.hypot(x - center_x, y - center_y) - radius)
    if float(np.max(errors)) > distance_tolerance * 3:
        return None
    origin = center_x * first + center_y * second
    return _canonical_direction(patch.direction), origin, radius


def _validated_seeded_cylinder_fit(
    mesh: object,
    face_indices: np.ndarray,
    axis: np.ndarray,
    origin: np.ndarray,
    radius: float,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
) -> _AnalyticFit | None:
    """Validate a known circular profile without requiring broad angular support.

    A short CAD arc may contain too little normal variation for the generic
    cylinder least-squares seed. Its profile still determines an exact circle.
    Accept that stronger seed only when every mesh node and face normal agrees
    with the resulting cylinder.
    """

    indices = np.asarray(face_indices, dtype=np.int64)
    if len(indices) < 6:
        return None
    axis = _canonical_direction(np.asarray(axis, dtype=float))
    origin = np.asarray(origin, dtype=float)
    vertex_indices = np.unique(mesh.faces[indices])
    if len(vertex_indices) < 8 or radius <= distance_tolerance:
        return None
    points = np.asarray(mesh.vertices[vertex_indices], dtype=float)
    relative = points - origin
    axial = relative @ axis
    radial = relative - np.outer(axial, axis)
    errors = np.abs(np.linalg.norm(radial, axis=1) - radius)
    rms_error, max_error = _fit_statistics(errors)
    if rms_error > distance_tolerance or max_error > distance_tolerance * 3:
        return None

    centers = np.asarray(mesh.triangles_center[indices], dtype=float)
    center_relative = centers - origin
    center_axial = center_relative @ axis
    expected = center_relative - np.outer(center_axial, axis)
    lengths = np.linalg.norm(expected, axis=1)
    if np.any(lengths <= 1e-12):
        return None
    expected /= lengths[:, None]
    normals = np.asarray(mesh.face_normals[indices], dtype=float)
    normal_error = _normal_error_degrees(normals, expected)
    if normal_error > normal_tolerance_degrees:
        return None
    start, end = float(np.min(axial)), float(np.max(axial))
    if end - start <= distance_tolerance * 2:
        return None
    axis_origin = origin + start * axis
    return _AnalyticFit(
        kind="cylinder",
        parameters={
            "origin": axis_origin,
            "axis": axis,
            "radius": float(radius),
            "start": 0.0,
            "end": end - start,
        },
        rms_error=rms_error,
        max_error=max_error,
        normal_error_degrees=normal_error,
        score=_fit_score(
            rms_error,
            max_error,
            normal_error,
            distance_tolerance,
            normal_tolerance_degrees,
            0.02,
        ),
    )


def _circular_extrusion_cylinder(
    patch: LinearExtrusionPatch,
    mesh: object,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
    index: int,
) -> CylindricalPatch | None:
    """Promote an entirely circular linear extrusion to an elementary cylinder."""

    candidate = _circular_extrusion_axis(patch, distance_tolerance)
    if candidate is None:
        return None
    axis, origin, radius = candidate
    fit = _validated_seeded_cylinder_fit(
        mesh,
        patch.face_indices,
        axis,
        origin,
        radius,
        distance_tolerance,
        normal_tolerance_degrees,
    )
    if fit is None:
        return None
    cylinder = _make_analytic_patch(mesh, fit, patch.face_indices, index)
    return cylinder if isinstance(cylinder, CylindricalPatch) else None


def _constant_radius_revolution_cylinder(
    patch: SurfaceOfRevolutionPatch,
    mesh: object,
    distance_tolerance: float,
    index: int,
) -> CylindricalPatch | None:
    """Promote a straight constant-radius revolution profile to a cylinder."""

    profile = np.asarray(patch.profile_points, dtype=float)
    relative = profile - patch.origin
    axial = relative @ patch.axis
    radial = np.linalg.norm(relative - np.outer(axial, patch.axis), axis=1)
    radius = float(np.median(radial))
    if (
        len(profile) < 4
        or radius <= distance_tolerance
        or float(np.max(np.abs(radial - radius))) > distance_tolerance * 3
    ):
        return None
    points = np.asarray(mesh.vertices[patch.vertex_indices], dtype=float)
    point_relative = points - patch.origin
    point_axial = point_relative @ patch.axis
    point_radial = np.linalg.norm(
        point_relative - np.outer(point_axial, patch.axis),
        axis=1,
    )
    errors = np.abs(point_radial - radius)
    rms_error, max_error = _fit_statistics(errors)
    if max_error > distance_tolerance * 3:
        return None
    return CylindricalPatch(
        patch_id=f"cylinder-{index:03d}",
        face_indices=patch.face_indices,
        vertex_indices=patch.vertex_indices,
        origin=patch.origin,
        axis=patch.axis,
        radius=radius,
        start=float(np.min(point_axial)),
        end=float(np.max(point_axial)),
        area=patch.area,
        boundary_loops=patch.boundary_loops,
        rms_error=rms_error,
        max_error=max_error,
        normal_error_degrees=patch.normal_error_degrees,
    )


def _split_tangent_swept_surfaces(
    data: MeshData,
    freeform_patches: list[FreeformPatch],
    extrusion_patches: list[LinearExtrusionPatch],
    cylindrical_patches: list[CylindricalPatch],
    distance_tolerance: float,
    normal_tolerance_degrees: float,
    minimum_area: float,
) -> tuple[
    list[FreeformPatch],
    list[CylindricalPatch],
    list[SurfaceOfRevolutionPatch],
]:
    """Separate tangent coaxial cylinders and revolved profiles.

    STL connectivity erases CAD face boundaries when two faces are tangent.
    Circular swept patches elsewhere on the same body provide a reliable axis
    hypothesis. Constant-radius regions are extracted against that axis using
    node residuals, after which disconnected general profiles can each be fit
    as one surface of revolution.
    """

    mesh = data.mesh
    axis_candidates: list[tuple[np.ndarray, np.ndarray, float]] = []
    for patch in cylindrical_patches:
        axis_candidates.append((patch.axis, patch.origin, patch.radius))
    for patch in extrusion_patches:
        candidate = _circular_extrusion_axis(patch, distance_tolerance)
        if candidate is not None:
            axis_candidates.append(candidate)
    if not axis_candidates:
        return freeform_patches, [], []

    residuals: list[FreeformPatch] = []
    cylinders: list[CylindricalPatch] = []
    revolutions: list[SurfaceOfRevolutionPatch] = []
    for original in freeform_patches:
        remaining = np.asarray(original.face_indices, dtype=np.int64)
        for _ in range(16):
            best_split: (
                tuple[float, np.ndarray, np.ndarray, np.ndarray, float] | None
            ) = None
            face_centers = np.asarray(mesh.triangles_center[remaining], dtype=float)
            normals = np.asarray(mesh.face_normals[remaining], dtype=float)
            for raw_axis, origin, _ in axis_candidates:
                axis = _canonical_direction(raw_axis)
                relative = face_centers - origin
                axial = relative @ axis
                radial_vectors = relative - np.outer(axial, axis)
                radial_distances = np.linalg.norm(radial_vectors, axis=1)
                expected = radial_vectors / np.maximum(
                    radial_distances[:, None],
                    1e-15,
                )
                angles = np.degrees(
                    np.arccos(
                        np.clip(
                            np.abs(np.einsum("ij,ij->i", normals, expected)),
                            -1.0,
                            1.0,
                        )
                    )
                )
                seed_indices = np.flatnonzero(
                    angles < max(0.5, normal_tolerance_degrees * 0.1)
                )
                if len(seed_indices) < 8:
                    continue
                ordered = seed_indices[np.argsort(radial_distances[seed_indices])]
                ordered_radii = radial_distances[ordered]
                breaks = np.flatnonzero(
                    np.diff(ordered_radii) > distance_tolerance * 4
                ) + 1
                for cluster in np.split(ordered, breaks):
                    if len(cluster) < 8:
                        continue
                    seed_faces = remaining[cluster]
                    seed_vertices = np.unique(mesh.faces[seed_faces])
                    seed_points = np.asarray(mesh.vertices[seed_vertices], dtype=float)
                    seed_relative = seed_points - origin
                    seed_axial = seed_relative @ axis
                    seed_radial = seed_relative - np.outer(seed_axial, axis)
                    radius = float(np.median(np.linalg.norm(seed_radial, axis=1)))

                    face_points = np.asarray(
                        mesh.vertices[mesh.faces[remaining]],
                        dtype=float,
                    )
                    face_relative = face_points - origin
                    face_axial = np.einsum("fvi,i->fv", face_relative, axis)
                    face_radial = face_relative - face_axial[:, :, None] * axis
                    vertex_errors = np.max(
                        np.abs(np.linalg.norm(face_radial, axis=2) - radius),
                        axis=1,
                    )
                    inlier_faces = remaining[
                        (vertex_errors <= distance_tolerance * 1.5)
                        & (angles <= normal_tolerance_degrees)
                    ]
                    available = np.zeros(len(mesh.faces), dtype=bool)
                    available[inlier_faces] = True
                    for component in _smooth_components(mesh, available, 30.0):
                        if len(component) < 8:
                            continue
                        area = float(np.sum(mesh.area_faces[component]))
                        if area < minimum_area:
                            continue
                        if best_split is None or area > best_split[0]:
                            best_split = (area, component, axis, origin, radius)
            if best_split is None:
                break
            _, cylinder_faces, axis, origin, radius = best_split
            cylinder_set = set(int(value) for value in cylinder_faces)
            remaining = np.asarray(
                [value for value in remaining if int(value) not in cylinder_set],
                dtype=np.int64,
            )
            cylinder_vertices = np.unique(mesh.faces[cylinder_faces])
            cylinder_points = np.asarray(mesh.vertices[cylinder_vertices], dtype=float)
            relative = cylinder_points - origin
            axial = relative @ axis
            radial = relative - np.outer(axial, axis)
            fitted_radius = float(np.median(np.linalg.norm(radial, axis=1)))
            errors = np.abs(np.linalg.norm(radial, axis=1) - fitted_radius)
            centers = np.asarray(mesh.triangles_center[cylinder_faces], dtype=float)
            measured = np.asarray(mesh.face_normals[cylinder_faces], dtype=float)
            center_relative = centers - origin
            center_axial = center_relative @ axis
            expected = center_relative - np.outer(center_axial, axis)
            expected /= np.maximum(np.linalg.norm(expected, axis=1)[:, None], 1e-15)
            cylinders.append(
                CylindricalPatch(
                    patch_id=f"cylinder-{len(cylindrical_patches) + len(cylinders) + 1:03d}",
                    face_indices=cylinder_faces,
                    vertex_indices=cylinder_vertices,
                    origin=np.asarray(origin, dtype=float),
                    axis=axis,
                    radius=fitted_radius,
                    start=float(np.min(axial)),
                    end=float(np.max(axial)),
                    area=float(np.sum(mesh.area_faces[cylinder_faces])),
                    boundary_loops=_boundary_loops(mesh, cylinder_faces),
                    rms_error=float(np.sqrt(np.mean(errors**2))),
                    max_error=float(np.max(errors)),
                    normal_error_degrees=_normal_error_degrees(measured, expected),
                )
            )
            if len(remaining) < 8:
                break

        available = np.zeros(len(mesh.faces), dtype=bool)
        available[remaining] = True
        components = _smooth_components(mesh, available, 30.0)
        for component in components:
            component_area = float(np.sum(mesh.area_faces[component]))
            extrusion = (
                _fit_linear_extrusion_patch(
                    mesh,
                    component,
                    distance_tolerance,
                    normal_tolerance_degrees,
                    len(extrusion_patches) + 1,
                )
                if component_area >= minimum_area
                else None
            )
            if extrusion is not None:
                extrusion_patches.append(extrusion)
                continue
            revolution = _fit_surface_of_revolution_patch(
                mesh,
                component,
                distance_tolerance,
                normal_tolerance_degrees,
                len(revolutions) + 1,
            )
            if revolution is not None:
                cylinder = _constant_radius_revolution_cylinder(
                    revolution,
                    mesh,
                    distance_tolerance,
                    len(cylindrical_patches) + len(cylinders) + 1,
                )
                if cylinder is not None:
                    cylinders.append(cylinder)
                else:
                    revolutions.append(revolution)
            else:
                # Keep a smooth tangent chain intact here.  Normal-span charts
                # turn one swept surface into many artificial fragments before
                # the robust analytic peelers have a chance to recover its full
                # support (a torus necessarily spans every normal direction).
                for chart in [component]:
                    residuals.append(
                        FreeformPatch(
                            patch_id="",
                            face_indices=chart,
                            vertex_indices=np.unique(mesh.faces[chart]),
                            area=float(np.sum(mesh.area_faces[chart])),
                            boundary_loops=_boundary_loops(mesh, chart),
                        )
                    )

    for index, patch in enumerate(residuals, start=1):
        patch.patch_id = f"freeform-{index:03d}"
    return residuals, cylinders, revolutions


def _normal_error_degrees(
    measured: np.ndarray,
    expected: np.ndarray,
) -> float:
    valid = np.all(np.isfinite(expected), axis=1)
    if not np.any(valid):
        return math.inf
    alignment = np.abs(
        np.einsum("ij,ij->i", measured[valid], expected[valid])
    )
    angles = np.degrees(np.arccos(np.clip(alignment, -1.0, 1.0)))
    return float(np.percentile(angles, 95))


def _fit_statistics(errors: np.ndarray) -> tuple[float, float]:
    errors = np.abs(np.asarray(errors, dtype=float))
    return float(np.sqrt(np.mean(errors**2))), float(np.max(errors))


def _fit_score(
    rms_error: float,
    max_error: float,
    normal_error: float,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
    complexity_penalty: float,
) -> float:
    return float(
        rms_error / distance_tolerance
        + 0.15 * max_error / distance_tolerance
        + 0.35 * normal_error / normal_tolerance_degrees
        + complexity_penalty
    )


def _fit_cylinder(
    points: np.ndarray,
    face_centers: np.ndarray,
    normals: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
) -> _AnalyticFit | None:
    _, singular_values, vectors = np.linalg.svd(normals, full_matrices=False)
    if (
        singular_values[0] <= 1e-12
        or singular_values[1] / singular_values[0] < 0.08
        or singular_values[2] / max(singular_values[1], 1e-12) > 0.08
    ):
        return None
    axis = _canonical_direction(vectors[-1])
    first, second = _plane_basis(axis)
    first_coordinates = points @ first
    second_coordinates = points @ second
    matrix = np.column_stack(
        (2 * first_coordinates, 2 * second_coordinates, np.ones(len(points)))
    )
    target = first_coordinates**2 + second_coordinates**2
    center_first, center_second, constant = np.linalg.lstsq(
        matrix,
        target,
        rcond=None,
    )[0]
    radius_squared = constant + center_first**2 + center_second**2
    if radius_squared <= distance_tolerance**2:
        return None
    radius = math.sqrt(float(radius_squared))
    radial_distances = np.hypot(
        first_coordinates - center_first,
        second_coordinates - center_second,
    )
    errors = radial_distances - radius
    rms_error, max_error = _fit_statistics(errors)

    center_perpendicular = center_first * first + center_second * second
    axial = points @ axis
    start, end = float(np.min(axial)), float(np.max(axial))
    if end - start <= distance_tolerance * 2:
        return None
    radial_vectors = (
        face_centers
        - np.outer(face_centers @ axis, axis)
        - center_perpendicular
    )
    radial_lengths = np.linalg.norm(radial_vectors, axis=1)
    expected = radial_vectors / np.maximum(radial_lengths[:, None], 1e-15)
    normal_error = _normal_error_degrees(normals, expected)
    if (
        rms_error > distance_tolerance
        or max_error > distance_tolerance * 3
        or normal_error > normal_tolerance_degrees
    ):
        return None
    origin = center_perpendicular + axis * start
    return _AnalyticFit(
        kind="cylinder",
        parameters={
            "origin": origin,
            "axis": axis,
            "radius": radius,
            "start": start,
            "end": end,
        },
        rms_error=rms_error,
        max_error=max_error,
        normal_error_degrees=normal_error,
        score=_fit_score(
            rms_error,
            max_error,
            normal_error,
            distance_tolerance,
            normal_tolerance_degrees,
            0.02,
        ),
    )


def _fit_sphere(
    points: np.ndarray,
    face_centers: np.ndarray,
    normals: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
) -> _AnalyticFit | None:
    centered_normals = normals - np.mean(normals, axis=0)
    singular_values = np.linalg.svd(centered_normals, compute_uv=False)
    if (
        singular_values[0] <= 1e-12
        or singular_values[1] / singular_values[0] < 0.08
        or singular_values[2] / max(singular_values[1], 1e-12) < 0.025
    ):
        return None
    matrix = np.column_stack((2 * points, np.ones(len(points))))
    target = np.einsum("ij,ij->i", points, points)
    solution = np.linalg.lstsq(matrix, target, rcond=None)[0]
    center = solution[:3]
    radius_squared = float(solution[3] + center @ center)
    if radius_squared <= distance_tolerance**2:
        return None
    radius = math.sqrt(radius_squared)
    distances = np.linalg.norm(points - center, axis=1)
    rms_error, max_error = _fit_statistics(distances - radius)
    radial = face_centers - center
    expected = radial / np.maximum(np.linalg.norm(radial, axis=1)[:, None], 1e-15)
    normal_error = _normal_error_degrees(normals, expected)
    if (
        rms_error > distance_tolerance
        or max_error > distance_tolerance * 3
        or normal_error > normal_tolerance_degrees
    ):
        return None
    return _AnalyticFit(
        kind="sphere",
        parameters={"center": center, "radius": radius},
        rms_error=rms_error,
        max_error=max_error,
        normal_error_degrees=normal_error,
        score=_fit_score(
            rms_error,
            max_error,
            normal_error,
            distance_tolerance,
            normal_tolerance_degrees,
            0.03,
        ),
    )


def _fit_partial_cone(
    points: np.ndarray,
    face_centers: np.ndarray,
    normals: np.ndarray,
    axis: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
    diagonal: float,
) -> _AnalyticFit | None:
    """Fit a narrow cone sector without letting it collapse to a cylinder.

    On a conical face, every surface normal has the same axial component:
    ``abs(dot(normal, axis)) == sin(semi_angle)``.  A narrow angular sector
    makes the normal covariance ill-conditioned, so an unconstrained point fit
    can move the apex far away and converge to a near-cylinder.  Deriving the
    angle from the normal field removes that ambiguity; the final all-node and
    normal checks remain the acceptance gate.
    """

    initial_axial_normal = abs(float(np.mean(normals @ axis)))
    maximum_axial_normal = math.sin(math.radians(87.0))
    if not 0.01 <= initial_axial_normal < maximum_axial_normal:
        return None
    initial_tangent = initial_axial_normal / math.sqrt(
        max(1.0 - initial_axial_normal**2, 1e-15)
    )
    first, second = _plane_basis(axis)
    u = points @ first
    v = points @ second
    z = points @ axis
    center_u, center_v = float(np.mean(u)), float(np.mean(v))
    radii = np.hypot(u - center_u, v - center_v)
    slope, intercept = np.polyfit(z, radii, 1)
    initial_apex_z = float(
        -intercept / slope
        if abs(float(slope)) >= 1e-3
        else np.mean(z) - np.mean(radii) / initial_tangent
    )
    sample_indices = np.linspace(
        0,
        len(points) - 1,
        min(len(points), 2000),
        dtype=int,
    )
    sample_u = u[sample_indices]
    sample_v = v[sample_indices]
    sample_z = z[sample_indices]
    sample_points = points[sample_indices]
    margin = max(diagonal, distance_tolerance * 20)

    def fit_with_bounds(
        radial_scale: float,
        axial_scale: float,
    ) -> _AnalyticFit | None:
        def fixed_axis_residual(parameters: np.ndarray) -> np.ndarray:
            candidate_u, candidate_v, apex_z = parameters
            radial = np.hypot(sample_u - candidate_u, sample_v - candidate_v)
            return radial - np.abs(sample_z - apex_z) * initial_tangent

        lower = np.asarray(
            [
                float(np.min(u) - radial_scale * margin),
                float(np.min(v) - radial_scale * margin),
                float(np.min(z) - axial_scale * margin),
            ]
        )
        upper = np.asarray(
            [
                float(np.max(u) + radial_scale * margin),
                float(np.max(v) + radial_scale * margin),
                float(np.max(z) + axial_scale * margin),
            ]
        )
        seeded = least_squares(
            fixed_axis_residual,
            np.clip(
                np.asarray([center_u, center_v, initial_apex_z]),
                lower + 1e-12,
                upper - 1e-12,
            ),
            bounds=(lower, upper),
            loss="soft_l1",
            f_scale=distance_tolerance,
            max_nfev=180,
        )
        if not seeded.success:
            return None
        fitted_u, fitted_v, fitted_apex_z = seeded.x
        seeded_apex = fitted_u * first + fitted_v * second + fitted_apex_z * axis
        world_margin = 5 * margin
        world_lower = np.r_[
            np.min(points, axis=0) - world_margin,
            [-1.0, -1.0, -1.0],
        ]
        world_upper = np.r_[
            np.max(points, axis=0) + world_margin,
            [1.0, 1.0, 1.0],
        ]

        def joint_residual(parameters: np.ndarray) -> np.ndarray:
            candidate_apex = parameters[:3]
            raw_axis = parameters[3:6]
            axis_length = max(float(np.linalg.norm(raw_axis)), 1e-15)
            candidate_axis = raw_axis / axis_length
            candidate_axial_normal = min(
                abs(float(np.mean(normals @ candidate_axis))),
                maximum_axial_normal,
            )
            candidate_tangent = candidate_axial_normal / math.sqrt(
                max(1.0 - candidate_axial_normal**2, 1e-15)
            )
            relative = sample_points - candidate_apex
            axial = relative @ candidate_axis
            radial = relative - np.outer(axial, candidate_axis)
            surface_error = (
                np.linalg.norm(radial, axis=1)
                - np.abs(axial) * candidate_tangent
            )
            axial_variation = normals @ candidate_axis
            axial_variation -= np.mean(axial_variation)
            return np.r_[
                surface_error,
                axial_variation * distance_tolerance * 2.0,
                (axis_length - 1.0) * distance_tolerance,
            ]

        joint = least_squares(
            joint_residual,
            np.clip(
                np.r_[seeded_apex, axis],
                world_lower + 1e-12,
                world_upper - 1e-12,
            ),
            bounds=(world_lower, world_upper),
            loss="soft_l1",
            f_scale=distance_tolerance,
            max_nfev=300,
        )
        if not joint.success:
            return None
        apex = np.asarray(joint.x[:3], dtype=float)
        fitted_axis = _canonical_direction(joint.x[3:6])
        axial_normal = min(
            abs(float(np.mean(normals @ fitted_axis))),
            maximum_axial_normal,
        )
        tangent = axial_normal / math.sqrt(
            max(1.0 - axial_normal**2, 1e-15)
        )
        semi_angle = math.atan(tangent)
        if not math.radians(0.5) <= semi_angle <= math.radians(87.0):
            return None
        relative = points - apex
        axial = relative @ fitted_axis
        radial = relative - np.outer(axial, fitted_axis)
        errors = np.linalg.norm(radial, axis=1) - np.abs(axial) * tangent
        rms_error, max_error = _fit_statistics(errors)
        center_relative = face_centers - apex
        center_axial = center_relative @ fitted_axis
        center_radial = center_relative - np.outer(center_axial, fitted_axis)
        radial_unit = center_radial / np.maximum(
            np.linalg.norm(center_radial, axis=1)[:, None],
            1e-15,
        )
        side = np.sign(center_axial)
        expected = radial_unit - side[:, None] * tangent * fitted_axis
        expected /= np.maximum(
            np.linalg.norm(expected, axis=1)[:, None],
            1e-15,
        )
        normal_error = _normal_error_degrees(normals, expected)
        if (
            rms_error > distance_tolerance
            or max_error > distance_tolerance * 3
            or normal_error > normal_tolerance_degrees
        ):
            return None
        return _AnalyticFit(
            kind="cone",
            parameters={
                "apex": apex,
                "axis": fitted_axis,
                "semi_angle": semi_angle,
                "start": float(np.min(axial)),
                "end": float(np.max(axial)),
            },
            rms_error=rms_error,
            max_error=max_error,
            normal_error_degrees=normal_error,
            score=_fit_score(
                rms_error,
                max_error,
                normal_error,
                distance_tolerance,
                normal_tolerance_degrees,
                0.04,
            ),
        )

    return fit_with_bounds(1.0, 5.0) or fit_with_bounds(5.0, 20.0)


def _fit_cone(
    points: np.ndarray,
    face_centers: np.ndarray,
    normals: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
    diagonal: float,
) -> _AnalyticFit | None:
    centered_normals = normals - np.mean(normals, axis=0)
    _, singular_values, vectors = np.linalg.svd(
        centered_normals,
        full_matrices=False,
    )
    if (
        singular_values[0] <= 1e-12
        or singular_values[1] / singular_values[0] < 0.04
    ):
        return None
    axis = _canonical_direction(vectors[-1])
    if singular_values[2] / max(singular_values[1], 1e-12) > 0.30:
        return _fit_partial_cone(
            points,
            face_centers,
            normals,
            axis,
            distance_tolerance,
            normal_tolerance_degrees,
            diagonal,
        )
    axial_normals = normals @ axis
    if abs(float(np.mean(axial_normals))) < 0.01:
        return None
    first, second = _plane_basis(axis)
    u = points @ first
    v = points @ second
    z = points @ axis
    center_u, center_v = float(np.mean(u)), float(np.mean(v))
    radii = np.hypot(u - center_u, v - center_v)
    slope, intercept = np.polyfit(z, radii, 1)
    if abs(float(slope)) < 0.005:
        return None
    initial_tangent = float(np.clip(abs(slope), 0.005, 20.0))
    initial_apex_z = float(-intercept / slope)

    sample_indices = np.linspace(
        0,
        len(points) - 1,
        min(len(points), 2000),
        dtype=int,
    )
    sample_u, sample_v, sample_z = u[sample_indices], v[sample_indices], z[sample_indices]

    def residual(parameters: np.ndarray) -> np.ndarray:
        candidate_u, candidate_v, apex_z, log_tangent = parameters
        tangent = math.exp(float(log_tangent))
        radial = np.hypot(sample_u - candidate_u, sample_v - candidate_v)
        return radial - np.abs(sample_z - apex_z) * tangent

    margin = max(diagonal, distance_tolerance * 20)
    result = least_squares(
        residual,
        np.asarray(
            [center_u, center_v, initial_apex_z, math.log(initial_tangent)]
        ),
        bounds=(
            np.asarray(
                [
                    float(np.min(u) - margin),
                    float(np.min(v) - margin),
                    float(np.min(z) - 5 * margin),
                    math.log(0.005),
                ]
            ),
            np.asarray(
                [
                    float(np.max(u) + margin),
                    float(np.max(v) + margin),
                    float(np.max(z) + 5 * margin),
                    math.log(20.0),
                ]
            ),
        ),
        loss="soft_l1",
        f_scale=distance_tolerance,
        max_nfev=160,
    )
    if not result.success:
        return None
    fitted_u, fitted_v, apex_z, log_tangent = result.x
    tangent = math.exp(float(log_tangent))
    semi_angle = math.atan(tangent)
    if not math.radians(0.5) <= semi_angle <= math.radians(87.0):
        return None
    radial = np.hypot(u - fitted_u, v - fitted_v)
    errors = radial - np.abs(z - apex_z) * tangent
    rms_error, max_error = _fit_statistics(errors)

    center_line = fitted_u * first + fitted_v * second
    center_radial = (
        face_centers
        - np.outer(face_centers @ axis, axis)
        - center_line
    )
    radial_lengths = np.linalg.norm(center_radial, axis=1)
    radial_unit = center_radial / np.maximum(radial_lengths[:, None], 1e-15)
    side = np.sign(face_centers @ axis - apex_z)
    expected = radial_unit - side[:, None] * tangent * axis
    expected /= np.maximum(np.linalg.norm(expected, axis=1)[:, None], 1e-15)
    normal_error = _normal_error_degrees(normals, expected)
    if (
        rms_error > distance_tolerance
        or max_error > distance_tolerance * 3
        or normal_error > normal_tolerance_degrees
    ):
        return None
    apex = center_line + axis * apex_z
    axial = (points - apex) @ axis
    return _AnalyticFit(
        kind="cone",
        parameters={
            "apex": apex,
            "axis": axis,
            "semi_angle": semi_angle,
            "start": float(np.min(axial)),
            "end": float(np.max(axial)),
        },
        rms_error=rms_error,
        max_error=max_error,
        normal_error_degrees=normal_error,
        score=_fit_score(
            rms_error,
            max_error,
            normal_error,
            distance_tolerance,
            normal_tolerance_degrees,
            0.04,
        ),
    )


def _torus_errors(
    points: np.ndarray,
    center: np.ndarray,
    axis: np.ndarray,
    major_radius: float,
    minor_radius: float,
) -> np.ndarray:
    relative = points - center
    axial = relative @ axis
    planar = relative - np.outer(axial, axis)
    radial = np.linalg.norm(planar, axis=1)
    return np.hypot(radial - major_radius, axial) - minor_radius


def _fit_torus(
    points: np.ndarray,
    face_centers: np.ndarray,
    normals: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
    diagonal: float,
    max_nfev: int = 180,
) -> _AnalyticFit | None:
    centered_normals = normals - np.mean(normals, axis=0)
    normal_singular_values = np.linalg.svd(centered_normals, compute_uv=False)
    if (
        normal_singular_values[1] <= 1e-12
        or normal_singular_values[2] / normal_singular_values[1] < 0.025
    ):
        return None
    center_guess = np.mean(points, axis=0)
    _, _, point_directions = np.linalg.svd(
        points - center_guess,
        full_matrices=False,
    )
    sample_indices = np.linspace(
        0,
        len(points) - 1,
        min(len(points), 1200),
        dtype=int,
    )
    sample_points = points[sample_indices]
    lower_center = np.min(points, axis=0) - diagonal
    upper_center = np.max(points, axis=0) + diagonal
    best: tuple[float, np.ndarray, np.ndarray, float, float] | None = None
    for initial_axis in point_directions:
        initial_axis = _canonical_direction(initial_axis)
        relative = points - center_guess
        axial = relative @ initial_axis
        planar = relative - np.outer(axial, initial_axis)
        radial = np.linalg.norm(planar, axis=1)
        initial_major = float(np.median(radial))
        initial_minor = float(
            np.median(np.hypot(radial - initial_major, axial))
        )
        if initial_major <= distance_tolerance or initial_minor <= distance_tolerance:
            continue

        def residual(parameters: np.ndarray) -> np.ndarray:
            center = parameters[:3]
            raw_axis = parameters[3:6]
            axis_length = max(float(np.linalg.norm(raw_axis)), 1e-15)
            axis = raw_axis / axis_length
            major_radius = math.exp(float(parameters[6]))
            minor_radius = math.exp(float(parameters[7]))
            surface_errors = _torus_errors(
                sample_points,
                center,
                axis,
                major_radius,
                minor_radius,
            )
            return np.r_[surface_errors, (axis_length - 1.0) * distance_tolerance]

        result = least_squares(
            residual,
            np.r_[
                center_guess,
                initial_axis,
                math.log(initial_major),
                math.log(initial_minor),
            ],
            bounds=(
                np.r_[
                    lower_center,
                    [-1.0, -1.0, -1.0],
                    math.log(max(distance_tolerance, diagonal * 1e-5)),
                    math.log(max(distance_tolerance, diagonal * 1e-5)),
                ],
                np.r_[
                    upper_center,
                    [1.0, 1.0, 1.0],
                    math.log(diagonal * 2.0),
                    math.log(diagonal),
                ],
            ),
            loss="soft_l1",
            f_scale=distance_tolerance,
            max_nfev=max_nfev,
        )
        if not result.success:
            continue
        center = result.x[:3]
        axis = _canonical_direction(result.x[3:6])
        major_radius = math.exp(float(result.x[6]))
        minor_radius = math.exp(float(result.x[7]))
        errors = _torus_errors(
            points,
            center,
            axis,
            major_radius,
            minor_radius,
        )
        rms_error, _ = _fit_statistics(errors)
        if best is None or rms_error < best[0]:
            best = (rms_error, center, axis, major_radius, minor_radius)
    if best is None:
        return None
    _, center, axis, major_radius, minor_radius = best
    if major_radius <= distance_tolerance or minor_radius <= distance_tolerance:
        return None
    errors = _torus_errors(
        points,
        center,
        axis,
        major_radius,
        minor_radius,
    )
    rms_error, max_error = _fit_statistics(errors)
    relative = face_centers - center
    axial = relative @ axis
    planar = relative - np.outer(axial, axis)
    planar_length = np.linalg.norm(planar, axis=1)
    ring_points = center + (
        major_radius * planar / np.maximum(planar_length[:, None], 1e-15)
    )
    expected = face_centers - ring_points
    expected /= np.maximum(np.linalg.norm(expected, axis=1)[:, None], 1e-15)
    normal_error = _normal_error_degrees(normals, expected)
    if (
        rms_error > distance_tolerance
        or max_error > distance_tolerance * 3
        or normal_error > normal_tolerance_degrees
    ):
        return None
    return _AnalyticFit(
        kind="torus",
        parameters={
            "center": center,
            "axis": axis,
            "major_radius": major_radius,
            "minor_radius": minor_radius,
        },
        rms_error=rms_error,
        max_error=max_error,
        normal_error_degrees=normal_error,
        score=_fit_score(
            rms_error,
            max_error,
            normal_error,
            distance_tolerance,
            normal_tolerance_degrees,
            0.08,
        ),
    )


def _smooth_components(
    mesh: object,
    available: np.ndarray,
    smooth_angle_degrees: float,
) -> list[np.ndarray]:
    adjacency = np.asarray(mesh.face_adjacency, dtype=np.int64)
    if len(adjacency) == 0:
        return [np.flatnonzero(available)] if np.any(available) else []
    normals = np.asarray(mesh.face_normals, dtype=float)
    normal_alignment = np.einsum(
        "ij,ij->i",
        normals[adjacency[:, 0]],
        normals[adjacency[:, 1]],
    )
    smooth = normal_alignment >= math.cos(math.radians(smooth_angle_degrees))
    smooth &= available[adjacency[:, 0]] & available[adjacency[:, 1]]
    edges = adjacency[smooth]
    rows = (
        np.concatenate((edges[:, 0], edges[:, 1]))
        if len(edges)
        else np.empty(0, dtype=np.int64)
    )
    columns = (
        np.concatenate((edges[:, 1], edges[:, 0]))
        if len(edges)
        else np.empty(0, dtype=np.int64)
    )
    graph = coo_matrix(
        (np.ones(len(rows), dtype=np.uint8), (rows, columns)),
        shape=(len(mesh.faces), len(mesh.faces)),
    )
    _, labels = connected_components(graph, directed=False)
    selected = np.flatnonzero(available)
    if len(selected) == 0:
        return []
    order = np.argsort(labels[selected], kind="stable")
    ordered = selected[order]
    ordered_labels = labels[ordered]
    return [
        np.asarray(component, dtype=np.int64)
        for component in np.split(
            ordered,
            np.flatnonzero(np.diff(ordered_labels) != 0) + 1,
        )
        if len(component) > 0
    ]


def _freeform_charts(
    mesh: object,
    face_indices: np.ndarray,
    smooth_angle_degrees: float,
    normal_span_degrees: float = 30.0,
    force: bool = False,
) -> list[np.ndarray]:
    """Split a residual at creases into node-fit-friendly surface charts.

    A smooth closed surface is deliberately retained as one region. Splitting is
    only activated when the component contains a sharp internal adjacency, which
    means one smooth graph path has looped around and joined distinct surface
    sheets. Each resulting chart is connected, contains no accepted crease edge,
    and has a bounded normal span suitable for one B-spline parameterization.
    """

    selected = set(int(index) for index in face_indices)
    adjacency: dict[int, list[int]] = {index: [] for index in selected}
    normals = np.asarray(mesh.face_normals, dtype=float)
    local_limit = math.cos(math.radians(smooth_angle_degrees))
    has_internal_crease = False
    for raw_first, raw_second in np.asarray(mesh.face_adjacency, dtype=np.int64):
        first, second = int(raw_first), int(raw_second)
        if first not in selected or second not in selected:
            continue
        adjacency[first].append(second)
        adjacency[second].append(first)
        if float(normals[first] @ normals[second]) < local_limit:
            has_internal_crease = True
    if not has_internal_crease and not force:
        return [np.asarray(face_indices, dtype=np.int64)]

    areas = np.asarray(mesh.area_faces, dtype=float)
    span_limit = math.cos(math.radians(normal_span_degrees))
    remaining = set(selected)
    charts: list[np.ndarray] = []
    while remaining:
        seed = max(remaining, key=lambda index: float(areas[index]))
        seed_normal = normals[seed]
        queue = [seed]
        chart = {seed}
        remaining.remove(seed)
        while queue:
            current = queue.pop()
            for candidate in adjacency[current]:
                if candidate not in remaining:
                    continue
                if float(normals[candidate] @ seed_normal) < span_limit:
                    continue
                chart_neighbors = [
                    neighbor
                    for neighbor in adjacency[candidate]
                    if neighbor in chart
                ]
                if any(
                    float(normals[candidate] @ normals[neighbor]) < local_limit
                    for neighbor in chart_neighbors
                ):
                    continue
                remaining.remove(candidate)
                chart.add(candidate)
                queue.append(candidate)
        charts.append(np.asarray(sorted(chart), dtype=np.int64))
    return charts


def _bounded_connected_charts(
    mesh: object,
    face_indices: np.ndarray,
    maximum_faces: int = 400,
) -> list[np.ndarray]:
    """Partition an oversized residual into compact connected fitting charts."""

    if len(face_indices) <= maximum_faces:
        return [np.asarray(face_indices, dtype=np.int64)]
    remaining = set(int(index) for index in face_indices)
    adjacency: dict[int, list[int]] = {index: [] for index in remaining}
    for raw_first, raw_second in np.asarray(mesh.face_adjacency, dtype=np.int64):
        first, second = int(raw_first), int(raw_second)
        if first in remaining and second in remaining:
            adjacency[first].append(second)
            adjacency[second].append(first)
    charts: list[np.ndarray] = []
    while remaining:
        seed = next(iter(remaining))
        remaining.remove(seed)
        queue = [seed]
        cursor = 0
        chart = [seed]
        while cursor < len(queue) and len(chart) < maximum_faces:
            current = queue[cursor]
            cursor += 1
            for candidate in adjacency[current]:
                if candidate not in remaining:
                    continue
                remaining.remove(candidate)
                chart.append(candidate)
                queue.append(candidate)
                if len(chart) >= maximum_faces:
                    break
        charts.append(np.asarray(sorted(chart), dtype=np.int64))
    return charts


def _coplanar_components(
    mesh: object,
    distance_tolerance: float,
) -> list[np.ndarray]:
    """Grow planes against one seed equation so curvature cannot creep in."""

    adjacency = np.asarray(mesh.face_adjacency, dtype=np.int64)
    if len(adjacency) == 0:
        return [np.arange(len(mesh.faces), dtype=np.int64)]
    normals = np.asarray(mesh.face_normals, dtype=float)
    centers = np.asarray(mesh.triangles_center, dtype=float)
    areas = np.asarray(mesh.area_faces, dtype=float)
    neighbors: list[list[int]] = [[] for _ in range(len(mesh.faces))]
    for raw_first, raw_second in adjacency:
        first, second = int(raw_first), int(raw_second)
        neighbors[first].append(second)
        neighbors[second].append(first)
    remaining = np.ones(len(mesh.faces), dtype=bool)
    seed_order = np.argsort(-areas, kind="stable")
    plane_distance_tolerance = max(distance_tolerance * 0.1, 1e-8)
    normal_limit = math.cos(math.radians(0.25))
    components: list[np.ndarray] = []
    for seed in seed_order:
        seed = int(seed)
        if not remaining[seed]:
            continue
        seed_normal = normals[seed]
        seed_origin = centers[seed]
        remaining[seed] = False
        queue = [seed]
        cursor = 0
        component = [seed]
        while cursor < len(queue):
            current = queue[cursor]
            cursor += 1
            for candidate in neighbors[current]:
                if not remaining[candidate]:
                    continue
                if float(normals[candidate] @ seed_normal) < normal_limit:
                    continue
                candidate_points = np.asarray(
                    mesh.vertices[mesh.faces[candidate]],
                    dtype=float,
                )
                if float(
                    np.max(np.abs((candidate_points - seed_origin) @ seed_normal))
                ) > plane_distance_tolerance:
                    continue
                remaining[candidate] = False
                component.append(candidate)
                queue.append(candidate)
        components.append(np.asarray(component, dtype=np.int64))
    return components


def _locally_coplanar_components(
    mesh: object,
    distance_tolerance: float,
) -> list[np.ndarray]:
    """Group locally coplanar facets before whole-node surface competition."""

    adjacency = np.asarray(mesh.face_adjacency, dtype=np.int64)
    if len(adjacency) == 0:
        return [np.arange(len(mesh.faces), dtype=np.int64)]
    normals = np.asarray(mesh.face_normals, dtype=float)
    centers = np.asarray(mesh.triangles_center, dtype=float)
    alignment = np.einsum(
        "ij,ij->i",
        normals[adjacency[:, 0]],
        normals[adjacency[:, 1]],
    )
    candidates = adjacency[alignment >= math.cos(math.radians(1.0))]
    accepted: list[tuple[int, int]] = []
    for raw_first, raw_second in candidates:
        first, second = int(raw_first), int(raw_second)
        first_error = abs(float((centers[second] - centers[first]) @ normals[first]))
        second_error = abs(float((centers[first] - centers[second]) @ normals[second]))
        if max(first_error, second_error) <= distance_tolerance:
            accepted.append((first, second))
    if accepted:
        edges = np.asarray(accepted, dtype=np.int64)
        rows = np.r_[edges[:, 0], edges[:, 1]]
        columns = np.r_[edges[:, 1], edges[:, 0]]
    else:
        rows = columns = np.empty(0, dtype=np.int64)
    graph = coo_matrix(
        (np.ones(len(rows), dtype=np.uint8), (rows, columns)),
        shape=(len(mesh.faces), len(mesh.faces)),
    )
    _, labels = connected_components(graph, directed=False)
    order = np.argsort(labels, kind="stable")
    ordered_labels = labels[order]
    return [
        np.asarray(component, dtype=np.int64)
        for component in np.split(
            order,
            np.flatnonzero(np.diff(ordered_labels) != 0) + 1,
        )
        if len(component) > 0
    ]


def _detect_planar_patches(
    data: MeshData,
    components: list[np.ndarray],
    distance_tolerance: float,
    minimum_area: float,
    continuous_angle_degrees: float,
) -> list[PlanarPatch]:
    mesh = data.mesh
    normals = np.asarray(mesh.face_normals, dtype=float)
    centers = np.asarray(mesh.triangles_center, dtype=float)
    component_labels = np.full(len(mesh.faces), -1, dtype=np.int64)
    for component_index, face_indices in enumerate(components):
        component_labels[face_indices] = component_index
    boundary_alignments: list[list[float]] = [[] for _ in components]
    for first_face, second_face in np.asarray(mesh.face_adjacency, dtype=np.int64):
        first_label = component_labels[first_face]
        second_label = component_labels[second_face]
        if first_label == second_label:
            continue
        alignment = float(normals[first_face] @ normals[second_face])
        if first_label >= 0:
            boundary_alignments[first_label].append(alignment)
        if second_label >= 0:
            boundary_alignments[second_label].append(alignment)
    raw: list[tuple[float, np.ndarray, np.ndarray, np.ndarray, float, float]] = []
    for component_index, face_indices in enumerate(components):
        area = float(np.sum(mesh.area_faces[face_indices]))
        if area < minimum_area:
            continue
        # A tessellated cylinder, cone, sphere, torus, or freeform consists of
        # locally flat facets too. A small facet is not a plane surface when its
        # neighboring nodes continue through a smooth normal field. Keep those
        # facets together so their combined node set can compete for one best-fit
        # mathematical surface below. Large planes remain reliable seeds even
        # when they meet a tangent blend.
        alignments = boundary_alignments[component_index]
        smooth_fraction = (
            float(
                np.mean(
                    np.asarray(alignments)
                    >= math.cos(math.radians(continuous_angle_degrees))
                )
            )
            if alignments
            else 0.0
        )
        if (
            smooth_fraction >= 0.4
            and (
                area < float(mesh.area) * 0.01
                or (
                    area < float(mesh.area) * 0.08
                    and len(face_indices) < 8
                )
            )
        ):
            continue
        weights = np.asarray(mesh.area_faces[face_indices], dtype=float)
        normal = np.average(normals[face_indices], axis=0, weights=weights)
        normal /= max(float(np.linalg.norm(normal)), 1e-12)
        origin = np.average(centers[face_indices], axis=0, weights=weights)
        vertex_indices = np.unique(mesh.faces[face_indices])
        residual = np.abs((mesh.vertices[vertex_indices] - origin) @ normal)
        rms_error, max_error = _fit_statistics(residual)
        patch_normal_error = _normal_error_degrees(
            normals[face_indices],
            np.tile(normal, (len(face_indices), 1)),
        )
        if (
            max_error > max(distance_tolerance * 0.1, 1e-8)
            or patch_normal_error > 0.25
        ):
            continue
        raw.append(
            (
                area,
                face_indices,
                origin,
                normal,
                rms_error,
                max_error,
            )
        )

    patches: list[PlanarPatch] = []
    for index, (
        area,
        face_indices,
        origin,
        normal,
        rms_error,
        max_error,
    ) in enumerate(sorted(raw, key=lambda item: -item[0]), start=1):
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
                vertex_indices=np.unique(mesh.faces[face_indices]),
                origin=origin,
                normal=normal,
                x_direction=x_direction,
                y_direction=y_direction,
                area=area,
                boundary_loops=loops_2d,
                polygon=polygon,
                boundary_loops_3d=loops_3d,
                rms_error=rms_error,
                max_error=max_error,
            )
        )
    return patches


def _fit_component(
    data: MeshData,
    face_indices: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
    *,
    torus_max_nfev: int = 180,
    allowed_kinds: tuple[str, ...] = ("cylinder", "cone", "sphere", "torus"),
) -> _AnalyticFit | None:
    mesh = data.mesh
    vertex_indices = np.unique(mesh.faces[face_indices])
    points = np.asarray(mesh.vertices[vertex_indices], dtype=float)
    face_centers = np.asarray(mesh.triangles_center[face_indices], dtype=float)
    normals = np.asarray(mesh.face_normals[face_indices], dtype=float)
    if len(points) < 8 or len(face_indices) < 8:
        return None
    candidates = [
        _fit_cylinder(
            points,
            face_centers,
            normals,
            distance_tolerance,
            normal_tolerance_degrees,
        )
        if "cylinder" in allowed_kinds
        else None,
        _fit_cone(
            points,
            face_centers,
            normals,
            distance_tolerance,
            normal_tolerance_degrees,
            data.diagonal,
        )
        if "cone" in allowed_kinds
        else None,
        _fit_sphere(
            points,
            face_centers,
            normals,
            distance_tolerance,
            normal_tolerance_degrees,
        )
        if "sphere" in allowed_kinds
        else None,
        _fit_torus(
            points,
            face_centers,
            normals,
            distance_tolerance,
            normal_tolerance_degrees,
            data.diagonal,
            max_nfev=torus_max_nfev,
        )
        if "torus" in allowed_kinds
        else None,
    ]
    valid = [candidate for candidate in candidates if candidate is not None]
    return min(valid, key=lambda candidate: candidate.score, default=None)


def _fit_seeded_torus(
    data: MeshData,
    face_indices: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
) -> _AnalyticFit | None:
    """Fit a torus from a local revolution axis and circular meridian."""

    mesh = data.mesh
    proposal = _fit_surface_of_revolution_patch(
        mesh,
        face_indices,
        distance_tolerance * 2,
        normal_tolerance_degrees * 1.25,
        1,
    )
    if proposal is None:
        return None
    vertex_indices = np.unique(mesh.faces[face_indices])
    points = np.asarray(mesh.vertices[vertex_indices], dtype=float)
    relative = points - proposal.origin
    axial = relative @ proposal.axis
    radial = np.linalg.norm(
        relative - np.outer(axial, proposal.axis),
        axis=1,
    )
    matrix = np.column_stack((2 * axial, 2 * radial, np.ones(len(points))))
    axial_center, major_radius, constant = np.linalg.lstsq(
        matrix,
        axial**2 + radial**2,
        rcond=None,
    )[0]
    minor_squared = float(
        constant + axial_center**2 + major_radius**2
    )
    major_radius = abs(float(major_radius))
    if minor_squared <= distance_tolerance**2 or major_radius <= distance_tolerance:
        return None
    minor_radius = math.sqrt(minor_squared)
    center = proposal.origin + float(axial_center) * proposal.axis

    def residual(parameters: np.ndarray) -> np.ndarray:
        raw_axis = parameters[3:6]
        axis_length = max(float(np.linalg.norm(raw_axis)), 1e-15)
        axis = raw_axis / axis_length
        return np.r_[
            _torus_errors(
                points,
                parameters[:3],
                axis,
                math.exp(float(parameters[6])),
                math.exp(float(parameters[7])),
            ),
            (axis_length - 1.0) * distance_tolerance,
        ]

    try:
        result = least_squares(
            residual,
            np.r_[
                center,
                proposal.axis,
                math.log(major_radius),
                math.log(minor_radius),
            ],
            loss="soft_l1",
            f_scale=distance_tolerance,
            max_nfev=100,
        )
    except Exception:
        return None
    center = np.asarray(result.x[:3], dtype=float)
    axis = _canonical_direction(result.x[3:6])
    major_radius = math.exp(float(result.x[6]))
    minor_radius = math.exp(float(result.x[7]))
    errors = _torus_errors(points, center, axis, major_radius, minor_radius)
    rms_error, max_error = _fit_statistics(errors)
    centers = np.asarray(mesh.triangles_center[face_indices], dtype=float)
    normals = np.asarray(mesh.face_normals[face_indices], dtype=float)
    center_relative = centers - center
    center_axial = center_relative @ axis
    planar = center_relative - np.outer(center_axial, axis)
    planar_length = np.linalg.norm(planar, axis=1)
    ring_points = center + (
        major_radius * planar / np.maximum(planar_length[:, None], 1e-15)
    )
    expected = centers - ring_points
    expected /= np.maximum(np.linalg.norm(expected, axis=1)[:, None], 1e-15)
    normal_error = _normal_error_degrees(normals, expected)
    if (
        rms_error > distance_tolerance
        or max_error > distance_tolerance * 3
        or normal_error > normal_tolerance_degrees
    ):
        return None
    return _AnalyticFit(
        kind="torus",
        parameters={
            "center": center,
            "axis": axis,
            "major_radius": major_radius,
            "minor_radius": minor_radius,
        },
        rms_error=rms_error,
        max_error=max_error,
        normal_error_degrees=normal_error,
        score=_fit_score(
            rms_error,
            max_error,
            normal_error,
            distance_tolerance,
            normal_tolerance_degrees,
            0.08,
        ),
    )


def _analytic_inlier_faces(
    fit: _AnalyticFit,
    mesh: object,
    face_indices: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
) -> np.ndarray:
    """Score a fitted primitive against face centers and normals for RANSAC."""

    centers = np.asarray(mesh.triangles_center[face_indices], dtype=float)
    normals = np.asarray(mesh.face_normals[face_indices], dtype=float)
    parameters = fit.parameters
    if fit.kind == "cylinder":
        axis = np.asarray(parameters["axis"], dtype=float)
        relative = centers - np.asarray(parameters["origin"], dtype=float)
        axial = relative @ axis
        radial = relative - np.outer(axial, axis)
        lengths = np.linalg.norm(radial, axis=1)
        face_points = np.asarray(mesh.vertices[mesh.faces[face_indices]], dtype=float)
        point_relative = face_points - np.asarray(parameters["origin"], dtype=float)
        point_axial = np.einsum("fvi,i->fv", point_relative, axis)
        point_radial = point_relative - point_axial[:, :, None] * axis
        residuals = np.max(
            np.abs(
                np.linalg.norm(point_radial, axis=2)
                - float(parameters["radius"])
            ),
            axis=1,
        )
        expected = radial / np.maximum(lengths[:, None], 1e-15)
    elif fit.kind == "cone":
        axis = np.asarray(parameters["axis"], dtype=float)
        relative = centers - np.asarray(parameters["apex"], dtype=float)
        axial = relative @ axis
        radial = relative - np.outer(axial, axis)
        lengths = np.linalg.norm(radial, axis=1)
        tangent = math.tan(float(parameters["semi_angle"]))
        residuals = np.abs(lengths - np.abs(axial) * tangent)
        expected = radial / np.maximum(lengths[:, None], 1e-15)
        expected -= np.sign(axial)[:, None] * tangent * axis
        expected /= np.maximum(np.linalg.norm(expected, axis=1)[:, None], 1e-15)
    elif fit.kind == "sphere":
        relative = centers - np.asarray(parameters["center"], dtype=float)
        lengths = np.linalg.norm(relative, axis=1)
        residuals = np.abs(lengths - float(parameters["radius"]))
        expected = relative / np.maximum(lengths[:, None], 1e-15)
    else:
        axis = np.asarray(parameters["axis"], dtype=float)
        center = np.asarray(parameters["center"], dtype=float)
        relative = centers - center
        axial = relative @ axis
        planar = relative - np.outer(axial, axis)
        planar_lengths = np.linalg.norm(planar, axis=1)
        ring_points = center + (
            float(parameters["major_radius"])
            * planar
            / np.maximum(planar_lengths[:, None], 1e-15)
        )
        ring_relative = centers - ring_points
        ring_lengths = np.linalg.norm(ring_relative, axis=1)
        face_points = np.asarray(mesh.vertices[mesh.faces[face_indices]], dtype=float)
        point_errors = _torus_errors(
            face_points.reshape((-1, 3)),
            center,
            axis,
            float(parameters["major_radius"]),
            float(parameters["minor_radius"]),
        ).reshape((-1, 3))
        residuals = np.max(np.abs(point_errors), axis=1)
        expected = ring_relative / np.maximum(ring_lengths[:, None], 1e-15)
    angles = np.degrees(
        np.arccos(
            np.clip(
                np.abs(np.einsum("ij,ij->i", normals, expected)),
                -1.0,
                1.0,
            )
        )
    )
    return np.asarray(face_indices)[
        (residuals <= distance_tolerance * 0.5)
        & (angles <= normal_tolerance_degrees * 1.5)
    ]


def _extract_local_analytic_regions(
    data: MeshData,
    face_indices: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
    minimum_area: float,
) -> tuple[list[tuple[_AnalyticFit, np.ndarray]], list[np.ndarray]]:
    """Detect tangent primitives by deterministic local-sample consensus."""

    mesh = data.mesh
    remaining = set(int(index) for index in face_indices)
    if len(remaining) < 20:
        return [], [np.asarray(sorted(remaining), dtype=np.int64)]
    neighbors: dict[int, list[int]] = {index: [] for index in remaining}
    for raw_first, raw_second in np.asarray(mesh.face_adjacency, dtype=np.int64):
        first, second = int(raw_first), int(raw_second)
        if first in remaining and second in remaining:
            neighbors[first].append(second)
            neighbors[second].append(first)
    detected: list[tuple[_AnalyticFit, np.ndarray]] = []
    # Very large filleted parts can contain many distinct cylinders in one
    # tangent chain. On smaller mixed/freeform models, a deep peel starts
    # approximating local B-spline fragments as unrelated primitives, so keep
    # the conservative historical cap there.
    maximum_regions = 24 if len(mesh.faces) >= 60_000 else 4
    for _ in range(maximum_regions):
        if len(remaining) < 20:
            break
        ordered_remaining = np.asarray(sorted(remaining), dtype=np.int64)
        seeds = ordered_remaining[
            np.linspace(
                0,
                len(ordered_remaining) - 1,
                min(16, len(ordered_remaining)),
                dtype=int,
            )
        ]
        best: tuple[float, _AnalyticFit, np.ndarray] | None = None
        samples: list[np.ndarray] = []
        for raw_seed in seeds:
            seed = int(raw_seed)
            # Multi-scale neighborhoods keep small cylindrical islands from
            # being drowned by hundreds of adjacent fillet triangles, while
            # retaining the larger samples needed for sparse broad cylinders.
            for maximum_faces in (64, 160, 400):
                queue = [seed]
                visited = {seed}
                cursor = 0
                while cursor < len(queue) and len(visited) < maximum_faces:
                    current = queue[cursor]
                    cursor += 1
                    for candidate in neighbors[current]:
                        if candidate not in remaining or candidate in visited:
                            continue
                        visited.add(candidate)
                        queue.append(candidate)
                        if len(visited) >= maximum_faces:
                            break
                if len(visited) >= 8:
                    samples.append(np.asarray(sorted(visited), dtype=np.int64))

        # Dense fillets often surround a sparsely tessellated large-radius
        # cylinder. A fixed-face-count neighborhood then contains mostly the
        # fillet and obscures the cylinder's normal nullspace. Grow additional
        # deterministic seeds through neighboring faces of comparable area;
        # the full-region inlier/refit checks below still decide acceptance.
        face_areas = np.asarray(mesh.area_faces, dtype=float)
        area_seeds = ordered_remaining[
            np.argsort(-face_areas[ordered_remaining], kind="stable")[
                : min(8, len(ordered_remaining))
            ]
        ]
        for raw_seed in area_seeds:
            seed = int(raw_seed)
            for ratio in (0.3, 0.1):
                minimum_seed_area = float(face_areas[seed]) * ratio
                queue = [seed]
                visited = {seed}
                cursor = 0
                while cursor < len(queue) and len(visited) < 400:
                    current = queue[cursor]
                    cursor += 1
                    for candidate in neighbors[current]:
                        if (
                            candidate not in remaining
                            or candidate in visited
                            or face_areas[candidate] < minimum_seed_area
                        ):
                            continue
                        visited.add(candidate)
                        queue.append(candidate)
                        if len(visited) >= 400:
                            break
                if len(visited) >= 8:
                    samples.append(np.asarray(sorted(visited), dtype=np.int64))

        seen_samples: set[tuple[int, ...]] = set()
        for sample in samples:
            sample_key = tuple(int(value) for value in sample)
            if sample_key in seen_samples:
                continue
            seen_samples.add(sample_key)
            fit = _fit_component(
                data,
                sample,
                distance_tolerance * 2,
                normal_tolerance_degrees * 1.25,
                torus_max_nfev=30,
                allowed_kinds=("cylinder",),
            )
            if fit is None:
                continue
            inliers = _analytic_inlier_faces(
                fit,
                mesh,
                ordered_remaining,
                distance_tolerance,
                normal_tolerance_degrees,
            )
            available = np.zeros(len(mesh.faces), dtype=bool)
            available[inliers] = True
            for component in _smooth_components(mesh, available, 30.0):
                area = float(np.sum(mesh.area_faces[component]))
                if len(component) < 12 or area < minimum_area:
                    continue
                refinement_faces = component[
                    np.linspace(
                        0,
                        len(component) - 1,
                        min(240, len(component)),
                        dtype=int,
                    )
                ]
                refined = _fit_component(
                    data,
                    refinement_faces,
                    distance_tolerance,
                    normal_tolerance_degrees,
                    torus_max_nfev=45,
                    allowed_kinds=("cylinder",),
                )
                if refined is None and fit.kind == "cylinder":
                    axis = np.asarray(fit.parameters["axis"], dtype=float)
                    origin = np.asarray(fit.parameters["origin"], dtype=float)
                    radius = float(fit.parameters["radius"])
                    component_vertices = np.unique(mesh.faces[component])
                    component_points = np.asarray(
                        mesh.vertices[component_vertices],
                        dtype=float,
                    )
                    relative = component_points - origin
                    axial = relative @ axis
                    radial = relative - np.outer(axial, axis)
                    errors = np.abs(np.linalg.norm(radial, axis=1) - radius)
                    rms_error, max_error = _fit_statistics(errors)
                    centers = np.asarray(
                        mesh.triangles_center[component],
                        dtype=float,
                    )
                    measured = np.asarray(mesh.face_normals[component], dtype=float)
                    center_relative = centers - origin
                    center_axial = center_relative @ axis
                    expected = center_relative - np.outer(center_axial, axis)
                    expected /= np.maximum(
                        np.linalg.norm(expected, axis=1)[:, None],
                        1e-15,
                    )
                    normal_error = _normal_error_degrees(measured, expected)
                    if (
                        rms_error <= distance_tolerance
                        and max_error <= distance_tolerance * 0.5
                        and normal_error <= normal_tolerance_degrees
                    ):
                        parameters = dict(fit.parameters)
                        parameters["start"] = float(np.min(axial))
                        parameters["end"] = float(np.max(axial))
                        refined = _AnalyticFit(
                            kind="cylinder",
                            parameters=parameters,
                            rms_error=rms_error,
                            max_error=max_error,
                            normal_error_degrees=normal_error,
                            score=_fit_score(
                                rms_error,
                                max_error,
                                normal_error,
                                distance_tolerance,
                                normal_tolerance_degrees,
                                0.02,
                            ),
                        )
                if refined is None:
                    continue
                confirmed = _analytic_inlier_faces(
                    refined,
                    mesh,
                    component,
                    distance_tolerance,
                    normal_tolerance_degrees,
                )
                if len(confirmed) < len(component) * 0.95:
                    continue
                if best is None or area > best[0]:
                    best = (area, refined, component)
        if best is None:
            break
        _, fit, component = best
        detected.append((fit, component))
        for face_index in component:
            remaining.discard(int(face_index))

    available = np.zeros(len(mesh.faces), dtype=bool)
    if remaining:
        available[np.asarray(sorted(remaining), dtype=np.int64)] = True
    residuals = _smooth_components(mesh, available, 30.0)
    return detected, residuals


def _extrusion_profile_cylinder_regions(
    data: MeshData,
    patch: LinearExtrusionPatch,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
) -> list[tuple[_AnalyticFit, np.ndarray]]:
    """Recover circular profile runs hidden inside a general extrusion."""

    profile = np.asarray(patch.profile_points, dtype=float)
    if len(profile) > 1:
        keep = np.r_[
            True,
            np.linalg.norm(np.diff(profile, axis=0), axis=1)
            > max(distance_tolerance * 0.01, 1e-10),
        ]
        profile = profile[keep]
    if len(profile) < 4:
        return []
    first, second = _plane_basis(patch.direction)
    coordinates = np.column_stack((profile @ first, profile @ second))
    local_circles: list[tuple[int, np.ndarray, float] | None] = []
    for index in range(1, len(coordinates) - 1):
        low, middle, high = coordinates[index - 1 : index + 2]
        matrix = 2 * np.asarray([middle - low, high - low], dtype=float)
        if abs(float(np.linalg.det(matrix))) <= 1e-12:
            local_circles.append(None)
            continue
        target = np.asarray(
            [middle @ middle - low @ low, high @ high - low @ low],
            dtype=float,
        )
        center = np.linalg.solve(matrix, target)
        radius = float(np.linalg.norm(low - center))
        local_circles.append(
            (index, center, radius)
            if math.isfinite(radius) and radius > distance_tolerance
            else None
        )

    runs: list[list[tuple[int, np.ndarray, float]]] = []
    current: list[tuple[int, np.ndarray, float]] = []
    relation_tolerance = max(distance_tolerance * 2, 1e-7)
    for circle in local_circles:
        if circle is None:
            if current:
                runs.append(current)
                current = []
            continue
        if current:
            _, previous_center, previous_radius = current[-1]
            _, center, radius = circle
            if (
                np.linalg.norm(center - previous_center) > relation_tolerance
                or abs(radius - previous_radius) > relation_tolerance
            ):
                runs.append(current)
                current = []
        current.append(circle)
    if current:
        runs.append(current)

    mesh = data.mesh
    candidates: list[tuple[float, _AnalyticFit, np.ndarray]] = []
    for run in runs:
        # Three adjacent profile chords provide four distinct circular nodes.
        # Two consistent local circumcircles are therefore sufficient to seed
        # a short CAD arc; the all-node and all-normal validation below is the
        # actual acceptance gate.
        if len(run) < 2:
            continue
        centers = np.asarray([item[1] for item in run], dtype=float)
        radii = np.asarray([item[2] for item in run], dtype=float)
        center = np.median(centers, axis=0)
        radius = float(np.median(radii))
        if (
            float(np.percentile(np.linalg.norm(centers - center, axis=1), 90))
            > relation_tolerance
            or float(np.percentile(np.abs(radii - radius), 90))
            > relation_tolerance
        ):
            continue
        origin = (
            center[0] * first
            + center[1] * second
            + patch.start * patch.direction
        )
        provisional = _AnalyticFit(
            kind="cylinder",
            parameters={
                "origin": origin,
                "axis": patch.direction,
                "radius": radius,
                "start": 0.0,
                "end": patch.end - patch.start,
            },
            rms_error=0.0,
            max_error=0.0,
            normal_error_degrees=0.0,
            score=0.0,
        )
        inliers = _analytic_inlier_faces(
            provisional,
            mesh,
            patch.face_indices,
            distance_tolerance,
            normal_tolerance_degrees,
        )
        available = np.zeros(len(mesh.faces), dtype=bool)
        available[inliers] = True
        for component in _smooth_components(mesh, available, 30.0):
            if len(component) < 6:
                continue
            refined = _fit_component(
                data,
                component,
                distance_tolerance,
                normal_tolerance_degrees,
                allowed_kinds=("cylinder",),
            )
            if refined is None:
                refined = _validated_seeded_cylinder_fit(
                    mesh,
                    component,
                    patch.direction,
                    origin,
                    radius,
                    distance_tolerance,
                    normal_tolerance_degrees,
                )
            if refined is None:
                continue
            area = float(np.sum(mesh.area_faces[component]))
            candidates.append((area, refined, component))

    claimed: set[int] = set()
    result: list[tuple[_AnalyticFit, np.ndarray]] = []
    for _, fit, component in sorted(candidates, key=lambda item: -item[0]):
        if any(int(face) in claimed for face in component):
            continue
        result.append((fit, component))
        claimed.update(int(face) for face in component)
    return result


def _extract_local_torus_regions(
    data: MeshData,
    face_indices: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
    minimum_area: float,
) -> tuple[list[tuple[_AnalyticFit, np.ndarray]], list[np.ndarray]]:
    """Peel tori from tangent chains using local revolution-axis seeds."""

    mesh = data.mesh
    remaining = set(int(index) for index in face_indices)
    if len(remaining) < 40:
        return [], [np.asarray(sorted(remaining), dtype=np.int64)]
    neighbors: dict[int, list[int]] = {index: [] for index in remaining}
    for raw_first, raw_second in np.asarray(mesh.face_adjacency, dtype=np.int64):
        first, second = int(raw_first), int(raw_second)
        if first in remaining and second in remaining:
            neighbors[first].append(second)
            neighbors[second].append(first)
    detected: list[tuple[_AnalyticFit, np.ndarray]] = []
    # Use the extended peel only for dense models where many true fillets can
    # share one connected region; retain the conservative cap for smaller
    # freeform/revolved parts.
    maximum_regions = 24 if len(mesh.faces) >= 60_000 else 6
    for _ in range(maximum_regions):
        if len(remaining) < 40:
            break
        ordered_remaining = np.asarray(sorted(remaining), dtype=np.int64)
        seeds = ordered_remaining[
            np.linspace(
                0,
                len(ordered_remaining) - 1,
                min(8, len(ordered_remaining)),
                dtype=int,
            )
        ]
        best: tuple[float, _AnalyticFit, np.ndarray] | None = None
        for raw_seed in seeds:
            seed = int(raw_seed)
            queue = [seed]
            visited = {seed}
            cursor = 0
            while cursor < len(queue) and len(visited) < 400:
                current = queue[cursor]
                cursor += 1
                for candidate in neighbors[current]:
                    if candidate not in remaining or candidate in visited:
                        continue
                    visited.add(candidate)
                    queue.append(candidate)
                    if len(visited) >= 400:
                        break
            sample = np.asarray(sorted(visited), dtype=np.int64)
            fit = _fit_seeded_torus(
                data,
                sample,
                distance_tolerance,
                normal_tolerance_degrees,
            )
            if fit is None:
                continue
            inliers = _analytic_inlier_faces(
                fit,
                mesh,
                ordered_remaining,
                distance_tolerance,
                normal_tolerance_degrees,
            )
            available = np.zeros(len(mesh.faces), dtype=bool)
            available[inliers] = True
            for component in _smooth_components(mesh, available, 30.0):
                area = float(np.sum(mesh.area_faces[component]))
                if len(component) < 16 or area < minimum_area:
                    continue
                vertex_indices = np.unique(mesh.faces[component])
                points = np.asarray(mesh.vertices[vertex_indices], dtype=float)
                parameters = fit.parameters
                errors = _torus_errors(
                    points,
                    np.asarray(parameters["center"], dtype=float),
                    np.asarray(parameters["axis"], dtype=float),
                    float(parameters["major_radius"]),
                    float(parameters["minor_radius"]),
                )
                rms_error, max_error = _fit_statistics(errors)
                centers = np.asarray(mesh.triangles_center[component], dtype=float)
                normals = np.asarray(mesh.face_normals[component], dtype=float)
                axis = np.asarray(parameters["axis"], dtype=float)
                center = np.asarray(parameters["center"], dtype=float)
                relative = centers - center
                axial = relative @ axis
                planar = relative - np.outer(axial, axis)
                planar_length = np.linalg.norm(planar, axis=1)
                ring_points = center + (
                    float(parameters["major_radius"])
                    * planar
                    / np.maximum(planar_length[:, None], 1e-15)
                )
                expected = centers - ring_points
                expected /= np.maximum(
                    np.linalg.norm(expected, axis=1)[:, None],
                    1e-15,
                )
                normal_error = _normal_error_degrees(normals, expected)
                if (
                    rms_error > distance_tolerance
                    or max_error > distance_tolerance * 0.5
                    or normal_error > normal_tolerance_degrees
                ):
                    continue
                validated = _AnalyticFit(
                    kind="torus",
                    parameters=dict(parameters),
                    rms_error=rms_error,
                    max_error=max_error,
                    normal_error_degrees=normal_error,
                    score=_fit_score(
                        rms_error,
                        max_error,
                        normal_error,
                        distance_tolerance,
                        normal_tolerance_degrees,
                        0.08,
                    ),
                )
                if best is None or area > best[0]:
                    best = (area, validated, component)
        if best is None:
            break
        _, fit, component = best
        detected.append((fit, component))
        for face_index in component:
            remaining.discard(int(face_index))

    available = np.zeros(len(mesh.faces), dtype=bool)
    if remaining:
        available[np.asarray(sorted(remaining), dtype=np.int64)] = True
    residuals = _smooth_components(mesh, available, 30.0)
    return detected, residuals


def _make_analytic_patch(
    mesh: object,
    fit: _AnalyticFit,
    face_indices: np.ndarray,
    index: int,
) -> CylindricalPatch | ConicalPatch | SphericalPatch | ToroidalPatch:
    common = {
        "face_indices": face_indices,
        "vertex_indices": np.unique(mesh.faces[face_indices]),
        "area": float(np.sum(mesh.area_faces[face_indices])),
        "boundary_loops": _boundary_loops(mesh, face_indices),
        "rms_error": fit.rms_error,
        "max_error": fit.max_error,
        "normal_error_degrees": fit.normal_error_degrees,
    }
    parameters = fit.parameters
    if fit.kind == "cylinder":
        return CylindricalPatch(
            patch_id=f"cylinder-{index:03d}",
            origin=np.asarray(parameters["origin"], dtype=float),
            axis=np.asarray(parameters["axis"], dtype=float),
            radius=float(parameters["radius"]),
            start=float(parameters["start"]),
            end=float(parameters["end"]),
            **common,
        )
    if fit.kind == "cone":
        return ConicalPatch(
            patch_id=f"cone-{index:03d}",
            apex=np.asarray(parameters["apex"], dtype=float),
            axis=np.asarray(parameters["axis"], dtype=float),
            semi_angle=float(parameters["semi_angle"]),
            start=float(parameters["start"]),
            end=float(parameters["end"]),
            **common,
        )
    if fit.kind == "sphere":
        return SphericalPatch(
            patch_id=f"sphere-{index:03d}",
            center=np.asarray(parameters["center"], dtype=float),
            radius=float(parameters["radius"]),
            **common,
        )
    return ToroidalPatch(
        patch_id=f"torus-{index:03d}",
        center=np.asarray(parameters["center"], dtype=float),
        axis=np.asarray(parameters["axis"], dtype=float),
        major_radius=float(parameters["major_radius"]),
        minor_radius=float(parameters["minor_radius"]),
        **common,
    )


def _merge_adjacent_torus_patches(
    data: MeshData,
    patches: list[ToroidalPatch],
    distance_tolerance: float,
    normal_tolerance_degrees: float,
) -> tuple[list[ToroidalPatch], list[np.ndarray]]:
    """Refit split pieces of one torus and return rejected boundary faces."""

    mesh = data.mesh
    working = list(patches)
    rejected: list[np.ndarray] = []
    while len(working) >= 2:
        owner = np.full(len(mesh.faces), -1, dtype=np.int32)
        for index, patch in enumerate(working):
            owner[patch.face_indices] = index
        adjacent_pairs: set[tuple[int, int]] = set()
        for raw_first, raw_second in np.asarray(mesh.face_adjacency, dtype=np.int64):
            first_owner = int(owner[int(raw_first)])
            second_owner = int(owner[int(raw_second)])
            if first_owner >= 0 and second_owner >= 0 and first_owner != second_owner:
                adjacent_pairs.add(tuple(sorted((first_owner, second_owner))))
        merged = False
        for first_index, second_index in sorted(adjacent_pairs):
            first = working[first_index]
            second = working[second_index]
            if abs(first.minor_radius - second.minor_radius) > distance_tolerance * 2:
                continue
            union = np.unique(
                np.r_[first.face_indices, second.face_indices]
            ).astype(np.int64)
            fit = _fit_component(
                data,
                union,
                distance_tolerance,
                normal_tolerance_degrees,
                torus_max_nfev=180,
                allowed_kinds=("torus",),
            )
            if fit is None:
                continue
            confirmed = _analytic_inlier_faces(
                fit,
                mesh,
                union,
                distance_tolerance,
                normal_tolerance_degrees,
            )
            confirmed_set = set(int(value) for value in confirmed)
            fractions = [
                sum(int(value) in confirmed_set for value in patch.face_indices)
                / len(patch.face_indices)
                for patch in (first, second)
            ]
            if min(fractions) < 0.95:
                continue
            available = np.zeros(len(mesh.faces), dtype=bool)
            available[confirmed] = True
            components = _smooth_components(mesh, available, 30.0)
            if not components:
                continue
            component = max(components, key=len)
            if len(component) < len(union) * 0.95:
                continue
            leftover = np.setdiff1d(union, component, assume_unique=False)
            if len(leftover):
                leftover_mask = np.zeros(len(mesh.faces), dtype=bool)
                leftover_mask[leftover] = True
                rejected.extend(_smooth_components(mesh, leftover_mask, 30.0))
            merged_patch = _make_analytic_patch(mesh, fit, component, 1)
            working = [
                patch
                for index, patch in enumerate(working)
                if index not in {first_index, second_index}
            ]
            working.append(merged_patch)
            merged = True
            break
        if not merged:
            break
    working.sort(key=lambda patch: -patch.area)
    for index, patch in enumerate(working, start=1):
        patch.patch_id = f"torus-{index:03d}"
    return working, rejected


def _cyclic_true_runs(mask: np.ndarray) -> list[np.ndarray]:
    """Return every maximal true run in a cyclic point mask."""

    mask = np.asarray(mask, dtype=bool)
    selected = np.flatnonzero(mask)
    if len(selected) == 0:
        return []
    if len(selected) == len(mask):
        return [np.arange(len(mask), dtype=np.int64)]
    runs = [
        np.asarray(run, dtype=np.int64)
        for run in np.split(selected, np.flatnonzero(np.diff(selected) > 1) + 1)
        if len(run) > 0
    ]
    if len(runs) > 1 and mask[0] and mask[-1]:
        joined = np.r_[runs[-1], runs[0]]
        runs = [joined, *runs[1:-1]]
    return runs


def _regularize_analytic_labels(
    mesh: object,
    face_patch_ids: np.ndarray,
    patches: list[SurfacePatch],
    distance_tolerance: float,
    normal_tolerance_degrees: float,
) -> np.ndarray:
    """Remove fit-threshold checkerboards along tangent analytic boundaries."""

    supported = {
        patch.patch_id: patch
        for patch in patches
        if isinstance(patch, (PlanarPatch, CylindricalPatch, ToroidalPatch))
    }
    if not supported:
        return face_patch_ids
    adjacency = np.asarray(mesh.face_adjacency, dtype=np.int64)
    neighbors: list[list[int]] = [[] for _ in range(len(mesh.faces))]
    for raw_first, raw_second in adjacency:
        first, second = int(raw_first), int(raw_second)
        neighbors[first].append(second)
        neighbors[second].append(first)
    labels = np.asarray(face_patch_ids, dtype=object).copy()

    def fit_energy(face_index: int, patch: SurfacePatch) -> float:
        points = np.asarray(mesh.vertices[mesh.faces[face_index]], dtype=float)
        center = np.asarray(mesh.triangles_center[face_index], dtype=float)
        normal = np.asarray(mesh.face_normals[face_index], dtype=float)
        if isinstance(patch, PlanarPatch):
            errors = np.abs((points - patch.origin) @ patch.normal)
            expected = patch.normal
        elif isinstance(patch, CylindricalPatch):
            relative = points - patch.origin
            axial = relative @ patch.axis
            radial = relative - np.outer(axial, patch.axis)
            errors = np.abs(np.linalg.norm(radial, axis=1) - patch.radius)
            center_relative = center - patch.origin
            center_radial = center_relative - patch.axis * float(
                center_relative @ patch.axis
            )
            expected = center_radial / max(float(np.linalg.norm(center_radial)), 1e-15)
        else:
            relative = points - patch.center
            axial = relative @ patch.axis
            radial_vectors = relative - np.outer(axial, patch.axis)
            radial = np.linalg.norm(radial_vectors, axis=1)
            errors = np.abs(
                np.hypot(radial - patch.major_radius, axial) - patch.minor_radius
            )
            center_relative = center - patch.center
            center_axial = float(center_relative @ patch.axis)
            center_planar = center_relative - center_axial * patch.axis
            center_planar /= max(float(np.linalg.norm(center_planar)), 1e-15)
            ring = patch.center + patch.major_radius * center_planar
            expected = center - ring
            expected /= max(float(np.linalg.norm(expected)), 1e-15)
        maximum_error = float(np.max(errors))
        alignment = abs(float(normal @ expected))
        angle = math.degrees(math.acos(float(np.clip(alignment, -1.0, 1.0))))
        if maximum_error > distance_tolerance * 3 or angle > normal_tolerance_degrees * 2:
            return math.inf
        return maximum_error / distance_tolerance + 0.25 * angle / max(
            normal_tolerance_degrees,
            1e-9,
        )

    for _ in range(3):
        changed = 0
        updated = labels.copy()
        differing = labels[adjacency[:, 0]] != labels[adjacency[:, 1]]
        boundary_faces = np.unique(adjacency[differing])
        for raw_face_index in boundary_faces:
            face_index = int(raw_face_index)
            adjacent = neighbors[face_index]
            candidate_ids = {
                str(labels[face_index]),
                *(str(labels[value]) for value in adjacent),
            }
            candidate_ids.intersection_update(supported)
            if len(candidate_ids) < 2:
                continue
            energies: list[tuple[float, str]] = []
            for patch_id in candidate_ids:
                unary = fit_energy(face_index, supported[patch_id])
                if not math.isfinite(unary):
                    continue
                disagreement = sum(labels[value] != patch_id for value in adjacent)
                energies.append((unary + 0.45 * disagreement, patch_id))
            if not energies:
                continue
            current_id = str(labels[face_index])
            current = next(
                (energy for energy, patch_id in energies if patch_id == current_id),
                math.inf,
            )
            best_energy, best_id = min(energies)
            if best_id != current_id and best_energy + 0.15 < current:
                updated[face_index] = best_id
                changed += 1
        labels = updated
        if changed == 0:
            break
    return labels


def _refresh_planar_patch_geometry(mesh: object, patch: PlanarPatch) -> None:
    patch.x_direction, patch.y_direction = _plane_basis(patch.normal)
    patch.boundary_loops_3d = _boundary_loops(mesh, patch.face_indices)
    patch.boundary_loops = [
        np.column_stack(
            (
                (loop - patch.origin) @ patch.x_direction,
                (loop - patch.origin) @ patch.y_direction,
            )
        )
        for loop in patch.boundary_loops_3d
    ]
    polygons = [
        Polygon(loop).buffer(0)
        for loop in patch.boundary_loops
        if len(loop) >= 4 and Polygon(loop).area > 0
    ]
    patch.polygon = max(polygons, key=lambda polygon: polygon.area) if polygons else None


def _regularize_plane_relations(
    mesh: object,
    patches: list[PlanarPatch],
    distance_tolerance: float,
    normal_tolerance_degrees: float,
) -> int:
    """Perfect high-confidence plane relations without decimal rounding.

    Parallel and perpendicular normals are made exact only if every supporting
    mesh node remains inside the reconstruction tolerance.  Repeated offsets
    are snapped with bounded rational ratios (for example, exactly 1/3), never
    by rounding their decimal coefficients.
    """

    if len(patches) < 2:
        return 0
    relation_angle = math.radians(
        min(0.75, max(0.1, normal_tolerance_degrees * 0.1))
    )
    relation_fit_tolerance = max(distance_tolerance * 0.1, 1e-8)
    parent = list(range(len(patches)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def join(first: int, second: int) -> None:
        first_root, second_root = root(first), root(second)
        if first_root != second_root:
            parent[second_root] = first_root

    for first in range(len(patches)):
        for second in range(first + 1, len(patches)):
            alignment = abs(float(patches[first].normal @ patches[second].normal))
            if math.acos(float(np.clip(alignment, -1.0, 1.0))) <= relation_angle:
                join(first, second)
    groups: list[list[int]] = []
    for group_root in sorted({root(index) for index in range(len(patches))}):
        groups.append(
            [index for index in range(len(patches)) if root(index) == group_root]
        )

    relation_count = 0

    def apply_direction(indices: list[int], direction: np.ndarray) -> bool:
        proposals: list[tuple[PlanarPatch, np.ndarray, np.ndarray, float, float]] = []
        direction = _canonical_direction(direction)
        for index in indices:
            patch = patches[index]
            normal = direction if float(direction @ patch.normal) >= 0 else -direction
            points = np.asarray(mesh.vertices[patch.vertex_indices], dtype=float)
            offset = float(np.median(points @ normal))
            errors = np.abs(points @ normal - offset)
            maximum = float(np.max(errors))
            if maximum > relation_fit_tolerance:
                return False
            origin = patch.origin + normal * (offset - float(patch.origin @ normal))
            proposals.append(
                (
                    patch,
                    normal,
                    origin,
                    float(np.sqrt(np.mean(errors**2))),
                    maximum,
                )
            )
        for patch, normal, origin, rms_error, max_error in proposals:
            patch.normal = normal
            patch.origin = origin
            patch.rms_error = rms_error
            patch.max_error = max_error
            _refresh_planar_patch_geometry(mesh, patch)
        return True

    # Area weighting prevents a tiny chamfer from rotating a primary datum.
    for indices in groups:
        if len(indices) < 2:
            continue
        covariance = sum(
            patch.area * np.outer(patch.normal, patch.normal)
            for patch in (patches[index] for index in indices)
        )
        _, vectors = np.linalg.eigh(covariance)
        if apply_direction(indices, vectors[:, -1]):
            relation_count += len(indices) - 1

    # Orthogonalize the less-supported direction against the stronger datum.
    group_areas = [sum(patches[index].area for index in indices) for indices in groups]
    for first in range(len(groups)):
        first_normal = patches[groups[first][0]].normal
        for second in range(first + 1, len(groups)):
            second_normal = patches[groups[second][0]].normal
            dot = float(first_normal @ second_normal)
            if abs(dot) > math.sin(relation_angle):
                continue
            fixed, moving = (
                (first, second)
                if group_areas[first] >= group_areas[second]
                else (second, first)
            )
            fixed_normal = patches[groups[fixed][0]].normal
            moving_normal = patches[groups[moving][0]].normal
            candidate = moving_normal - fixed_normal * float(
                moving_normal @ fixed_normal
            )
            if np.linalg.norm(candidate) <= 1e-12:
                continue
            if apply_direction(groups[moving], candidate):
                relation_count += 1

    # Preserve ratios such as one third exactly, but only when the inferred
    # move is tiny relative to both the fitting tolerance and total span.
    for indices in groups:
        if len(indices) < 3:
            continue
        normal = patches[indices[0]].normal
        ordered = sorted(indices, key=lambda index: float(patches[index].origin @ normal))
        low = float(patches[ordered[0]].origin @ normal)
        high = float(patches[ordered[-1]].origin @ normal)
        span = high - low
        if span <= distance_tolerance * 10:
            continue
        movement_limit = min(distance_tolerance * 0.05, span * 1e-4)
        for index in ordered[1:-1]:
            patch = patches[index]
            current = float(patch.origin @ normal)
            ratio = (current - low) / span
            rational = Fraction(ratio).limit_denominator(12)
            target = low + span * (rational.numerator / rational.denominator)
            movement = target - current
            if abs(movement) > movement_limit:
                continue
            points = np.asarray(mesh.vertices[patch.vertex_indices], dtype=float)
            errors = np.abs(points @ normal - target)
            if float(np.max(errors)) > relation_fit_tolerance:
                continue
            patch.origin = patch.origin + normal * movement
            patch.rms_error = float(np.sqrt(np.mean(errors**2)))
            patch.max_error = float(np.max(errors))
            _refresh_planar_patch_geometry(mesh, patch)
            relation_count += 1
    return relation_count


def _regularize_axis_relations(
    mesh: object,
    planes: list[PlanarPatch],
    patches: list[CylindricalPatch | ConicalPatch | ToroidalPatch],
    distance_tolerance: float,
    normal_tolerance_degrees: float,
) -> int:
    """Snap near-parallel primitive axes to validated datum directions.

    Independent least-squares fits can leave nominally coaxial holes and
    chamfers a few millionths of a radian apart. Those tiny discrepancies are
    geometrically harmless but create different p-curves at their shared
    circles. A direction is made exact only when every supporting node remains
    well inside the fitting tolerance after the change.
    """

    if not planes or not patches:
        return 0
    relation_angle = math.radians(
        min(0.25, max(0.05, normal_tolerance_degrees * 0.05))
    )
    fit_limit = max(distance_tolerance * 0.1, 1e-8)
    datum_directions = [
        _canonical_direction(np.asarray(plane.normal, dtype=float))
        for plane in planes
    ]
    changed = 0
    for patch in patches:
        current = _canonical_direction(np.asarray(patch.axis, dtype=float))
        candidate = max(
            datum_directions,
            key=lambda direction: abs(float(direction @ current)),
        )
        alignment = abs(float(candidate @ current))
        angular_difference = math.acos(float(np.clip(alignment, -1.0, 1.0)))
        candidate = candidate if float(candidate @ patch.axis) >= 0 else -candidate
        points = np.asarray(mesh.vertices[patch.vertex_indices], dtype=float)
        if isinstance(patch, ConicalPatch) and angular_difference <= math.radians(2.0):
            # A narrow cone sector is poorly conditioned for an unconstrained
            # axis fit. Refit it against a nearby planar datum, then prefer a
            # simple rational semi-angle only when the complete node field
            # independently validates that relation.
            first, second = _plane_basis(candidate)
            u, v, z = points @ first, points @ second, points @ candidate
            initial = np.asarray(
                [
                    float(patch.apex @ first),
                    float(patch.apex @ second),
                    float(patch.apex @ candidate),
                    math.log(max(math.tan(patch.semi_angle), 0.005)),
                ]
            )

            def cone_residual(
                parameters: np.ndarray,
                local_u: np.ndarray = u,
                local_v: np.ndarray = v,
                local_z: np.ndarray = z,
            ) -> np.ndarray:
                center_u, center_v, apex_z, log_tangent = parameters
                tangent = math.exp(float(log_tangent))
                return np.hypot(local_u - center_u, local_v - center_v) - np.abs(
                    local_z - apex_z
                ) * tangent

            relation = least_squares(
                cone_residual,
                initial,
                loss="soft_l1",
                f_scale=distance_tolerance,
                max_nfev=120,
            )
            if relation.success:
                center_u, center_v, apex_z, log_tangent = relation.x
                tangent = math.exp(float(log_tangent))
                fitted_angle = math.atan(tangent)
                rational = Fraction(fitted_angle / math.pi).limit_denominator(24)
                rational_angle = math.pi * rational.numerator / rational.denominator
                if (
                    math.radians(0.5) <= rational_angle <= math.radians(87.0)
                    and abs(rational_angle - fitted_angle) <= math.radians(0.05)
                ):
                    fixed_tangent = math.tan(rational_angle)

                    def fixed_angle_residual(
                        parameters: np.ndarray,
                        local_u: np.ndarray = u,
                        local_v: np.ndarray = v,
                        local_z: np.ndarray = z,
                        local_tangent: float = fixed_tangent,
                    ) -> np.ndarray:
                        fixed_u, fixed_v, fixed_apex_z = parameters
                        return np.hypot(local_u - fixed_u, local_v - fixed_v) - np.abs(
                            local_z - fixed_apex_z
                        ) * local_tangent

                    fixed = least_squares(
                        fixed_angle_residual,
                        np.asarray([center_u, center_v, apex_z]),
                        loss="soft_l1",
                        # Once a datum axis and rational angle are selected,
                        # estimate their location from the high-consensus node
                        # field instead of letting a handful of chart-boundary
                        # outliers move a nominally tangent surface.
                        f_scale=max(distance_tolerance * 1e-5, 1e-9),
                        max_nfev=80,
                    )
                    if fixed.success:
                        center_u, center_v, apex_z = fixed.x
                        tangent = fixed_tangent
                        fitted_angle = rational_angle
                signed_errors = np.hypot(u - center_u, v - center_v) - np.abs(
                    z - apex_z
                ) * tangent
                rms_error, max_error = _fit_statistics(signed_errors)
                centers = np.asarray(mesh.triangles_center[patch.face_indices], dtype=float)
                normals = np.asarray(mesh.face_normals[patch.face_indices], dtype=float)
                center_line = center_u * first + center_v * second
                center_radial = (
                    centers
                    - np.outer(centers @ candidate, candidate)
                    - center_line
                )
                radial_lengths = np.linalg.norm(center_radial, axis=1)
                radial_unit = center_radial / np.maximum(
                    radial_lengths[:, None],
                    1e-15,
                )
                side = np.sign(centers @ candidate - apex_z)
                expected = radial_unit - side[:, None] * tangent * candidate
                expected /= np.maximum(np.linalg.norm(expected, axis=1)[:, None], 1e-15)
                normal_error = _normal_error_degrees(normals, expected)
                if (
                    max_error <= fit_limit
                    and rms_error <= fit_limit * 0.5
                    and normal_error <= normal_tolerance_degrees
                ):
                    patch.apex = center_line + candidate * apex_z
                    patch.axis = candidate
                    patch.semi_angle = fitted_angle
                    axial = (points - patch.apex) @ candidate
                    patch.start, patch.end = float(np.min(axial)), float(np.max(axial))
                    patch.rms_error = rms_error
                    patch.max_error = max_error
                    patch.normal_error_degrees = normal_error
                    changed += 1
                    continue
        if angular_difference > relation_angle:
            continue
        if isinstance(patch, CylindricalPatch):
            relative = points - patch.origin
            axial = relative @ candidate
            radial = relative - np.outer(axial, candidate)
            errors = np.abs(np.linalg.norm(radial, axis=1) - patch.radius)
        elif isinstance(patch, ConicalPatch):
            relative = points - patch.apex
            axial = relative @ candidate
            radial = relative - np.outer(axial, candidate)
            errors = np.abs(
                np.linalg.norm(radial, axis=1)
                - np.abs(axial) * math.tan(patch.semi_angle)
            )
        else:
            errors = np.abs(
                _torus_errors(
                    points,
                    patch.center,
                    candidate,
                    patch.major_radius,
                    patch.minor_radius,
                )
            )
        rms_error, max_error = _fit_statistics(errors)
        if max_error > fit_limit or rms_error > fit_limit * 0.5:
            continue
        patch.axis = candidate
        patch.rms_error = rms_error
        patch.max_error = max_error
        if isinstance(patch, CylindricalPatch):
            axial = (points - patch.origin) @ candidate
            patch.start, patch.end = float(np.min(axial)), float(np.max(axial))
        changed += 1
    return changed


def _absorb_cylindrical_chord_fragments(
    mesh: object,
    planar_patches: list[PlanarPatch],
    cylindrical_patches: list[CylindricalPatch],
    face_patch_ids: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
) -> None:
    """Return tiny tangent chords to an adjacent analytic cylinder.

    A coarse circular tessellation can leave one or two triangles as an exact
    plane even though all their nodes and normals continue the neighboring
    cylinder.  Treating that chord as a CAD plane creates artificial trim
    edges and prevents sewing.  The node, normal, tangent, size, and adjacency
    gates below deliberately exclude planar end caps and real planar walls.
    """

    if not planar_patches or not cylindrical_patches:
        return
    cylinders_by_id = {patch.patch_id: patch for patch in cylindrical_patches}
    neighbors: dict[str, set[str]] = {}
    for first_face, second_face in np.asarray(mesh.face_adjacency, dtype=np.int64):
        first_id = str(face_patch_ids[int(first_face)])
        second_id = str(face_patch_ids[int(second_face)])
        if first_id == second_id:
            continue
        neighbors.setdefault(first_id, set()).add(second_id)
        neighbors.setdefault(second_id, set()).add(first_id)

    retained: list[PlanarPatch] = []
    for plane in planar_patches:
        adjacent = [
            cylinders_by_id[patch_id]
            for patch_id in neighbors.get(plane.patch_id, set())
            if patch_id in cylinders_by_id
        ]
        if len(plane.face_indices) > 4 or len(adjacent) != 1:
            retained.append(plane)
            continue
        cylinder = adjacent[0]
        if abs(float(plane.normal @ cylinder.axis)) > math.sin(math.radians(1.0)):
            retained.append(plane)
            continue

        points = np.asarray(mesh.vertices[plane.vertex_indices], dtype=float)
        relative = points - cylinder.origin
        axial = relative @ cylinder.axis
        radial = relative - np.outer(axial, cylinder.axis)
        node_errors = np.abs(np.linalg.norm(radial, axis=1) - cylinder.radius)
        strict_distance = max(distance_tolerance * 0.1, 1e-8)
        if len(node_errors) == 0 or float(np.max(node_errors)) > strict_distance:
            retained.append(plane)
            continue

        centers = np.asarray(mesh.triangles_center[plane.face_indices], dtype=float)
        center_relative = centers - cylinder.origin
        center_axial = center_relative @ cylinder.axis
        expected = center_relative - np.outer(center_axial, cylinder.axis)
        expected_norms = np.linalg.norm(expected, axis=1)
        if np.any(expected_norms <= 1e-12):
            retained.append(plane)
            continue
        expected /= expected_norms[:, None]
        actual = np.asarray(mesh.face_normals[plane.face_indices], dtype=float)
        alignments = np.abs(np.sum(expected * actual, axis=1))
        normal_errors = np.degrees(np.arccos(np.clip(alignments, -1.0, 1.0)))
        if float(np.max(normal_errors)) > min(normal_tolerance_degrees, 1.0):
            retained.append(plane)
            continue

        cylinder.face_indices = np.unique(
            np.concatenate((cylinder.face_indices, plane.face_indices))
        ).astype(np.int64)
        cylinder.vertex_indices = np.unique(mesh.faces[cylinder.face_indices])
        cylinder.area = float(np.sum(mesh.area_faces[cylinder.face_indices]))
        cylinder.boundary_loops = _boundary_loops(mesh, cylinder.face_indices)
        cylinder_points = np.asarray(
            mesh.vertices[cylinder.vertex_indices],
            dtype=float,
        )
        cylinder_axial = (cylinder_points - cylinder.origin) @ cylinder.axis
        cylinder.start = float(np.min(cylinder_axial))
        cylinder.end = float(np.max(cylinder_axial))
        face_patch_ids[plane.face_indices] = cylinder.patch_id

    planar_patches[:] = retained


def _absorb_tangent_planar_slivers(
    mesh: object,
    planar_patches: list[PlanarPatch],
    freeform_patches: list[FreeformPatch],
    face_patch_ids: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
) -> None:
    """Return microscopic tangent triangles to a smooth residual chart.

    A triangle is mathematically planar in isolation, so the primitive pass
    can peel a single tessellation cell out of an otherwise smooth transition.
    Absorb only source-scale slivers whose shared-edge normals are tangent and
    whose union remains a single-valued graph. Real chamfers, caps, folds, and
    small planar features therefore keep their own support.
    """

    if not planar_patches or not freeform_patches:
        return
    freeforms_by_id = {patch.patch_id: patch for patch in freeform_patches}
    adjacency: dict[str, dict[str, list[float]]] = {}
    normals = np.asarray(mesh.face_normals, dtype=float)
    for raw_first, raw_second in np.asarray(mesh.face_adjacency, dtype=np.int64):
        first, second = int(raw_first), int(raw_second)
        first_id = str(face_patch_ids[first])
        second_id = str(face_patch_ids[second])
        if first_id == second_id:
            continue
        angle = math.degrees(
            math.acos(
                float(
                    np.clip(
                        abs(float(normals[first] @ normals[second])),
                        -1.0,
                        1.0,
                    )
                )
            )
        )
        adjacency.setdefault(first_id, {}).setdefault(second_id, []).append(angle)
        adjacency.setdefault(second_id, {}).setdefault(first_id, []).append(angle)

    area_limit = max((distance_tolerance * 10) ** 2, 1e-14)
    angle_limit = min(normal_tolerance_degrees, 5.0)
    retained: list[PlanarPatch] = []
    for plane in planar_patches:
        if len(plane.face_indices) > 2 or plane.area > area_limit:
            retained.append(plane)
            continue
        candidates = [
            (max(angles), freeforms_by_id[patch_id])
            for patch_id, angles in adjacency.get(plane.patch_id, {}).items()
            if patch_id in freeforms_by_id and angles and max(angles) <= angle_limit
        ]
        if not candidates:
            retained.append(plane)
            continue
        # When both sides are smooth enough, perturb the larger chart. This
        # keeps a microscopic connector from materially changing the PCA frame
        # and parameter boundary of a small transition sheet.
        _, target = min(candidates, key=lambda item: (-item[1].area, item[0]))
        combined = np.unique(
            np.r_[target.face_indices, plane.face_indices]
        ).astype(np.int64)
        vertices = np.unique(mesh.faces[combined])
        points = np.asarray(mesh.vertices[vertices], dtype=float)
        try:
            _, _, basis = np.linalg.svd(
                points - np.mean(points, axis=0),
                full_matrices=False,
            )
        except np.linalg.LinAlgError:
            retained.append(plane)
            continue
        coordinates = (points - np.mean(points, axis=0)) @ basis[:2].T
        local = np.full(len(mesh.vertices), -1, dtype=np.int64)
        local[vertices] = np.arange(len(vertices), dtype=np.int64)
        triangles = coordinates[local[np.asarray(mesh.faces[combined], dtype=np.int64)]]
        signed_area = (
            (triangles[:, 1, 0] - triangles[:, 0, 0])
            * (triangles[:, 2, 1] - triangles[:, 0, 1])
            - (triangles[:, 1, 1] - triangles[:, 0, 1])
            * (triangles[:, 2, 0] - triangles[:, 0, 0])
        )
        area_floor = max(float(np.prod(np.ptp(coordinates, axis=0))) * 1e-14, 1e-14)
        if not (
            np.all(signed_area > area_floor)
            or np.all(signed_area < -area_floor)
        ):
            retained.append(plane)
            continue
        target.face_indices = combined
        target.vertex_indices = vertices
        target.area = float(np.sum(mesh.area_faces[combined]))
        target.boundary_loops = _boundary_loops(mesh, combined)
        face_patch_ids[plane.face_indices] = target.patch_id
    planar_patches[:] = retained


def _recover_cylinders_from_planar_strips(
    data: MeshData,
    planar_patches: list[PlanarPatch],
    face_patch_ids: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
    first_index: int,
) -> list[CylindricalPatch]:
    """Recover short circular arcs split into narrow planar STL strips.

    Planarity is evaluated before sweep semantics, so a coarsely tessellated
    extrusion can arrive here as a chain of exact quadrilateral planes. Group
    only adjacent tiny planes, infer their common translation, then recover
    circular profile runs. Straight and arbitrary polygonal runs remain planes;
    a cylinder is emitted only after exact node and normal validation.
    """

    tiny = {
        patch.patch_id: patch
        for patch in planar_patches
        if len(patch.face_indices) <= 4
    }
    if len(tiny) < 3:
        return []
    parent = {patch_id: patch_id for patch_id in tiny}

    def root(patch_id: str) -> str:
        while parent[patch_id] != patch_id:
            parent[patch_id] = parent[parent[patch_id]]
            patch_id = parent[patch_id]
        return patch_id

    def join(first: str, second: str) -> None:
        first_root, second_root = root(first), root(second)
        if first_root != second_root:
            parent[second_root] = first_root

    mesh = data.mesh
    for first_face, second_face in np.asarray(mesh.face_adjacency, dtype=np.int64):
        first_id = str(face_patch_ids[int(first_face)])
        second_id = str(face_patch_ids[int(second_face)])
        if first_id in tiny and second_id in tiny:
            join(first_id, second_id)

    groups: dict[str, list[PlanarPatch]] = {}
    for patch_id, patch in tiny.items():
        groups.setdefault(root(patch_id), []).append(patch)

    detected: list[tuple[_AnalyticFit, np.ndarray]] = []
    for group in groups.values():
        if len(group) < 3:
            continue
        faces = np.unique(
            np.concatenate([patch.face_indices for patch in group])
        ).astype(np.int64)
        extrusion = _fit_linear_extrusion_patch(
            mesh,
            faces,
            distance_tolerance,
            normal_tolerance_degrees,
            0,
        )
        if extrusion is None:
            continue
        detected.extend(
            _extrusion_profile_cylinder_regions(
                data,
                extrusion,
                distance_tolerance,
                normal_tolerance_degrees,
            )
        )
    if not detected:
        return []

    claimed: set[int] = set()
    cylinders: list[CylindricalPatch] = []
    for fit, component in sorted(
        detected,
        key=lambda item: -float(np.sum(mesh.area_faces[item[1]])),
    ):
        if any(int(face) in claimed for face in component):
            continue
        cylinder = _make_analytic_patch(
            mesh,
            fit,
            component,
            first_index + len(cylinders),
        )
        if not isinstance(cylinder, CylindricalPatch):
            continue
        cylinders.append(cylinder)
        claimed.update(int(face) for face in component)
        face_patch_ids[component] = cylinder.patch_id

    if not claimed:
        return []
    retained: list[PlanarPatch] = []
    for plane in planar_patches:
        remaining = np.asarray(
            [face for face in plane.face_indices if int(face) not in claimed],
            dtype=np.int64,
        )
        if len(remaining) == 0:
            continue
        if len(remaining) != len(plane.face_indices):
            plane.face_indices = remaining
            plane.vertex_indices = np.unique(mesh.faces[remaining])
            plane.area = float(np.sum(mesh.area_faces[remaining]))
            _refresh_planar_patch_geometry(mesh, plane)
        retained.append(plane)
    planar_patches[:] = retained
    return cylinders


def _rebalance_tangent_cylinder_torus_boundaries(
    mesh: object,
    cylindrical_patches: list[CylindricalPatch],
    toroidal_patches: list[ToroidalPatch],
    face_patch_ids: np.ndarray,
    distance_tolerance: float,
    normal_tolerance_degrees: float,
) -> None:
    """Give ambiguous tangent rows to the analytic support they fit best."""

    if not cylindrical_patches or not toroidal_patches:
        return
    adjacency = np.asarray(mesh.face_adjacency, dtype=np.int64)
    neighbors: list[list[int]] = [[] for _ in range(len(mesh.faces))]
    adjacent_ids: set[tuple[str, str]] = set()
    for raw_first, raw_second in adjacency:
        first, second = int(raw_first), int(raw_second)
        neighbors[first].append(second)
        neighbors[second].append(first)
        first_id, second_id = str(face_patch_ids[first]), str(face_patch_ids[second])
        if first_id != second_id:
            adjacent_ids.add((first_id, second_id))
            adjacent_ids.add((second_id, first_id))

    strict_error = max(distance_tolerance * 0.1, 1e-8)
    for cylinder in cylindrical_patches:
        for torus in toroidal_patches:
            if (cylinder.patch_id, torus.patch_id) not in adjacent_ids:
                continue
            if abs(float(cylinder.axis @ torus.axis)) < math.cos(math.radians(0.5)):
                continue
            center_relative = torus.center - cylinder.origin
            center_radial = center_relative - cylinder.axis * float(
                center_relative @ cylinder.axis
            )
            tangent_radii = (
                torus.major_radius - torus.minor_radius,
                torus.major_radius + torus.minor_radius,
            )
            if (
                float(np.linalg.norm(center_radial)) > distance_tolerance * 2
                or min(abs(cylinder.radius - value) for value in tangent_radii)
                > distance_tolerance * 2
            ):
                continue

            faces = np.asarray(cylinder.face_indices, dtype=np.int64)
            points = np.asarray(mesh.vertices[mesh.faces[faces]], dtype=float)
            cylinder_relative = points - cylinder.origin
            cylinder_axial = np.einsum(
                "fvi,i->fv",
                cylinder_relative,
                cylinder.axis,
            )
            cylinder_radial = (
                cylinder_relative
                - cylinder_axial[:, :, None] * cylinder.axis
            )
            cylinder_error = np.max(
                np.abs(np.linalg.norm(cylinder_radial, axis=2) - cylinder.radius),
                axis=1,
            )
            torus_relative = points - torus.center
            torus_axial = np.einsum("fvi,i->fv", torus_relative, torus.axis)
            torus_radial = (
                torus_relative - torus_axial[:, :, None] * torus.axis
            )
            torus_error = np.max(
                np.abs(
                    np.hypot(
                        np.linalg.norm(torus_radial, axis=2) - torus.major_radius,
                        torus_axial,
                    )
                    - torus.minor_radius
                ),
                axis=1,
            )
            candidate_faces = set(
                int(face)
                for face, cylinder_value, torus_value in zip(
                    faces,
                    cylinder_error,
                    torus_error,
                    strict=True,
                )
                if torus_value <= strict_error
                and torus_value * 4 < max(cylinder_value, 1e-12)
            )
            if not candidate_faces:
                continue

            torus_faces = set(int(face) for face in torus.face_indices)
            frontier = [
                face
                for face in candidate_faces
                if any(neighbor in torus_faces for neighbor in neighbors[face])
            ]
            moved: set[int] = set(frontier)
            while frontier:
                face = frontier.pop()
                for neighbor in neighbors[face]:
                    if neighbor in candidate_faces and neighbor not in moved:
                        moved.add(neighbor)
                        frontier.append(neighbor)
            if not moved:
                continue
            moved_indices = np.asarray(sorted(moved), dtype=np.int64)
            # The residual gate uses nodes; independently require the face
            # normals to agree with the torus before changing ownership.
            torus_fit = _AnalyticFit(
                kind="torus",
                parameters={
                    "center": torus.center,
                    "axis": torus.axis,
                    "major_radius": torus.major_radius,
                    "minor_radius": torus.minor_radius,
                },
                rms_error=torus.rms_error,
                max_error=torus.max_error,
                normal_error_degrees=torus.normal_error_degrees,
                score=0.0,
            )
            confirmed = set(
                int(face)
                for face in _analytic_inlier_faces(
                    torus_fit,
                    mesh,
                    moved_indices,
                    distance_tolerance,
                    normal_tolerance_degrees,
                )
            )
            moved = {face for face in moved if face in confirmed}
            if not moved:
                continue
            moved_indices = np.asarray(sorted(moved), dtype=np.int64)
            cylinder.face_indices = np.asarray(
                [face for face in cylinder.face_indices if int(face) not in moved],
                dtype=np.int64,
            )
            cylinder.vertex_indices = np.unique(mesh.faces[cylinder.face_indices])
            cylinder.area = float(np.sum(mesh.area_faces[cylinder.face_indices]))
            cylinder.boundary_loops = _boundary_loops(mesh, cylinder.face_indices)
            cylinder_points = np.asarray(
                mesh.vertices[cylinder.vertex_indices],
                dtype=float,
            )
            axial = (cylinder_points - cylinder.origin) @ cylinder.axis
            cylinder.start, cylinder.end = float(np.min(axial)), float(np.max(axial))
            torus.face_indices = np.unique(
                np.r_[torus.face_indices, moved_indices]
            ).astype(np.int64)
            torus.vertex_indices = np.unique(mesh.faces[torus.face_indices])
            torus.area = float(np.sum(mesh.area_faces[torus.face_indices]))
            torus.boundary_loops = _boundary_loops(mesh, torus.face_indices)
            face_patch_ids[moved_indices] = torus.patch_id


def _build_adjacency(
    mesh: object,
    face_patch_ids: np.ndarray,
    patches: list[SurfacePatch],
    tolerance: float,
) -> list[SurfaceAdjacency]:
    grouped_edges: dict[tuple[str, str], list[np.ndarray]] = {}
    for faces, edge in zip(
        np.asarray(mesh.face_adjacency, dtype=np.int64),
        np.asarray(mesh.face_adjacency_edges, dtype=np.int64),
        strict=True,
    ):
        first_id = face_patch_ids[int(faces[0])]
        second_id = face_patch_ids[int(faces[1])]
        if not first_id or not second_id or first_id == second_id:
            continue
        pair = tuple(sorted((str(first_id), str(second_id))))
        grouped_edges.setdefault(pair, []).append(edge)
    curves_by_pair: dict[tuple[str, str], list[np.ndarray]] = {}
    for pair, edges in grouped_edges.items():
        curves_by_pair[pair] = [
            np.asarray(mesh.vertices[chain], dtype=float)
            for chain in _edge_chains(np.asarray(edges, dtype=np.int64))
        ]

    # Independently tessellated STEP faces often reach the same geometric seam
    # with different vertex counts, so no triangle edge exists across it. Match
    # boundary polylines by point-to-segment distance and prefer the more
    # complete geometric record over the partial topological one.
    patch_loops = {
        patch.patch_id: [
            np.asarray(loop, dtype=float)
            for loop in getattr(patch, "boundary_loops_3d", patch.boundary_loops)
            if len(loop) >= 2
        ]
        for patch in patches
    }
    patch_bounds = {
        patch_id: (
            np.min(np.vstack(loops), axis=0),
            np.max(np.vstack(loops), axis=0),
        )
        for patch_id, loops in patch_loops.items()
        if loops
    }
    for first_index, first in enumerate(patches):
        first_bounds = patch_bounds.get(first.patch_id)
        if first_bounds is None:
            continue
        for second in patches[first_index + 1 :]:
            geometric: list[np.ndarray] = []
            second_loops = patch_loops.get(second.patch_id, [])
            if not second_loops:
                continue
            pair = tuple(sorted((first.patch_id, second.patch_id)))
            # A shared triangle-edge chain is exact topology. Proximity-based
            # matching exists for independently tessellated, nonconformal
            # seams and must not replace an already known watertight chain.
            if pair in curves_by_pair:
                continue
            second_bounds = patch_bounds[second.patch_id]
            if np.any(first_bounds[1] + tolerance * 1.5 < second_bounds[0]) or np.any(
                second_bounds[1] + tolerance * 1.5 < first_bounds[0]
            ):
                continue
            for loop in patch_loops[first.patch_id]:
                distances = np.full(len(loop), math.inf, dtype=float)
                for other in second_loops:
                    distances = np.minimum(
                        distances,
                        _point_to_polyline_distances(loop, other),
                    )
                for run in _cyclic_true_runs(distances <= tolerance * 1.5):
                    if len(run) >= 2:
                        geometric.append(loop[run])
            if not geometric:
                continue
            curves_by_pair[pair] = geometric

    result: list[SurfaceAdjacency] = []
    for (first_id, second_id), curves in sorted(curves_by_pair.items()):
        result.append(
            SurfaceAdjacency(
                first_patch_id=first_id,
                second_patch_id=second_id,
                boundary_curves=curves,
            )
        )
    return result


def detect_surface_graph(
    data: MeshData,
    *,
    distance_tolerance: float | None = None,
    normal_tolerance_degrees: float = 5.0,
    smooth_angle_degrees: float = 30.0,
    minimum_area_ratio: float = 0.0002,
) -> SurfaceGraph:
    """Recognize elementary analytic surfaces and retain all residual patches.

    Surface equations are solved against unique mesh nodes. Triangle topology is
    used only to establish connected neighborhoods and estimate orientation; a
    triangle is never itself considered a candidate CAD surface. Locally planar
    facets that continue through a smooth node field remain grouped so their
    combined nodes compete as cylinders, cones, spheres and tori. Everything
    else becomes one node-constrained freeform region, and every input face is
    assigned to exactly one fitted surface.
    """

    mesh = data.mesh
    if not hasattr(mesh, "face_adjacency") or len(mesh.faces) == 0:
        return SurfaceGraph()
    tolerance = (
        float(distance_tolerance)
        if distance_tolerance is not None
        else max(data.diagonal * 0.0003, 1e-7)
    )
    cache = getattr(data, "section_cache", None)
    cache_key = (
        "surface-graph-v47:"
        f"{tolerance:.12g}:"
        f"{normal_tolerance_degrees:.8g}:"
        f"{smooth_angle_degrees:.8g}:"
        f"{minimum_area_ratio:.8g}",
        0.0,
    )
    if isinstance(cache, dict) and cache_key in cache:
        return cache[cache_key]
    minimum_area = max(
        float(mesh.area) * minimum_area_ratio,
        data.diagonal**2 * 1e-7,
    )
    if len(mesh.faces) >= 60_000:
        coplanar_components = _coplanar_components(mesh, tolerance)
        planar_patches = _detect_planar_patches(
            data,
            coplanar_components,
            tolerance,
            minimum_area,
            smooth_angle_degrees,
        )
    else:
        local_components = _locally_coplanar_components(mesh, tolerance)
        local_planes = _detect_planar_patches(
            data,
            local_components,
            tolerance,
            minimum_area,
            smooth_angle_degrees,
        )
        strict_components = _coplanar_components(mesh, tolerance)
        strict_planes = _detect_planar_patches(
            data,
            strict_components,
            tolerance,
            minimum_area,
            smooth_angle_degrees,
        )
        bounded_strict_gain = (
            len(local_planes) < len(strict_planes)
            <= max(len(local_planes) * 2, len(local_planes) + 2)
        )
        if bounded_strict_gain:
            coplanar_components, planar_patches = strict_components, strict_planes
        else:
            coplanar_components, planar_patches = local_components, local_planes
    face_patch_ids = np.full(len(mesh.faces), "", dtype=object)
    for patch in planar_patches:
        face_patch_ids[patch.face_indices] = patch.patch_id

    available = face_patch_ids == ""
    all_components = _smooth_components(
        mesh,
        available,
        smooth_angle_degrees,
    )
    components = [
        face_indices
        for face_indices in all_components
        if np.any(available[face_indices])
    ]
    recognized: dict[
        str,
        list[CylindricalPatch | ConicalPatch | SphericalPatch | ToroidalPatch],
    ] = {"cylinder": [], "cone": [], "sphere": [], "torus": []}
    extrusion_patches: list[LinearExtrusionPatch] = []
    freeform_patches: list[FreeformPatch] = []
    counters = {"cylinder": 0, "cone": 0, "sphere": 0, "torus": 0}
    for face_indices in sorted(
        components,
        key=lambda indices: -float(np.sum(mesh.area_faces[indices])),
    ):
        face_indices = face_indices[available[face_indices]]
        if len(face_indices) == 0:
            continue
        area = float(np.sum(mesh.area_faces[face_indices]))
        fit = (
            _fit_component(
                data,
                face_indices,
                tolerance,
                normal_tolerance_degrees,
            )
            if len(face_indices) >= 8 and area >= minimum_area
            else None
        )
        if fit is None:
            extrusion = (
                _fit_linear_extrusion_patch(
                    mesh,
                    face_indices,
                    tolerance,
                    normal_tolerance_degrees,
                    len(extrusion_patches) + 1,
                )
                if area >= minimum_area
                else None
            )
            if extrusion is not None:
                extrusion_patches.append(extrusion)
                face_patch_ids[face_indices] = extrusion.patch_id
                continue
            # Do not parameterize residuals into bounded-normal charts before
            # primitive extraction.  A tangent CAD chain may contain a crease
            # elsewhere, and charting the entire component here hides complete
            # cylinders and tori from the later point-consensus passes.
            for chart in [face_indices]:
                index = len(freeform_patches) + 1
                patch = FreeformPatch(
                    patch_id=f"freeform-{index:03d}",
                    face_indices=chart,
                    vertex_indices=np.unique(mesh.faces[chart]),
                    area=float(np.sum(mesh.area_faces[chart])),
                    boundary_loops=_boundary_loops(mesh, chart),
                )
                freeform_patches.append(patch)
                face_patch_ids[chart] = patch.patch_id
            continue
        else:
            counters[fit.kind] += 1
            patch = _make_analytic_patch(
                mesh,
                fit,
                face_indices,
                counters[fit.kind],
            )
            recognized[fit.kind].append(patch)
        face_patch_ids[face_indices] = patch.patch_id

    base_cylinders = [
        patch
        for patch in recognized["cylinder"]
        if isinstance(patch, CylindricalPatch)
    ]
    if freeform_patches:
        original_freeforms = freeform_patches
        for patch in original_freeforms:
            face_patch_ids[patch.face_indices] = ""
        freeform_patches, tangent_cylinders, revolution_patches = (
            _split_tangent_swept_surfaces(
                data,
                original_freeforms,
                extrusion_patches,
                base_cylinders,
                tolerance,
                normal_tolerance_degrees,
                minimum_area,
            )
        )
        base_cylinders.extend(tangent_cylinders)
        # A general revolution is a useful hypothesis, but it is less specific
        # than an elementary torus.  Keep its support unresolved until local
        # cylinder/torus consensus has had a chance to claim exact subsets.
        for revolution in revolution_patches:
            freeform_patches.append(
                FreeformPatch(
                    patch_id="",
                    face_indices=revolution.face_indices,
                    vertex_indices=revolution.vertex_indices,
                    area=revolution.area,
                    boundary_loops=revolution.boundary_loops,
                )
            )
        revolution_patches = []
        for patch in [
            *freeform_patches,
            *tangent_cylinders,
            *extrusion_patches,
        ]:
            face_patch_ids[patch.face_indices] = patch.patch_id
    else:
        revolution_patches = []

    # A provisional general extrusion can itself be a tangent chain of
    # elementary cylinders. Primitive supports are more editable and much
    # cheaper to trim than one dense swept B-spline, so peel exact local
    # cylinders before accepting the less-specific extrusion hypothesis.
    if extrusion_patches:
        original_extrusions = extrusion_patches
        for patch in original_extrusions:
            face_patch_ids[patch.face_indices] = ""
        retained_extrusions: list[LinearExtrusionPatch] = []
        next_extrusion_index = (
            max(
                (
                    int(patch.patch_id.rsplit("-", 1)[-1])
                    for patch in original_extrusions
                    if patch.patch_id.rsplit("-", 1)[-1].isdigit()
                ),
                default=0,
            )
            + 1
        )
        counters["cylinder"] = len(base_cylinders)
        for original in original_extrusions:
            detected = _extrusion_profile_cylinder_regions(
                data,
                original,
                tolerance,
                normal_tolerance_degrees,
            )
            if detected:
                claimed = {
                    int(face)
                    for _, component in detected
                    for face in component
                }
                available_residuals = np.zeros(len(mesh.faces), dtype=bool)
                available_residuals[
                    np.asarray(
                        [
                            face
                            for face in original.face_indices
                            if int(face) not in claimed
                        ],
                        dtype=np.int64,
                    )
                ] = True
                residual_components = _smooth_components(
                    mesh,
                    available_residuals,
                    30.0,
                )
            elif len(original.face_indices) >= 20:
                detected, residual_components = _extract_local_analytic_regions(
                    data,
                    original.face_indices,
                    tolerance,
                    normal_tolerance_degrees,
                    max(minimum_area * 0.25, original.area * 0.01),
                )
            else:
                detected, residual_components = [], [original.face_indices]
            if not detected:
                retained_extrusions.append(original)
                face_patch_ids[original.face_indices] = original.patch_id
                continue
            for fit, component in detected:
                counters[fit.kind] += 1
                analytic_patch = _make_analytic_patch(
                    mesh,
                    fit,
                    component,
                    counters[fit.kind],
                )
                if fit.kind == "cylinder":
                    base_cylinders.append(analytic_patch)
                else:
                    recognized[fit.kind].append(analytic_patch)
                face_patch_ids[component] = analytic_patch.patch_id
            for component in residual_components:
                if len(component) == 0:
                    continue
                residual_area = float(np.sum(mesh.area_faces[component]))
                refitted = _fit_linear_extrusion_patch(
                    mesh,
                    component,
                    tolerance,
                    normal_tolerance_degrees,
                    next_extrusion_index,
                )
                if refitted is not None:
                    next_extrusion_index += 1
                    retained_extrusions.append(refitted)
                    face_patch_ids[component] = refitted.patch_id
                    continue
                residual_patch = FreeformPatch(
                    patch_id=f"freeform-{len(freeform_patches) + 1:03d}",
                    face_indices=component,
                    vertex_indices=np.unique(mesh.faces[component]),
                    area=residual_area,
                    boundary_loops=_boundary_loops(mesh, component),
                )
                freeform_patches.append(residual_patch)
                face_patch_ids[component] = residual_patch.patch_id
        extrusion_patches = retained_extrusions

    # A smooth connected STL region can cross several tangent CAD faces.  A
    # single global primitive fit then fails even though sizeable subsets are
    # exact cylinders, cones, spheres, or tori.  Recover those subsets with
    # deterministic local-sample consensus and validate them against the full
    # node/normal field. Multi-scale samples keep the pass useful for both
    # small transition surfaces and large tangent chains.
    aggressive_local_consensus = bool(planar_patches or len(mesh.faces) >= 60_000)
    if aggressive_local_consensus and any(
        len(patch.face_indices) >= 20 for patch in freeform_patches
    ):
        original_freeforms = freeform_patches
        for patch in original_freeforms:
            face_patch_ids[patch.face_indices] = ""
        freeform_patches = []
        counters["cylinder"] = len(base_cylinders)
        for kind in ("cone", "sphere", "torus"):
            counters[kind] = len(recognized[kind])
        for original in sorted(original_freeforms, key=lambda patch: -patch.area):
            if len(original.face_indices) < 20:
                detected: list[tuple[_AnalyticFit, np.ndarray]] = []
                residual_components = [original.face_indices]
            else:
                detected, residual_components = _extract_local_analytic_regions(
                    data,
                    original.face_indices,
                    tolerance,
                    normal_tolerance_degrees,
                    max(minimum_area * 0.25, original.area * 0.01),
                )
            for fit, component in detected:
                counters[fit.kind] += 1
                analytic_patch = _make_analytic_patch(
                    mesh,
                    fit,
                    component,
                    counters[fit.kind],
                )
                if fit.kind == "cylinder":
                    base_cylinders.append(analytic_patch)
                else:
                    recognized[fit.kind].append(analytic_patch)
                face_patch_ids[component] = analytic_patch.patch_id
            for component in residual_components:
                if len(component) == 0:
                    continue
                index = len(freeform_patches) + 1
                residual_patch = FreeformPatch(
                    patch_id=f"freeform-{index:03d}",
                    face_indices=component,
                    vertex_indices=np.unique(mesh.faces[component]),
                    area=float(np.sum(mesh.area_faces[component])),
                    boundary_loops=_boundary_loops(mesh, component),
                )
                freeform_patches.append(residual_patch)
                face_patch_ids[component] = residual_patch.patch_id

    if aggressive_local_consensus and any(
        len(patch.face_indices) >= 40 for patch in freeform_patches
    ):
        original_freeforms = freeform_patches
        for patch in original_freeforms:
            face_patch_ids[patch.face_indices] = ""
        freeform_patches = []
        counters["torus"] = len(recognized["torus"])
        for original in sorted(original_freeforms, key=lambda patch: -patch.area):
            if len(original.face_indices) < 40:
                detected_tori: list[tuple[_AnalyticFit, np.ndarray]] = []
                residual_components = [original.face_indices]
            else:
                detected_tori, residual_components = _extract_local_torus_regions(
                    data,
                    original.face_indices,
                    tolerance,
                    normal_tolerance_degrees,
                    max(minimum_area * 0.25, original.area * 0.002),
                )
            for fit, component in detected_tori:
                counters["torus"] += 1
                torus_patch = _make_analytic_patch(
                    mesh,
                    fit,
                    component,
                    counters["torus"],
                )
                recognized["torus"].append(torus_patch)
                face_patch_ids[component] = torus_patch.patch_id
            for component in residual_components:
                if len(component) == 0:
                    continue
                index = len(freeform_patches) + 1
                residual_patch = FreeformPatch(
                    patch_id=f"freeform-{index:03d}",
                    face_indices=component,
                    vertex_indices=np.unique(mesh.faces[component]),
                    area=float(np.sum(mesh.area_faces[component])),
                    boundary_loops=_boundary_loops(mesh, component),
                )
                freeform_patches.append(residual_patch)
                face_patch_ids[component] = residual_patch.patch_id

    # Toroidal fillets can dominate every local neighborhood around a tangent
    # cylinder.  Once those tori have been peeled, retry cylinder consensus on
    # the exposed residual rather than permanently accepting an order-dependent
    # miss from the first analytic pass.
    if aggressive_local_consensus and freeform_patches:
        original_freeforms = freeform_patches
        for patch in original_freeforms:
            face_patch_ids[patch.face_indices] = ""
        freeform_patches = []
        counters["cylinder"] = len(base_cylinders)
        for original in sorted(original_freeforms, key=lambda patch: -patch.area):
            direct = (
                _fit_component(
                    data,
                    original.face_indices,
                    tolerance,
                    normal_tolerance_degrees,
                    allowed_kinds=("cylinder",),
                )
                if len(original.face_indices) >= 8 and original.area >= minimum_area
                else None
            )
            if direct is not None:
                detected_cylinders = [(direct, original.face_indices)]
                residual_components: list[np.ndarray] = []
            elif len(original.face_indices) >= 20:
                detected_cylinders, residual_components = (
                    _extract_local_analytic_regions(
                        data,
                        original.face_indices,
                        tolerance,
                        normal_tolerance_degrees,
                        max(minimum_area * 0.25, original.area * 0.01),
                    )
                )
            else:
                detected_cylinders = []
                residual_components = [original.face_indices]
            for fit, component in detected_cylinders:
                counters["cylinder"] += 1
                cylinder_patch = _make_analytic_patch(
                    mesh,
                    fit,
                    component,
                    counters["cylinder"],
                )
                base_cylinders.append(cylinder_patch)
                face_patch_ids[component] = cylinder_patch.patch_id
            for component in residual_components:
                if len(component) == 0:
                    continue
                index = len(freeform_patches) + 1
                residual_patch = FreeformPatch(
                    patch_id=f"freeform-{index:03d}",
                    face_indices=component,
                    vertex_indices=np.unique(mesh.faces[component]),
                    area=float(np.sum(mesh.area_faces[component])),
                    boundary_loops=_boundary_loops(mesh, component),
                )
                freeform_patches.append(residual_patch)
                face_patch_ids[component] = residual_patch.patch_id

    torus_candidates = [
        patch
        for patch in recognized["torus"]
        if isinstance(patch, ToroidalPatch)
    ]
    if len(torus_candidates) >= 2:
        for patch in torus_candidates:
            face_patch_ids[patch.face_indices] = ""
        merged_tori, rejected_torus_faces = _merge_adjacent_torus_patches(
            data,
            torus_candidates,
            tolerance,
            normal_tolerance_degrees,
        )
        recognized["torus"] = merged_tori
        for patch in merged_tori:
            face_patch_ids[patch.face_indices] = patch.patch_id
        for component in rejected_torus_faces:
            index = len(freeform_patches) + 1
            residual_patch = FreeformPatch(
                patch_id=f"freeform-{index:03d}",
                face_indices=component,
                vertex_indices=np.unique(mesh.faces[component]),
                area=float(np.sum(mesh.area_faces[component])),
                boundary_loops=_boundary_loops(mesh, component),
            )
            freeform_patches.append(residual_patch)
            face_patch_ids[component] = residual_patch.patch_id

    # Grow accepted analytic equations across any provisional revolution or
    # freeform faces that satisfy the same all-node equation.  This repairs
    # small order-dependent boundary fragments without relaxing the fit.
    residual_groups = [
        patch.face_indices
        for patch in [*revolution_patches, *freeform_patches]
        if len(patch.face_indices)
    ]
    if residual_groups:
        residual_faces = set(
            int(value) for value in np.unique(np.concatenate(residual_groups))
        )
        for patch in [*revolution_patches, *freeform_patches]:
            face_patch_ids[patch.face_indices] = ""
        revolution_patches = []
        freeform_patches = []

        def connected_growth(
            base_faces: np.ndarray,
            accepted_faces: np.ndarray,
        ) -> np.ndarray:
            mask = np.zeros(len(mesh.faces), dtype=bool)
            mask[base_faces] = True
            mask[accepted_faces] = True
            base_set = set(int(value) for value in base_faces)
            components = _smooth_components(mesh, mask, 30.0)
            return max(
                components,
                key=lambda component: sum(
                    int(value) in base_set for value in component
                ),
                default=np.asarray(base_faces, dtype=np.int64),
            )

        for patch in planar_patches:
            if not residual_faces:
                break
            candidates = np.asarray(sorted(residual_faces), dtype=np.int64)
            face_points = np.asarray(mesh.vertices[mesh.faces[candidates]], dtype=float)
            distances = np.max(
                np.abs(np.einsum("fvi,i->fv", face_points - patch.origin, patch.normal)),
                axis=1,
            )
            alignment = np.abs(
                np.asarray(mesh.face_normals[candidates], dtype=float) @ patch.normal
            )
            accepted = candidates[
                (distances <= max(tolerance * 0.1, 1e-8))
                & (alignment >= math.cos(math.radians(0.25)))
            ]
            if len(accepted) == 0:
                continue
            grown = connected_growth(patch.face_indices, accepted)
            additions = np.asarray(
                [value for value in grown if int(value) in residual_faces],
                dtype=np.int64,
            )
            if len(additions) == 0:
                continue
            patch.face_indices = np.unique(np.r_[patch.face_indices, additions])
            patch.vertex_indices = np.unique(mesh.faces[patch.face_indices])
            patch.area = float(np.sum(mesh.area_faces[patch.face_indices]))
            patch.boundary_loops_3d = _boundary_loops(mesh, patch.face_indices)
            patch.boundary_loops = [
                np.column_stack(
                    (
                        (loop - patch.origin) @ patch.x_direction,
                        (loop - patch.origin) @ patch.y_direction,
                    )
                )
                for loop in patch.boundary_loops_3d
            ]
            polygons = [
                Polygon(loop).buffer(0)
                for loop in patch.boundary_loops
                if len(loop) >= 4 and Polygon(loop).area > 0
            ]
            patch.polygon = (
                max(polygons, key=lambda polygon: polygon.area)
                if polygons
                else None
            )
            for value in additions:
                residual_faces.discard(int(value))
            face_patch_ids[additions] = patch.patch_id

        analytic_growth: list[tuple[_AnalyticFit, CylindricalPatch | ToroidalPatch]] = []
        for patch in base_cylinders:
            analytic_growth.append(
                (
                    _AnalyticFit(
                        kind="cylinder",
                        parameters={
                            "origin": patch.origin,
                            "axis": patch.axis,
                            "radius": patch.radius,
                            "start": patch.start,
                            "end": patch.end,
                        },
                        rms_error=patch.rms_error,
                        max_error=patch.max_error,
                        normal_error_degrees=patch.normal_error_degrees,
                        score=0.0,
                    ),
                    patch,
                )
            )
        for patch in recognized["torus"]:
            if not isinstance(patch, ToroidalPatch):
                continue
            analytic_growth.append(
                (
                    _AnalyticFit(
                        kind="torus",
                        parameters={
                            "center": patch.center,
                            "axis": patch.axis,
                            "major_radius": patch.major_radius,
                            "minor_radius": patch.minor_radius,
                        },
                        rms_error=patch.rms_error,
                        max_error=patch.max_error,
                        normal_error_degrees=patch.normal_error_degrees,
                        score=0.0,
                    ),
                    patch,
                )
            )
        for fit, patch in analytic_growth:
            if not residual_faces:
                break
            candidates = np.asarray(sorted(residual_faces), dtype=np.int64)
            proposal_tolerance = (
                tolerance * 4 if isinstance(patch, ToroidalPatch) else tolerance
            )
            accepted = _analytic_inlier_faces(
                fit,
                mesh,
                candidates,
                proposal_tolerance,
                normal_tolerance_degrees,
            )
            if len(accepted) == 0:
                continue
            grown = connected_growth(patch.face_indices, accepted)
            additions = np.asarray(
                [value for value in grown if int(value) in residual_faces],
                dtype=np.int64,
            )
            if len(additions) == 0:
                continue
            if isinstance(patch, ToroidalPatch) and proposal_tolerance > tolerance:
                proposed_union = np.unique(
                    np.r_[patch.face_indices, additions]
                ).astype(np.int64)
                refined = _fit_component(
                    data,
                    proposed_union,
                    tolerance,
                    normal_tolerance_degrees,
                    torus_max_nfev=180,
                    allowed_kinds=("torus",),
                )
                if refined is None:
                    accepted = _analytic_inlier_faces(
                        fit,
                        mesh,
                        additions,
                        tolerance,
                        normal_tolerance_degrees,
                    )
                    additions = accepted
                else:
                    confirmed = _analytic_inlier_faces(
                        refined,
                        mesh,
                        proposed_union,
                        tolerance,
                        normal_tolerance_degrees,
                    )
                    confirmed_set = set(int(value) for value in confirmed)
                    base_fraction = sum(
                        int(value) in confirmed_set for value in patch.face_indices
                    ) / len(patch.face_indices)
                    if base_fraction < 0.95:
                        continue
                    additions = np.asarray(
                        [value for value in additions if int(value) in confirmed_set],
                        dtype=np.int64,
                    )
                    if len(additions):
                        patch.center = np.asarray(refined.parameters["center"], dtype=float)
                        patch.axis = np.asarray(refined.parameters["axis"], dtype=float)
                        patch.major_radius = float(refined.parameters["major_radius"])
                        patch.minor_radius = float(refined.parameters["minor_radius"])
                        patch.rms_error = refined.rms_error
                        patch.max_error = refined.max_error
                        patch.normal_error_degrees = refined.normal_error_degrees
            if len(additions) == 0:
                continue
            patch.face_indices = np.unique(np.r_[patch.face_indices, additions])
            patch.vertex_indices = np.unique(mesh.faces[patch.face_indices])
            patch.area = float(np.sum(mesh.area_faces[patch.face_indices]))
            patch.boundary_loops = _boundary_loops(mesh, patch.face_indices)
            if isinstance(patch, CylindricalPatch):
                points = np.asarray(mesh.vertices[patch.vertex_indices], dtype=float)
                axial = (points - patch.origin) @ patch.axis
                patch.start, patch.end = float(np.min(axial)), float(np.max(axial))
            for value in additions:
                residual_faces.discard(int(value))
            face_patch_ids[additions] = patch.patch_id

        residual_mask = np.zeros(len(mesh.faces), dtype=bool)
        if residual_faces:
            residual_mask[np.asarray(sorted(residual_faces), dtype=np.int64)] = True
        for component in _smooth_components(mesh, residual_mask, 30.0):
            index = len(freeform_patches) + 1
            residual_patch = FreeformPatch(
                patch_id=f"freeform-{index:03d}",
                face_indices=component,
                vertex_indices=np.unique(mesh.faces[component]),
                area=float(np.sum(mesh.area_faces[component])),
                boundary_loops=_boundary_loops(mesh, component),
            )
            freeform_patches.append(residual_patch)
            face_patch_ids[component] = residual_patch.patch_id

    # Removing robustly validated cylinders exposes formerly tangent fillet
    # bands as complete regions. Refit those whole regions now, preferring an
    # elementary torus and then a general node-profile revolution. This stage
    # never subdivides a residual, so a failed proposal cannot fragment it.
    if freeform_patches:
        original_freeforms = freeform_patches
        for patch in original_freeforms:
            face_patch_ids[patch.face_indices] = ""
        freeform_patches = []
        counters["torus"] = len(recognized["torus"])
        for original in sorted(original_freeforms, key=lambda patch: -patch.area):
            torus_fit = (
                _fit_component(
                    data,
                    original.face_indices,
                    tolerance,
                    normal_tolerance_degrees,
                    torus_max_nfev=120,
                    allowed_kinds=("torus",),
                )
                if len(original.face_indices) >= 8 and original.area >= minimum_area
                else None
            )
            if torus_fit is not None:
                counters["torus"] += 1
                torus_patch = _make_analytic_patch(
                    mesh,
                    torus_fit,
                    original.face_indices,
                    counters["torus"],
                )
                recognized["torus"].append(torus_patch)
                face_patch_ids[torus_patch.face_indices] = torus_patch.patch_id
                continue
            revolution = (
                _fit_surface_of_revolution_patch(
                    mesh,
                    original.face_indices,
                    tolerance,
                    normal_tolerance_degrees,
                    len(revolution_patches) + 1,
                )
                if original.area >= minimum_area
                else None
            )
            if revolution is not None:
                revolution_patches.append(revolution)
                face_patch_ids[revolution.face_indices] = revolution.patch_id
                continue
            # Primitive extraction is complete, so it is now safe to split a
            # genuinely non-analytic residual at creases into fit-friendly
            # freeform charts.  Delaying this operation is what lets tangent
            # cylinders and tori be recognized as whole mathematical supports.
            for chart in _freeform_charts(
                mesh,
                original.face_indices,
                smooth_angle_degrees,
            ):
                index = len(freeform_patches) + 1
                residual = FreeformPatch(
                    patch_id=f"freeform-{index:03d}",
                    face_indices=chart,
                    vertex_indices=np.unique(mesh.faces[chart]),
                    area=float(np.sum(mesh.area_faces[chart])),
                    boundary_loops=_boundary_loops(mesh, chart),
                )
                freeform_patches.append(residual)
                face_patch_ids[chart] = residual.patch_id

    # Sub-minimum-area remnants are often tessellation strips left between two
    # already accepted supports. Snap one only when an adjacent analytic
    # equation stays within three fitting tolerances over every node.
    if freeform_patches:
        analytic_by_id: dict[str, PlanarPatch | CylindricalPatch | ToroidalPatch] = {
            patch.patch_id: patch
            for patch in [*planar_patches, *base_cylinders, *recognized["torus"]]
            if isinstance(patch, (PlanarPatch, CylindricalPatch, ToroidalPatch))
        }
        retained_freeforms: list[FreeformPatch] = []
        for residual in freeform_patches:
            if residual.area > minimum_area:
                retained_freeforms.append(residual)
                continue
            residual_set = set(int(value) for value in residual.face_indices)
            neighbor_ids: set[str] = set()
            for raw_first, raw_second in np.asarray(mesh.face_adjacency, dtype=np.int64):
                first, second = int(raw_first), int(raw_second)
                if first in residual_set and second not in residual_set:
                    neighbor_ids.add(str(face_patch_ids[second]))
                elif second in residual_set and first not in residual_set:
                    neighbor_ids.add(str(face_patch_ids[first]))
            points = np.asarray(mesh.vertices[residual.vertex_indices], dtype=float)
            candidates: list[
                tuple[float, float, PlanarPatch | CylindricalPatch | ToroidalPatch]
            ] = []
            for patch_id in neighbor_ids:
                patch = analytic_by_id.get(patch_id)
                if isinstance(patch, PlanarPatch):
                    errors = np.abs((points - patch.origin) @ patch.normal)
                elif isinstance(patch, CylindricalPatch):
                    relative = points - patch.origin
                    axial = relative @ patch.axis
                    radial = relative - np.outer(axial, patch.axis)
                    errors = np.abs(np.linalg.norm(radial, axis=1) - patch.radius)
                elif isinstance(patch, ToroidalPatch):
                    relative = points - patch.center
                    axial = relative @ patch.axis
                    radial = np.linalg.norm(
                        relative - np.outer(axial, patch.axis),
                        axis=1,
                    )
                    errors = np.abs(
                        np.hypot(radial - patch.major_radius, axial)
                        - patch.minor_radius
                    )
                else:
                    continue
                candidates.append(
                    (
                        float(np.max(errors)),
                        float(np.sqrt(np.mean(errors**2))),
                        patch,
                    )
                )
            best = min(candidates, key=lambda item: (item[0], item[1]), default=None)
            if best is None or best[0] > tolerance * 3 or best[1] > tolerance * 2:
                retained_freeforms.append(residual)
                continue
            target = best[2]
            target.face_indices = np.unique(
                np.r_[target.face_indices, residual.face_indices]
            ).astype(np.int64)
            target.vertex_indices = np.unique(mesh.faces[target.face_indices])
            target.area = float(np.sum(mesh.area_faces[target.face_indices]))
            loops_3d = _boundary_loops(mesh, target.face_indices)
            if isinstance(target, PlanarPatch):
                target.boundary_loops_3d = loops_3d
                target.boundary_loops = [
                    np.column_stack(
                        (
                            (loop - target.origin) @ target.x_direction,
                            (loop - target.origin) @ target.y_direction,
                        )
                    )
                    for loop in loops_3d
                ]
            else:
                target.boundary_loops = loops_3d
                if isinstance(target, CylindricalPatch):
                    target_points = np.asarray(
                        mesh.vertices[target.vertex_indices],
                        dtype=float,
                    )
                    axial = (target_points - target.origin) @ target.axis
                    target.start, target.end = float(np.min(axial)), float(np.max(axial))
            face_patch_ids[residual.face_indices] = target.patch_id
        freeform_patches = retained_freeforms
        for index, patch in enumerate(freeform_patches, start=1):
            patch.patch_id = f"freeform-{index:03d}"
            face_patch_ids[patch.face_indices] = patch.patch_id

    # The initial plane pass deliberately defers tiny facets embedded in a
    # smooth field so cylinders and fillets are not fragmented. Once those
    # curved supports have been peeled, promote any remaining exactly planar
    # chart, including a two-triangle CAD face that was below the global area
    # threshold. The all-node and normal gates prevent an organic mesh facet
    # pair from being mistaken for a plane merely because each triangle is flat.
    if freeform_patches:
        retained_freeforms = []
        normals = np.asarray(mesh.face_normals, dtype=float)
        centers = np.asarray(mesh.triangles_center, dtype=float)
        for residual in freeform_patches:
            indices = np.asarray(residual.face_indices, dtype=np.int64)
            weights = np.asarray(mesh.area_faces[indices], dtype=float)
            normal = np.average(normals[indices], axis=0, weights=weights)
            normal /= max(float(np.linalg.norm(normal)), 1e-12)
            origin = np.average(centers[indices], axis=0, weights=weights)
            vertices = np.unique(mesh.faces[indices])
            errors = np.abs((np.asarray(mesh.vertices[vertices]) - origin) @ normal)
            rms_error, max_error = _fit_statistics(errors)
            normal_error = _normal_error_degrees(
                normals[indices],
                np.tile(normal, (len(indices), 1)),
            )
            if (
                max_error > max(tolerance * 0.1, 1e-8)
                or normal_error > 0.25
            ):
                retained_freeforms.append(residual)
                continue
            x_direction, y_direction = _plane_basis(normal)
            loops_3d = _boundary_loops(mesh, indices)
            loops_2d = [
                np.column_stack(
                    (
                        (loop - origin) @ x_direction,
                        (loop - origin) @ y_direction,
                    )
                )
                for loop in loops_3d
            ]
            polygons = [
                Polygon(loop).buffer(0)
                for loop in loops_2d
                if len(loop) >= 4 and Polygon(loop).area > 0
            ]
            polygon = max(polygons, key=lambda item: item.area) if polygons else None
            plane = PlanarPatch(
                patch_id=f"plane-{len(planar_patches) + 1:03d}",
                face_indices=indices,
                vertex_indices=vertices,
                origin=origin,
                normal=normal,
                x_direction=x_direction,
                y_direction=y_direction,
                area=float(np.sum(weights)),
                boundary_loops=loops_2d,
                polygon=polygon,
                boundary_loops_3d=loops_3d,
                rms_error=rms_error,
                max_error=max_error,
            )
            planar_patches.append(plane)
            face_patch_ids[indices] = plane.patch_id
        freeform_patches = retained_freeforms
        for index, patch in enumerate(freeform_patches, start=1):
            patch.patch_id = f"freeform-{index:03d}"
            face_patch_ids[patch.face_indices] = patch.patch_id

    # Defensive coverage for non-manifold inputs whose faces did not enter a
    # connected component.  This invariant makes downstream stitching safe.
    for face_index in np.flatnonzero(face_patch_ids == ""):
        index = len(freeform_patches) + 1
        face_indices = np.asarray([face_index], dtype=np.int64)
        patch = FreeformPatch(
            patch_id=f"freeform-{index:03d}",
            face_indices=face_indices,
            vertex_indices=np.unique(mesh.faces[face_indices]),
            area=float(np.sum(mesh.area_faces[face_indices])),
            boundary_loops=_boundary_loops(mesh, face_indices),
        )
        freeform_patches.append(patch)
        face_patch_ids[face_indices] = patch.patch_id

    # Earlier primitive growth and tangent-chain splitting can leave a
    # residual that becomes a clean linear extrusion only after its final face
    # ownership is known. Refit those final connected regions before freezing
    # them as freeform; the fit itself verifies normal alignment and every
    # reduced node against the recovered profile.
    if freeform_patches:
        retained_freeforms: list[FreeformPatch] = []
        next_extrusion_index = (
            max(
                (
                    int(patch.patch_id.rsplit("-", 1)[-1])
                    for patch in extrusion_patches
                    if patch.patch_id.rsplit("-", 1)[-1].isdigit()
                ),
                default=0,
            )
            + 1
        )
        for residual in freeform_patches:
            extrusion = _fit_linear_extrusion_patch(
                mesh,
                residual.face_indices,
                tolerance,
                normal_tolerance_degrees,
                next_extrusion_index,
            )
            if extrusion is None:
                retained_freeforms.append(residual)
                continue
            extrusion_patches.append(extrusion)
            face_patch_ids[extrusion.face_indices] = extrusion.patch_id
            next_extrusion_index += 1
        freeform_patches = retained_freeforms
        for index, patch in enumerate(freeform_patches, start=1):
            patch.patch_id = f"freeform-{index:03d}"
            face_patch_ids[patch.face_indices] = patch.patch_id

    # A very small torus major radius is a common numerical degeneracy when a
    # short conical band is fit from local revolution samples. Prefer the
    # simpler cone only when a full independent cone refit validates the same
    # nodes and normals; genuine ring and spindle tori are otherwise retained.
    retained_tori: list[SurfacePatch] = []
    for patch in recognized["torus"]:
        if not isinstance(patch, ToroidalPatch) or (
            patch.major_radius >= patch.minor_radius * 0.1
        ):
            retained_tori.append(patch)
            continue
        cone_fit = _fit_component(
            data,
            patch.face_indices,
            tolerance,
            normal_tolerance_degrees,
            allowed_kinds=("cone",),
        )
        if cone_fit is None:
            retained_tori.append(patch)
            continue
        cone = _make_analytic_patch(
            mesh,
            cone_fit,
            patch.face_indices,
            len(recognized["cone"]) + 1,
        )
        recognized["cone"].append(cone)
        face_patch_ids[cone.face_indices] = cone.patch_id
    recognized["torus"] = retained_tori

    # Circular profiles are elementary cylinders, even when they were found
    # only by the late extrusion recovery above. Promote them before the final
    # chord pass so neighboring planar tessellation strips can join the same
    # analytic support.
    if extrusion_patches:
        retained_extrusions: list[LinearExtrusionPatch] = []
        for extrusion in extrusion_patches:
            cylinder = _circular_extrusion_cylinder(
                extrusion,
                mesh,
                tolerance,
                normal_tolerance_degrees,
                len(base_cylinders) + 1,
            )
            if cylinder is None:
                retained_extrusions.append(extrusion)
                continue
            base_cylinders.append(cylinder)
            face_patch_ids[cylinder.face_indices] = cylinder.patch_id
        extrusion_patches = retained_extrusions

    # Some short circular profile arcs are tessellated as only three planar
    # strips and never create an extrusion candidate on their own. Recover them
    # from connected strip networks with the same circular-profile validator.
    base_cylinders.extend(
        _recover_cylinders_from_planar_strips(
            data,
            planar_patches,
            face_patch_ids,
            tolerance,
            normal_tolerance_degrees,
            len(base_cylinders) + 1,
        )
    )

    # A partial cone can be split by local consensus into several oblique
    # cylinders and short general extrusions. Recombine a smooth connected
    # group when its union passes a stricter cone fit than the ordinary
    # primitive gate. Datum-aligned cylinders are excluded, preserving real
    # bores and bosses that touch the candidate at a circular edge.
    sweep_candidates: list[CylindricalPatch | LinearExtrusionPatch] = []
    datum_normals = [
        _canonical_direction(np.asarray(patch.normal, dtype=float))
        for patch in planar_patches
    ]
    for patch in [*base_cylinders, *extrusion_patches]:
        direction = patch.axis if isinstance(patch, CylindricalPatch) else patch.direction
        datum_alignment = max(
            (abs(float(direction @ normal)) for normal in datum_normals),
            default=0.0,
        )
        if datum_alignment < math.cos(math.radians(5.0)):
            sweep_candidates.append(patch)
    if len(sweep_candidates) >= 2:
        candidate_by_id = {patch.patch_id: patch for patch in sweep_candidates}
        parent = {patch.patch_id: patch.patch_id for patch in sweep_candidates}

        def sweep_root(patch_id: str) -> str:
            while parent[patch_id] != patch_id:
                parent[patch_id] = parent[parent[patch_id]]
                patch_id = parent[patch_id]
            return patch_id

        def sweep_join(first_id: str, second_id: str) -> None:
            first_root, second_root = sweep_root(first_id), sweep_root(second_id)
            if first_root != second_root:
                parent[second_root] = first_root

        smooth_limit = math.cos(math.radians(min(smooth_angle_degrees, 10.0)))
        for first_face, second_face in np.asarray(mesh.face_adjacency, dtype=np.int64):
            first_id = str(face_patch_ids[int(first_face)])
            second_id = str(face_patch_ids[int(second_face)])
            if (
                first_id == second_id
                or first_id not in candidate_by_id
                or second_id not in candidate_by_id
            ):
                continue
            alignment = float(
                np.asarray(mesh.face_normals[int(first_face)])
                @ np.asarray(mesh.face_normals[int(second_face)])
            )
            if alignment >= smooth_limit:
                sweep_join(first_id, second_id)

        groups: dict[str, list[CylindricalPatch | LinearExtrusionPatch]] = {}
        for patch in sweep_candidates:
            groups.setdefault(sweep_root(patch.patch_id), []).append(patch)
        replaced_ids: set[str] = set()
        counters["cone"] = len(recognized["cone"])
        for group in groups.values():
            if len(group) < 2:
                continue
            combined_faces = np.unique(
                np.concatenate([patch.face_indices for patch in group])
            ).astype(np.int64)
            fit = _fit_component(
                data,
                combined_faces,
                tolerance,
                normal_tolerance_degrees,
                allowed_kinds=("cone",),
            )
            if (
                fit is None
                or fit.rms_error > tolerance * 0.25
                or fit.max_error > tolerance * 0.5
            ):
                continue
            counters["cone"] += 1
            cone = _make_analytic_patch(
                mesh,
                fit,
                combined_faces,
                counters["cone"],
            )
            if not isinstance(cone, ConicalPatch):
                continue
            recognized["cone"].append(cone)
            face_patch_ids[combined_faces] = cone.patch_id
            replaced_ids.update(patch.patch_id for patch in group)
        if replaced_ids:
            base_cylinders = [
                patch for patch in base_cylinders if patch.patch_id not in replaced_ids
            ]
            extrusion_patches = [
                patch for patch in extrusion_patches if patch.patch_id not in replaced_ids
            ]

    # Planar slots can split one conical support into disconnected sectors, so
    # the smooth-adjacency pass above never sees their combined normal span.
    # Cluster only independently validated partial-cone alternatives, refit
    # each cluster as a whole, then keep one trimmed face per original sector.
    # The strict union fit prevents unrelated bores or bosses from merging.
    partial_cone_candidates: list[
        tuple[CylindricalPatch | ConicalPatch, _AnalyticFit]
    ] = []
    for patch in [*base_cylinders, *recognized["cone"]]:
        if not isinstance(patch, (CylindricalPatch, ConicalPatch)):
            continue
        indices = np.asarray(patch.face_indices, dtype=np.int64)
        if len(indices) < 8:
            continue
        normals = np.asarray(mesh.face_normals[indices], dtype=float)
        centered = normals - np.mean(normals, axis=0)
        _, singular_values, vectors = np.linalg.svd(
            centered,
            full_matrices=False,
        )
        if (
            singular_values[1] <= 1e-12
            or singular_values[2] / singular_values[1] <= 0.30
        ):
            continue
        vertex_indices = np.unique(mesh.faces[indices])
        alternative = _fit_partial_cone(
            np.asarray(mesh.vertices[vertex_indices], dtype=float),
            np.asarray(mesh.triangles_center[indices], dtype=float),
            normals,
            _canonical_direction(vectors[-1]),
            tolerance,
            normal_tolerance_degrees,
            data.diagonal,
        )
        if alternative is not None:
            partial_cone_candidates.append((patch, alternative))

    if len(partial_cone_candidates) >= 2:
        parent = list(range(len(partial_cone_candidates)))

        def partial_root(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def partial_join(first: int, second: int) -> None:
            first_root, second_root = partial_root(first), partial_root(second)
            if first_root != second_root:
                parent[second_root] = first_root

        axis_limit = math.cos(math.radians(12.0))
        apex_limit = data.diagonal * 0.22
        for first in range(len(partial_cone_candidates)):
            first_fit = partial_cone_candidates[first][1]
            first_axis = np.asarray(first_fit.parameters["axis"], dtype=float)
            first_apex = np.asarray(first_fit.parameters["apex"], dtype=float)
            first_angle = float(first_fit.parameters["semi_angle"])
            for second in range(first + 1, len(partial_cone_candidates)):
                second_fit = partial_cone_candidates[second][1]
                second_axis = np.asarray(second_fit.parameters["axis"], dtype=float)
                second_apex = np.asarray(second_fit.parameters["apex"], dtype=float)
                second_angle = float(second_fit.parameters["semi_angle"])
                if abs(float(first_axis @ second_axis)) < axis_limit:
                    continue
                if abs(first_angle - second_angle) > math.radians(10.0):
                    continue
                if float(np.linalg.norm(first_apex - second_apex)) > apex_limit:
                    continue
                partial_join(first, second)

        candidate_groups: dict[int, list[CylindricalPatch | ConicalPatch]] = {}
        for index, (patch, _) in enumerate(partial_cone_candidates):
            candidate_groups.setdefault(partial_root(index), []).append(patch)
        replaced_partial_ids: set[str] = set()
        replacement_cones: list[ConicalPatch] = []
        next_cone_index = len(recognized["cone"]) + 1
        for group in candidate_groups.values():
            if len(group) < 2:
                continue
            combined_faces = np.unique(
                np.concatenate([patch.face_indices for patch in group])
            ).astype(np.int64)
            fit = _fit_component(
                data,
                combined_faces,
                tolerance,
                normal_tolerance_degrees,
                allowed_kinds=("cone",),
            )
            if (
                fit is None
                or fit.rms_error > tolerance * 0.25
                or fit.max_error > tolerance * 0.5
            ):
                continue
            for original in group:
                cone = _make_analytic_patch(
                    mesh,
                    fit,
                    original.face_indices,
                    next_cone_index,
                )
                if not isinstance(cone, ConicalPatch):
                    continue
                replacement_cones.append(cone)
                face_patch_ids[cone.face_indices] = cone.patch_id
                replaced_partial_ids.add(original.patch_id)
                next_cone_index += 1
        if replaced_partial_ids:
            base_cylinders = [
                patch
                for patch in base_cylinders
                if patch.patch_id not in replaced_partial_ids
            ]
            recognized["cone"] = [
                patch
                for patch in recognized["cone"]
                if patch.patch_id not in replaced_partial_ids
            ]
            recognized["cone"].extend(replacement_cones)

    _rebalance_tangent_cylinder_torus_boundaries(
        mesh,
        base_cylinders,
        [
            patch
            for patch in recognized["torus"]
            if isinstance(patch, ToroidalPatch)
        ],
        face_patch_ids,
        tolerance,
        normal_tolerance_degrees,
    )
    _absorb_cylindrical_chord_fragments(
        mesh,
        planar_patches,
        base_cylinders,
        face_patch_ids,
        tolerance,
        normal_tolerance_degrees,
    )
    _absorb_tangent_planar_slivers(
        mesh,
        planar_patches,
        freeform_patches,
        face_patch_ids,
        tolerance,
        normal_tolerance_degrees,
    )
    # Late primitive promotion changes the ownership on both sides of every
    # new interface. Refresh residual boundaries after all such moves so an
    # embedded planar island becomes an explicit hole instead of disappearing
    # from the surrounding smooth chart's trim topology.
    for patch in freeform_patches:
        patch.boundary_loops = _boundary_loops(mesh, patch.face_indices)
    analytic_patches: list[SurfacePatch] = [
        *planar_patches,
        *base_cylinders,
        *[
            patch
            for patch in recognized["torus"]
            if isinstance(patch, ToroidalPatch)
        ],
    ]
    # Primitive patches already grow from node residuals and connected
    # topology. A later face-label ICM can steal faces from swept/freeform
    # patches without refitting those owners, creating overlaps and malformed
    # trim loops. Keep the fitted ownership invariant instead.
    regularized = face_patch_ids
    if np.any(regularized != face_patch_ids):
        face_patch_ids = regularized
        for patch in analytic_patches:
            indices = np.flatnonzero(face_patch_ids == patch.patch_id).astype(np.int64)
            if len(indices) == 0:
                continue
            patch.face_indices = indices
            patch.vertex_indices = np.unique(mesh.faces[indices])
            patch.area = float(np.sum(mesh.area_faces[indices]))
            loops_3d = _boundary_loops(mesh, indices)
            if isinstance(patch, PlanarPatch):
                patch.boundary_loops_3d = loops_3d
                patch.boundary_loops = [
                    np.column_stack(
                        (
                            (loop - patch.origin) @ patch.x_direction,
                            (loop - patch.origin) @ patch.y_direction,
                        )
                    )
                    for loop in loops_3d
                ]
                polygons = [
                    Polygon(loop).buffer(0)
                    for loop in patch.boundary_loops
                    if len(loop) >= 4 and Polygon(loop).area > 0
                ]
                patch.polygon = (
                    max(polygons, key=lambda polygon: polygon.area)
                    if polygons
                    else None
                )
            else:
                patch.boundary_loops = loops_3d
                if isinstance(patch, CylindricalPatch):
                    points = np.asarray(mesh.vertices[patch.vertex_indices], dtype=float)
                    axial = (points - patch.origin) @ patch.axis
                    patch.start, patch.end = float(np.min(axial)), float(np.max(axial))

    # Dense tangent-fillet networks amplify minute datum rotations into large
    # changes at remote periodic trims. They need a later joint surface/edge
    # solve, so do not apply the plane-only optimizer in isolation there.
    if len(mesh.faces) < 60_000:
        _regularize_plane_relations(
            mesh,
            planar_patches,
            tolerance,
            normal_tolerance_degrees,
        )
        _regularize_axis_relations(
            mesh,
            planar_patches,
            [
                *base_cylinders,
                *[
                    patch
                    for patch in recognized["cone"]
                    if isinstance(patch, ConicalPatch)
                ],
                *[
                    patch
                    for patch in recognized["torus"]
                    if isinstance(patch, ToroidalPatch)
                ],
            ],
            tolerance,
            normal_tolerance_degrees,
        )

    graph = SurfaceGraph(
        planar_patches=planar_patches,
        cylindrical_patches=base_cylinders,
        conical_patches=[
            patch
            for patch in recognized["cone"]
            if isinstance(patch, ConicalPatch)
        ],
        spherical_patches=[
            patch
            for patch in recognized["sphere"]
            if isinstance(patch, SphericalPatch)
        ],
        toroidal_patches=[
            patch
            for patch in recognized["torus"]
            if isinstance(patch, ToroidalPatch)
        ],
        extrusion_patches=extrusion_patches,
        revolution_patches=revolution_patches,
        freeform_patches=freeform_patches,
        face_patch_ids=face_patch_ids,
    )
    graph.adjacency = _build_adjacency(
        mesh,
        face_patch_ids,
        graph.patches,
        tolerance,
    )
    if isinstance(cache, dict):
        cache[cache_key] = graph
    return graph


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
