import numpy as np
import pytest
import trimesh

from app.mesh import load_mesh, mesh_warnings
from app.surface_brep import build_surface_brep, surface_graph_json


def test_preflight_removes_only_duplicate_and_zero_area_faces(tmp_path):
    source = trimesh.creation.box(extents=(10, 8, 6))
    mesh = trimesh.Trimesh(
        vertices=source.vertices,
        faces=np.vstack((source.faces, source.faces[:1], [[0, 0, 1]])),
        process=False,
    )
    path = tmp_path / "dirty.stl"
    mesh.export(path)

    data = load_mesh(path, path.name)

    assert data.report.removed_duplicate_triangles == 1
    assert data.report.removed_degenerate_triangles == 1
    assert data.report.watertight
    assert data.report.boundary_edge_count == 0
    assert data.report.nonmanifold_edge_count == 0
    assert data.report.volume_mm3 == pytest.approx(source.volume)
    assert data.mesh.bounds == pytest.approx(source.bounds)
    assert mesh_warnings(data)


def test_preflight_exposes_open_edges_without_filling_them(tmp_path):
    mesh = trimesh.creation.box()
    mesh.update_faces(np.arange(len(mesh.faces) - 1))
    path = tmp_path / "open.stl"
    mesh.export(path)

    data = load_mesh(path, path.name)

    assert not data.report.watertight
    assert data.report.boundary_edge_count == 3
    assert len(data.mesh.faces) == len(mesh.faces)
    assert data.report.volume_mm3 is None
    assert "not automatically filled" in mesh_warnings(data)[0]


def test_cleanup_keeps_overlay_indices_attached_to_original_triangles(tmp_path):
    box = trimesh.creation.box()
    faces = np.vstack((box.faces[:4], [[0, 0, 1]], box.faces[4:]))
    mesh = trimesh.Trimesh(vertices=box.vertices, faces=faces, process=False)
    path = tmp_path / "dirty.stl"
    mesh.export(path)
    data = load_mesh(path, path.name)
    result = build_surface_brep(data)

    graph = surface_graph_json(
        result.graph, faceted_patch_ids=[patch.patch_id for patch in result.graph.patches]
    )

    assert graph["visualization"]["faceted_source_face_indices"] == [
        index for index in range(13) if index != 4
    ]
