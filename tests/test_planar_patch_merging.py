import json

import cadquery as cq
import numpy as np
import pytest
import trimesh

import app.surface_brep as brep
from app.mesh import load_mesh
from app.step import load_step_shape
from app.surface_graph import PlanarPatch


@pytest.mark.parametrize('scale', [1.0, 0.04])
def test_failed_plane_trims_recover_complete_faces_without_source_subdivision(
    tmp_path, monkeypatch, scale,
):
    path = tmp_path / 'input.stl'
    trimesh.creation.box(extents=np.array((3, 2, 1)) * scale).export(path)
    data = load_mesh(path, 'input.stl')
    for name in ('_analytic_face', '_planar_boundary_face', '_uv_boundary_face'):
        monkeypatch.setattr(brep, name, lambda *_args, **_kwargs: None)

    result = brep.build_surface_brep(data)
    brep.export_surface_brep(result, tmp_path)
    reopened = load_step_shape(tmp_path / 'reconstruction.step')

    assert result.valid and result.closed and result.free_edge_count == 0
    assert reopened.isValid() and len(reopened.Solids()) == 1
    assert reopened.Volume() == pytest.approx(data.mesh.volume, rel=1e-9)
    assert len(reopened.Faces()) == 6
    assert all(face.geomType() == 'PLANE' for face in reopened.Faces())
    assert not result.faceted_fallback and not result.source_mesh_fallback
    assert result.faceted_face_count == result.faceted_patch_count == 0
    payload = json.loads((tmp_path / 'surface_graph.json').read_text())
    assert payload['visualization']['faceted_source_face_indices'] == []


def test_planar_merge_rejects_a_nonplanar_source_corner():
    points = np.array(((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)), dtype=float)
    mesh = trimesh.Trimesh(vertices=points, faces=((0, 1, 2), (0, 2, 3)), process=False)
    patch = PlanarPatch(
        'plane', np.arange(2), np.arange(4), np.zeros(3), np.array((0, 0, 1)),
        np.array((1, 0, 0)), np.array((0, 1, 0)), 1, [], None,
    )
    faces = brep._faceted_residual_faces(patch, [], mesh, 0.002)
    assert brep._merged_planar_patch_face(faces, points, 0.002) is not None
    points[3, 2] = 1e-4
    assert brep._merged_planar_patch_face(faces, points, 0.002) is None


def test_recovered_planar_caps_preserve_a_through_hole_after_step_roundtrip(tmp_path, monkeypatch):
    source = cq.Workplane('XY').box(3, 2, 1).faces('>Z').workplane().hole(0.6).val()
    path = tmp_path / 'input.stl'
    source.exportStl(str(path), tolerance=0.0005, angularTolerance=0.04,
                     relative=False, parallel=False)
    data = load_mesh(path, 'input.stl')
    original = brep._analytic_face

    def trim(model, *args, **kwargs):
        return None if isinstance(model.patch, PlanarPatch) else original(model, *args, **kwargs)

    monkeypatch.setattr(brep, '_analytic_face', trim)
    for name in ('_planar_boundary_face', '_uv_boundary_face'):
        monkeypatch.setattr(brep, name, lambda *_args, **_kwargs: None)
    result = brep.build_surface_brep(data)
    brep.export_surface_brep(result, tmp_path)
    reopened = load_step_shape(tmp_path / 'reconstruction.step')

    assert result.valid and result.closed and not result.faceted_fallback
    assert reopened.isValid() and len(reopened.Solids()) == 1
    assert len(reopened.Faces()) == 7
    assert sum(face.geomType() == 'CYLINDER' for face in reopened.Faces()) == 1
    assert sum(len(face.Wires()) == 2 for face in reopened.Faces()) == 2
    assert reopened.Volume() == pytest.approx(source.Volume(), rel=1e-4)
