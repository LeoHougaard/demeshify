from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

from .schemas import MeshReport

UNIT_SCALE = {
    "mm": 1.0,
    "cm": 10.0,
    "m": 1000.0,
    "in": 25.4,
}


@dataclass(slots=True)
class MeshData:
    mesh: trimesh.Trimesh
    report: MeshReport
    source_path: Path
    section_cache: dict[tuple[str, float], Any] = field(default_factory=dict)
    source_face_indices: np.ndarray | None = None
    shell_face_indices: list[np.ndarray] | None = None
    shell_parents: list[int] | None = None
    boundary_subdivisions: int = 0

    @property
    def diagonal(self) -> float:
        return float(np.linalg.norm(self.mesh.extents))


def containment_parents(
    bounds: np.ndarray, volumes: list[float], contains: Callable[[int, int], bool],
) -> list[int]:
    """Find the smallest enclosing shell, never inferring containment from bounds alone."""
    parents = [-1] * len(bounds)
    for child in range(len(bounds)):
        candidates = [
            parent for parent in range(len(bounds))
            if volumes[parent] > volumes[child]
            and np.all(bounds[parent, 0] < bounds[child, 0])
            and np.all(bounds[parent, 1] > bounds[child, 1])
        ]
        for parent in sorted(candidates, key=lambda index: volumes[index]):
            if contains(parent, child):
                parents[child] = parent
                break
    return parents


def shell_depths(parents: list[int]) -> list[int]:
    depths = []
    for parent in parents:
        depth = 0
        while parent != -1:
            depth += 1
            parent = parents[parent]
        depths.append(depth)
    return depths


def closed_shell_topology(mesh: trimesh.Trimesh) -> tuple[list[np.ndarray], list[int]]:
    components = list(trimesh.graph.connected_components(
        mesh.face_adjacency, nodes=np.arange(len(mesh.faces)), min_len=1,
    ))
    parts = [mesh.submesh([indices], append=True, repair=False) for indices in components]

    def contains(parent: int, child: int) -> bool:
        # Test every node and triangle interior, including small local details.
        # Chunk the ray queries so large enclosed shells do not allocate a large
        # intermediate intersection array. Shell centroids can lie in a void.
        points = np.vstack((parts[child].vertices, parts[child].triangles_center))
        return all(
            np.all(parts[parent].contains(points[start:start + 512]))
            for start in range(0, len(points), 512)
        )

    parents = containment_parents(
        np.asarray([part.bounds for part in parts]),
        [abs(float(part.volume)) for part in parts], contains,
    )
    return components, parents


def resolve_boundary_subdivisions(
    mesh: trimesh.Trimesh, source_face_indices: np.ndarray,
) -> tuple[trimesh.Trimesh, np.ndarray, int]:
    """Resolve exact collinear T junctions without adding or moving surface geometry."""

    incidence = np.bincount(mesh.edges_unique_inverse, minlength=len(mesh.edges_unique))
    boundary_ids = np.flatnonzero(incidence == 1)
    if not len(boundary_ids) or np.any(incidence > 2):
        return mesh, source_face_indices, 0
    boundary_vertices = np.unique(mesh.edges_unique[boundary_ids])
    # Cleanup must remain bounded for malformed uploads with many open edges.
    if len(boundary_ids) * len(boundary_vertices) > 1_000_000:
        return mesh, source_face_indices, 0

    points = mesh.vertices[boundary_vertices]
    subdivisions = {}
    exact_vertices = {}

    def exact_vertex(index: int) -> tuple[Fraction, ...]:
        if index not in exact_vertices:
            exact_vertices[index] = tuple(Fraction(float(value)) for value in mesh.vertices[index])
        return exact_vertices[index]

    for edge_id in boundary_ids:
        a, b = (int(index) for index in mesh.edges_unique[edge_id])
        delta = mesh.vertices[b] - mesh.vertices[a]
        offsets = points - mesh.vertices[a]
        axis = int(np.argmax(np.abs(delta)))
        parameter = offsets[:, axis] / delta[axis]
        # Float arithmetic screens candidates cheaply, but subtraction can erase
        # a retained off-edge offset. Confirm using the original binary values.
        collinear = np.all(np.cross(offsets, delta) == 0, axis=1)
        inside = np.flatnonzero(collinear & (parameter > 0) & (parameter < 1))
        if len(inside):
            start, end = exact_vertex(a), exact_vertex(b)
            exact_delta = tuple(right - left for left, right in zip(start, end, strict=True))
            confirmed = []
            for candidate in inside:
                vertex_id = int(boundary_vertices[candidate])
                point = exact_vertex(vertex_id)
                exact_offset = tuple(
                    value - origin for value, origin in zip(point, start, strict=True)
                )
                exact_parameter = exact_offset[axis] / exact_delta[axis]
                if 0 < exact_parameter < 1 and all(
                    exact_offset[dimension] * exact_delta[axis]
                    == exact_offset[axis] * exact_delta[dimension]
                    for dimension in range(3) if dimension != axis
                ):
                    confirmed.append((exact_parameter, vertex_id))
            if confirmed:
                subdivisions[(a, b)] = [vertex_id for _, vertex_id in sorted(confirmed)]
    if not subdivisions:
        return mesh, source_face_indices, 0

    faces, provenance = [], []
    for original, source_id in zip(mesh.faces, source_face_indices, strict=True):
        triangles = [original.tolist()]
        for position in range(3):
            a, b = int(original[position]), int(original[(position + 1) % 3])
            inserted = subdivisions.get((min(a, b), max(a, b)))
            if not inserted:
                continue
            inserted = inserted if a < b else inserted[::-1]
            for index, triangle in enumerate(triangles):
                if a in triangle and b in triangle:
                    third = next(vertex for vertex in triangle if vertex not in (a, b))
                    chain = [a, *inserted, b]
                    triangles[index:index + 1] = [
                        [left, right, third]
                        for left, right in zip(chain[:-1], chain[1:], strict=True)
                    ]
                    break
        faces.extend(triangles)
        provenance.extend([int(source_id)] * len(triangles))
    repaired = trimesh.Trimesh(vertices=mesh.vertices.copy(), faces=faces, process=False)
    if not (
        repaired.is_watertight and repaired.is_winding_consistent
        and repaired.body_count == mesh.body_count
        and np.all(repaired.area_faces > 0)
        and np.isclose(repaired.area, mesh.area, rtol=1e-12, atol=0)
        and np.isclose(repaired.volume, mesh.volume, rtol=1e-12, atol=0)
    ):
        return mesh, source_face_indices, 0
    return repaired, np.asarray(provenance, dtype=np.int64), len(faces) - len(mesh.faces)


