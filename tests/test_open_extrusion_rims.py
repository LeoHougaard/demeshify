import cadquery as cq
import numpy as np
import pytest
import trimesh

from app.surface_brep import _linear_extrusion_face, _shared_trim_edges, _surface_model
from app.surface_graph import LinearExtrusionPatch, PlanarPatch, SurfaceAdjacency, SurfaceGraph


@pytest.mark.parametrize('scale', [1.0, 0.04])
@pytest.mark.parametrize('fault', [None, 'interior_node', 'endpoint', 'plane', 'missing_border'])
def test_open_extrusion_shares_all_four_finite_edges_or_retains_original_trims(scale, fault):
    angle = np.linspace(-np.pi / 2, np.pi / 2, 65)
    profile = scale * np.column_stack((-5 * np.cos(angle), np.zeros(65), 10 * np.sin(angle)))
    top = profile + (0, scale * 0.7, 0)
    mesh = trimesh.Trimesh(
        vertices=np.vstack((profile, top)),
        faces=[face for i in range(64)
               for face in ((i, i + 1, i + 66), (i, i + 66, i + 65))], process=False,
    )
    patch = LinearExtrusionPatch(
        'open-band', np.arange(len(mesh.faces)), np.arange(len(mesh.vertices)),
        np.array((0, 1, 0)), profile, 0, scale * 0.7, mesh.area, [], 0, 0, 0,
    )
    borders = [profile.copy(), top.copy(), np.array((profile[0], top[0])),
               np.array((profile[-1], top[-1]))]
    normals = [np.array(n) for n in ((0, 1, 0), (0, 1, 0), (0, 0, 1), (0, 0, 1))]
    planes = []
    for i, (points, normal) in enumerate(zip(borders, normals, strict=True)):
        origin = points[0].copy()
        if fault == 'plane' and i == 1:
            origin += scale * 0.1 * normal
        x = np.array((1, 0, 0))
        planes.append(PlanarPatch(
            f'plane-{i}', np.empty(0, dtype=int), np.empty(0, dtype=int), origin,
            normal, x, np.cross(normal, x), 1, [], None,
        ))
    if fault == 'interior_node':
        # This node is omitted by a 32-point sample of the full chain.
        borders[0][2, 0] += scale * 0.02
    elif fault == 'endpoint':
        borders[0][0] = borders[0][1]
    graph = SurfaceGraph(planar_patches=planes, extrusion_patches=[patch], adjacency=[
        SurfaceAdjacency(patch.patch_id, plane.patch_id, [border])
        for plane, border in zip(planes, borders, strict=True)
    ])
    for cap in (0, 1):
        for end in (0, -1):
            point = borders[cap][end].copy()
            outside = point.copy()
            outside[0] = scale * 4
            graph.adjacency.append(SurfaceAdjacency(
                planes[cap].patch_id, planes[2 if end == 0 else 3].patch_id,
                [np.array((point, outside))],
            ))
    if fault == 'missing_border':
        graph.adjacency.pop(3)
    fitted = _linear_extrusion_face(patch, [], mesh, scale * 0.003)
    assert fitted is not None
    edges = cq.Face(fitted).Edges()
    assert len(edges) == 4
    models = {plane.patch_id: _surface_model(plane, mesh) for plane in planes}
    fitted_faces = {patch.patch_id: fitted}
    trims, _, _ = _shared_trim_edges(graph, models, scale * 0.003, fitted_faces)
    edges = cq.Face(fitted_faces[patch.patch_id]).Edges()
    reused = [trim for trim in trims[patch.patch_id]
              if any(trim.edge.IsSame(edge.wrapped) for edge in edges)]
    if fault:
        assert not reused
    else:
        assert len(trims[patch.patch_id]) == len(reused) == 4
        for plane in planes:
            assert any(trim.edge.IsSame(other.edge)
                       for other in trims[plane.patch_id] for trim in reused)
        # At a three-face junction, every incident edge must reuse the same
        # topological vertex, not merely coincident points left for sewing.
        for cap in planes[:2]:
            vertices = [vertex for trim in trims[cap.patch_id]
                        for vertex in cq.Edge(trim.edge).Vertices()]
            for point in (profile[0], profile[-1], top[0], top[-1]):
                at_corner = [v for v in vertices if np.linalg.norm(v.toTuple() - point) < 1e-7]
                assert all(v.wrapped.IsSame(at_corner[0].wrapped) for v in at_corner)
