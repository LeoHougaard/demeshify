import json
from types import SimpleNamespace

import cadquery as cq
import numpy as np
import pytest
import trimesh
from OCP.BRep import BRep_Tool
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
from OCP.BRepLib import BRepLib
from OCP.BRepTools import BRepTools

from tools import reference_mesh as reference


def box():
    return cq.Workplane('XY').box(2, 2, 2).val()


def tetra_mesh():
    return trimesh.Trimesh(
        vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
        faces=[[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]], process=False,
    )


def native_tetrahedron():
    mesh = tetra_mesh()
    sewing = BRepBuilderAPI_Sewing(1e-7)
    for indices in mesh.faces:
        wire = cq.Wire.makePolygon([cq.Vector(*mesh.vertices[i]) for i in indices], close=True)
        sewing.Add(cq.Face.makeFromWires(wire).wrapped)
    sewing.Perform()
    shell = cq.Shape.cast(sewing.SewedShape()).Shells()[0]
    native = BRepBuilderAPI_MakeSolid(shell.wrapped).Solid()
    BRepLib.OrientClosedSolid_s(native)
    return cq.Shape.cast(native)


@pytest.mark.parametrize('kind', ['box', 'cylinder', 'through_hole', 'multiple_solids'])
def test_tiny_owned_reference_primitives_have_complete_ready_meshes(kind):
    shape, expected_solids, euler = box(), 1, 2
    if kind == 'cylinder':
        shape = cq.Workplane('XY').circle(1).extrude(2).val()
    elif kind == 'through_hole':
        shape = cq.Workplane('XY').box(2, 2, 2).faces('>Z').workplane().hole(.5).val()
        euler = 0
    elif kind == 'multiple_solids':
        shape = cq.Compound.makeCompound([box(), box().translate((4, 0, 0))])
        expected_solids, euler = 2, 4
    result = reference.validated_reference_mesh(
        shape, .005, expected_solid_count=expected_solids, expected_shell_count=expected_solids,
    )
    assert result.mesh.is_watertight and result.mesh.is_winding_consistent
    assert result.mesh.euler_number == euler
    assert result.diagnostics['fixture_ready'] is True
    assert result.diagnostics['deflection_mm'] == .005
    assert result.diagnostics['angular_tolerance_rad'] == .04
    assert result.diagnostics['mesh_face_connected_components'] == expected_solids
    assert result.diagnostics['unrepresented_native_faces'] == []
    assert len(result.diagnostics['native_face_triangle_counts']) == len(shape.Faces())
    assert all(result.diagnostics['native_face_triangle_counts'])
    # Reference readiness is never a Demeshify pass/fallback classification.
    assert not {'accepted', 'strict_accepted', 'faceted_face_count'} & result.diagnostics.keys()


def test_exact_triangular_plane_fastpath_does_not_require_occt_triangulations():
    shape = native_tetrahedron()
    assert shape.isValid()
    BRepTools.Clean_s(shape.wrapped)
    result = reference.validated_reference_mesh(shape, .005)
    assert result.diagnostics['exact_native_triangular_fastpath'] is True
    assert result.diagnostics['native_face_triangle_counts'] == [1, 1, 1, 1]
    assert len(result.mesh.faces) == 4 and result.mesh.is_watertight


def test_computed_closed_shell_accepts_valid_box_with_unset_advisory_closed_flag():
    shape = box()
    shell = shape.Shells()[0]
    shell.wrapped.Closed(False)
    assert shape.isValid() and not shell.wrapped.Closed()
    assert BRep_Tool.IsClosed_s(shell.wrapped)
    result = reference.validated_reference_mesh(shape, .005)
    assert result.mesh.is_watertight and result.diagnostics['fixture_ready']
    assert not shell.wrapped.Closed()  # Read topology, do not repair/overwrite metadata.


def test_advisory_closed_flag_cannot_qualify_an_actually_open_shell():
    shell = cq.Shell.makeShell(box().Faces()[:-1])
    shell.wrapped.Closed(True)
    assert shell.wrapped.Closed() and not BRep_Tool.IsClosed_s(shell.wrapped)
    maker = BRepBuilderAPI_MakeSolid(shell.wrapped)
    assert maker.IsDone()
    shape = cq.Shape.cast(maker.Solid())
    with pytest.raises(reference.ReferenceFixtureError) as caught:
        reference.validated_reference_mesh(shape, .005)
    assert caught.value.reason in ('invalid_native_brep', 'unsupported_open_native_shell')


