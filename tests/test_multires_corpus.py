import numpy as np
import pytest
import trimesh

from tools.benchmark_multires_corpus import summary
from tools.build_multires_corpus import PRESETS, mesh_digest
from tools.select_multires_cohort import select_cases


def test_ten_export_presets_tighten_both_geometric_constraints():
    assert len(PRESETS) == 10
    assert all(a > c and b > d for (a, b), (c, d) in zip(
        PRESETS[:-1], PRESETS[1:], strict=True))


def test_geometry_identity_ignores_order_and_winding_but_detects_movement():
    source = trimesh.creation.icosphere(subdivisions=1)
    changed = trimesh.Trimesh(vertices=source.vertices.copy(),
                             faces=source.faces[::-1, ::-1], process=False)
    assert mesh_digest(source) == mesh_digest(changed)
    changed.vertices[0] += np.array((0.01, 0, 0))
    assert mesh_digest(source) != mesh_digest(changed)


def test_partial_results_never_claim_full_source_model_success():
    rows = [{'source_model_id': 'a', 'preset': f'q{i:02d}', 'split': 'development',
             'accepted': True} for i in range(9)]
    report = summary(rows, rows + [{**rows[-1], 'preset': 'q09'}])
    assert not report['complete']
    assert report['source_models_all_ten_pass'] == 0
    rows.append({**rows[-1], 'preset': 'q09', 'accepted': False})
    report = summary(rows, rows)
    assert report['complete']
    assert report['strict_case_passes'] == 9
    assert report['source_models_ten_evaluated'] == 1
    assert report['source_models_all_ten_pass'] == 0


def test_materializer_rejects_unchanged_planar_presets(tmp_path):
    import cadquery as cq
    import pytest

    from tools.build_multires_corpus import materialize

    source = tmp_path / 'planar.step'
    cq.exporters.export(cq.Workplane('XY').box(10, 10, 10), str(source))
    with pytest.raises(ValueError, match='identical triangle geometry'):
        materialize(source, tmp_path, {})


def test_materializer_exports_ten_real_geometries_from_one_curved_source(tmp_path):
    import cadquery as cq

    from tools.build_multires_corpus import materialize

    source = tmp_path / 'curved.step'
    cq.exporters.export(cq.Workplane('XY').circle(10).extrude(5), str(source))
    record = materialize(source, tmp_path, {})
    assert len(record['cases']) == 10
    assert len({c['mesh_geometry_sha256'] for c in record['cases']}) == 10
    assert len({c['source_sha256'] for c in record['cases']}) == 1
    assert len({c['split'] for c in record['cases']}) == 1


@pytest.mark.parametrize('change,accepted', [('same', True), ('shift', False), ('extra', False)])
def test_reference_grader_rejects_displaced_and_extra_geometry(
    tmp_path, monkeypatch, change, accepted,
):
    import cadquery as cq

    from tools import benchmark_multires_corpus as multires

    reference = cq.Workplane('XY').box(10, 8, 6).val()
    output = reference
    if change == 'shift':
        output = reference.translate((2, 0, 0))
    elif change == 'extra':
        output = cq.Compound.makeCompound([reference, reference.translate((30, 0, 0))])
    source_path, output_path = tmp_path / 'truth.step', tmp_path / 'output.step'
    cq.exporters.export(reference, str(source_path))
    cq.exporters.export(output, str(output_path))

    def fake_benchmark(case, parent, artifacts, timeout):
        multires.benchmark.step_surface_metrics(output_path)
        multires.benchmark.step_surface_metrics(source_path)
        return {'accepted': True}

    monkeypatch.setattr(multires.benchmark, 'benchmark_case', fake_benchmark)
    result = multires.evaluate({'source_model_id': 'a', 'split': 'development', 'preset': 'q00'},
                               tmp_path / 'manifest.json', None)
    assert result['accepted'] is accepted
    assert result['reference_geometry_accepted'] is accepted


def test_inner_cohort_keeps_holdout_out_and_balances_surface_strata():
    def source(identifier, split, kinds, triangles):
        return [{'source_model_id': identifier, 'split': split, 'preset': f'q{i:02d}',
                 'triangles': triangles + i,
                 'ground_truth': {'solid_count': 1, 'brep_face_type_counts': kinds}}
                for i in range(10)]

    cases = (source('expensive-cylinder', 'development', {'CYLINDER': 1}, 1000)
             + source('cheap-cylinder', 'development', {'CYLINDER': 1}, 100)
             + source('cone', 'development', {'CONE': 1}, 400)
             + source('holdout', 'holdout', {'TORUS': 1}, 10))
    selected = select_cases(cases, 2, {'q00', 'q05', 'q09'})
    assert len(selected) == 6
    assert {c['source_model_id'] for c in selected} == {'cheap-cylinder', 'cone'}
    assert all(c['split'] == 'development' for c in selected)
    assert {c['preset'] for c in selected} == {'q00', 'q05', 'q09'}


def test_inner_cohort_rejects_incomplete_source_instead_of_hiding_missing_exports():
    cases = [{'source_model_id': 'a', 'split': 'development', 'preset': 'q00'}]
    with pytest.raises(ValueError, match='all ten'):
        select_cases(cases, 1, {'q00'})
