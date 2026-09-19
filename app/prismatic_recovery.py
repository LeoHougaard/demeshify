"""Recover constant-section solids before attempting independent face trimming."""

from __future__ import annotations

import json
import math
import shutil
import tempfile
from collections import Counter
from pathlib import Path

import cadquery as cq
import numpy as np

from .cad import build_plan
from .mesh import MeshData, mesh_warnings
from .profiles import AXES, _clean, generate_layered_candidates, generate_prismatic_candidates
from .schemas import Axis, ReconstructionReport, SurfaceBRepReport
from .scoring import score_exported_shape
from .surface_graph import SurfaceGraph, _plane_basis, detect_surface_graph
from .verification import acceptance_threshold, passes_geometry_gate


def _cut_conical_bores(
    shape: cq.Shape, data: MeshData, graph: SurfaceGraph, axis: Axis,
) -> cq.Shape | None:
    """Restore complete concave cone bands omitted by midpoint layer sections."""
    direction = AXES[axis][0]
    first, second = _plane_basis(direction)
    tolerance = max(data.diagonal * 0.00003, 0.002)
    tools = []
    for patch in graph.conical_patches:
        if abs(float(patch.axis @ direction)) < 1 - 1e-8:
            continue
        points = np.asarray(data.mesh.vertices[patch.vertex_indices])
        relative = points - patch.apex
        angles = np.sort(np.mod(np.arctan2(relative @ second, relative @ first), 2 * math.pi))
        coverage = 2 * math.pi - float(np.max(np.diff(np.r_[angles, angles[0] + 2 * math.pi])))
        if coverage < math.radians(330):
            continue
        heights = points @ direction
        start, end = _clean(float(np.min(heights))), _clean(float(np.max(heights)))
        apex_height = float(patch.apex @ direction)
        tangent = math.tan(patch.semi_angle)
        radius_start = _clean(abs(start - apex_height) * tangent)
        radius_end = _clean(abs(end - apex_height) * tangent)
        if min(radius_start, radius_end) <= tolerance or end - start <= tolerance:
            continue
        if abs(radius_end - radius_start) < 1e-6:
            continue
        origin = np.array([_clean(float(value)) for value in
                           patch.apex + (start - apex_height) * direction])
        # Match the sketch's coordinate precision only when every source node
        # still validates the rounded cone, including both rim radii.
        relative = points - origin
        axial = relative @ direction
        radial = relative - np.outer(axial, direction)
        slope = (radius_end - radius_start) / (end - start)
        errors = np.abs(np.linalg.norm(radial, axis=1) - (radius_start + slope * axial))
        if float(np.max(errors)) > tolerance:
            continue
        centers = np.asarray(data.mesh.triangles_center[patch.face_indices]) - origin
        radial = centers - np.outer(centers @ direction, direction)
        expected = radial / np.maximum(np.linalg.norm(radial, axis=1)[:, None], 1e-12)
        expected -= slope * direction
        expected /= np.linalg.norm(expected, axis=1)[:, None]
        alignment = np.einsum("ij,ij->i", data.mesh.face_normals[patch.face_indices], expected)
        if float(np.median(alignment)) > -0.95:
            continue
        tools.append(cq.Solid.makeCone(
            radius_start, radius_end, end - start, cq.Vector(*origin), cq.Vector(*direction),
        ))
    return shape.cut(*tools) if tools else None


