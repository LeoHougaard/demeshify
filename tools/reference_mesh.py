"""Qualify reference tessellation before it becomes evaluation ground truth.

This is fixture readiness, never a reconstruction success decision. Preserve the
caller's millimetre deflection and the existing _step_mesh angular setting. The
checks detect missing faces, broken meshes and unambiguous shell/component loss;
they do not certify every trimmed region, geometric deviation or feature history.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cadquery as cq
import numpy as np
import trimesh
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepTools import BRepTools_WireExplorer
from OCP.GeomAbs import GeomAbs_Line, GeomAbs_Plane
from OCP.TopLoc import TopLoc_Location
from OCP.TopTools import TopTools_IndexedMapOfShape

from app.scoring import _step_mesh
from app.step import unowned_geometry_counts


class ReferenceFixtureError(ValueError):
    """The reference cannot grade a solve; exclude/replace or repair its fixture."""

    def __init__(self, reason: str, diagnostics: dict):
        self.reason = reason
        self.diagnostics = {**diagnostics, 'fixture_ready': False, 'reason': reason}
        super().__init__(f'Reference fixture is not ready: {reason}')


@dataclass(frozen=True, slots=True)
class ReferenceMesh:
    mesh: trimesh.Trimesh
    diagnostics: dict


def _exact_triangle(face: cq.Face) -> bool:
    """Identify the existing scorer's native-triangle fast path, without meshing."""
    if BRepAdaptor_Surface(face.wrapped).GetType() != GeomAbs_Plane:
        return False
    wires = face.Wires()
    if len(wires) != 1:
        return False
    explorer = BRepTools_WireExplorer(wires[0].wrapped, face.wrapped)
    count = 0
    while explorer.More():
        if BRepAdaptor_Curve(explorer.Current()).GetType() != GeomAbs_Line:
            return False
        count += 1
        explorer.Next()
    return count == 3


