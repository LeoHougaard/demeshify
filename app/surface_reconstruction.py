from __future__ import annotations

import struct
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .mesh import load_mesh
from .schemas import ReconstructionReport, SurfaceBRepReport
from .scoring import score_exported_shape
from .storage import atomic_write_text
from .surface_brep import build_faceted_brep, build_surface_brep, export_surface_brep


def _acceptance_threshold(diagonal: float) -> float:
    return max(0.12, diagonal * 0.003)


def _passes_surface_gate(result: object, score: object, threshold: float) -> bool:
    return bool(
        result.valid
        and result.closed
        and not result.faceted_fallback
        and not result.source_mesh_fallback
        and score.valid_brep
        and score.valid_solid
        and result.free_edge_count == 0
        and score.chamfer_p95_mm <= threshold
        and score.volume_error_percent <= 2.0
    )


def _report_has_valid_solid(report: ReconstructionReport) -> bool:
    """Apply the geometric/STEP gate without relabeling facets as analytic."""

    surface = report.surface
    score = report.score
    diagonal = sum(value * value for value in report.mesh.dimensions_mm) ** 0.5
    return bool(
        surface
        and score
        and surface.closed
        and surface.free_edge_count == 0
        and score.valid_brep
        and score.valid_solid
        and score.chamfer_p95_mm <= _acceptance_threshold(diagonal)
        and score.volume_error_percent <= 2.0
    )


def _preserve_analytic_diagnostic(directory: Path) -> None:
    """Keep a failed fitted result beside the verified fallback carrier."""

    for source_name, diagnostic_name in (
        ("reconstruction.step", "analytic_diagnostic.step"),
        ("reconstruction.stl", "analytic_diagnostic.stl"),
        ("joined_surfaces.step", "analytic_diagnostic_joined_surfaces.step"),
        ("surface_graph.json", "analytic_diagnostic_surface_graph.json"),
        ("report.json", "analytic_diagnostic_report.json"),
    ):
        source = directory / source_name
        if source.is_file():
            source.replace(directory / diagnostic_name)


