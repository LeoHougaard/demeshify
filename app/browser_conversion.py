"""Run the surface engine inside a dedicated browser worker."""

import json
from collections.abc import Callable
from pathlib import Path

import cadquery as cq
from OCP.BRep import BRep_Tool

from .progress import apply_stage, new_progress, public_progress
from .surface_reconstruction import (
    _preserve_analytic_diagnostic,
    _reconstruct_faceted_only,
    _report_has_valid_solid,
    reconstruct_surfaces,
)


def _add_step_edges(directory: Path) -> None:
    graph_path = directory / "surface_graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.exists() else {}
    model = cq.importers.importStep(str(directory / "reconstruction.step"))
    edges = set()
    for face in model.faces().vals():
        for edge in face.Edges():
            if not BRep_Tool.IsClosed_s(edge.wrapped, face.wrapped):
                edges.add(edge)

    curves = []
    for edge in edges:
        try:
            deflection = min(0.02, max(edge.Length() * 0.0005, 1e-7))
            points, _ = edge.sample(deflection)
        except (ValueError, RuntimeError):
            continue
        if len(points) >= 2:
            curve = [point.toTuple() for point in points]
            if edge.IsClosed():
                curve.append(curve[0])
            curves.append(curve)
    if curves:
        graph.setdefault("visualization", {})["step_edges"] = curves
        graph_path.write_text(json.dumps(graph), encoding="utf-8")


def convert(
    input_path: str,
    filename: str,
    units: str,
    run_id: str,
    send_progress: Callable[[str], None],
    recovery_reason: str = "",
) -> str:
    path = Path(input_path)
    progress = new_progress(run_id)

    def update(label: str) -> None:
        apply_stage(progress, label)
        # Browser artifacts become downloadable together at job completion.
        progress["preview_path"] = None
        send_progress(json.dumps(public_progress(progress)))

    pending_recovery = ""
    if recovery_reason:
        report = _reconstruct_faceted_only(run_id, path, filename, units, recovery_reason, update)
    else:
        report = reconstruct_surfaces(run_id, path, filename, units, update, isolate=False)
        if report.mesh.watertight and not _report_has_valid_solid(report):
            _preserve_analytic_diagnostic(path.parent)
            pending_recovery = "the analytic STEP did not pass the solid and geometry checks"
    surface = report.surface
    if (not pending_recovery and surface and not surface.faceted_fallback
            and 0 < surface.brep_face_count <= 2000):
        try:
            _add_step_edges(path.parent)
        except Exception:
            # An edge display failure must leave the STEP and preview downloadable.
            pass
    return json.dumps({
        "report": json.loads(report.model_dump_json()),
        "pending_recovery": pending_recovery,
    })
