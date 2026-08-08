from __future__ import annotations

import json
from types import SimpleNamespace

import cadquery as cq
import numpy as np
import pytest
import trimesh
from shapely.geometry import Polygon

import app.surface_brep as surface_brep
from app.surface_brep import build_surface_brep, export_surface_brep
from app.surface_graph import (
    CylindricalPatch,
    FreeformPatch,
    PlanarPatch,
    SurfaceAdjacency,
    SurfaceGraph,
)
from app.surface_reconstruction import reconstruct_surfaces


def _mesh_data(mesh: trimesh.Trimesh) -> SimpleNamespace:
    return SimpleNamespace(
        mesh=mesh,
        diagonal=float((mesh.extents**2).sum() ** 0.5),
        section_cache={},
    )


@pytest.mark.parametrize(
    ("mesh", "expected_types"),
    [
        (trimesh.creation.box(extents=(10, 8, 6)), {"PLANE"}),
        (
            trimesh.creation.cylinder(radius=3, height=8, sections=96),
            {"PLANE", "CYLINDER"},
        ),
        (
            trimesh.creation.cone(radius=4, height=9, sections=96),
            {"PLANE", "CONE"},
        ),
        (trimesh.creation.icosphere(subdivisions=3, radius=5), {"SPHERE"}),
        (
            trimesh.creation.torus(
                major_radius=6,
                minor_radius=2,
                major_sections=96,
                minor_sections=48,
            ),
            {"TORUS"},
        ),
    ],
)
def test_build_surface_brep_preserves_elementary_surface_types(
    mesh: trimesh.Trimesh,
    expected_types: set[str],
) -> None:
    result = build_surface_brep(_mesh_data(mesh))

    assert result.valid
    assert result.solid_count == 1
    assert result.free_edge_count == 0
    assert {face.geomType() for face in result.shape.Faces()} == expected_types
    assert not result.warnings


def test_intersected_fillets_create_ten_analytic_trimmed_faces() -> None:
    source = cq.Workplane("XY").box(20, 16, 8).edges("|Z").fillet(2).val()
    vertices, triangles = source.tessellate(0.1, 0.1)
    mesh = trimesh.Trimesh(
        vertices=[vertex.toTuple() for vertex in vertices],
        faces=triangles,
        process=True,
    )

    result = build_surface_brep(_mesh_data(mesh))

    assert result.surface_counts["plane"] == 6
    assert result.surface_counts["cylinder"] == 4
    assert result.face_count == 10
    assert result.topology_vertex_count > 0
    assert result.topology_edge_count > 0
    assert result.free_edge_count == 0
    assert result.shape.Volume() == pytest.approx(source.Volume(), abs=1e-6)
    assert {face.geomType() for face in result.shape.Faces()} == {"PLANE", "CYLINDER"}
    assert not result.warnings


def test_export_and_full_surface_reconstruction_write_verified_step(tmp_path) -> None:
    mesh = trimesh.creation.cylinder(radius=3, height=8, sections=96)
    stl_path = tmp_path / "input.stl"
    mesh.export(stl_path)

    report = reconstruct_surfaces("abc123def456", stl_path, "cylinder.stl")

    assert report.engine == "surface_brep"
    assert report.status == "complete"
    assert report.plan is None
    assert report.surface is not None
    assert report.surface.surface_counts["cylinder"] == 1
    assert report.surface.free_edge_count == 0
    assert report.score is not None and report.score.valid_solid
    assert (tmp_path / "reconstruction.step").is_file()
    assert (tmp_path / "reconstruction.stl").is_file()
    assert (tmp_path / "surface_graph.json").is_file()
    assert (tmp_path / "report.json").is_file()

    imported = cq.importers.importStep(str(tmp_path / "reconstruction.step")).val()
    assert imported.isValid()
    assert {face.geomType() for face in imported.Faces()} == {"PLANE", "CYLINDER"}