def recover_prismatic_solid(
    data: MeshData, run_id: str, directory: Path,
) -> ReconstructionReport | None:
    if not data.report.watertight or data.report.body_count != 1:
        return None
    # Avoid expensive repeated sections of dense scans. The general surface
    # engine handles those without this optional construction attempt.
    if len(data.mesh.faces) > 50_000:
        return None
    def candidates():
        name = Path(data.report.file_name).stem
        for generate, consistency, method in (
            (generate_prismatic_candidates, 1 - 1e-6, "constant_section_extrusion"),
            (generate_layered_candidates, 0.999, "layered_extrusion"),
        ):
            try:
                proposals = (
                    generate(data, name, consistency)
                    if generate is generate_prismatic_candidates
                    else generate(data, name, consistency, 3)
                )
            except Exception:
                continue
            for candidate in proposals:
                if (
                    candidate.section_consistency >= consistency
                    and len(candidate.plan.operations) <= 3
                ):
                    yield candidate, method, False
        # Midpoint sections omit short countersinks at either end of a bore.
        # Thin layered parts can still be reconstructed as sketches plus the
        # independently recognized conical cuts. Full STEP checks decide
        # acceptance; the section-volume estimate only screens proposals.
        if min(data.mesh.extents) / max(data.mesh.extents) < 0.12:
            try:
                proposals = generate_layered_candidates(
                    data, name, 0.98, 3, profile_tolerance_scale=0.1,
                )
            except Exception:
                return
            for candidate in proposals:
                if candidate.section_consistency >= 0.98 and len(candidate.plan.operations) <= 3:
                    yield candidate, "layered_extrusion_with_conical_bores", True

    for candidate, method, restore_bores in candidates():
        with tempfile.TemporaryDirectory(dir=directory, prefix="sweep-") as temporary:
            working = Path(temporary)
            try:
                shape = build_plan(candidate.plan).val()
                if not shape.isValid() or len(shape.Solids()) != 1:
                    continue
                if restore_bores:
                    graph = detect_surface_graph(data)
                    shape = _cut_conical_bores(shape, data, graph, candidate.plan.base.axis)
                    if shape is None or not shape.isValid() or len(shape.Solids()) != 1:
                        continue
                source_volume = abs(float(data.mesh.volume))
                if abs(shape.Volume() - source_volume) > source_volume * 0.02:
                    continue
                graph = detect_surface_graph(data)
                faces = shape.Faces()
                fitted_areas: Counter[str] = Counter()
                for patch in graph.patches:
                    if (
                        patch.kind in {"cylinder", "cone", "sphere", "torus"}
                        and patch.normal_error_degrees <= 1.0
                    ):
                        fitted_areas[patch.kind] += patch.area
                output_areas: Counter[str] = Counter()
                for face in faces:
                    output_areas[face.geomType().lower()] += face.Area()
                # A close polygonal silhouette can erase short circular arcs.
                # Keep recognized curved supports even when that loss would
                # fit within the absolute geometric error budget.
                if any(output_areas[kind] < area * 0.95
                       for kind, area in fitted_areas.items()):
                    continue
                cq.exporters.export(shape, str(working / "reconstruction.step"))
                score = score_exported_shape(data, working)
                if not passes_geometry_gate(score, acceptance_threshold(data.diagonal)):
                    continue
                if not shape.exportStl(
                    str(working / "reconstruction.stl"), tolerance=0.01,
                    angularTolerance=0.04, relative=False, parallel=False,
                ):
                    continue
                counts = Counter(face.geomType().lower() for face in faces)
                boundaries = [
                    [point.toTuple() for point in edge.sample(64)[0]]
                    for edge in shape.Edges()
                ]
                graph = {
                    "recovery_method": method,
                    "surfaces": [
                        {"id": f"face-{index}", "kind": face.geomType().lower(),
                         "representation": "fitted", "area_mm2": face.Area()}
                        for index, face in enumerate(faces, 1)
                    ],
                    "adjacency": [],
                    "visualization": {
                        "source_mesh_carrier": False,
                        "global_faceted_fallback": False,
                        "faceted_patch_ids": [],
                        "faceted_source_face_indices": [],
                        "surface_boundaries": boundaries,
                    },
                }
                (working / "surface_graph.json").write_text(
                    json.dumps(graph, indent=2), encoding="utf-8",
                )
                report = ReconstructionReport(
                    id=run_id, status="complete", engine="surface_brep",
                    mesh=data.report, plan=None, score=score,
                    surface=SurfaceBRepReport(
                        recognized_surface_count=len(faces), surface_counts=dict(counts),
                        adjacency_count=len(shape.Edges()), brep_face_count=len(faces),
                        solid_count=1, free_edge_count=0, sewing_tolerance_mm=0,
                        closed=True, topology_vertex_count=len(shape.Vertices()),
                        topology_edge_count=len(shape.Edges()),
                    ),
                    warnings=mesh_warnings(data), elapsed_seconds=0,
                )
            except Exception:
                # This construction is optional; its failure must leave the
                # original surface recognizer and exact-facet recovery usable.
                continue
            for name in ("reconstruction.step", "reconstruction.stl", "surface_graph.json"):
                shutil.copy2(working / name, directory / name)
            return report
    return None
