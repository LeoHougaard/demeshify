from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cadquery as cq
import numpy as np
import point_cloud_utils as pcu
import trimesh

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


def score_plan(
    data: MeshData,
    plan: ReconstructionPlan,
    directory: Path,
    candidate_count: int,
) -> ScoredCandidate:
    export_plan(plan, directory)
    candidate_mesh = _load_candidate(directory / "reconstruction.stl")

    target_points = surface_samples(data.mesh)
    candidate_points = surface_samples(candidate_mesh)
    candidate_to_target = _point_mesh_distances(data.mesh, candidate_points)
    target_to_candidate = _point_mesh_distances(candidate_mesh, target_points)
    distances = np.concatenate((candidate_to_target, target_to_candidate))

    rms = float(np.sqrt(np.mean(distances**2)))
    p95 = float(np.percentile(distances, 95))
    maximum = float(np.max(distances))
    target_volume = abs(float(data.mesh.volume)) if data.mesh.is_watertight else None
    candidate_volume = abs(float(candidate_mesh.volume))
    volume_error = (
        abs(candidate_volume - target_volume) / max(target_volume, 1e-9) * 100
        if target_volume is not None
        else 0.0
    )
    # OCCT is authoritative for the STEP solid. STL tessellation can contain seam
    # duplication at tangent or coplanar boolean boundaries even when the B-rep is
    # valid.  Meshing can also attach a triangulation that makes ``isValid`` on the
    # in-memory object report false after STL export, so reload and validate the
    # neutral B-rep rather than the tessellated object.
    step_shape = cq.importers.importStep(str(directory / "reconstruction.step")).val()
    valid = bool(step_shape.isValid() and candidate_volume > 0)
    diagonal = max(data.diagonal, 1e-9)
    # Conciseness is only a tie-breaker. A larger penalty caused small real
    # fillets, chamfers, and holes to be deleted because omitting them reduced
    # the score more than their localized surface error increased it.
    active_operation_count = sum(
        not operation.suppressed for operation in plan.operations
    )
    complexity = (
        active_operation_count + len(getattr(plan.base, "holes", []))
    ) * 0.00001
    score = (
        rms / diagonal
        + 0.5 * p95 / diagonal
        + 0.2 * volume_error / 100
        + complexity
        + (0 if valid else 100)
    )
    report = ScoreReport(
        score=float(score),
        chamfer_rms_mm=rms,
        chamfer_p95_mm=p95,
        chamfer_max_mm=maximum,
        volume_error_percent=float(volume_error),
        valid_solid=valid,
        candidate_count=candidate_count,
    )
    return ScoredCandidate(plan=plan, report=report, directory=directory)