def test_export_surface_brep_can_be_called_directly(tmp_path) -> None:
    result = build_surface_brep(_mesh_data(trimesh.creation.box(extents=(5, 4, 3))))

    export_surface_brep(result, tmp_path)

    assert cq.importers.importStep(str(tmp_path / "reconstruction.step")).val().isValid()
    graph = json.loads((tmp_path / "surface_graph.json").read_text(encoding="utf-8"))
    assert all(surface["fit_basis"] == "mesh_nodes" for surface in graph["surfaces"])
    assert all(surface["support_node_count"] >= 3 for surface in graph["surfaces"])
    assert all("surface_function" in surface for surface in graph["surfaces"])


def test_freeform_region_is_one_node_fitted_bspline_not_triangle_faces() -> None:
    size = 6
    x, y = np.meshgrid(np.linspace(0, 5, size), np.linspace(0, 5, size))
    z = 0.35 * np.sin(x * 0.7) * np.cos(y * 0.6)
    vertices = np.column_stack((x.ravel(), y.ravel(), z.ravel()))
    triangles: list[tuple[int, int, int]] = []
    for row in range(size - 1):
        for column in range(size - 1):
            first = row * size + column
            triangles.extend(
                ((first, first + 1, first + size + 1), (first, first + size + 1, first + size))
            )
    mesh = trimesh.Trimesh(vertices=vertices, faces=triangles, process=False)
    boundary_indices = (
        list(range(size))
        + [row * size + size - 1 for row in range(1, size)]
        + list(range(size * size - 2, size * (size - 1) - 1, -1))
        + [row * size for row in range(size - 2, 0, -1)]
        + [0]
    )
    patch = FreeformPatch(
        patch_id="freeform-test",
        face_indices=np.arange(len(triangles), dtype=np.int64),
        vertex_indices=np.arange(len(vertices), dtype=np.int64),
        area=float(mesh.area),
        boundary_loops=[vertices[boundary_indices]],
    )

    face = surface_brep._point_fitted_face(
        patch,
        [],
        mesh,
        max(float(np.linalg.norm(mesh.extents)) * 0.0006, 2e-7),
    )

    assert face is not None
    assert cq.Face(face).isValid()
    assert cq.Face(face).geomType() == "BSPLINE"
    assert len(triangles) == 50


def test_tiny_mesh_uses_scale_aware_kernel_tolerances() -> None:
    mesh = trimesh.creation.cylinder(radius=0.003, height=0.008, sections=96)

    result = build_surface_brep(_mesh_data(mesh))

    assert result.valid
    assert result.closed
    assert result.face_count == 3
    assert result.free_edge_count == 0


def test_box_boundaries_use_one_canonical_edge_and_vertex_graph() -> None:
    result = build_surface_brep(_mesh_data(trimesh.creation.box(extents=(10, 8, 6))))

    assert result.topology_vertex_count == 8
    assert result.topology_edge_count == 12
    assert result.closed
    assert result.valid


def test_open_surface_sewing_exports_fitted_quilt_without_triangle_fallback(
    monkeypatch,
) -> None:
    real_sew_faces = surface_brep._sew_faces

    class OpenSewing:
        def __init__(self, sewing) -> None:
            self.sewing = sewing

        @staticmethod
        def NbFreeEdges() -> int:
            return 3

        def SewedShape(self):
            return self.sewing.SewedShape()

    def force_sewing_open(faces, tolerance):
        sewing, _ = real_sew_faces(faces, tolerance)
        return OpenSewing(sewing), []

    monkeypatch.setattr(surface_brep, "_sew_faces", force_sewing_open)

    result = build_surface_brep(_mesh_data(trimesh.creation.box(extents=(10, 8, 6))))

    assert result.valid
    assert not result.closed
    assert not result.faceted_fallback
    assert result.solid_count == 0
    assert result.free_edge_count == 3
    assert result.face_count == 6
    assert result.joined_shape is not None
    assert len(result.joined_shape.Shells()) == 1
    assert any("3 open boundary edges" in warning for warning in result.warnings)


