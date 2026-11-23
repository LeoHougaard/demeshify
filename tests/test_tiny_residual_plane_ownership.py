import numpy as np
import trimesh

from app.mesh import load_mesh
from app.surface_graph import detect_surface_graph


def attached_strip(tmp_path, height):
    points = np.array(((0, 0, 0), (10, 0, 0), (10, .1, 0), (10, 10, 0), (0, 10, 0),
                       (10.001, 0, height), (10.001, .1, height)), dtype=float)
    mesh = trimesh.Trimesh(vertices=points, faces=((0, 1, 2), (0, 2, 3), (0, 3, 4),
                                                 (1, 5, 6), (1, 6, 2)), process=False)
    path = tmp_path / 'small-attached-strip.stl'
    mesh.export(path)
    return load_mesh(path, path.name)


def test_tiny_bent_region_cannot_be_flattened_into_a_neighboring_plane(tmp_path):
    data = attached_strip(tmp_path, .005)
    graph = detect_surface_graph(data, distance_tolerance=.01)
    largest = max(graph.planar_patches, key=lambda p: p.area)
    support = np.asarray(data.mesh.vertices[largest.vertex_indices])
    actual_error = float(np.max(np.abs((support - largest.origin) @ largest.normal)))
    assert len(largest.face_indices) == 3
    assert actual_error <= .001
    assert largest.max_error == actual_error
    assert sum(len(p.face_indices) for p in graph.patches) == len(data.mesh.faces)


def test_coplanar_small_continuation_keeps_one_planar_owner(tmp_path):
    data = attached_strip(tmp_path, 0)
    graph = detect_surface_graph(data, distance_tolerance=.01)
    assert len(graph.planar_patches) == 1
    assert len(graph.planar_patches[0].face_indices) == len(data.mesh.faces)
