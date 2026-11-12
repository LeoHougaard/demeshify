from pathlib import Path

import numpy as np
import pytest
import trimesh

from app.mesh import load_mesh, mesh_warnings


def junction_box(edge_count=1):
    box = trimesh.creation.box(extents=(10, 10, 10))
    vertices = box.vertices.tolist()
    replacements = {}
    degenerate = []
    for position in range(edge_count):
        a, b = box.faces[0][[position, (position + 1) % 3]]
        neighbor = next(
            index for index, face in enumerate(box.faces)
            if index and a in face and b in face
        )
        face = box.faces[neighbor].tolist()
        oriented = next(
            (face[i], face[(i + 1) % 3], face[(i + 2) % 3])
            for i in range(3) if {face[i], face[(i + 1) % 3]} == {a, b}
        )
        left, right, third = oriented
        midpoint = len(vertices)
        vertices.append(((box.vertices[a] + box.vertices[b]) / 2).tolist())
        replacements[neighbor] = [[left, midpoint, third], [midpoint, right, third]]
        degenerate.append([int(a), midpoint, int(b)])
    faces = []
    for index, face in enumerate(box.faces):
        faces.extend(replacements.get(index, [face.tolist()]))
    faces.extend(degenerate)
    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


@pytest.mark.parametrize("edge_count", [1, 2])
@pytest.mark.parametrize("units,scale", [("mm", 1), ("cm", 10)])
def test_collinear_subdivisions_preserve_geometry_and_provenance(
    tmp_path: Path, edge_count: int, units: str, scale: int,
):
    source = junction_box(edge_count)
    path = tmp_path / "junction.stl"
    source.export(path)
    raw = trimesh.load(path, process=False, force="mesh")
    data = load_mesh(path, path.name, units)

    assert data.report.watertight and data.report.winding_consistent
    assert data.report.boundary_edge_count == data.report.nonmanifold_edge_count == 0
    assert data.report.body_count == data.report.solid_count == 1
    assert data.boundary_subdivisions == edge_count
    assert data.report.removed_degenerate_triangles == edge_count
    assert data.report.volume_mm3 == pytest.approx(1000 * scale**3)
    assert data.report.surface_area_mm2 == pytest.approx(600 * scale**2)
    np.testing.assert_array_equal(
        np.unique(data.mesh.vertices, axis=0), np.unique(raw.vertices * scale, axis=0),
    )
    # Each new triangle belongs to its original uploaded face, with the same
    # plane/orientation and total area; removed zero-area faces stay absent.
    originals = raw.triangles * scale
    valid_ids = np.flatnonzero(raw.area_faces > 0)
    np.testing.assert_array_equal(np.unique(data.source_face_indices), valid_ids)
    for source_id in valid_ids:
        children = data.mesh.triangles[data.source_face_indices == source_id]
        normal = np.cross(originals[source_id, 1] - originals[source_id, 0],
                          originals[source_id, 2] - originals[source_id, 0])
        assert np.all(np.abs((children - originals[source_id, 0]) @ normal) < 1e-10)
        child_normals = np.cross(children[:, 1] - children[:, 0], children[:, 2] - children[:, 0])
        assert np.all(child_normals @ normal > 0)
        assert np.linalg.norm(child_normals, axis=1).sum() == pytest.approx(np.linalg.norm(normal))
    assert any(
        "without moving vertices or filling openings" in warning
        for warning in mesh_warnings(data)
    )


def test_real_opening_is_not_filled(tmp_path: Path):
    mesh = junction_box()
    mesh.update_faces(np.arange(len(mesh.faces)) != 7)
    path = tmp_path / "opening.stl"
    mesh.export(path)
    data = load_mesh(path, path.name)
    assert not data.report.watertight
    assert data.report.boundary_edge_count > 0
    assert data.report.volume_mm3 is None
    assert data.boundary_subdivisions == 0


def test_near_collinear_gap_is_not_snapped_shut(tmp_path: Path):
    mesh = junction_box()
    midpoint = len(mesh.vertices) - 1
    mesh.vertices[midpoint, 0] += 1e-4
    # Keep a separate zero-area cleanup trigger while retaining the actual gap.
    a, _, b = mesh.faces[-1]
    mesh.faces[-1] = [a, a, b]
    path = tmp_path / "gap.stl"
    mesh.export(path)
    data = load_mesh(path, path.name)
    assert not data.report.watertight
    assert data.report.boundary_edge_count == 3
    assert data.boundary_subdivisions == 0
    assert data.report.removed_degenerate_triangles == 1


@pytest.mark.parametrize("offset", [
    0.0, 1e-16, 1e-17, float(np.nextafter(np.float32(0), np.float32(1))),
])
def test_subtraction_cancellation_does_not_close_off_edge_gap(tmp_path: Path, offset: float):
    mesh = junction_box(2)
    mesh.vertices[:, 0] += mesh.vertices[:, 2]
    mesh.vertices[-1, 2] += offset
    a, _, b = mesh.faces[-1]
    mesh.faces[-1] = [a, a, b]
    path = tmp_path / "sheared-gap.stl"
    mesh.export(path)
    raw = trimesh.load(path, process=False, force="mesh")
    # Binary STL retains this offset, although subtraction from the endpoints
    # loses it before the floating-point cross product is evaluated.
    point = raw.vertices[np.argmin(np.linalg.norm(raw.vertices - mesh.vertices[-1], axis=1))]
    assert point[2] == float(np.float32(offset))
    np.testing.assert_array_equal(
        np.cross(point - mesh.vertices[a], mesh.vertices[b] - mesh.vertices[a]),
        np.zeros(3),
    )
    data = load_mesh(path, path.name)
    assert data.report.watertight == (offset == 0)
    assert data.boundary_subdivisions == (2 if offset == 0 else 0)
    if offset:
        assert data.report.boundary_edge_count > 0
        assert data.report.volume_mm3 is None


def test_touching_nonmanifold_solids_remain_unaccepted(tmp_path: Path):
    first = trimesh.creation.box(extents=(10, 10, 10))
    second = first.copy()
    second.apply_translation((10, 10, 0))
    mesh = trimesh.util.concatenate([first, second])
    mesh.faces = np.vstack((mesh.faces, [0, 0, 1]))
    path = tmp_path / "nonmanifold.stl"
    mesh.export(path)
    data = load_mesh(path, path.name)
    assert not data.report.watertight
    assert data.report.nonmanifold_edge_count > 0
    assert data.report.volume_mm3 is None
    assert data.boundary_subdivisions == 0
