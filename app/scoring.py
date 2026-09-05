from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cadquery as cq
import numpy as np
import point_cloud_utils as pcu
import trimesh
from OCP.BRepMesh import BRepMesh_IncrementalMesh

from .cad import export_plan
from .mesh import MeshData, surface_samples
from .schemas import ReconstructionPlan, ScoreReport


@dataclass(slots=True)
class ScoredCandidate:
    plan: ReconstructionPlan
    report: ScoreReport
    directory: Path


def _load_candidate(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, force="mesh", process=True)
    if isinstance(loaded, trimesh.Scene):
        loaded = loaded.to_mesh()
    if not isinstance(loaded, trimesh.Trimesh):
        raise ValueError("Candidate did not produce a mesh")
    return loaded


def _step_mesh(shape: cq.Shape, tolerance: float) -> trimesh.Trimesh:
    # CadQuery's mesh helper uses relative deflection. Verification tolerances
    # are millimetres, so create an absolute-deflection mesh before extracting it.
    mesher = BRepMesh_IncrementalMesh(shape.wrapped, tolerance, False, 0.04, False)
    if not mesher.IsDone():
        raise ValueError("The exported STEP could not be tessellated for verification")
    vertices, triangles = shape.tessellate(tolerance, angularTolerance=0.04)
    if not vertices or not triangles:
        raise ValueError("The exported STEP produced no verification triangles")
    return trimesh.Trimesh(
        vertices=np.asarray([vertex.toTuple() for vertex in vertices], dtype=np.float64),
        faces=np.asarray(triangles, dtype=np.int64),
        process=True,
    )