def _reconstruct_surfaces_direct(
    run_id: str,
    stl_path: Path,
    original_name: str,
    input_units: str = "mm",
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
    threshold = _acceptance_threshold(data.diagonal)
    # Preserve the fitted artifact even when it misses a downstream topology
    # or geometry gate. Replacing it in-place with the source triangle carrier
    # hid the actual reconstruction failure and made a faceted model appear to
    # be a successful analytic result. Process-level crash/timeout recovery is
    # still isolated below, but it is explicitly reported as best effort.
    update("surface_export_start")
    export_surface_brep(
        result,
        stl_path.parent,
        data.source_path,
        source_unit_scale=data.report.unit_scale,
    )
    update("surface_verification_start preview=reconstruction.stl")
    score = score_exported_shape(data, stl_path.parent, require_solid=False)
    passed = _passes_surface_gate(result, score, threshold)

    warnings = list(result.warnings)
    if result.point_fitted_face_count:
        warnings.append(
            f"{result.point_fitted_face_count} residual regions were represented "
            "by B-spline surfaces fitted to STL boundary and interior nodes."
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
        source_mesh_fallback=result.source_mesh_fallback,
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
    )
    atomic_write_text(stl_path.parent / "report.json", report.model_dump_json(indent=2))
    update(
        f"surface_reconstruction_complete surfaces={len(result.graph.patches)} "
        "preview=reconstruction.stl"
    )
    return report


def _reconstruct_faceted_only(
    run_id: str,
    stl_path: Path,
    original_name: str,
    input_units: str,
    reason: str,
    progress_callback: Callable[[str], None] | None,
) -> ReconstructionReport:
    started = time.perf_counter()

    def update(label: str) -> None:
        if progress_callback is not None:
            progress_callback(label)

    update("faceted_recovery_started")
    data = load_mesh(stl_path, original_name, input_units)
    result = build_faceted_brep(data, reason=reason)
    export_surface_brep(
        result,
        stl_path.parent,
        data.source_path,
        source_unit_scale=data.report.unit_scale,
        verify_roundtrip=False,
    )
    score = score_exported_shape(data, stl_path.parent, require_solid=False)
    passed = _passes_surface_gate(
        result,
        score,
        _acceptance_threshold(data.diagonal),
    )
    surface = SurfaceBRepReport(
        recognized_surface_count=0,
        surface_counts=result.surface_counts,
        adjacency_count=0,
        brep_face_count=result.face_count,
        solid_count=result.solid_count,
        free_edge_count=result.free_edge_count,
        sewing_tolerance_mm=result.sewing_tolerance,
        closed=result.closed,
        topology_vertex_count=result.topology_vertex_count,
        topology_edge_count=result.topology_edge_count,
        faceted_fallback=True,
        faceted_patch_count=1,
        faceted_face_count=result.faceted_face_count,
        source_mesh_fallback=True,
    )
    report = ReconstructionReport(
        id=run_id,
        status="complete" if passed else "best_effort",
        engine="surface_brep",
        mesh=data.report,
        plan=None,
        surface=surface,
        score=score,
        warnings=result.warnings,
        elapsed_seconds=time.perf_counter() - started,
    )
    atomic_write_text(stl_path.parent / "report.json", report.model_dump_json(indent=2))
    update(
        f"faceted_recovery_complete faces={result.faceted_face_count} "
        "preview=reconstruction.stl"
    )
    return report


def reconstruct_surfaces(
    run_id: str,
    stl_path: Path,
    original_name: str,
    input_units: str = "mm",
    progress_callback: Callable[[str], None] | None = None,
    *,
    isolate: bool = True,
) -> ReconstructionReport:
    """Run native surface fitting in isolation and recover with exact facets.

    OpenCascade fitting routines can terminate the Python process for a small
    class of malformed parameterizations. A child process contains that native
    failure. The parent accepts its verified report or constructs the manifold
    source-topology carrier when the child crashes or exceeds its time budget.
    """

    if not isolate:
        return _reconstruct_surfaces_direct(
            run_id,
            stl_path,
            original_name,
            input_units,
            progress_callback,
        )
    if progress_callback is not None:
        progress_callback("surface_isolated_worker_start")
    worker_timeout = 120
    try:
        file_size = stl_path.stat().st_size
        with stl_path.open("rb") as stream:
            stream.seek(80)
            triangle_count = struct.unpack("<I", stream.read(4))[0]
        if file_size == 84 + triangle_count * 50 and triangle_count > 50_000:
            # Near-threshold dense cases finish their analytic pass in roughly
            # 54 seconds before process startup/export overhead, so a 60-second
            # cap discarded completed graphs. Larger meshes retain the shorter
            # budget because constructing their recovery carrier also consumes
            # a substantial share of the corpus-level 240-second safety bound.
            worker_timeout = 90 if triangle_count <= 60_000 else 60
    except (OSError, struct.error):
        pass
    worker_progress_path = stl_path.parent / "surface-worker-progress.log"
    worker_progress_path.unlink(missing_ok=True)
    command = [
        sys.executable,
        "-m",
        "app.surface_worker",
        "--run-id",
        run_id,
        "--stl-path",
        str(stl_path.resolve()),
        "--original-name",
        original_name,
        "--input-units",
        input_units,
        "--progress-path",
        str(worker_progress_path.resolve()),
    ]
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        completed = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=worker_timeout,
            creationflags=creation_flags,
            check=False,
        )
    except subprocess.TimeoutExpired:
        completed = None
        reason = (
            "the isolated analytic worker exceeded "
            f"{worker_timeout} seconds"
        )
    else:
        reason = (
            "the isolated analytic worker terminated unexpectedly "
            f"(exit code {completed.returncode})"
        )
    report_path = stl_path.parent / "report.json"
    if completed is not None and completed.returncode == 0 and report_path.is_file():
        try:
            report = ReconstructionReport.model_validate_json(
                report_path.read_text(encoding="utf-8")
            )
            if _report_has_valid_solid(report) or not report.mesh.watertight:
                if progress_callback is not None:
                    progress_callback(
                        "surface_isolated_worker_done preview=reconstruction.stl"
                    )
                return report
            _preserve_analytic_diagnostic(stl_path.parent)
            reason = (
                "the isolated analytic reconstruction did not pass the strict "
                "solid, error, and STEP round-trip gate; its diagnostic artifacts "
                "were preserved separately"
            )
            if progress_callback is not None:
                progress_callback(
                    "surface_isolated_worker_invalid recovering=faceted"
                )
        except Exception:
            reason = "the isolated analytic worker returned an unreadable report"
    if progress_callback is not None:
        progress_callback("surface_isolated_worker_recovering")
    return _reconstruct_faceted_only(
        run_id,
        stl_path,
        original_name,
        input_units,
        reason,
        progress_callback,
    )
