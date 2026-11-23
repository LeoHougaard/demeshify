import numpy as np
import trimesh

from app.scoring import _local_samples

GRID_SIZE = 100
BOX_SIZE = 20.0


def _dense_box_with_boss() -> trimesh.Trimesh:
    """Create a dense box with one top triangle raised into a 1 mm boss."""

    step = BOX_SIZE / GRID_SIZE
    vertices = []
    vertex_ids = {}

    def vertex_id(point):
        key = tuple(np.round(point, 12))
        if key not in vertex_ids:
            vertex_ids[key] = len(vertices)
            vertices.append(key)
        return vertex_ids[key]

    def grid(origin, u_direction, v_direction):
        faces = []
        origin = np.asarray(origin, dtype=float)
        u_direction = np.asarray(u_direction, dtype=float)
        v_direction = np.asarray(v_direction, dtype=float)
        for i in range(GRID_SIZE):
            for j in range(GRID_SIZE):
                point = origin + i * step * u_direction + j * step * v_direction
                a = vertex_id(point)
                b = vertex_id(point + step * u_direction)
                c = vertex_id(point + step * (u_direction + v_direction))
                d = vertex_id(point + step * v_direction)
                faces.extend(((a, b, c), (a, c, d)))
        return faces

    faces = []
    faces += grid((0, 0, 0), (1, 0, 0), (0, 1, 0))
    faces += grid((0, 0, BOX_SIZE), (1, 0, 0), (0, 1, 0))
    faces += grid((0, 0, 0), (1, 0, 0), (0, 0, 1))
    faces += grid((0, BOX_SIZE, 0), (1, 0, 0), (0, 0, 1))
    faces += grid((0, 0, 0), (0, 1, 0), (0, 0, 1))
    faces += grid((BOX_SIZE, 0, 0), (0, 1, 0), (0, 0, 1))
    raised_face = next(
        index
        for index, face in enumerate(faces)
        if all(abs(vertices[vertex][2] - BOX_SIZE) < 1e-9 for vertex in face)
        and 8 < np.mean([vertices[vertex][0] for vertex in face]) < 10
        and 8 < np.mean([vertices[vertex][1] for vertex in face]) < 10
    )
    base = list(faces[raised_face])
    faces = [face for index, face in enumerate(faces) if index != raised_face]
    top = [
        vertex_id((vertices[vertex][0], vertices[vertex][1], BOX_SIZE + 1))
        for vertex in base
    ]
    for index in range(3):
        a = base[index]
        b = base[(index + 1) % 3]
        c = top[(index + 1) % 3]
        d = top[index]
        faces.extend(((a, b, c), (a, c, d)))
    faces.append(tuple(top))
    return trimesh.Trimesh(
        np.asarray(vertices), np.asarray(faces), process=True
    )


def _permuted(mesh: trimesh.Trimesh, seed: int) -> trimesh.Trimesh:
    rng = np.random.default_rng(seed)
    face_permutation = rng.permutation(len(mesh.faces))
    vertex_permutation = rng.permutation(len(mesh.vertices))
    inverse_vertex_permutation = np.empty_like(vertex_permutation)
    inverse_vertex_permutation[vertex_permutation] = np.arange(len(vertex_permutation))
    return trimesh.Trimesh(
        mesh.vertices[vertex_permutation],
        inverse_vertex_permutation[mesh.faces[face_permutation]],
        process=False,
    )


def test_dense_local_samples_cover_boss_with_permuted_indexing():
    mesh = _dense_box_with_boss()
    permuted = _permuted(mesh, seed=63)

    samples = _local_samples(permuted)

    assert np.max(samples[:, 2]) == BOX_SIZE + 1
    assert len(samples) <= 40_000
    np.testing.assert_array_equal(samples, _local_samples(mesh))


def test_small_mesh_local_samples_remain_exact():
    mesh = trimesh.creation.box()

    samples = _local_samples(mesh)

    np.testing.assert_array_equal(
        samples, np.vstack((mesh.vertices, mesh.triangles_center))
    )
