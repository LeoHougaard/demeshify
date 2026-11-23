import numpy as np
import pytest
import trimesh

from app.surface_graph import (
    _make_analytic_patch,
    _merge_coincident_connected_cylinders,
    _validated_seeded_cylinder_fit,
)


def split_cylinder(groups=2):
    angles = np.linspace(0, 1.2, 41)
    vertices = [[3 * np.cos(angle), 3 * np.sin(angle), z]
                for z in (0, 6) for angle in angles]
    faces = []
    for index in range(40):
        a, b = index, index + 41
        faces.extend(((a, a + 1, b + 1), (a, b + 1, b)))
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    labels = np.empty(len(faces), dtype=object)
    cylinders = []
    # Split only at complete source quad borders; all owners share actual edges.
    for number, quads in enumerate(np.array_split(np.arange(40), groups), start=1):
        indices = np.ravel(np.column_stack((quads * 2, quads * 2 + 1)))
        fit = _validated_seeded_cylinder_fit(
            mesh, indices, np.array((0, 0, 1)), np.zeros(3), 3, .001, 5,
        )
        assert fit is not None
        cylinder = _make_analytic_patch(mesh, fit, indices, number)
        cylinders.append(cylinder)
        labels[indices] = cylinder.patch_id
    return mesh, cylinders, labels


@pytest.mark.parametrize('groups', [2, 3])
def test_adjacent_identical_support_unifies_ownership_and_refreshes_region(groups):
    mesh, cylinders, labels = split_cylinder(groups)
    seed = cylinders[0]
    geometry = (seed.origin.copy(), seed.axis.copy(), seed.radius)
    # Axially shifted origins still denote the exact same infinite cylinder.
    cylinders[-1].origin = cylinders[-1].origin + np.array((0, 0, 2))
    _merge_coincident_connected_cylinders(mesh, cylinders, labels, .001, 5)
    assert cylinders == [seed]
    assert np.array_equal(seed.face_indices, np.arange(len(mesh.faces)))
    assert np.array_equal(seed.vertex_indices, np.arange(len(mesh.vertices)))
    assert np.all(labels == seed.patch_id)
    assert np.array_equal(seed.origin, geometry[0])
    assert np.array_equal(seed.axis, geometry[1])
    assert seed.radius == geometry[2]
    assert (seed.start, seed.end) == pytest.approx((0, 6))
    assert seed.area == pytest.approx(mesh.area)
    assert seed.max_error < 1e-12
    assert seed.normal_error_degrees < 1
    assert len(seed.boundary_loops) == 1
    # Every source face has precisely one surviving owner, including the seam.
    counts = np.zeros(len(mesh.faces), dtype=int)
    for patch in cylinders:
        counts[patch.face_indices] += 1
    assert np.all(counts == 1)


@pytest.mark.parametrize('change', ['offset', 'radius', 'axis', 'invalid_union', 'wrong_owner'])
def test_adjacent_distinct_or_unvalidated_supports_keep_original_owners(change):
    mesh, cylinders, labels = split_cylinder()
    if change == 'offset':
        cylinders[1].origin = cylinders[1].origin + np.array((1e-6, 0, 0))
    elif change == 'radius':
        cylinders[1].radius += 1e-6
    elif change == 'axis':
        cylinders[1].axis = np.array((1e-6, 0, 1))
        cylinders[1].axis /= np.linalg.norm(cylinders[1].axis)
    elif change == 'invalid_union':
        index = mesh.faces[cylinders[1].face_indices[-1], -1]
        mesh.vertices[index, :2] *= 1.01
    else:
        labels[-1] = 'other'
    old_labels = labels.copy()
    old_indices = [patch.face_indices.copy() for patch in cylinders]
    _merge_coincident_connected_cylinders(mesh, cylinders, labels, .001, 5)
    assert len(cylinders) == 2
    assert np.array_equal(labels, old_labels)
    assert all(np.array_equal(patch.face_indices, indices)
               for patch, indices in zip(cylinders, old_indices, strict=True))


def test_disjoint_same_cylinder_does_not_merge():
    first, cylinders, _ = split_cylinder()
    second = first.copy()
    second.apply_translation((0, 0, 20))
    mesh = trimesh.util.concatenate((first, second))
    groups = [np.arange(len(first.faces)), np.arange(len(first.faces), len(mesh.faces))]
    labels = np.empty(len(mesh.faces), dtype=object)
    for patch, indices in zip(cylinders, groups, strict=True):
        patch.face_indices = indices
        patch.vertex_indices = np.unique(mesh.faces[indices])
        labels[indices] = patch.patch_id
    old = labels.copy()
    _merge_coincident_connected_cylinders(mesh, cylinders, labels, .001, 5)
    assert len(cylinders) == 2 and np.array_equal(labels, old)
