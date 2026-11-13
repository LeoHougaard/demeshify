import math

import cadquery as cq
import numpy as np
import pytest
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.TopLoc import TopLoc_Location

from app.scoring import _step_mesh
from tools.geometry_dimensions import (
    GeometryBounds,
    GeometryMeasurementError,
    native_geometry_bounds,
    topology_tolerance_diagnostics,
)


def inflate_mesh_metadata(shape, amount=.25):
    for face in shape.Faces():
        triangulation = BRep_Tool.Triangulation_s(face.wrapped, TopLoc_Location())
        assert triangulation is not None
        triangulation.Deflection(amount)


def mesh_deflections(shape):
    return [BRep_Tool.Triangulation_s(face.wrapped, TopLoc_Location()).Deflection()
            for face in shape.Faces()]


def test_curved_geometry_dimensions_ignore_inflated_mesh_deflection_without_mutation():
    shape = cq.Workplane('XY').circle(2).extrude(3).val()
    before = native_geometry_bounds(shape)
    volume, area = shape.Volume(), shape.Area()
    vertices = [vertex.toTuple() for vertex in shape.Vertices()]
    _step_mesh(shape, .005)
    inflate_mesh_metadata(shape)
    padded = shape.BoundingBox()
    assert max(abs(a - b) for a, b in zip(
        (padded.xlen, padded.ylen, padded.zlen), before.dimensions_mm, strict=True,
    )) > .12  # Reproduce the conservative measurement difference; do not waive it.
    metadata = mesh_deflections(shape)
    after = native_geometry_bounds(shape)
    assert after.dimensions_mm == pytest.approx((4, 4, 3), abs=1e-6)
    assert after.minimum_mm == pytest.approx(before.minimum_mm, abs=1e-12)
    assert after.maximum_mm == pytest.approx(before.maximum_mm, abs=1e-12)
    assert shape.Volume() == volume and shape.Area() == area
    assert [vertex.toTuple() for vertex in shape.Vertices()] == vertices
    assert mesh_deflections(shape) == metadata  # Never clear/rebuild/lower cached metadata.


def test_native_surface_extrema_are_measured_even_when_vertices_cannot_describe_them():
    shape = cq.Solid.makeSphere(1.5, angleDegrees1=-90, angleDegrees2=90)
    vertices = np.asarray([vertex.toTuple() for vertex in shape.Vertices()])
    assert np.ptp(vertices[:, 0]) < 1e-6 and np.ptp(vertices[:, 1]) < 1e-6
    bounds = native_geometry_bounds(shape)
    assert bounds.minimum_mm == pytest.approx((-1.5, -1.5, -1.5), abs=1e-6)
    assert bounds.maximum_mm == pytest.approx((1.5, 1.5, 1.5), abs=1e-6)
    assert bounds.dimensions_mm == pytest.approx((3, 3, 3), abs=1e-6)


@pytest.mark.parametrize('transform,minimum,maximum', [
    ('shift', (6, -6, 4), (16, 2, 10)),
    ('rotate', (-4, -5, -3), (4, 5, 3)),
    ('scale', (-1.25, -1, -.75), (1.25, 1, .75)),
])
def test_known_transformed_native_bounds(transform, minimum, maximum):
    shape = cq.Workplane('XY').box(10, 8, 6).val()
    if transform == 'shift':
        shape = shape.translate((11, -2, 7))
    elif transform == 'rotate':
        shape = shape.rotate((0, 0, 0), (0, 0, 1), 90)
    else:
        shape = shape.scale(.25)
    bounds = native_geometry_bounds(shape)
    assert bounds.minimum_mm == pytest.approx(minimum, abs=1e-6)
    assert bounds.maximum_mm == pytest.approx(maximum, abs=1e-6)


def test_boundary_uncertainty_is_reported_without_changing_geometry_or_tolerances():
    shape = cq.Workplane('XY').box(10, 8, 6).val()
    before = native_geometry_bounds(shape)
    builder = BRep_Builder()
    for vertex in shape.Vertices():
        builder.UpdateVertex(vertex.wrapped, .227862519)
    for edge in shape.Edges():
        builder.UpdateEdge(edge.wrapped, .103536242)
    diagnostics = topology_tolerance_diagnostics(shape)
    after = native_geometry_bounds(shape)
    assert after.dimensions_mm == pytest.approx(before.dimensions_mm, abs=1e-12)
    assert diagnostics['available']
    assert diagnostics['max_vertex_tolerance_mm'] == pytest.approx(.227862519)
    assert diagnostics['max_edge_tolerance_mm'] == pytest.approx(.103536242)
    assert topology_tolerance_diagnostics(shape) == diagnostics