@pytest.mark.parametrize('damage,reason', [
    ('drop_triangle', 'mesh_triangle_coverage_mismatch'),
    ('missing_native_face', 'unrepresented_native_faces'),
    ('nonfinite', 'nonfinite_mesh'),
    ('nonwatertight', 'mesh_not_watertight'),
    ('winding', 'mesh_winding_inconsistent'),
    ('wrong_components', 'mesh_component_count_mismatch'),
    ('empty', 'empty_or_malformed_mesh'),
])
def test_incomplete_or_invalid_reference_meshes_raise_distinct_fixture_errors(
    monkeypatch, damage, reason,
):
    real_mesher = reference._step_mesh

    def damaged(shape, tolerance):
        mesh = real_mesher(shape, tolerance)
        if damage == 'drop_triangle':
            mesh.update_faces(np.arange(len(mesh.faces)) != 0)
        elif damage == 'missing_native_face':
            # Remove a real native face's triangulation while leaving a
            # watertight returned mesh: aggregate mesh validity cannot mask it.
            BRepTools.Clean_s(shape.Faces()[0].wrapped)
        elif damage == 'nonfinite':
            mesh.vertices[0, 0] = np.nan
        elif damage == 'nonwatertight':
            mesh.faces[0] = mesh.faces[1]
        elif damage == 'winding':
            mesh.faces[0] = mesh.faces[0, ::-1]
        elif damage == 'wrong_components':
            # Same twelve triangle count as the box and three valid closed
            # components, so only component correspondence exposes the loss.
            mesh = trimesh.util.concatenate([
                tetra_mesh().apply_translation((4 * i, 0, 0)) for i in range(3)
            ])
        elif damage == 'empty':
            mesh = trimesh.Trimesh(vertices=[], faces=[])
        return mesh

    monkeypatch.setattr(reference, '_step_mesh', damaged)
    with pytest.raises(reference.ReferenceFixtureError) as caught:
        reference.validated_reference_mesh(box(), .005)
    assert caught.value.reason == reason
    assert caught.value.diagnostics['fixture_ready'] is False
    assert 'accepted' not in caught.value.diagnostics


def test_mesher_exceptions_are_fixture_errors_and_deflection_is_not_relaxed(monkeypatch):
    seen = []

    def failing(_shape, tolerance):
        seen.append(tolerance)
        raise ValueError('mesher omitted all verification triangles')

    monkeypatch.setattr(reference, '_step_mesh', failing)
    with pytest.raises(reference.ReferenceFixtureError) as caught:
        reference.validated_reference_mesh(box(), .00123)
    assert seen == [.00123]
    assert caught.value.reason == 'reference_preparation_error'
    assert 'omitted all' in caught.value.diagnostics['preparation_error']


def test_native_count_mismatch_is_a_fixture_metadata_error():
    with pytest.raises(reference.ReferenceFixtureError, match='native_solid_count_mismatch'):
        reference.validated_reference_mesh(box(), .005, expected_solid_count=2)
    with pytest.raises(reference.ReferenceFixtureError, match='native_shell_count_mismatch'):
        reference.validated_reference_mesh(box(), .005, expected_shell_count=2)


def test_open_or_unowned_native_geometry_is_not_usable_ground_truth():
    plane = cq.Face.makePlane(1, 1)
    with pytest.raises(reference.ReferenceFixtureError, match='unsupported_non_solid_reference'):
        reference.validated_reference_mesh(plane, .005)
    extra = cq.Compound.makeCompound([box(), cq.Edge.makeLine((5, 0, 0), (6, 0, 0))])
    with pytest.raises(reference.ReferenceFixtureError, match='unowned_native_geometry'):
        reference.validated_reference_mesh(extra, .005)


def test_nonfinite_native_geometry_is_refused_before_meshing(monkeypatch):
    monkeypatch.setattr(cq.Shape, 'BoundingBox', lambda _: SimpleNamespace(
        xmin=float('nan'), ymin=0, zmin=0, xmax=1, ymax=1, zmax=1,
    ))
    with pytest.raises(reference.ReferenceFixtureError, match='nonfinite_native_geometry'):
        reference.validated_reference_mesh(box(), .005)


@pytest.mark.parametrize('deflection', [0, -.01, float('nan'), float('inf'), True])
def test_invalid_deflection_is_a_serializable_fixture_error_before_native_meshing(
    monkeypatch, deflection,
):
    def unexpected(*_args):
        pytest.fail('An invalid deflection must not invoke the mesher')

    monkeypatch.setattr(reference, '_step_mesh', unexpected)
    with pytest.raises(reference.ReferenceFixtureError, match='invalid_deflection') as caught:
        reference.validated_reference_mesh(box(), deflection)
    json.dumps(caught.value.diagnostics, allow_nan=False)
