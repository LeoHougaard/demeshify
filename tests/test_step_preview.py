from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import cadquery as cq
import numpy as np
import pytest
import trimesh
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeSolid
from OCP.BRepTools import BRepTools
from OCP.gp import gp_Ax3, gp_Dir, gp_Pnt, gp_Pnt2d, gp_Sphere
from OCP.Poly import Poly_Triangle, Poly_Triangulation
from OCP.TopoDS import TopoDS, TopoDS_Shell
from OCP.TopTools import TopTools_FormatVersion_VERSION_3

from app import step_preview


def original_bytes(shape):
    stream = BytesIO()
    BRepTools.Write_s(shape.wrapped, stream, True, False, TopTools_FormatVersion_VERSION_3)
    return stream.getvalue()


def read_preview(path):
    mesh = trimesh.load_mesh(path, process=True)
    assert isinstance(mesh, trimesh.Trimesh)
    assert np.all(np.isfinite(mesh.vertices))
    return mesh


def test_analytic_cylinder_exports_and_reopens_without_mutating_cached_source(tmp_path):
    shape = cq.Solid.makeCylinder(.25, 2)
    builder = BRep_Builder()
    for vertex in shape.Vertices():
        builder.UpdateVertex(vertex.wrapped, .04)
    shape.mesh(.1)
    before = original_bytes(shape)
    tolerances = [BRep_Tool.Tolerance_s(v.wrapped) for v in shape.Vertices()]
    path = tmp_path / 'preview.stl'
    count = step_preview.export_stl_preview(shape, path)
    mesh = read_preview(path)
    assert count == len(mesh.faces) > 12
    assert mesh.is_watertight and mesh.is_winding_consistent
    assert mesh.volume == pytest.approx(shape.Volume(), rel=.005)
    assert original_bytes(shape) == before
    assert [BRep_Tool.Tolerance_s(v.wrapped) for v in shape.Vertices()] == tolerances


def test_holed_solid_keeps_the_through_hole_and_complete_surface(tmp_path):
    body = cq.Solid.makeBox(6, 5, 2)
    cutter = cq.Solid.makeCylinder(1, 4, cq.Vector(3, 2.5, -1))
    shape = body.cut(cutter)
    path = tmp_path / 'hole.stl'
    count = step_preview.export_stl_preview(shape, path)
    mesh = read_preview(path)
    assert count == len(mesh.faces)
    assert mesh.is_watertight and mesh.is_winding_consistent
    assert mesh.euler_number == 0
    assert len(mesh.split()) == 1
    assert mesh.volume == pytest.approx(shape.Volume(), rel=.005)


def test_two_solids_are_both_exported(tmp_path):
    shape = cq.Compound.makeCompound([
        cq.Solid.makeBox(1, 2, 3), cq.Solid.makeBox(1, 2, 3, cq.Vector(4, 0, 0)),
    ])
    path = tmp_path / 'two.stl'
    count = step_preview.export_stl_preview(shape, path)
    mesh = read_preview(path)
    assert count == len(mesh.faces) == 24
    assert len(mesh.split()) == 2
    assert mesh.is_watertight and mesh.is_winding_consistent
    assert mesh.volume == pytest.approx(12)
    assert mesh.extents == pytest.approx((5, 2, 3))


@pytest.mark.parametrize('reversed_orientation', [False, True])
def test_caller_coordinates_scale_and_orientation_are_retained(tmp_path, reversed_orientation):
    shape = cq.Solid.makeBox(1, 2, 3, cq.Vector(7, -4, 2)).scale(2)
    if reversed_orientation:
        shape = cq.Shape.cast(shape.wrapped.Reversed())
    before = original_bytes(shape)
    path = tmp_path / 'coordinates.stl'
    step_preview.export_stl_preview(shape, path, linear_deflection=.02, angular_deflection=.05)
    mesh = read_preview(path)
    np.testing.assert_allclose(mesh.bounds, ((14, -8, 4), (16, -4, 10)))
    assert mesh.volume == pytest.approx(-48 if reversed_orientation else 48)
    assert original_bytes(shape) == before


def test_partial_mesher_success_never_publishes_an_incomplete_stl(tmp_path, monkeypatch):
    destination = tmp_path / 'preview.stl'
    destination.write_bytes(b'previous output')
    monkeypatch.setattr(step_preview, 'BRepMesh_IncrementalMesh',
                        lambda *args: SimpleNamespace(IsDone=lambda: True))

    def unexpected_writer():
        pytest.fail('Incomplete native triangles must be rejected before the writer')

    monkeypatch.setattr(step_preview, 'StlAPI_Writer', unexpected_writer)
    with pytest.raises(ValueError, match='omitted a face'):
        step_preview.export_stl_preview(cq.Solid.makeBox(1, 1, 1), destination)
    assert destination.read_bytes() == b'previous output'


@pytest.mark.parametrize('truncated_success', [False, True])
def test_writer_failure_or_truncated_success_preserves_existing_output(
    tmp_path, monkeypatch, truncated_success,
):
    destination = tmp_path / 'preview.stl'
    destination.write_bytes(b'previous output')

    class BrokenWriter:
        ASCIIMode = True

        def Write(self, shape, filename):
            Path(filename).write_bytes(b'incomplete')
            return truncated_success

    monkeypatch.setattr(step_preview, 'StlAPI_Writer', BrokenWriter)
    expected = 'every tessellated triangle' if truncated_success else 'writer failed'
    with pytest.raises(ValueError, match=expected):
        step_preview.export_stl_preview(cq.Solid.makeBox(1, 1, 1), destination)
    assert destination.read_bytes() == b'previous output'
    assert not list(tmp_path.glob('.step-preview-*'))