@pytest.mark.parametrize('minimum,maximum', [
    ((0, 0, 0), (1, 0, 1)), ((0, 0, 0), (-1, 1, 1)),
    ((0, 0, 0), (1, float('nan'), 1)), ((0, 0, 0), (1, float('inf'), 1)),
    ((0, 0), (1, 1, 1)), ((False, 0, 0), (1, 1, 1)),
])
def test_unavailable_or_nonfinite_dimensions_are_typed_measurement_errors(minimum, maximum):
    with pytest.raises(GeometryMeasurementError):
        GeometryBounds(minimum, maximum)


def test_empty_native_geometry_has_no_invented_dimensions():
    with pytest.raises(GeometryMeasurementError, match='empty or unbounded'):
        native_geometry_bounds(cq.Compound.makeCompound([]))


def grade_actual_primitives(tmp_path, monkeypatch, source, output, inflate=False):
    """Actual CAD reference grading; accepted stub isolates it from conversion.

    These tiny synthetic CAD cases are evaluator checks, never reconstruction
    yield evidence, and use no corpus/holdout geometry.
    """
    from app import scoring
    from tools import benchmark_multires_corpus as evaluator

    source_path, output_path = tmp_path / 'reference.step', tmp_path / 'output.step'
    cq.exporters.export(source, str(source_path))
    cq.exporters.export(output, str(output_path))
    original_mesher = scoring._step_mesh

    def with_metadata(shape, tolerance):
        mesh = original_mesher(shape, tolerance)
        inflate_mesh_metadata(shape)
        return mesh

    if inflate:
        monkeypatch.setattr(scoring, '_step_mesh', with_metadata)

    def benchmark(case, parent, artifacts, timeout, *, finalize_result):
        evaluator.benchmark.step_surface_metrics(output_path)
        evaluator.benchmark.step_surface_metrics(source_path)
        result = {'accepted': True, 'status': 'complete'}
        finalize_result(result)
        return result

    monkeypatch.setattr(evaluator.benchmark, 'benchmark_case', benchmark)
    return evaluator.evaluate({'source_model_id': 'primitive', 'split': 'development',
                               'preset': 'q00'}, tmp_path / 'manifest.json', None)


@pytest.mark.parametrize('error,accepted', [(.119, True), (.121, False), (.3, False)])
def test_actual_wrong_physical_dimensions_still_face_the_original_gate(
    tmp_path, monkeypatch, error, accepted,
):
    source = cq.Workplane('XY').box(20, 10, 5).val()
    output = cq.Workplane('XY').box(20 + error, 10, 5).val()
    result = grade_actual_primitives(tmp_path, monkeypatch, source, output)
    assert result['reference_limit_mm'] == .12
    assert result['reference_dimension_max_error_mm'] == pytest.approx(error, abs=1e-6)
    assert result['reference_volume_error_percent'] < 2
    assert result['reference_geometry_accepted'] is accepted
    assert result['accepted'] is accepted
    if math.isclose(error, .121):
        # Dimension-only rejection: sampled distance/volume still pass their
        # existing checks, so a wrong physical size cannot hide behind them.
        assert result['reference_maximum_mm'] < .12
        assert result['reference_local_max_mm'] < .12


def test_native_dimension_diagnostics_do_not_waive_the_conservative_gate(tmp_path, monkeypatch):
    source = cq.Workplane('XY').circle(2).extrude(3).val()
    result = grade_actual_primitives(tmp_path, monkeypatch, source, source, inflate=True)
    assert not result['reference_geometry_accepted'] and not result['accepted']
    assert result['reference_dimension_max_error_mm'] > result['reference_limit_mm']
    assert result['reference_native_dimension_max_error_mm'] < 1e-10
    assert result['reference_source_native_dimensions_mm'] == pytest.approx((4, 4, 3), abs=1e-6)
    assert result['reference_output_native_dimensions_mm'] == pytest.approx((4, 4, 3), abs=1e-6)


def test_unavailable_native_diagnostics_do_not_change_original_acceptance(tmp_path, monkeypatch):
    from tools import geometry_dimensions

    def unavailable(_shape):
        raise GeometryMeasurementError('native diagnostic unavailable')

    monkeypatch.setattr(geometry_dimensions, 'native_geometry_bounds', unavailable)
    source = cq.Workplane('XY').box(20, 10, 5).val()
    result = grade_actual_primitives(tmp_path, monkeypatch, source, source)
    assert result['reference_geometry_accepted'] and result['accepted']
    assert result['reference_native_dimension_measurement_available'] is False
    assert 'reference_native_dimension_max_error_mm' not in result
    assert result['reference_native_dimension_measurement_error'] == 'native diagnostic unavailable'


def test_shifted_geometry_still_fails_when_its_dimensions_match(tmp_path, monkeypatch):
    source = cq.Workplane('XY').box(20, 10, 5).val()
    result = grade_actual_primitives(tmp_path, monkeypatch, source, source.translate((.3, 0, 0)))
    assert result['reference_dimension_max_error_mm'] < 1e-6
    assert result['reference_maximum_mm'] > .12
    assert not result['reference_geometry_accepted']