def _local_samples(mesh: trimesh.Trimesh) -> np.ndarray:
    """Include mesh nodes and small triangle interiors regardless of their area.

    Uniform area sampling alone can miss a narrow bore. Bound the extra work on
    dense inputs; component correspondence is checked separately and never sampled.
    """

    maximum = 20_000
    vertices = np.asarray(mesh.vertices)
    if len(vertices) > maximum:
        vertices = vertices[np.linspace(0, len(vertices) - 1, maximum, dtype=int)]
    count = len(mesh.faces)
    indices = (
        np.arange(count)
        if count <= maximum
        else np.unique(
            np.r_[
                np.linspace(0, count - 1, maximum // 2, dtype=int),
                np.argpartition(mesh.area_faces, maximum // 2)[: maximum // 2],
            ]
        )
    )
    centers = mesh.vertices[mesh.faces[indices]].mean(axis=1)
    return np.vstack((vertices, centers))


def _point_mesh_distances(
    mesh: trimesh.Trimesh,
    points: np.ndarray,
) -> np.ndarray:
    """Return exact point-to-triangle distances using a compiled AABB query.

    ``trimesh.proximity.closest_point`` is accurate, but its Python-facing
    traversal is expensive for the dense tessellations used as ground truth.
    Point Cloud Utils performs the same closest-triangle query in compiled
    code. Keep the trimesh path as a compatibility fallback for malformed or
    unusual meshes that the accelerated backend rejects.
    """

    try:
        distances, _, _ = pcu.closest_points_on_mesh(
            np.ascontiguousarray(points, dtype=np.float64),
            np.ascontiguousarray(mesh.vertices, dtype=np.float64),
            np.ascontiguousarray(mesh.faces, dtype=np.int32),
        )
        return np.asarray(distances, dtype=np.float64)
    except (RuntimeError, ValueError):
        return np.asarray(
            trimesh.proximity.closest_point(mesh, points)[1],
            dtype=np.float64,
        )


def score_exported_shape(
    data: MeshData,
    directory: Path,
    candidate_count: int = 1,
    complexity: float = 0.0,
    require_solid: bool = True,
    *,
    verify_step_geometry: bool = True,
) -> ScoreReport:
    """Measure the delivered STEP. Preview-only scoring is restricted to search."""

    step_shape = cq.importers.importStep(str(directory / "reconstruction.step")).val()
    solids = step_shape.Solids()
    valid_brep = bool(step_shape.isValid() and step_shape.Faces())
    valid_solid = bool(
        valid_brep
        and solids
        and all(solid.isValid() for solid in solids)
        and sum(len(solid.Faces()) for solid in solids) == len(step_shape.Faces())
    )
    tessellation = max(min(data.diagonal * 0.00005, 0.01), 1e-7)
    candidate_mesh = (
        _step_mesh(step_shape, tessellation)
        if verify_step_geometry
        else _load_candidate(directory / "reconstruction.stl")
    )
    target_points = surface_samples(data.mesh)
    candidate_points = surface_samples(candidate_mesh)
    candidate_to_target = _point_mesh_distances(data.mesh, candidate_points)
    target_to_candidate = _point_mesh_distances(candidate_mesh, target_points)
    distances = np.concatenate((candidate_to_target, target_to_candidate))

    rms = float(np.sqrt(np.mean(distances**2)))
    p95 = float(np.percentile(distances, 95))
    maximum = float(np.max(distances))
    local_max = None
    if verify_step_geometry:
        local_max = max(
            float(np.max(_point_mesh_distances(candidate_mesh, _local_samples(data.mesh)))),
            float(np.max(_point_mesh_distances(data.mesh, _local_samples(candidate_mesh)))),
        )
    candidate_is_watertight = bool(candidate_mesh.is_watertight)
    volume_comparable = bool(
        data.mesh.is_watertight
        and (valid_solid if verify_step_geometry else candidate_is_watertight)
    )
    target_volume = abs(float(data.mesh.volume)) if volume_comparable else None
    candidate_solid_volume = (
        sum(abs(float(solid.Volume())) for solid in solids)
        if verify_step_geometry and valid_solid
        else abs(float(candidate_mesh.volume)) if candidate_is_watertight else None
    )
    candidate_volume = candidate_solid_volume if volume_comparable else None
    volume_error = (
        abs(candidate_volume - target_volume) / max(target_volume, 1e-9) * 100
        if target_volume is not None and candidate_volume is not None
        else 0.0
    )
    valid = valid_solid if require_solid else valid_brep
    diagonal = max(data.diagonal, 1e-9)
    score = (
        rms / diagonal
        + 0.5 * p95 / diagonal
        + 0.2 * volume_error / 100
        + complexity
        + (0 if valid else 100)
    )
    return ScoreReport(
        score=float(score),
        chamfer_rms_mm=rms,
        chamfer_p95_mm=p95,
        chamfer_max_mm=maximum,
        volume_error_percent=float(volume_error),
        valid_solid=valid_solid,
        candidate_count=candidate_count,
        valid_brep=valid_brep,
        volume_comparable=volume_comparable,
        step_geometry_verified=verify_step_geometry,
        component_count_match=(
            data.report.body_count == len(step_shape.Shells())
            if data.mesh.is_watertight and verify_step_geometry
            else None
        ),
        source_component_count=data.report.body_count,
        step_shell_count=len(step_shape.Shells()),
        verification_tessellation_mm=tessellation if verify_step_geometry else None,
        local_max_mm=local_max,
    )


def score_plan(
    data: MeshData,
    plan: ReconstructionPlan,
    directory: Path,
    candidate_count: int,
) -> ScoredCandidate:
    # Search candidates use a coarser preview tessellation. The STEP B-rep is
    # identical, while avoiding high-quality browser meshing for every plan in
    # the search. The selected final model is exported at display quality.
    export_plan(plan, directory, high_quality_stl=False)
    # Conciseness is only a tie-breaker. A larger penalty caused small real
    # fillets, chamfers, and holes to be deleted because omitting them reduced
    # the score more than their localized surface error increased it.
    active_operation_count = sum(
        not operation.suppressed for operation in plan.operations
    )
    complexity = (
        active_operation_count + len(getattr(plan.base, "holes", []))
    ) * 0.00001
    report = score_exported_shape(
        data,
        directory,
        candidate_count=candidate_count,
        complexity=complexity,
        verify_step_geometry=False,
    )
    return ScoredCandidate(plan=plan, report=report, directory=directory)
