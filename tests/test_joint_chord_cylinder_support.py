import numpy as np
import pytest
import trimesh
from shapely.geometry import Polygon
from test_coincident_chord_ownership import chord_fixture

from app import surface_graph as graph


def refresh_models(mesh, planes, cylinders):
    for number, previous in enumerate(tuple(cylinders), start=1):
        indices = previous.face_indices
        fit = graph._fit_cylinder(
            mesh.vertices[np.unique(mesh.faces[indices])], mesh.triangles_center[indices],
            mesh.face_normals[indices], .001, 5, mesh.triangles[indices],
        )
        assert fit is not None
        cylinders[number - 1] = graph._make_analytic_patch(mesh, fit, indices, number)
    for plane in planes:
        indices = plane.face_indices
        normal = mesh.face_normals[indices].mean(axis=0)
        normal /= np.linalg.norm(normal)
        origin = mesh.triangles_center[indices].mean(axis=0)
        first, second = graph._plane_basis(normal)
        loops = graph._boundary_loops(mesh, indices)
        projected = [np.column_stack(((loop - origin) @ first, (loop - origin) @ second))
                     for loop in loops]
        plane.normal, plane.origin = normal, origin
        plane.x_direction, plane.y_direction = first, second
        plane.boundary_loops_3d, plane.boundary_loops = loops, projected
        plane.polygon, plane.area = Polygon(projected[0]), float(mesh.area_faces[indices].sum())


def noisy_circle(scale=1):
    mesh, planes, cylinders, labels = chord_fixture()
    points = mesh.vertices.astype(np.float32).astype(float) * scale
    mesh = trimesh.Trimesh(vertices=points, faces=mesh.faces, process=False)
    refresh_models(mesh, planes, cylinders)
    assert not graph._same_cylinder_support(*cylinders)
    return mesh, planes, cylinders, labels


@pytest.mark.parametrize('scale', [1, 25.4])
def test_float32_independent_fits_share_one_data_supported_cylinder(scale):
    mesh, planes, cylinders, labels = noisy_circle(scale)
    before = [(patch.origin.copy(), patch.radius) for patch in cylinders]
    graph._absorb_cylindrical_chord_fragments(
        mesh, planes, cylinders, labels, .001, 5, coordinate_scale=scale,
    )
    assert not planes and len(cylinders) == 1
    cylinder = cylinders[0]
    assert np.array_equal(cylinder.face_indices, np.arange(len(mesh.faces)))
    assert np.all(labels == cylinder.patch_id)
    assert cylinder.radius == pytest.approx(3 * scale, abs=1e-5 * scale)
    assert cylinder.max_error < 1e-6 * scale
    assert cylinder.area == pytest.approx(mesh.area)
    assert len(cylinder.boundary_loops) == 1
    assert any(cylinder.radius != radius for _, radius in before)


@pytest.mark.parametrize('change', ['radius', 'offset', 'axis'])
def test_physically_distinct_cylinders_are_not_jointly_merged(change):
    mesh, planes, cylinders, labels = chord_fixture()
    indices = cylinders[1].vertex_indices
    points = mesh.vertices.copy()
    if change == 'radius':
        points[indices, :2] *= (3 + 2e-5) / 3
    elif change == 'offset':
        points[indices] += 2e-5 * planes[0].normal
    else:
        # Tilt the actual second cylinder about an axis normal to the joining
        # wall. The connecting quadrilateral remains a real planar surface.
        axis = planes[0].normal
        angle = 1e-4
        skew = np.array(((0, -axis[2], axis[1]),
                         (axis[2], 0, -axis[0]), (-axis[1], axis[0], 0)))
        rotation = np.eye(3) + np.sin(angle) * skew + (1 - np.cos(angle)) * (skew @ skew)
        pivot = points[indices[np.argmin(points[indices, 2])]].copy()
        points[indices] = (points[indices] - pivot) @ rotation.T + pivot
    mesh = trimesh.Trimesh(vertices=points, faces=mesh.faces, process=False)
    refresh_models(mesh, planes, cylinders)
    # Each separate model explains real, different source geometry to numerical
    # precision; the common equation cannot do so at that precision.
    assert max(patch.max_error for patch in cylinders) < 1e-10
    old = labels.copy()
    owners = [patch.face_indices.copy() for patch in cylinders]
    graph._absorb_cylindrical_chord_fragments(mesh, planes, cylinders, labels, .001, 5)
    assert len(planes) == 1 and len(cylinders) == 2
    assert np.array_equal(labels, old)
    assert all(np.array_equal(patch.face_indices, indices)
               for patch, indices in zip(cylinders, owners, strict=True))


def test_joint_fit_still_requires_original_full_union_validator(monkeypatch):
    mesh, planes, cylinders, labels = noisy_circle()
    calls = []

    def reject(mesh, indices, *args):
        calls.append(indices.copy())
        return None

    monkeypatch.setattr(graph, '_validated_seeded_cylinder_fit', reject)
    old = labels.copy()
    graph._absorb_cylindrical_chord_fragments(mesh, planes, cylinders, labels, .001, 5)
    assert len(calls) == 1 and np.array_equal(calls[0], np.arange(len(mesh.faces)))
    assert len(planes) == 1 and len(cylinders) == 2
    assert np.array_equal(labels, old)


def test_real_broad_flat_wall_is_not_a_cylindrical_chord():
    mesh, planes, cylinders, labels = chord_fixture(wide=True)
    old = labels.copy()
    graph._absorb_cylindrical_chord_fragments(mesh, planes, cylinders, labels, .001, 5)
    assert len(planes) == 1 and len(cylinders) == 2 and np.array_equal(labels, old)


def test_real_planar_endcap_remains_planar():
    mesh = trimesh.creation.cylinder(radius=3, height=6, sections=32)
    side = np.flatnonzero(np.abs(mesh.face_normals[:, 2]) < .1)
    cap = np.flatnonzero(mesh.face_normals[:, 2] > .9)[:2]
    fit = graph._validated_seeded_cylinder_fit(
        mesh, side, np.array((0, 0, 1)), np.zeros(3), 3, .001, 5,
    )
    assert fit is not None
    cylinders = [graph._make_analytic_patch(mesh, fit, side, 1)]
    points = mesh.vertices[np.unique(mesh.faces[cap])]
    origin, normal = points.mean(axis=0), np.array((0, 0, 1))
    first, second = graph._plane_basis(normal)
    loops = graph._boundary_loops(mesh, cap)
    projected = [np.column_stack(((loop - origin) @ first, (loop - origin) @ second))
                 for loop in loops]
    planes = [graph.PlanarPatch(
        patch_id='cap', face_indices=cap, vertex_indices=np.unique(mesh.faces[cap]),
        origin=origin, normal=normal, x_direction=first, y_direction=second,
        area=float(mesh.area_faces[cap].sum()), boundary_loops=projected,
        polygon=Polygon(projected[0]), boundary_loops_3d=loops,
    )]
    labels = np.full(len(mesh.faces), 'other', dtype=object)
    labels[side], labels[cap] = cylinders[0].patch_id, 'cap'
    old = labels.copy()
    graph._absorb_cylindrical_chord_fragments(mesh, planes, cylinders, labels, .001, 5)
    assert len(planes) == 1 and np.array_equal(labels, old)
