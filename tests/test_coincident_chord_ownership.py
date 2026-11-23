import numpy as np
import pytest
import trimesh
from shapely.geometry import Polygon

from app import surface_graph as graph


def chord_fixture(chords=(20,), *, wide=False):
    angles = np.linspace(0, 1.2, 41)
    if wide:
        angles[21:] += .6
    vertices = [[3 * np.cos(angle), 3 * np.sin(angle), z]
                for z in (0, 6) for angle in angles]
    faces = []
    for index in range(40):
        a, b = index, index + 41
        faces.extend(((a, a + 1, b + 1), (a, b + 1, b)))
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    labels = np.empty(len(faces), dtype=object)
    planes, cylinders = [], []
    remaining = np.ones(40, dtype=bool)
    remaining[list(chords)] = False
    remaining_quads = np.flatnonzero(remaining)
    groups = np.split(remaining_quads, np.flatnonzero(np.diff(remaining_quads) > 1) + 1)
    for number, quads in enumerate(groups, start=1):
        indices = np.ravel(np.column_stack((quads * 2, quads * 2 + 1)))
        fit = graph._validated_seeded_cylinder_fit(
            mesh, indices, np.array((0, 0, 1)), np.zeros(3), 3, .001, 5,
        )
        assert fit is not None
        cylinder = graph._make_analytic_patch(mesh, fit, indices, number)
        cylinders.append(cylinder)
        labels[indices] = cylinder.patch_id
    for number, quad in enumerate(chords, start=1):
        indices = np.array((quad * 2, quad * 2 + 1))
        normal = mesh.face_normals[indices].mean(axis=0)
        normal /= np.linalg.norm(normal)
        origin = mesh.triangles_center[indices].mean(axis=0)
        x_direction, y_direction = graph._plane_basis(normal)
        loops = graph._boundary_loops(mesh, indices)
        projected = [np.column_stack(((loop - origin) @ x_direction,
                                     (loop - origin) @ y_direction)) for loop in loops]
        plane = graph.PlanarPatch(
            patch_id=f'plane-{number:03d}', face_indices=indices,
            vertex_indices=np.unique(mesh.faces[indices]), origin=origin, normal=normal,
            x_direction=x_direction, y_direction=y_direction,
            area=float(mesh.area_faces[indices].sum()), boundary_loops=projected,
            polygon=Polygon(projected[0]), boundary_loops_3d=loops,
        )
        planes.append(plane)
        labels[indices] = plane.patch_id
    return mesh, planes, cylinders, labels


@pytest.mark.parametrize('chords', [(20,), (10, 20), (19, 20), (39,)])
def test_one_or_multiple_coincident_neighbors_absorb_only_validated_chords(chords):
    mesh, planes, cylinders, labels = chord_fixture(chords)
    seed = cylinders[0]
    geometry = (seed.origin.copy(), seed.axis.copy(), seed.radius)
    if len(cylinders) > 1:
        cylinders[-1].origin += np.array((0, 0, 2))
    graph._absorb_cylindrical_chord_fragments(mesh, planes, cylinders, labels, .001, 5)
    assert not planes
    assert cylinders == [seed]
    assert np.all(labels == seed.patch_id)
    assert np.array_equal(seed.face_indices, np.arange(len(mesh.faces)))
    assert np.array_equal(seed.vertex_indices, np.arange(len(mesh.vertices)))
    assert np.array_equal(seed.origin, geometry[0])
    assert np.array_equal(seed.axis, geometry[1])
    assert seed.radius == geometry[2]
    assert (seed.start, seed.end) == pytest.approx((0, 6))
    assert seed.area == pytest.approx(mesh.area)
    assert seed.max_error < 1e-12 and seed.normal_error_degrees < 1
    assert len(seed.boundary_loops) == 1
    counts = np.zeros(len(mesh.faces), dtype=int)
    for patch in (*planes, *cylinders):
        counts[patch.face_indices] += 1
    assert np.all(counts == 1)


