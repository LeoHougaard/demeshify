from types import SimpleNamespace

import numpy as np
import pytest
import trimesh

from app.surface_graph import (
    FreeformPatch,
    _boundary_loops,
    _make_analytic_patch,
    _recover_supported_residual_primitives,
    _validated_seeded_cylinder_fit,
)


def tangent_fixture():
    vertices = [[0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1]]
    faces = [[0, 1, 2], [0, 2, 3]]
    for axis in ('x', 'z'):
        start = len(vertices)
        for along in (0, 1):
            for angle in np.linspace(0, np.pi / 2, 9):
                if axis == 'x':
                    vertices.append([along, -1 + np.cos(angle), 1 + np.sin(angle)])
                else:
                    vertices.append([1 + np.sin(angle), 1 - np.cos(angle), along])
        for index in range(8):
            a, b = start + index, start + index + 9
            faces.extend([[a, a + 1, b + 1], [a, b + 1, b]])
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    mesh.merge_vertices(digits_vertex=12)
    cylinders = []
    for number, (axis, origin) in enumerate([
        ((1, 0, 0), (0, -1, 1)), ((0, 0, 1), (1, 1, 0)),
    ], start=1):
        indices = np.arange(2 + (number - 1) * 16, 2 + number * 16)
        fit = _validated_seeded_cylinder_fit(
            mesh, indices, np.array(axis), np.array(origin), 1, 0.001, 5,
        )
        assert fit is not None
        cylinders.append(_make_analytic_patch(mesh, fit, indices, number))
    residual = FreeformPatch(
        patch_id='freeform-001', face_indices=np.array([0, 1]),
        vertex_indices=np.unique(mesh.faces[:2]), area=float(mesh.area_faces[:2].sum()),
        boundary_loops=_boundary_loops(mesh, np.array([0, 1])),
    )
    labels = np.full(len(mesh.faces), residual.patch_id, dtype=object)
    for patch in cylinders:
        labels[patch.face_indices] = patch.patch_id
    return mesh, cylinders, residual, labels


@pytest.mark.parametrize('change,expected', [
    ('none', True), ('one_support', False), ('bad_support', False), ('warped', False),
])
def test_plane_requires_independent_valid_tangent_supports(change, expected):
    mesh, cylinders, residual, labels = tangent_fixture()
    if change == 'one_support':
        cylinders = cylinders[:1]
    elif change == 'bad_support':
        cylinders[1].radius = 0.8
    elif change == 'warped':
        corner = mesh.faces[0, 0]
        mesh.vertices[corner, 1] = 0.05
    old_owners = labels.copy()
    old_groups = [patch.face_indices.copy() for patch in cylinders]
    planes, freeforms = [], [residual]
    _recover_supported_residual_primitives(
        SimpleNamespace(mesh=mesh), planes, cylinders, freeforms, labels, 0.001, 5,
    )
    assert bool(planes) is expected
    if expected:
        assert len(planes) == 1 and not freeforms
        assert planes[0].polygon.is_valid and planes[0].polygon.area == pytest.approx(1)
        assert set(planes[0].face_indices) == {0, 1}
        assert planes[0].max_error < 1e-10
    else:
        assert freeforms == [residual]
        assert np.array_equal(labels[:2], old_owners[:2])
    for patch, indices in zip(cylinders, old_groups, strict=True):
        assert np.array_equal(patch.face_indices, indices)
        assert np.array_equal(labels[indices], old_owners[indices])


def test_cylinder_seed_can_recognize_a_separate_region_without_growing_old_owner():
    first = trimesh.creation.cylinder(radius=3, height=6, sections=96)
    other = first.copy()
    other.apply_translation((0, 0, 20))
    mesh = trimesh.util.concatenate((first, other))
    original_count = len(first.faces)
    indices = np.arange(len(mesh.faces))
    side = np.abs(mesh.face_normals[:, 2]) < 0.1
    seed = indices[side & (indices < original_count)]
    remote = indices[side & (indices >= original_count)]
    fit = _validated_seeded_cylinder_fit(
        mesh, seed, np.array((0, 0, 1)), np.zeros(3), 3, 0.002, 5,
    )
    assert fit is not None
    cylinder = _make_analytic_patch(mesh, fit, seed, 1)
    before = (cylinder.origin.copy(), cylinder.axis.copy(), cylinder.start, cylinder.end)
    labels = np.full(len(mesh.faces), 'other', dtype=object)
    labels[seed] = cylinder.patch_id
    labels[remote] = 'freeform-001'
    residual = FreeformPatch(
        patch_id='freeform-001', face_indices=remote,
        vertex_indices=np.unique(mesh.faces[remote]),
        area=float(mesh.area_faces[remote].sum()), boundary_loops=_boundary_loops(mesh, remote),
    )
    planes, cylinders, freeforms = [], [cylinder], [residual]
    _recover_supported_residual_primitives(
        SimpleNamespace(mesh=mesh), planes, cylinders, freeforms, labels, 0.002, 5,
    )
    assert not planes and not freeforms and len(cylinders) == 2
    assert np.array_equal(cylinder.face_indices, seed)
    assert np.array_equal(cylinder.origin, before[0])
    assert np.array_equal(cylinder.axis, before[1])
    assert (cylinder.start, cylinder.end) == before[2:]
    assert np.array_equal(cylinders[1].face_indices, remote)
    assert np.all(labels[seed] == cylinder.patch_id)
    assert np.all(labels[remote] == cylinders[1].patch_id)


@pytest.mark.parametrize('adjacent', [True, False])
def test_small_cylinder_region_needs_adjacent_validated_union(adjacent):
    first = trimesh.creation.cylinder(radius=3, height=6, sections=96)
    mesh = first
    if not adjacent:
        other = first.copy()
        other.apply_translation((0, 0, 20))
        mesh = trimesh.util.concatenate((first, other))
    sides = np.flatnonzero(np.abs(mesh.face_normals[:, 2]) < 0.1)
    initial_sides = sides[sides < len(first.faces)]
    group = initial_sides[:3] if adjacent else sides[sides >= len(first.faces)][:3]
    seed = np.setdiff1d(initial_sides, group)
    fit = _validated_seeded_cylinder_fit(
        mesh, seed, np.array((0, 0, 1)), np.zeros(3), 3, 0.002, 5,
    )
    assert fit is not None
    cylinder = _make_analytic_patch(mesh, fit, seed, 1)
    labels = np.full(len(mesh.faces), 'other', dtype=object)
    labels[seed] = cylinder.patch_id
    labels[group] = 'freeform-001'
    residual = FreeformPatch(
        patch_id='freeform-001', face_indices=group,
        vertex_indices=np.unique(mesh.faces[group]), area=float(mesh.area_faces[group].sum()),
        boundary_loops=_boundary_loops(mesh, group),
    )
    planes, cylinders, freeforms = [], [cylinder], [residual]
    _recover_supported_residual_primitives(
        SimpleNamespace(mesh=mesh), planes, cylinders, freeforms, labels, 0.002, 5,
    )
    assert np.array_equal(cylinder.face_indices, seed)
    assert np.all(labels[seed] == cylinder.patch_id)
    if adjacent:
        assert not freeforms and len(cylinders) == 2
        assert np.array_equal(cylinders[1].face_indices, group)
        assert cylinders[1].max_error < 1e-8
        assert cylinders[1].end - cylinders[1].start == pytest.approx(6)
    else:
        assert freeforms == [residual] and len(cylinders) == 1
        assert np.all(labels[group] == residual.patch_id)
