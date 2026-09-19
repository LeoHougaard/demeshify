from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import point_cloud_utils as pcu
import trimesh

from .mesh import MeshData, load_mesh, mesh_warnings
from .prismatic_recovery import recover_prismatic_solid
from .schemas import ReconstructionReport, SurfaceBRepReport
from .scoring import _local_samples, _point_mesh_distances, score_exported_shape
from .storage import atomic_write_text
from .surface_brep import build_faceted_brep, build_surface_brep, export_surface_brep
from .verification import acceptance_threshold, passes_geometry_gate, verification_warnings


def _acceptance_threshold(diagonal: float) -> float:
    return acceptance_threshold(diagonal)


def _passes_surface_gate(result: object, score: object, threshold: float) -> bool:
    return bool(
        result.valid
        and result.closed
        and not result.faceted_fallback
        and not result.source_mesh_fallback
        and result.free_edge_count == 0
        and passes_geometry_gate(score, threshold)
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
        and passes_geometry_gate(score, _acceptance_threshold(diagonal))
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
    recovered = recover_prismatic_solid(data, run_id, stl_path.parent)
    if recovered is not None:
        recovered.elapsed_seconds = time.perf_counter() - started
        atomic_write_text(stl_path.parent / "report.json", recovered.model_dump_json(indent=2))
        update("surface_reconstruction_complete preview=reconstruction.stl")
        return recovered
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

    if (
        not passed and data.report.watertight and data.report.body_count == 1
        and len(data.mesh.faces) < 20_000 and not result.graph.freeform_patches
        and time.perf_counter() - started < 60
    ):
        # Broad trim tolerances can collapse a small shoulder or hole boundary.
        # Retry only failed constructions; existing accepted models are retained.
        # The worker's process deadline also bounds this optional attempt.
        update("surface_fine_trimming_start")
        with tempfile.TemporaryDirectory(dir=stl_path.parent, prefix="fine-trim-") as temporary:
            working = Path(temporary)
            try:
                refined = build_surface_brep(data, update, trimming_tolerance_scale=0.1)
                export_surface_brep(
                    refined, working, data.source_path,
                    source_unit_scale=data.report.unit_scale,
                )
                refined_score = score_exported_shape(data, working, require_solid=False)
                if _passes_surface_gate(refined, refined_score, threshold):
                    for name in ("reconstruction.step", "reconstruction.stl", "surface_graph.json"):
                        shutil.copy2(working / name, stl_path.parent / name)
                    result, score, passed = refined, refined_score, True
                    result.warnings.append("Recovered the solid with a finer trimming tolerance.")
            except Exception:
                pass

    warnings = [*mesh_warnings(data), *result.warnings]
    warnings.extend(verification_warnings(score, threshold))
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
    recovery_data = data
    if data.report.watertight and len(data.mesh.faces) > 20_000:
        update("faceted_recovery_simplifying")
        try:
            source_samples = _local_samples(data.mesh)
            reduction_vertices = np.asarray(data.mesh.vertices)
            reduction_faces = np.asarray(data.mesh.faces, dtype=np.int32)
            budgets = (12_000, 24_000, 48_000, 60_000)
            staging_budget = 48_000
            if len(reduction_faces) > staging_budget * 4:
                # Collapse the millions of finest edges once. Each budget then
                # starts from the same intermediate, while all acceptance
                # checks still use the original full-resolution input.
                reduction_vertices, reduction_faces, _, _ = pcu.decimate_triangle_mesh(
                    reduction_vertices, reduction_faces, staging_budget,
                )
            for budget in budgets:
                if budget >= len(data.mesh.faces):
                    break
                if budget > staging_budget:
                    # Very thin parts can still lose more than 2% volume at
                    # 48,000 faces. Retry from the original mesh: a coarser
                    # intermediate cannot recover the discarded detail.
                    vertices, faces, _, _ = pcu.decimate_triangle_mesh(
                        np.asarray(data.mesh.vertices),
                        np.asarray(data.mesh.faces, dtype=np.int32), budget,
                    )
                elif budget < len(reduction_faces):
                    vertices, faces, _, _ = pcu.decimate_triangle_mesh(
                        reduction_vertices, reduction_faces, budget,
                    )
                else:
                    vertices, faces = reduction_vertices, reduction_faces
                mesh = trimesh.Trimesh(vertices, faces, process=True)
                if (
                    not mesh.is_watertight or not mesh.is_winding_consistent
                    or mesh.body_count != data.report.body_count
                    or abs(mesh.volume - data.mesh.volume) > abs(data.mesh.volume) * 0.02
                ):
                    continue
                deviation = max(
                    float(np.max(_point_mesh_distances(mesh, source_samples))),
                    float(np.max(_point_mesh_distances(data.mesh, _local_samples(mesh)))),
                )
                if deviation <= _acceptance_threshold(data.diagonal):
                    recovery_data = MeshData(mesh, data.report, data.source_path)
                    break
        except (RuntimeError, ValueError):
            pass
    simplified = recovery_data is not data
    with tempfile.TemporaryDirectory(dir=stl_path.parent, prefix="faceted-") as temporary:
        source_scale = data.report.unit_scale
        if simplified:
            recovery_data.source_path = Path(temporary) / "input-mm.stl"
            recovery_data.mesh.export(recovery_data.source_path)
            source_scale = 1.0
        result = build_faceted_brep(recovery_data, reason=reason)
        if simplified:
            result.warnings = [
                f"Built a simplified faceted STEP with {result.face_count:,} faces "
                f"from {data.report.triangle_count:,} source triangles because {reason}. "
                "Verification compares the STEP with the full-resolution input mesh."
            ]
        export_surface_brep(
            result, stl_path.parent, recovery_data.source_path,
            source_unit_scale=source_scale, verify_roundtrip=False,
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
        simplified_fallback=simplified,
        fallback_geometry_verified=bool(
            result.closed and passes_geometry_gate(score, _acceptance_threshold(data.diagonal))
        ),
    )
    report = ReconstructionReport(
        id=run_id,
        status="complete" if passed else "best_effort",
        engine="surface_brep",
        mesh=data.report,
        plan=None,
        surface=surface,
        score=score,
        warnings=[
            *mesh_warnings(data), *result.warnings,
            *verification_warnings(score, _acceptance_threshold(data.diagonal)),
        ],
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

    started = time.perf_counter()
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
    except subprocess.TimeoutExpired as exc:
        completed = None
        worker_stdout, worker_stderr = exc.stdout, exc.stderr
        reason = (
            "the isolated analytic worker exceeded "
            f"{worker_timeout} seconds"
        )
    else:
        worker_stdout = getattr(completed, "stdout", "")
        worker_stderr = getattr(completed, "stderr", "")
        reason = (
            "the isolated analytic worker terminated unexpectedly "
            f"(exit code {completed.returncode})"
        )
    for name, output in (
        ("surface-worker.stdout.log", worker_stdout),
        ("surface-worker.stderr.log", worker_stderr),
    ):
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        (stl_path.parent / name).write_text(output or "", encoding="utf-8")
    report_path = stl_path.parent / "report.json"
    if completed is not None and completed.returncode == 0 and report_path.is_file():
        try:
            report = ReconstructionReport.model_validate_json(
                report_path.read_text(encoding="utf-8")
            )
            if _report_has_valid_solid(report) or not report.mesh.watertight:
                report.elapsed_seconds = time.perf_counter() - started
                atomic_write_text(report_path, report.model_dump_json(indent=2))
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
    _preserve_analytic_diagnostic(stl_path.parent)
    report = _reconstruct_faceted_only(
        run_id,
        stl_path,
        original_name,
        input_units,
        reason,
        progress_callback,
    )
    report.elapsed_seconds = time.perf_counter() - started
    atomic_write_text(report_path, report.model_dump_json(indent=2))
    return report