def _prepare(
    shape: cq.Shape, deflection_mm: float, diagnostics: dict,
    expected_solid_count: int | None, expected_shell_count: int | None,
) -> ReferenceMesh:
    def fail(reason):
        raise ReferenceFixtureError(reason, diagnostics)

    if (isinstance(deflection_mm, bool) or not isinstance(deflection_mm, (int, float))
            or not math.isfinite(deflection_mm) or deflection_mm <= 0):
        fail('invalid_deflection')
    for count in (expected_solid_count, expected_shell_count):
        if count is not None and (isinstance(count, bool) or not isinstance(count, int)
                                  or count < 1):
            fail('invalid_expected_topology_count')
    if not shape.isValid():
        fail('invalid_native_brep')
    faces, solids, shells = shape.Faces(), shape.Solids(), shape.Shells()
    diagnostics.update(native_face_count=len(faces), native_solid_count=len(solids),
                       native_shell_count=len(shells))
    if not faces or not solids or not shells:
        fail('unsupported_non_solid_reference')
    if any(not solid.isValid() for solid in solids):
        fail('invalid_native_solid')
    unowned = unowned_geometry_counts(shape)
    diagnostics['unowned_geometry_counts'] = unowned
    if unowned:
        fail('unowned_native_geometry')
    if expected_solid_count is not None and len(solids) != expected_solid_count:
        fail('native_solid_count_mismatch')
    if expected_shell_count is not None and len(shells) != expected_shell_count:
        fail('native_shell_count_mismatch')
    # Closed() is advisory metadata; IsClosed_s computes free-boundary closure
    # for shells and cannot be satisfied just by setting the cached flag.
    if any(not BRep_Tool.IsClosed_s(shell.wrapped) for shell in shells):
        fail('unsupported_open_native_shell')
    # Shared topological edges between shells make a merged triangle mesh's
    # surface-component interpretation ambiguous. Do not invent an expected count.
    owned_edges = TopTools_IndexedMapOfShape()
    for shell in shells:
        for edge in shell.Edges():
            if owned_edges.Contains(edge.wrapped):
                fail('unsupported_shared_shell_topology')
            owned_edges.Add(edge.wrapped)
    box = shape.BoundingBox()
    bounds = [box.xmin, box.ymin, box.zmin, box.xmax, box.ymax, box.zmax]
    areas = [face.Area() for face in faces]
    volumes = [solid.Volume() for solid in solids]
    vertices = [coordinate for vertex in shape.Vertices() for coordinate in vertex.toTuple()]
    if not all(math.isfinite(value) for value in [*bounds, *areas, *volumes, *vertices]):
        fail('nonfinite_native_geometry')
    if any(area <= 0 for area in areas) or any(volume <= 0 for volume in volumes):
        fail('nonpositive_native_geometry')
    diagnostics.update(native_bounds_mm=bounds, native_area_mm2=sum(areas),
                       native_volume_mm3=sum(volumes))
    triangular = all(_exact_triangle(face) for face in faces)
    mesh = _step_mesh(shape, deflection_mm)
    if not isinstance(mesh, trimesh.Trimesh):
        fail('unsupported_mesh_result')
    points, triangles = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    if (points.ndim != 2 or points.shape[1] != 3 or not len(points)
            or triangles.ndim != 2 or triangles.shape[1] != 3 or not len(triangles)):
        fail('empty_or_malformed_mesh')
    if not np.all(np.isfinite(points)) or not np.all(np.isfinite(triangles)):
        fail('nonfinite_mesh')
    if (not np.issubdtype(triangles.dtype, np.integer) or np.min(triangles) < 0
            or np.max(triangles) >= len(points)):
        fail('invalid_mesh_indices')
    diagnostics.update(mesh_vertex_count=len(points), mesh_triangle_count=len(triangles),
                       exact_native_triangular_fastpath=triangular)
    if triangular:
        # The scorer emits each exact native triangular plane directly. Such
        # faces need not have OCCT triangulations attached at all.
        counts = [1] * len(faces)
    else:
        counts = []
        for face in faces:
            triangulation = BRep_Tool.Triangulation_s(face.wrapped, TopLoc_Location())
            counts.append(triangulation.NbTriangles() if triangulation is not None else 0)
    missing = [i for i, count in enumerate(counts) if count == 0]
    diagnostics.update(native_face_triangle_counts=counts, unrepresented_native_faces=missing)
    if missing:
        fail('unrepresented_native_faces')
    if sum(counts) != len(triangles):
        fail('mesh_triangle_coverage_mismatch')
    if not mesh.is_watertight:
        fail('mesh_not_watertight')
    if not mesh.is_winding_consistent:
        fail('mesh_winding_inconsistent')
    if not np.all(np.isfinite(mesh.area_faces)) or not math.isfinite(mesh.volume):
        fail('nonfinite_mesh_measurements')
    components = len(trimesh.graph.connected_components(
        mesh.face_adjacency, nodes=np.arange(len(triangles)), min_len=1,
    ))
    diagnostics.update(mesh_face_connected_components=components,
                       expected_face_connected_components=len(shells),
                       mesh_area_mm2=float(mesh.area), mesh_volume_mm3=float(mesh.volume),
                       mesh_dimensions_mm=np.asarray(mesh.extents).tolist())
    if components != len(shells):
        fail('mesh_component_count_mismatch')
    diagnostics['fixture_ready'] = True
    return ReferenceMesh(mesh, diagnostics)


def validated_reference_mesh(
    shape: cq.Shape, deflection_mm: float, *, expected_solid_count: int | None = None,
    expected_shell_count: int | None = None,
) -> ReferenceMesh:
    """Return a ready fine reference mesh or a distinct fixture qualification error.

    Native closed owned solids are required. Optional expected counts validate
    fixture metadata. Unsupported/open/ambiguous references are refused; callers
    must not put these errors into Demeshify solve-failure denominators.
    """
    reported_deflection = (deflection_mm if isinstance(deflection_mm, (int, float))
                           and math.isfinite(deflection_mm) else repr(deflection_mm))
    diagnostics = {'deflection_mm': reported_deflection, 'angular_tolerance_rad': .04,
                   'scope': 'reference_fixture_readiness_only'}
    try:
        return _prepare(shape, deflection_mm, diagnostics,
                        expected_solid_count, expected_shell_count)
    except ReferenceFixtureError:
        raise
    except Exception as exc:
        diagnostics['preparation_error'] = f'{type(exc).__name__}: {exc}'
        raise ReferenceFixtureError('reference_preparation_error', diagnostics) from exc
