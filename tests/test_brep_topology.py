import cadquery as cq
import pytest
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.TopoDS import TopoDS_Face, TopoDS_Shell

from app.step import load_step_shape
from tools.brep_topology import compare_topology, shell_topology, topology_metrics


def plate(holes=0, blind=False):
    result = cq.Workplane('XY').box(10, 8, 3)
    if holes:
        points = [(2 * i - holes + 1, 0) for i in range(holes)]
        result = result.faces('>Z').workplane().pushPoints(points).hole(
            .2, depth=1 if blind else None)
    return result.val()


@pytest.mark.parametrize(('build', 'genus'), [
    (lambda: plate(), 0),
    (lambda: cq.Solid.makeCylinder(2, 3), 0),
    (lambda: cq.Solid.makeSphere(2, angleDegrees1=-90, angleDegrees2=90), 0),
    (lambda: cq.Solid.makeTorus(3, 1), 1),
    (lambda: plate(1), 1),
    (lambda: plate(2), 2),
    (lambda: plate(1, blind=True), 0),
])
def test_periodic_poles_inner_wires_and_blind_holes(build, genus, tmp_path):
    shape = build()
    before = topology_metrics(shape)
    assert before['supported'], before
    assert before['solid_count'] == before['shell_count'] == 1
    shell = before['solids'][0]['shells'][0]
    assert shell['genus'] == genus
    assert shell['euler_characteristic'] == 2 - 2 * genus
    assert shell['manifold_edges'] and shell['coherent_edge_orientations']
    path = tmp_path / 'roundtrip.step'
    cq.exporters.export(shape, str(path))
    after = load_step_shape(path)
    comparison = compare_topology(shape, after)
    assert comparison['status'] == 'match', comparison


def test_sphere_degenerate_pole_edges_are_excluded():
    sphere = cq.Solid.makeSphere(2, angleDegrees1=-90, angleDegrees2=90)
    metrics = shell_topology(sphere.Shells()[0])
    assert metrics['degenerate_edge_count'] == 2
    assert metrics['nondegenerate_edge_count'] == 1
    assert metrics['euler_characteristic'] == 2


def test_cavity_shell_is_owned_by_its_solid_and_distinct_from_another_body(tmp_path):
    outer = cq.Workplane('XY').box(10, 8, 6)
    cavity = outer.cut(cq.Workplane('XY').box(2, 2, 2)).val()
    metrics = topology_metrics(cavity)
    assert metrics['supported'], metrics
    assert metrics['solid_count'] == 1 and metrics['shell_count'] == 2
    assert sorted((s['role'], s['genus']) for s in metrics['solids'][0]['shells']) == [
        ('cavity', 0), ('outer', 0)]
    second = cq.Compound.makeCompound([outer.val(), cq.Solid.makeSphere(
        1, pnt=(20, 0, 0), angleDegrees1=-90, angleDegrees2=90)])
    comparison = compare_topology(cavity, second)
    assert comparison['status'] == 'mismatch'
    path = tmp_path / 'cavity.step'
    cq.exporters.export(cavity, str(path))
    assert compare_topology(cavity, load_step_shape(path))['topology_match'] is True


def test_component_order_and_incidental_face_counts_do_not_define_topology():
    box = plate()
    cylinder = cq.Solid.makeCylinder(2, 3).translate((20, 0, 0))
    first = cq.Compound.makeCompound([box, cylinder])
    second = cq.Compound.makeCompound([cylinder, box])
    assert compare_topology(first, second)['topology_match'] is True
    assert compare_topology(box, cylinder)['topology_match'] is True


def test_tiny_through_hole_mismatch_is_not_hidden_by_same_solid_shell_counts():
    comparison = compare_topology(plate(1), plate())
    assert comparison['status'] == 'mismatch'
    assert comparison['reference']['solid_count'] == comparison['output']['solid_count'] == 1
    assert comparison['reference']['shell_count'] == comparison['output']['shell_count'] == 1
    assert compare_topology(plate(1, blind=True), plate())['topology_match'] is True


def test_unowned_geometry_and_open_shells_are_indeterminate():
    box = plate()
    edge = cq.Edge.makeLine((20, 0, 0), (21, 0, 0))
    compound = cq.Compound.makeCompound([box, edge])
    report = compare_topology(box, compound)
    assert report['topology_match'] is None
    assert report['status'] == 'indeterminate'
    assert report['output']['unowned_geometry_counts']['edge'] == 1
    opened = cq.Shell.makeShell(box.Faces()[:-1])
    shell = shell_topology(opened)
    assert not shell['supported'] and shell['genus'] is None
    assert 'open_shell' in shell['indeterminate_reasons']


def test_reversed_solid_material_orientation_is_indeterminate():
    shape = plate()
    reversed_shape = cq.Shape.cast(shape.wrapped.Reversed())
    report = topology_metrics(reversed_shape)
    assert not report['supported']
    assert report['solids'][0]['finite_material'] is False


def test_doubly_closed_surface_without_two_independent_cuts_is_indeterminate():
    torus = cq.Solid.makeTorus(3, 1)
    surface = BRep_Tool.Surface_s(torus.Faces()[0].wrapped)
    native = TopoDS_Face()
    BRep_Builder().MakeFace(native, surface, 1e-7)
    native_shell = TopoDS_Shell()
    builder = BRep_Builder()
    builder.MakeShell(native_shell)
    builder.Add(native_shell, native)
    report = shell_topology(cq.Shell(native_shell))
    assert not report['supported'] and report['genus'] is None
    assert 'face_without_boundary_cells' in report['indeterminate_reasons']
