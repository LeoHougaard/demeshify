from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from .mesh import load_mesh
from .schemas import ReconstructionReport, SurfaceBRepReport
from .scoring import score_exported_shape
from .surface_brep import build_surface_brep, export_surface_brep


def _acceptance_threshold(diagonal: float) -> float:
    return max(0.12, diagonal * 0.003)


def reconstruct_surfaces(
    run_id: str,
    stl_path: Path,
    original_name: str,
    input_units: str = "mm",
    prompt: str = "",
    progress_callback: Callable[[str], None] | None = None,
) -> ReconstructionReport:
    """Recover, trim, sew, and verify a surface-based OpenCascade solid."""

    started = time.perf_counter()

    def update(label: str) -> None:
        if progress_callback is not None:
            progress_callback(label)

    update("reconstruction_started")
    data = load_mesh(stl_path, original_name, input_units)
    update(
        f"mesh_loaded triangles={data.report.triangle_count} "
        f"watertight={data.report.watertight}"
    )
    update("surface_detection_start")
    result = build_surface_brep(data, update)
    update("surface_export_start")
    export_surface_brep(result, stl_path.parent)
    update("surface_verification_start preview=reconstruction.stl")
    score = score_exported_shape(data, stl_path.parent, require_solid=False)

    warnings = list(result.warnings)
    if result.point_fitted_face_count:
        warnings.append(
            f"{result.point_fitted_face_count} residual regions were represented "
            "by B-spline surfaces fitted to STL boundary and interior nodes."
        )
    threshold = _acceptance_threshold(data.diagonal)
    passed = (
        result.valid
        and result.closed
        and score.valid_brep
        and score.valid_solid
        and result.free_edge_count == 0
        and score.chamfer_p95_mm <= threshold
        and score.volume_error_percent <= 2.0
    )
    if not passed:
        message = (
            "Surface recognition produced a valid fitted B-rep face set, but the "
            "joining experiment is not yet watertight. Closure is not used as a "
            "recognition criterion."
            if result.valid and score.valid_brep and not result.closed
            else "The fitted surface model did not meet the automatic "
            "high-confidence tolerance; inspect it before manufacturing."
        )
        warnings.append(message)

    surface = SurfaceBRepReport(
        recognized_surface_count=len(result.graph.patches),
        surface_counts=result.surface_counts,
        adjacency_count=len(result.graph.adjacency),
        brep_face_count=result.face_count,
        solid_count=result.solid_count,
        free_edge_count=result.free_edge_count,
        sewing_tolerance_mm=result.sewing_tolerance,
        closed=result.closed,
        point_fitted_face_count=result.point_fitted_face_count,
        topology_vertex_count=result.topology_vertex_count,
        topology_edge_count=result.topology_edge_count,
        faceted_fallback=result.faceted_fallback,
        faceted_patch_count=result.faceted_patch_count,
        faceted_face_count=result.faceted_face_count,
    )
    report = ReconstructionReport(
        id=run_id,
        status="complete" if passed else "best_effort",
        engine="surface_brep",
        mesh=data.report,
        plan=None,
        surface=surface,
        score=score,
        warnings=warnings,
        elapsed_seconds=time.perf_counter() - started,
        prompt=prompt,
    )
    (stl_path.parent / "report.json").write_text(
        report.model_dump_json(indent=2),
        encoding="utf-8",
    )
    update(
        f"surface_reconstruction_complete surfaces={len(result.graph.patches)} "
        "preview=reconstruction.stl"
    )
    return report