def test_faceted_residual_deforms_boundary_triangles_onto_analytic_trim(
    monkeypatch,
    tmp_path,
) -> None:
    section_count = 24
    radius = 3.0
    angles = np.arange(section_count) * 2 * np.pi / section_count
    bottom = np.column_stack(
        (radius * np.cos(angles), radius * np.sin(angles), np.zeros(section_count))
    )
    top = np.column_stack(
        (
            radius * np.cos(angles),
            radius * np.sin(angles),
            4.0 + 0.25 * np.sin(3 * angles),
        )
    )
    vertices = np.vstack((bottom, top, [0.0, 0.0, 0.0], [0.0, 0.0, 4.0]))
    bottom_center = section_count * 2
    top_center = bottom_center + 1
    side_faces: list[tuple[int, int, int]] = []
    bottom_faces: list[tuple[int, int, int]] = []
    top_faces: list[tuple[int, int, int]] = []
    for index in range(section_count):
        following = (index + 1) % section_count
        side_faces.extend(
            (
                (index, following, section_count + following),
                (index, section_count + following, section_count + index),
            )
        )
        bottom_faces.append((bottom_center, following, index))
        top_faces.append((top_center, section_count + index, section_count + following))
    faces = np.asarray([*side_faces, *bottom_faces, *top_faces], dtype=np.int64)
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    assert mesh.is_watertight

    side_indices = np.arange(0, section_count * 2, dtype=np.int64)
    bottom_indices = np.arange(section_count * 2, section_count * 3, dtype=np.int64)
    top_indices = np.arange(section_count * 3, section_count * 4, dtype=np.int64)
    bottom_loop = np.vstack((bottom, bottom[0]))
    top_loop = np.vstack((top, top[0]))
    plane = PlanarPatch(
        "bottom",
        bottom_indices,
        np.unique(faces[bottom_indices]),
        np.zeros(3),
        np.asarray([0.0, 0.0, -1.0]),
        np.asarray([1.0, 0.0, 0.0]),
        np.asarray([0.0, -1.0, 0.0]),
        float(mesh.area_faces[bottom_indices].sum()),
        [bottom_loop],
        Polygon(bottom[:, :2]),
        [bottom_loop],
    )
    cylinder = CylindricalPatch(
        "side",
        side_indices,
        np.unique(faces[side_indices]),
        np.zeros(3),
        np.asarray([0.0, 0.0, 1.0]),
        radius,
        0.0,
        4.25,
        float(mesh.area_faces[side_indices].sum()),
        [bottom_loop, top_loop],
        0.0,
        0.0,
        0.0,
    )
    residual = FreeformPatch(
        "organic",
        top_indices,
        np.unique(faces[top_indices]),
        float(mesh.area_faces[top_indices].sum()),
        [top_loop],
    )
    graph = SurfaceGraph(
        planar_patches=[plane],
        cylindrical_patches=[cylinder],
        freeform_patches=[residual],
        adjacency=[
            SurfaceAdjacency("bottom", "side", [bottom_loop]),
            SurfaceAdjacency("side", "organic", [top_loop]),
        ],
        face_patch_ids=np.asarray(
            ["side"] * (section_count * 2)
            + ["bottom"] * section_count
            + ["organic"] * section_count,
            dtype=object,
        ),
    )
    monkeypatch.setattr(surface_brep, "detect_surface_graph", lambda data: graph)
    monkeypatch.setattr(surface_brep, "_point_fitted_face", lambda *args: None)

    result = build_surface_brep(_mesh_data(mesh))
    export_surface_brep(result, tmp_path)

    assert result.faceted_fallback
    assert result.faceted_patch_count == 1
    assert result.faceted_face_count == section_count
    assert result.valid
    assert result.closed
    assert result.solid_count == 1
    assert result.free_edge_count == 0
    assert result.face_count == section_count + 2
    assert {face.geomType() for face in result.shape.Faces()} == {
        "PLANE",
        "CYLINDER",
        "BSPLINE",
    }
    roundtrip = cq.importers.importStep(str(tmp_path / "reconstruction.step")).val()
    assert roundtrip.isValid()
    assert len(roundtrip.Solids()) == 1
