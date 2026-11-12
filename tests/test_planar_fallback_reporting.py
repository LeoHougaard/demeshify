import json

import numpy as np
import pytest
import trimesh

import app.surface_brep as brep
from app.mesh import load_mesh
from app.step import load_step_shape
from tools.benchmark_surface_corpus import is_clean_analytic_reconstruction


@pytest.mark.parametrize('scale', [1.0, 0.04])
def test_source_triangle_subdivision_on_a_plane_is_reported_as_fallback(
    tmp_path, monkeypatch, scale,
):
    mesh = trimesh.creation.box(extents=np.array((3, 2, 1)) * scale)
    path = tmp_path / 'input.stl'
    mesh.export(path)
    data = load_mesh(path, 'input.stl')
    # Exercise the conservative source-triangle path after plane trimming
    # fails, with real geometry and unchanged sewing/STEP verification.
    for name in ('_analytic_face', '_planar_boundary_face', '_uv_boundary_face'):
        monkeypatch.setattr(brep, name, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(brep, '_merged_planar_patch_face', lambda *_args: None)
    result = brep.build_surface_brep(data)
    brep.export_surface_brep(result, tmp_path)
    reopened = load_step_shape(tmp_path / 'reconstruction.step')

    assert result.valid and result.closed and result.free_edge_count == 0
    assert reopened.isValid() and len(reopened.Solids()) == 1
    assert reopened.Volume() == pytest.approx(data.mesh.volume, rel=1e-9)
    assert len(reopened.Faces()) == len(mesh.faces) == 12
    assert result.faceted_fallback and not result.source_mesh_fallback
    assert result.faceted_face_count == 12 and result.faceted_patch_count == 6
    assert set(result.faceted_patch_ids) == {p.patch_id for p in result.graph.patches}
    payload = json.loads((tmp_path / 'surface_graph.json').read_text())
    assert set(payload['visualization']['faceted_source_face_indices']) == set(range(12))
    assert all(p['representation'] == 'faceted' for p in payload['surfaces'])
    assert not is_clean_analytic_reconstruction(result, {
        'reference_comparison_available': True,
        'minimum_analytic_area_recall': 1.0, 'minimum_analytic_area_precision': 1.0,
        'missing_analytic_surface_types': [], 'unexpected_nonanalytic_surface_types': [],
    }, result.warnings)
