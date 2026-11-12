import cadquery as cq
import numpy as np
import pytest
import trimesh

from app import surface_brep as brep
from app.mesh import load_mesh
from app.step import load_step_shape
from app.surface_graph import LinearExtrusionPatch, PlanarPatch, SurfaceAdjacency, SurfaceGraph


@pytest.mark.parametrize('scale', [1.0, 0.04])
def test_closed_extrusion_and_caps_share_two_complete_rims(tmp_path, monkeypatch, scale):
    count = 48
    angle = np.linspace(0, 2 * np.pi, count, endpoint=False)
    ring = scale * np.column_stack((np.cos(angle), np.zeros(count), 0.6 * np.sin(angle)))
    vertices = np.vstack((ring, ring + (0, 0.7 * scale, 0), (0, 0, 0), (0, 0.7 * scale, 0)))
    faces = []
    for i in range(count):
        j = (i + 1) % count
        faces.extend(((i, j + count, j), (i, i + count, j + count),
                      (2 * count, i, j), (2 * count + 1, j + count, i + count)))
    source = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    source.fix_normals()
    input_path = tmp_path / 'ellipse.stl'
    source.export(input_path)
    data = load_mesh(input_path, 'input.stl')
    mesh = data.mesh
    height = float(mesh.bounds[1, 1])
    lower = mesh.vertices[(np.abs(mesh.vertices[:, 1]) < scale * 1e-6)
                          & (np.linalg.norm(mesh.vertices[:, [0, 2]], axis=1) > scale * 0.1)]
    lower = lower[np.argsort(np.arctan2(lower[:, 2], lower[:, 0]))]
    profile = np.vstack((lower, lower[0]))
    side_indices = np.flatnonzero(np.abs(mesh.face_normals[:, 1]) < 0.5)
    patch = LinearExtrusionPatch(
        'closed-band', side_indices, np.unique(mesh.faces[side_indices]), np.array((0, 1, 0)),
        profile, 0, height, float(mesh.area_faces[side_indices].sum()),
        [profile, profile + (0, height, 0)], 0, 0, 0,
    )
    caps = []
    for i, (y, sign) in enumerate(((0, -1), (height, 1))):
        indices = np.flatnonzero(mesh.face_normals[:, 1] * sign > 0.9)
        caps.append(PlanarPatch(
            f'cap-{i}', indices, np.unique(mesh.faces[indices]), np.array((0, y, 0)),
            np.array((0, sign, 0)), np.array((1, 0, 0)), np.array((0, 0, -sign)),
            float(mesh.area_faces[indices].sum()), [profile + (0, y, 0)], None,
        ))
    graph = SurfaceGraph(planar_patches=caps, extrusion_patches=[patch], adjacency=[
        SurfaceAdjacency(patch.patch_id, cap.patch_id, [profile + (0, y, 0)])
        for cap, y in zip(caps, (0, height), strict=True)
    ])
    # Isolate joining from recognition; every support/trim point comes from the STL.
    monkeypatch.setattr(brep, 'detect_surface_graph', lambda *a, **k: graph)
    result = brep.build_surface_brep(data)
    assert result.closed and result.valid and not result.faceted_fallback
    assert result.face_count == 3 and result.topology_edge_count == 2
    assert result.shape.Volume() == pytest.approx(mesh.volume, rel=0.01)
    path = tmp_path / 'joined.step'
    cq.exporters.export(result.shape, str(path))
    delivered = load_step_shape(path)
    assert delivered.isValid() and len(delivered.Solids()) == 1
    assert len(delivered.Edges()) == 3  # Two rims and one paired periodic seam.
    cap_faces = [face for face in delivered.Faces() if face.geomType() == 'PLANE']
    swept = next(face for face in delivered.Faces() if face.geomType() == 'EXTRUSION')
    assert len(cap_faces) == 2
    for cap in cap_faces:
        assert len(cap.Edges()) == 1
        assert any(cap.Edges()[0].wrapped.IsSame(edge.wrapped) for edge in swept.Edges())
