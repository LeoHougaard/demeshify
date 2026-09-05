from __future__ import annotations

from dataclasses import dataclass, field
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

    @property
    def diagonal(self) -> float:
        return float(np.linalg.norm(self.mesh.extents))


def load_mesh(path: Path, file_name: str, input_units: str = "mm") -> MeshData:
    if input_units not in UNIT_SCALE:
        raise ValueError(f"Unsupported input unit: {input_units}")

    loaded = trimesh.load(path, force="mesh", process=True)
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
    mesh.fix_normals(multibody=True)

    incidence = np.bincount(mesh.edges_unique_inverse, minlength=len(mesh.edges_unique))

    volume = abs(float(mesh.volume)) if mesh.is_watertight else None
    report = MeshReport(
        file_name=file_name,
        triangle_count=int(len(mesh.faces)),
        vertex_count=int(len(mesh.vertices)),
        watertight=bool(mesh.is_watertight),
        body_count=int(mesh.body_count),
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
        mesh=mesh, report=report, source_path=path, source_face_indices=source_face_indices
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
