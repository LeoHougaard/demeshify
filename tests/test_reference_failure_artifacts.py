import json

import pytest


@pytest.mark.parametrize('offset,accepted', [(0, True), (2, False)])
def test_reference_only_failure_keeps_actual_step_and_final_grading(
    tmp_path, monkeypatch, offset, accepted,
):
    import cadquery as cq
    import trimesh

    from app.step import load_step_shape
    from app.surface_reconstruction import reconstruct_surfaces
    from tools import benchmark_multires_corpus as multires

    # The STL and its reconstruction agree; only the shifted reference rejects
    # the second case. Keep the real conversion, export, reimport and grading.
    mesh = tmp_path / 'box.stl'
    trimesh.creation.box(extents=(10, 8, 6)).export(mesh)
    reference = tmp_path / 'reference.step'
    cq.exporters.export(cq.Workplane('XY').box(10, 8, 6).translate((offset, 0, 0)),
                        str(reference))

    def convert(run_id, stl_path, name, units, *, engine):
        assert engine == 'surface_brep'
        return reconstruct_surfaces(run_id, stl_path, name, units)

    monkeypatch.setattr(multires, 'reconstruct_in_worker', convert)
    artifacts = tmp_path / 'failures'
    case = {'id': 'box', 'source_model_id': 'box', 'split': 'development', 'preset': 'q00',
            'stl': str(mesh), 'source_step': str(reference)}
    result = multires.evaluate(case, tmp_path / 'manifest.json', artifacts)
    assert result['stl_strict_accepted']
    assert result['reference_geometry_accepted'] is accepted
    assert result['accepted'] is accepted
    saved = artifacts / 'box'
    if accepted:
        assert not saved.exists()
    else:
        shape = load_step_shape(saved / 'reconstruction.step')
        assert shape.isValid() and len(shape.Solids()) == 1
        assert (saved / 'input.stl').read_bytes() == mesh.read_bytes()
        report = json.loads((saved / 'benchmark_result.json').read_text())
        assert report['stl_strict_accepted'] and not report['accepted']
        assert not report['reference_geometry_accepted']
        assert report['reference_maximum_mm'] > report['reference_limit_mm']