def test_ineligible_perpendicular_end_support_stays_owned_and_unchanged():
    mesh, planes, cylinders, labels = chord_fixture()
    plane = planes[0]
    end_ids = [int(i) for i in plane.vertex_indices if mesh.vertices[i, 2] == 0]
    first, second = sorted(end_ids)
    p, q = mesh.vertices[[first, second]]
    tangent = (q - p) / np.linalg.norm(q - p)
    half = np.linalg.norm(q - p) / 2
    z_axis = np.array((0, 0, 1))
    origin = (p + q) / 2 - np.sqrt(1 - half**2) * z_axis
    angle = np.arcsin(half)
    vertices = mesh.vertices.tolist()
    rows = []
    for along in (0, -1):
        row = []
        for index, theta in enumerate((-angle, angle, angle + .15, angle + .30)):
            if along == 0 and index < 2:
                row.append((first, second)[index])
            else:
                row.append(len(vertices))
                vertices.append((origin + np.sin(theta) * tangent + np.cos(theta) * z_axis
                                 + along * plane.normal).tolist())
        rows.append(row)
    faces = mesh.faces.tolist()
    original_faces = len(faces)
    for index in range(3):
        a, b = rows[0][index], rows[1][index]
        faces.extend(((a, rows[0][index + 1], rows[1][index + 1]),
                      (a, rows[1][index + 1], b)))
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    indices = np.arange(original_faces, len(mesh.faces))
    fit = graph._validated_seeded_cylinder_fit(
        mesh, indices, plane.normal, origin, 1, .001, 5,
    )
    assert fit is not None
    end = graph._make_analytic_patch(mesh, fit, indices, 3)
    cylinders.append(end)
    labels = np.r_[labels, np.full(len(indices), end.patch_id, dtype=object)]
    # This is a real third mesh neighbor sharing the chord's short end border.
    adjacent = {labels[b] if a in plane.face_indices else labels[a]
                for a, b in mesh.face_adjacency
                if (a in plane.face_indices) != (b in plane.face_indices)}
    assert adjacent == {patch.patch_id for patch in cylinders}
    assert abs(plane.normal @ end.axis) > .99
    end_geometry = (end.origin.copy(), end.axis.copy(), end.radius, end.area)
    seed = cylinders[0]
    graph._absorb_cylindrical_chord_fragments(mesh, planes, cylinders, labels, .001, 5)
    assert not planes and cylinders == [seed, end]
    assert np.all(labels[:original_faces] == seed.patch_id)
    assert np.all(labels[indices] == end.patch_id)
    assert np.array_equal(end.face_indices, indices)
    assert np.array_equal(end.origin, end_geometry[0])
    assert np.array_equal(end.axis, end_geometry[1])
    assert (end.radius, end.area) == end_geometry[2:]
    counts = np.zeros(len(mesh.faces), dtype=int)
    for patch in cylinders:
        counts[patch.face_indices] += 1
    assert np.all(counts == 1)


@pytest.mark.parametrize('change', ['radius', 'axis', 'offset', 'refused_validator', 'true_wall'])
def test_distinct_supports_or_real_wall_preserve_all_original_owners(change, monkeypatch):
    mesh, planes, cylinders, labels = chord_fixture(wide=change == 'true_wall')
    if change == 'radius':
        cylinders[1].radius += 1e-6
    elif change == 'axis':
        cylinders[1].axis = np.array((1e-6, 0, 1))
        cylinders[1].axis /= np.linalg.norm(cylinders[1].axis)
    elif change == 'offset':
        cylinders[1].origin += np.array((1e-6, 0, 0))
    elif change == 'refused_validator':
        calls = []

        def reject_union(mesh, indices, *args):
            calls.append(indices.copy())
            return None

        monkeypatch.setattr(graph, '_validated_seeded_cylinder_fit', reject_union)
    old_labels = labels.copy()
    old_planes = list(planes)
    old_cylinders = list(cylinders)
    old_indices = [patch.face_indices.copy() for patch in cylinders]
    graph._absorb_cylindrical_chord_fragments(mesh, planes, cylinders, labels, .001, 5)
    assert planes == old_planes and cylinders == old_cylinders
    assert np.array_equal(labels, old_labels)
    assert all(np.array_equal(patch.face_indices, indices)
               for patch, indices in zip(cylinders, old_indices, strict=True))
    if change == 'refused_validator':
        assert len(calls) == 1
        assert np.array_equal(calls[0], np.arange(len(mesh.faces)))