def load_mesh(path: Path, file_name: str, input_units: str = "mm") -> MeshData:
    if input_units not in UNIT_SCALE:
        raise ValueError(f"Unsupported input unit: {input_units}")

    # Processing removes invalid vertices and can change their face references.
    # Validate the raw STL first so malformed geometry cannot become plausible
    # geometry, and retain face numbers from the uploaded file for overlays.
    loaded = trimesh.load(path, force="mesh", process=False)
    if isinstance(loaded, trimesh.Scene):
        if not loaded.geometry:
            raise ValueError("The STL contains no geometry")
        loaded = loaded.to_mesh()
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise ValueError("The STL contains no triangles")

    mesh = loaded.copy()
    source_face_indices = np.arange(len(mesh.faces), dtype=np.int64)
    if not np.all(np.isfinite(mesh.vertices)):
        raise ValueError("The STL contains non-finite coordinates")
    mesh.merge_vertices()
    scale = UNIT_SCALE[input_units]
    if scale != 1.0:
        mesh.apply_scale(scale)
    nonzero = np.asarray(mesh.area_faces) > 0
    removed_degenerate = int(np.count_nonzero(~nonzero))
    if removed_degenerate:
        mesh.update_faces(nonzero)
        source_face_indices = source_face_indices[nonzero]
    unique = mesh.unique_faces()
    removed_duplicate = int(np.count_nonzero(~unique))
    if removed_duplicate:
        mesh.update_faces(unique)
        source_face_indices = source_face_indices[unique]
    if len(mesh.faces) == 0:
        raise ValueError("The STL contains no non-degenerate triangles")
    mesh.remove_unreferenced_vertices()
    subdivisions = 0
    if removed_degenerate:
        mesh, source_face_indices, subdivisions = resolve_boundary_subdivisions(
            mesh, source_face_indices,
        )
    mesh.fix_normals(multibody=True)
    shell_faces = None
    shell_parents = None
    cavity_count = 0
    solid_count = int(mesh.body_count) if mesh.is_watertight else None
    if mesh.is_watertight and mesh.body_count > 1:
        shell_faces, shell_parents = closed_shell_topology(mesh)
        depths = shell_depths(shell_parents)
        for face_ids, depth in zip(shell_faces, depths, strict=True):
            if depth % 2:
                mesh.faces[face_ids] = mesh.faces[face_ids, ::-1]
                cavity_count += 1
        solid_count = len(depths) - cavity_count

    incidence = np.bincount(mesh.edges_unique_inverse, minlength=len(mesh.edges_unique))

    volume = abs(float(mesh.volume)) if mesh.is_watertight else None
    report = MeshReport(
        file_name=file_name,
        triangle_count=int(len(mesh.faces)),
        vertex_count=int(len(mesh.vertices)),
        watertight=bool(mesh.is_watertight),
        body_count=int(mesh.body_count),
        solid_count=solid_count,
        cavity_shell_count=cavity_count,
        dimensions_mm=tuple(float(value) for value in mesh.extents),
        volume_mm3=volume,
        surface_area_mm2=float(mesh.area),
        input_units=input_units,
        unit_scale=scale,
        boundary_edge_count=int(np.count_nonzero(incidence == 1)),
        nonmanifold_edge_count=int(np.count_nonzero(incidence > 2)),
        winding_consistent=bool(mesh.is_winding_consistent),
        removed_degenerate_triangles=removed_degenerate,
        removed_duplicate_triangles=removed_duplicate,
    )
    return MeshData(
        mesh=mesh, report=report, source_path=path, source_face_indices=source_face_indices,
        shell_face_indices=shell_faces, shell_parents=shell_parents,
        boundary_subdivisions=subdivisions,
    )


def mesh_warnings(data: MeshData) -> list[str]:
    report = data.report
    warnings = []
    removed = report.removed_degenerate_triangles + report.removed_duplicate_triangles
    if removed:
        warnings.append(
            f"Removed {report.removed_degenerate_triangles} zero-area and "
            f"{report.removed_duplicate_triangles} duplicate triangles without moving vertices."
        )
    if data.boundary_subdivisions:
        warnings.append(
            f"Resolved {data.boundary_subdivisions} collinear boundary subdivisions "
            "without moving vertices or filling openings."
        )
    if report.boundary_edge_count or report.nonmanifold_edge_count:
        warnings.append(
            f"Input topology has {report.boundary_edge_count} boundary edges and "
            f"{report.nonmanifold_edge_count} non-manifold edges. "
            "Openings were not automatically filled."
        )
    return warnings


def surface_samples(mesh: trimesh.Trimesh, count: int = 5000) -> np.ndarray:
    count = max(500, min(count, max(500, len(mesh.faces) * 3)))
    points, _ = trimesh.sample.sample_surface(mesh, count, seed=42)
    return np.asarray(points, dtype=np.float64)
