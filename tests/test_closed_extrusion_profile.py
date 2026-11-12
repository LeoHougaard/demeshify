import numpy as np
import trimesh

from app.surface_brep import _linear_extrusion_face
from app.surface_graph import LinearExtrusionPatch


def test_dense_closed_extrusion_keeps_periodic_seam_after_step_roundtrip(tmp_path):
    import cadquery as cq

    from app.scoring import _step_mesh
    from app.step import load_step_shape

    angle = np.linspace(0, 2 * np.pi, 258, endpoint=False)
    radius = 0.03 * (1 + 0.1 * np.cos(3 * angle))
    ring = np.column_stack((radius * np.cos(angle), np.zeros(len(angle)),
                            radius * np.sin(angle)))
    profile = np.vstack((ring, ring[0]))
    vertices = np.vstack((ring, ring + (0, 0.7, 0)))
    count = len(ring)
    faces = []
    for index in range(count):
        following = (index + 1) % count
        faces.extend(((index, following, following + count),
                      (index, following + count, index + count)))
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    patch = LinearExtrusionPatch(
        patch_id='closed-sweep', face_indices=np.arange(len(faces)),
        vertex_indices=np.arange(len(vertices)), direction=np.array((0, 1, 0)),
        profile_points=profile, start=0, end=0.7, area=mesh.area,
        boundary_loops=[profile, profile + (0, 0.7, 0)],
        rms_error=0, max_error=0, normal_error_degrees=0,
    )
    face = _linear_extrusion_face(patch, [], mesh, 0.0003)
    assert face is not None
    wrapped = cq.Face(face)
    assert wrapped.isValid() and wrapped._geomAdaptor().IsUPeriodic()
    path = tmp_path / 'closed-sweep.step'
    cq.exporters.export(wrapped, str(path))
    imported = load_step_shape(path)
    assert imported.isValid()
    assert imported.Faces()[0]._geomAdaptor().IsUPeriodic()
    delivered = _step_mesh(imported, 0.0001)
    assert len(delivered.faces) > 0
    assert np.allclose(delivered.extents, mesh.extents, atol=0.0001)
