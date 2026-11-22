import numpy as np
import point_cloud_utils as native
import pytest
import trimesh

from web.browser_python import point_cloud_utils as browser


def test_native_distance_face_and_barycentric_parity():
    mesh = trimesh.creation.icosphere(subdivisions=5)
    rng = np.random.default_rng(31)
    rows = rng.choice(len(mesh.faces), 1200, replace=False)
    probes = mesh.triangles_center[rows] + mesh.face_normals[rows] * 0.0001
    probes = np.vstack((probes, rng.normal(size=(100, 3))))
    vertices, faces = np.asarray(mesh.vertices), np.asarray(mesh.faces, dtype=np.int32)
    d, indices, weights = browser.closest_points_on_mesh(probes, vertices, faces)
    ref, indices_ref, _ = native.closest_points_on_mesh(probes, vertices, faces)
    np.testing.assert_allclose(d, ref, atol=1e-12)
    np.testing.assert_array_equal(indices[: len(rows)], indices_ref[: len(rows)])
    closest = (vertices[faces[indices]] * weights[:, :, None]).sum(axis=1)
    np.testing.assert_allclose(np.linalg.norm(probes - closest, axis=1), d, atol=1e-12)
    np.testing.assert_allclose(weights.sum(axis=1), 1)
    assert weights.min() >= 0


def test_long_triangle_and_collapsed_primitives(monkeypatch):
    vertices = np.array([[0, 0, 0], [10000, 0, 0], [0, 1, 0], [0, 0, 2], [0, 3, 2]])
    faces = np.array([[0, 1, 2], [3, 3, 3], [3, 4, 4]] * 40)
    points = [[0.1, 0.1, 0.01], [0, 1.5, 2.5], [0, -1, 2], [9000, 0.01, 0]]
    monkeypatch.setattr(browser, "_PAIR_BUDGET", 17)
    d, indices, _ = browser.closest_points_on_mesh(points, vertices, faces)
    np.testing.assert_allclose(d, [0.01, 0.5, 1, 0], atol=1e-12)
    assert indices[0] % 3 == 0


def test_winding_orientation_and_open_soup():
    mesh = trimesh.creation.box()
    points = np.array([[0.0, 0.0, 0.0], [0.49, 0.1, 0.4], [1.0, 2.0, 3.0]])
    for faces in (mesh.faces, mesh.faces[:, ::-1], mesh.faces[2:]):
        faces = np.ascontiguousarray(faces, dtype=np.int32)
        winding = browser.triangle_soup_fast_winding_number(mesh.vertices, faces, points)
        ref = native.triangle_soup_fast_winding_number(mesh.vertices, faces, points)
        np.testing.assert_allclose(winding, ref, atol=2e-4)


def test_decimation_signals_recovery_fallback():
    with pytest.raises(NotImplementedError):
        browser.decimate_triangle_mesh([], [], 10)