def test_loose_geometry_cannot_be_silently_dropped(tmp_path):
    shape = cq.Compound.makeCompound([
        cq.Solid.makeBox(1, 1, 1),
        cq.Edge.makeLine(cq.Vector(5, 0, 0), cq.Vector(6, 0, 0)),
    ])
    destination = tmp_path / 'loose.stl'
    with pytest.raises(ValueError, match='outside its faces'):
        step_preview.export_stl_preview(shape, destination)
    assert not destination.exists()


def test_invalid_deflection_is_refused_before_native_meshing(tmp_path):
    with pytest.raises(ValueError, match='finite and positive'):
        step_preview.export_stl_preview(cq.Solid.makeBox(1, 1, 1), tmp_path / 'bad.stl',
                                        linear_deflection=float('nan'))


def test_binary_rounding_cannot_publish_collapsed_triangles(tmp_path):
    shape = cq.Solid.makeBox(1, 1, 1, cq.Vector(1e8, 1e8, 1e8))
    destination = tmp_path / 'collapsed.stl'
    with pytest.raises(ValueError, match='degenerate triangle'):
        step_preview.export_stl_preview(shape, destination)
    assert not destination.exists()


def natural_sphere_shell(radius, center):
    # Recognition creates natural closed faces, which need no explicit pole
    # edges. This exercises the actual CI failure representation.
    position = gp_Ax3(gp_Pnt(*center), gp_Dir(0, 0, 1), gp_Dir(1, 0, 0))
    face = BRepBuilderAPI_MakeFace(gp_Sphere(position, radius)).Face()
    shell = TopoDS_Shell()
    builder = BRep_Builder()
    builder.MakeShell(shell)
    builder.Add(shell, face)
    shell.Closed(True)
    return shell


@pytest.mark.parametrize('center', [(0., 0., 0.), (.125, -.25, .375)])
def test_natural_spherical_cavity_removes_only_zero_surface_pole_artifacts(tmp_path, center):
    maker = BRepBuilderAPI_MakeSolid(natural_sphere_shell(5, center))
    maker.Add(TopoDS.Shell_s(natural_sphere_shell(4, center).Reversed()))
    shape = cq.Shape.cast(maker.Solid())
    assert shape.isValid() and len(shape.Shells()) == 2
    shape.mesh(.1)
    before = original_bytes(shape)
    path = tmp_path / 'spherical-cavity.stl'

    count = step_preview.export_stl_preview(shape, path)
    mesh = read_preview(path)

    assert count == len(mesh.faces)
    assert mesh.is_watertight and mesh.is_winding_consistent
    assert mesh.euler_number == 4
    shells = mesh.split(only_watertight=False)
    assert len(shells) == 2
    assert sorted(component.volume for component in shells)[0] < 0
    assert sorted(component.volume for component in shells)[1] > 0
    assert mesh.volume == pytest.approx(shape.Volume(), rel=.001)
    corners = mesh.triangles
    assert np.all(np.linalg.norm(np.cross(corners[:, 1]-corners[:, 0],
                                         corners[:, 2]-corners[:, 0]), axis=1) > 0)
    assert original_bytes(shape) == before


@pytest.mark.parametrize('same_uv', [False, True])
def test_repeated_native_vertices_on_regular_surface_are_not_silently_removed(same_uv):
    face = next(face for face in cq.Solid.makeCylinder(1, 1).Faces()
                if face.geomType() == 'CYLINDER')
    poly = Poly_Triangulation(4, 2, True)
    points = [(1., 0., 0.), (1., 0., 0.), (0., 1., 1.), (0., -1., 1.)]
    for index, point in enumerate(points, start=1):
        poly.SetNode(index, gp_Pnt(*point))
    uv = [(0., 0.), (0. if same_uv else 2*np.pi, 0.), (np.pi/2, 1.), (3*np.pi/2, 1.)]
    for index, point in enumerate(uv, start=1):
        poly.SetUVNode(index, gp_Pnt2d(*point))
    poly.SetTriangle(1, Poly_Triangle(1, 2, 3))
    poly.SetTriangle(2, Poly_Triangle(1, 3, 4))
    BRep_Builder().UpdateFace(face.wrapped, poly)

    with pytest.raises(ValueError, match='degenerate triangle'):
        step_preview._complete_triangles(face)


def test_collinear_native_triangle_is_not_a_certified_singular_edge():
    face = cq.Face.makePlane(2, 2)
    poly = Poly_Triangulation(4, 2, True)
    for index, point in enumerate([(0., 0.), (1., 0.), (2., 0.), (0., 1.)], start=1):
        poly.SetNode(index, gp_Pnt(*point, 0.))
        poly.SetUVNode(index, gp_Pnt2d(*point))
    poly.SetTriangle(1, Poly_Triangle(1, 2, 3))
    poly.SetTriangle(2, Poly_Triangle(1, 3, 4))
    BRep_Builder().UpdateFace(face.wrapped, poly)

    with pytest.raises(ValueError, match='degenerate triangle'):
        step_preview._complete_triangles(face)
