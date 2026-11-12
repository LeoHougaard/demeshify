import numpy as np
import pytest
import trimesh

from app.surface_brep import _closed_loop_loft_face, _shared_trim_edges, _surface_model
from app.surface_graph import FreeformPatch, PlanarPatch, SurfaceAdjacency, SurfaceGraph


def annular_patch(bow=0.0, radial_scale=1.0, height_scale=1.0, layers=3):
    angle = np.linspace(0, 2 * np.pi, 48, endpoint=False)
    profile = radial_scale * np.column_stack((np.cos(angle), np.zeros(48), 0.6 * np.sin(angle)))
    rings = [profile * (1 + 0.2 * t + bow * np.sin(np.pi * t))
             + (0, t * height_scale, 0) for t in np.linspace(0, 1, layers)]
    vertices = np.vstack(rings)
    faces = []
    for row in range(layers - 1):
        for column in range(48):
            following = (column + 1) % 48
            a, b = row * 48 + column, row * 48 + following
            faces.extend(((a, b, b + 48), (a, b + 48, a + 48)))
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    loops = [np.vstack((ring, ring[0])) for ring in (rings[0], rings[-1])]
    patch = FreeformPatch(
        patch_id='annulus', face_indices=np.arange(len(faces)),
        vertex_indices=np.arange(len(vertices)), area=mesh.area, boundary_loops=loops,
    )
    return patch, mesh


@pytest.mark.parametrize('radial_scale,height_scale', [(1.0, 1.0), (0.04, 0.8)])
def test_annular_loft_recovers_one_valid_smooth_surface(tmp_path, radial_scale, height_scale):
    import cadquery as cq

    from app.scoring import _step_mesh
    from app.step import load_step_shape

    patch, mesh = annular_patch(radial_scale=radial_scale, height_scale=height_scale)
    face = _closed_loop_loft_face(patch, mesh, radial_scale * 0.003)
    assert face is not None
    wrapped = cq.Face(face)
    assert wrapped.isValid()
    assert wrapped.geomType() == 'BSPLINE'
    assert wrapped.Area() == pytest.approx(mesh.area, rel=0.005)
    path = tmp_path / 'annular.step'
    cq.exporters.export(wrapped, str(path))
    imported = load_step_shape(path)
    assert imported.isValid()
    delivered = _step_mesh(imported, radial_scale * 0.003)
    assert len(delivered.faces) > 0
    assert delivered.extents == pytest.approx(
        (2.4 * radial_scale, height_scale, 1.44 * radial_scale), abs=radial_scale * 0.005)


def test_annular_loft_is_independent_of_boundary_loop_start_and_winding():
    import cadquery as cq

    patch, mesh = annular_patch()
    baseline = _closed_loop_loft_face(patch, mesh, 0.003)
    ring = np.roll(patch.boundary_loops[1][:-1][::-1], 13, axis=0)
    patch.boundary_loops[1] = np.vstack((ring, ring[0]))
    changed = _closed_loop_loft_face(patch, mesh, 0.003)
    assert baseline is not None and changed is not None
    assert cq.Face(changed).Area() == pytest.approx(cq.Face(baseline).Area(), rel=1e-7)


def test_annular_loft_rejects_unsupported_interior_bulge():
    patch, mesh = annular_patch(bow=0.2)
    assert _closed_loop_loft_face(patch, mesh, 0.003) is None


def test_dense_interior_retains_boundary_fit_and_checks_all_support_nodes():
    patch, mesh = annular_patch(layers=22)
    assert len(patch.vertex_indices) > 500
    assert _closed_loop_loft_face(patch, mesh, 0.003) is not None
    mesh.vertices[48 * 11] *= 1.5
    assert _closed_loop_loft_face(patch, mesh, 0.003) is None


def test_annular_loft_does_not_close_a_missing_boundary():
    patch, mesh = annular_patch()
    patch.boundary_loops[0] = patch.boundary_loops[0][:-1]
    assert _closed_loop_loft_face(patch, mesh, 0.003) is None


@pytest.mark.parametrize('bad_plane', [False, True])
def test_caps_and_annulus_share_complete_curves_or_keep_original_trims(bad_plane):
    patch, mesh = annular_patch()
    fitted = _closed_loop_loft_face(patch, mesh, 0.003)
    assert fitted is not None
    planes = [PlanarPatch(
        patch_id=f'cap-{index}', face_indices=np.empty(0, dtype=int),
        vertex_indices=np.empty(0, dtype=int),
        origin=np.array((0, height, 0), dtype=float), normal=np.array((0, 1, 0)),
        x_direction=np.array((1, 0, 0)), y_direction=np.array((0, 0, -1)),
        area=1, boundary_loops=[], polygon=None,
    ) for index, height in enumerate((0, 1.1 if bad_plane else 1))]
    graph = SurfaceGraph(planar_patches=planes, freeform_patches=[patch], adjacency=[
        SurfaceAdjacency(patch.patch_id, plane.patch_id, [loop])
        for plane, loop in zip(planes, patch.boundary_loops, strict=True)
    ])
    models = {plane.patch_id: _surface_model(plane, mesh) for plane in planes}
    trims, _, _ = _shared_trim_edges(graph, models, 0.003, {patch.patch_id: fitted})
    import cadquery as cq

    smooth_edges = cq.Face(fitted).Edges()
    if bad_plane:
        assert not any(trim.edge.IsSame(edge.wrapped)
                       for trim in trims[patch.patch_id] for edge in smooth_edges)
    else:
        assert len(trims[patch.patch_id]) == 2
        for plane in planes:
            assert len(trims[plane.patch_id]) == 1
            assert any(trim.edge.IsSame(trims[plane.patch_id][0].edge)
                       for trim in trims[patch.patch_id])
