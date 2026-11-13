import json
import shutil
from types import SimpleNamespace

import cadquery as cq
import pytest

from tools import benchmark_surface_corpus as benchmark


def plate(hole=False):
    result = cq.Workplane('XY').box(10, 8, 3)
    if hole:
        result = result.faces('>Z').workplane().hole(.2)
    return result.val()


def test_actual_lost_through_hole_is_reported_despite_equal_body_and_shell_counts():
    diagnostic = benchmark.reference_topology_diagnostic(
        {'shape': plate(True)}, {'shape': plate()},
    )
    assert diagnostic['available'] and diagnostic['status'] == 'mismatch'
    assert diagnostic['topology_match'] is False
    assert diagnostic['reference']['solid_count'] == diagnostic['output']['solid_count'] == 1
    assert diagnostic['reference']['shell_count'] == diagnostic['output']['shell_count'] == 1
    assert diagnostic['reference']['solids'][0]['shells'][0]['genus'] == 1
    assert diagnostic['output']['solids'][0]['shells'][0]['genus'] == 0


def test_actual_unowned_output_geometry_remains_indeterminate_with_native_reasons():
    shape = plate()
    output = cq.Compound.makeCompound([
        shape, cq.Edge.makeLine((20, 0, 0), (21, 0, 0)),
    ])
    diagnostic = benchmark.reference_topology_diagnostic({'shape': shape}, {'shape': output})
    assert diagnostic['available'] and diagnostic['status'] == 'indeterminate'
    assert diagnostic['topology_match'] is None
    assert not diagnostic['output']['supported']
    assert diagnostic['output']['unowned_geometry_counts']['edge'] == 1


def protocol_benchmark(tmp_path, monkeypatch, reference_shape, output_shape, *, faceted=False):
    """Real STEP diagnostics/artifact workflow, stubbed conversion report only.

    These primitives and protocol input bytes are evaluator tests, never CAD
    corpus fixtures or measured reconstruction yields.
    """
    source = tmp_path / 'input.stl'
    source.write_bytes(b'protocol input; conversion is deliberately stubbed')
    reference_path, output_path = tmp_path / 'reference.step', tmp_path / 'output.step'
    cq.exporters.export(reference_shape, str(reference_path))
    cq.exporters.export(output_shape, str(output_path))
    surface = SimpleNamespace(
        closed=True, free_edge_count=0, faceted_fallback=faceted, source_mesh_fallback=False,
        faceted_patch_count=1 if faceted else 0, faceted_face_count=12 if faceted else 0,
        recognized_surface_count=6, surface_counts={'plane': 6}, point_fitted_face_count=0,
        topology_vertex_count=8, topology_edge_count=12,
    )
    score = SimpleNamespace(
        step_geometry_verified=True, valid_brep=True, valid_solid=True, volume_comparable=True,
        component_count_match=True, chamfer_p95_mm=0., chamfer_max_mm=0., local_max_mm=0.,
        volume_error_percent=0.,
    )
    report = SimpleNamespace(
        status='complete', elapsed_seconds=.01, surface=surface, score=score, warnings=[],
        mesh=SimpleNamespace(dimensions_mm=(10., 8., 3.), triangle_count=12, input_units='mm'),
    )

    def convert(_run_id, path, _name, _units):
        shutil.copy2(output_path, path.parent / 'reconstruction.step')
        (path.parent / 'closed_shell_roundtrip_diagnostic.step').write_bytes(b'kept diagnostic')
        (path.parent / 'closed_shell_roundtrip_score.json').write_text('{}')
        return report

    monkeypatch.setattr(benchmark, 'reconstruct_surfaces', convert)
    return benchmark.benchmark_case(
        {'id': 'primitive', 'stl': str(source), 'source_step': str(reference_path)},
        tmp_path, tmp_path / 'artifacts',
    )


def test_actual_step_topology_is_retained_in_failure_artifacts(tmp_path, monkeypatch):
    result = protocol_benchmark(tmp_path, monkeypatch, plate(True), plate(), faceted=True)
    assert result['reference_topology']['status'] == 'mismatch'
    assert result['reference_topology']['topology_match'] is False
    assert not result['accepted'] and result['faceted_face_count'] == 12
    saved = tmp_path / 'artifacts/primitive'
    assert (saved / 'closed_shell_roundtrip_diagnostic.step').read_bytes() == b'kept diagnostic'
    assert (saved / 'closed_shell_roundtrip_score.json').read_text() == '{}'
    retained = json.loads((saved / 'benchmark_result.json').read_text())
    assert retained['reference_topology'] == json.loads(json.dumps(result['reference_topology']))


def test_comparator_error_is_nonfatal_and_cannot_invent_a_topology_match(tmp_path, monkeypatch):
    def fail(_reference, _output):
        raise ValueError('unsupported diagnostic probe')

    monkeypatch.setattr(benchmark, 'compare_topology', fail)
    result = protocol_benchmark(tmp_path, monkeypatch, plate(), plate())
    assert result['accepted']  # Existing conversion/quality gates, not the diagnostic.
    diagnostic = result['reference_topology']
    assert not diagnostic['available'] and diagnostic['topology_match'] is None
    assert diagnostic['status'] == 'indeterminate'
    assert diagnostic['reference'] is diagnostic['output'] is None
    assert diagnostic['indeterminate_reasons'] == ['diagnostic_error']
    assert diagnostic['diagnostic_error'] == 'ValueError: unsupported diagnostic probe'


@pytest.mark.parametrize('source,output,missing', [
    ({}, {}, ['reference_shape_unavailable', 'output_shape_unavailable']),
    ({}, {'shape': object()}, ['reference_shape_unavailable']),
])
def test_missing_reference_does_not_become_an_empty_or_matching_signature(source, output, missing):
    diagnostic = benchmark.reference_topology_diagnostic(source, output)
    assert not diagnostic['available'] and diagnostic['topology_match'] is None
    assert diagnostic['status'] == 'indeterminate'
    assert diagnostic['indeterminate_reasons'] == missing
