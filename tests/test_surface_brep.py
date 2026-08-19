from __future__ import annotations

import json
from types import SimpleNamespace

import cadquery as cq
import numpy as np
import pytest
import trimesh
from shapely.geometry import Polygon

import app.surface_brep as surface_brep
import app.surface_reconstruction as surface_reconstruction
from app.mesh import load_mesh
from app.schemas import (
    MeshReport,
    ReconstructionReport,
    ScoreReport,
    SurfaceBRepReport,
)
from app.surface_brep import (
    build_faceted_brep,
    build_surface_brep,
    export_surface_brep,
    surface_graph_json,
)
from app.surface_graph import (
    CylindricalPatch,
    FreeformPatch,
    PlanarPatch,
    SurfaceAdjacency,
    SurfaceGraph,
    ToroidalPatch,
    _plane_basis,
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


def test_large_graph_residual_is_one_trimmed_bspline_face() -> None:
    rows, columns = 9, 13
    x, y = np.meshgrid(
        np.linspace(-4.0, 4.0, columns),
        np.linspace(-2.5, 2.5, rows),
    )
    z = 0.4 * np.sin(x * 0.6) * np.cos(y * 0.7) + 0.03 * x * y
    vertices = np.column_stack((x.ravel(), y.ravel(), z.ravel()))
    triangles: list[tuple[int, int, int]] = []
    for row in range(rows - 1):
        for column in range(columns - 1):
            first = row * columns + column
            triangles.extend(
                (
                    (first, first + 1, first + columns + 1),
                    (first, first + columns + 1, first + columns),
                )
            )
    mesh = trimesh.Trimesh(vertices=vertices, faces=triangles, process=False)
    boundary_indices = (
        list(range(columns))
        + [row * columns + columns - 1 for row in range(1, rows)]
        + list(range(rows * columns - 2, (rows - 1) * columns - 1, -1))
        + [row * columns for row in range(rows - 2, 0, -1)]
        + [0]
    )
    patch = FreeformPatch(
        patch_id="graph-freeform-test",
        face_indices=np.arange(len(triangles), dtype=np.int64),
        vertex_indices=np.arange(len(vertices), dtype=np.int64),
        area=float(mesh.area),
        boundary_loops=[vertices[boundary_indices]],
    )

    face = surface_brep._graph_bspline_face(patch, [], mesh, 0.08)

    assert face is not None
    assert cq.Face(face).isValid()
    assert cq.Face(face).geomType() == "BSPLINE"
    assert cq.Face(face).Area() == pytest.approx(mesh.area, rel=0.08)


def test_tiny_mesh_uses_scale_aware_kernel_tolerances() -> None:
    mesh = trimesh.creation.cylinder(radius=0.003, height=0.008, sections=96)

    result = build_surface_brep(_mesh_data(mesh))

    assert result.valid
    assert result.closed
    assert result.face_count == 3
    assert result.free_edge_count == 0


def test_clipped_torus_uses_segmented_uv_boundary_wire() -> None:
    major_radius = 8.0
    minor_radius = 1.5
    axis = np.asarray([0.0, 0.0, 1.0])
    first, second = _plane_basis(axis)
    u_values = np.linspace(0.2, 0.8, 80)
    v_values = np.linspace(-0.7, 0.87, 40)
    u_grid, v_grid = np.meshgrid(u_values, v_values, indexing="ij")
    radial = (
        np.cos(u_grid)[..., None] * first
        + np.sin(u_grid)[..., None] * second
    )
    vertices = (
        (major_radius + minor_radius * np.cos(v_grid))[..., None] * radial
        + (minor_radius * np.sin(v_grid))[..., None] * axis
    ).reshape((-1, 3))
    faces: list[tuple[int, int, int]] = []
    columns = len(v_values)
    for row in range(len(u_values) - 1):
        for column in range(columns - 1):
            first_index = row * columns + column
            faces.extend(
                (
                    (first_index, first_index + columns, first_index + columns + 1),
                    (first_index, first_index + columns + 1, first_index + 1),
                )
            )
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    boundary_indices = (
        [row * columns for row in range(len(u_values))]
        + [
            (len(u_values) - 1) * columns + column
            for column in range(1, columns)
        ]
        + [
            row * columns + columns - 1
            for row in range(len(u_values) - 2, -1, -1)
        ]
        + [column for column in range(columns - 2, 0, -1)]
        + [0]
    )
    patch = ToroidalPatch(
        patch_id="clipped-torus",
        face_indices=np.arange(len(faces), dtype=np.int64),
        vertex_indices=np.arange(len(vertices), dtype=np.int64),
        center=np.zeros(3),
        axis=axis,
        major_radius=major_radius,
        minor_radius=minor_radius,
        area=float(mesh.area),
        boundary_loops=[vertices[boundary_indices]],
        rms_error=0.0,
        max_error=0.0,
        normal_error_degrees=0.0,
    )
    model = surface_brep._surface_model(patch, mesh)

    face = surface_brep._uv_boundary_face(model, mesh, 0.01)

    assert face is not None
    assert cq.Face(face).isValid()
    assert cq.Face(face).geomType() == "TORUS"
    assert len(cq.Face(face).Edges()) > 4
    assert cq.Face(face).Area() == pytest.approx(mesh.area, rel=0.01)


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
    graph_payload = json.loads((tmp_path / "surface_graph.json").read_text())
    visualization = graph_payload["visualization"]
    assert visualization["faceted_patch_ids"] == ["organic"]
    assert visualization["faceted_source_face_indices"] == top_indices.tolist()
    assert len(visualization["surface_boundaries"]) == 2
    assert next(
        surface
        for surface in graph_payload["surfaces"]
        if surface["id"] == "organic"
    )["representation"] == "faceted"
    carrier_visualization = surface_graph_json(
        graph,
        faceted_patch_ids=["organic"],
        source_mesh_fallback=True,
    )["visualization"]
    assert carrier_visualization["source_mesh_carrier"]
    assert not carrier_visualization["global_faceted_fallback"]
    assert carrier_visualization["faceted_source_face_indices"] == top_indices.tolist()
    roundtrip = cq.importers.importStep(str(tmp_path / "reconstruction.step")).val()
    assert roundtrip.isValid()
    assert len(roundtrip.Solids()) == 1


def test_tiny_residual_facets_use_local_topology_tolerance() -> None:
    vertices = np.asarray(
        [
            (0.0, 0.0, 0.0),
            (0.04, 0.0, 0.0),
            (0.04, 0.04, 0.0),
            (0.0, 0.04, 0.0),
        ]
    )
    faces = np.asarray([(0, 1, 2), (0, 2, 3)], dtype=np.int64)
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    patch = FreeformPatch(
        "tiny-transition",
        np.arange(len(faces), dtype=np.int64),
        np.arange(len(vertices), dtype=np.int64),
        float(mesh.area),
        [np.vstack((vertices, vertices[0]))],
    )

    rebuilt = surface_brep._faceted_residual_faces(patch, [], mesh, 0.02)

    assert len(rebuilt) == 2
    assert all(cq.Face(face).isValid() for face in rebuilt)


def test_microscopic_closed_sewing_gap_is_capped_below_global_tolerance() -> None:
    points = [
        cq.Vector(0.0, 0.0, 0.0),
        cq.Vector(0.001, 0.0, 0.0),
        cq.Vector(0.0, 0.001, 0.0),
    ]
    wire = cq.Wire.makePolygon(points, close=True)
    edges = [edge.wrapped for edge in wire.Edges()]

    class MicroscopicGap:
        @staticmethod
        def NbFreeEdges() -> int:
            return 3

        @staticmethod
        def FreeEdge(index: int):
            return edges[index - 1]

        @staticmethod
        def SewedShape():
            return cq.Workplane("XY").box(10, 10, 10).val().wrapped

    gap_faces = surface_brep._free_boundary_fill_faces(MicroscopicGap(), 0.01)

    assert len(gap_faces) == 1
    assert cq.Face(gap_faces[0]).Area() == pytest.approx(5e-7)


def test_source_topology_faceted_fallback_is_a_valid_step_solid(tmp_path) -> None:
    mesh = trimesh.creation.icosphere(subdivisions=2, radius=4)
    data = _mesh_data(mesh)

    result = build_faceted_brep(data, reason="test recovery")
    export_surface_brep(result, tmp_path)

    assert result.source_mesh_fallback
    assert result.closed
    assert result.free_edge_count == 0
    assert result.face_count == len(mesh.faces)
    assert result.topology_vertex_count == len(mesh.vertices)
    graph_payload = json.loads((tmp_path / "surface_graph.json").read_text())
    assert graph_payload["visualization"]["global_faceted_fallback"]
    assert not graph_payload["visualization"]["faceted_source_face_indices"]
    roundtrip = cq.importers.importStep(str(tmp_path / "reconstruction.step")).val()
    assert roundtrip.isValid()
    assert len(roundtrip.Solids()) == 1


def test_faceted_fallback_preview_uses_scaled_input_units(tmp_path) -> None:
    source = tmp_path / "inch-box.stl"
    source.write_bytes(trimesh.creation.box(extents=(1, 2, 3)).export(file_type="stl"))
    data = load_mesh(source, source.name, "in")
    result = build_faceted_brep(data, reason="test unit recovery")

    export_surface_brep(
        result,
        tmp_path,
        source,
        source_unit_scale=data.report.unit_scale,
        verify_roundtrip=False,
    )

    preview = trimesh.load(tmp_path / "reconstruction.stl", force="mesh", process=False)
    assert preview.extents == pytest.approx((25.4, 50.8, 76.2), rel=1e-5)


def test_source_topology_faceted_fallback_preserves_multiple_bodies(
    tmp_path,
) -> None:
    first = trimesh.creation.box(extents=(10, 8, 6))
    second = trimesh.creation.box(extents=(2, 2, 2))
    second.apply_translation((20, 0, 0))
    mesh = trimesh.util.concatenate((first, second))

    result = build_faceted_brep(
        _mesh_data(mesh),
        reason="test multibody recovery",
    )

    assert result.valid
    assert result.closed
    assert result.free_edge_count == 0
    assert result.solid_count == 2
    assert len(result.shape.Solids()) == 2
    assert all(solid.isValid() for solid in result.shape.Solids())

    export_surface_brep(result, tmp_path, verify_roundtrip=False)
    roundtrip = cq.importers.importStep(str(tmp_path / "reconstruction.step")).val()
    assert roundtrip.isValid()
    assert len(roundtrip.Solids()) == 2
    assert all(solid.isValid() for solid in roundtrip.Solids())


def test_source_topology_faceted_fallback_keeps_single_body_fast_path(
    monkeypatch,
) -> None:
    mesh = trimesh.creation.box(extents=(10, 8, 6))
    assert mesh.body_count == 1

    def unexpected_component_graph(*_args, **_kwargs):
        raise AssertionError("single-body carrier rebuilt the component graph")

    monkeypatch.setattr(
        trimesh.graph,
        "connected_components",
        unexpected_component_graph,
    )

    result = build_faceted_brep(
        _mesh_data(mesh),
        reason="test single-body recovery fast path",
    )

    assert result.valid
    assert result.solid_count == 1


def test_native_worker_failure_is_reported_as_faceted_best_effort(
    monkeypatch,
    tmp_path,
) -> None:
    mesh = trimesh.creation.box(extents=(7, 5, 3))
    stl_path = tmp_path / "input.stl"
    mesh.export(stl_path)
    monkeypatch.setattr(
        surface_reconstruction.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=3221225477),
    )

    report = surface_reconstruction.reconstruct_surfaces(
        "native-failure-test",
        stl_path,
        "input.stl",
    )

    assert report.status == "best_effort"
    assert report.surface is not None
    assert report.surface.faceted_fallback
    assert report.surface.faceted_face_count == len(mesh.faces)
    assert report.surface.closed
    assert report.score is not None and report.score.valid_solid


def test_invalid_worker_solid_preserves_diagnostic_then_uses_faceted_carrier(
    monkeypatch,
    tmp_path,
) -> None:
    mesh = trimesh.creation.box(extents=(7, 5, 3))
    stl_path = tmp_path / "input.stl"
    mesh.export(stl_path)
    invalid_report = ReconstructionReport(
        id="invalid-worker-test",
        status="best_effort",
        engine="surface_brep",
        mesh=MeshReport(
            file_name="input.stl",
            triangle_count=len(mesh.faces),
            vertex_count=len(mesh.vertices),
            watertight=True,
            body_count=1,
            dimensions_mm=tuple(float(value) for value in mesh.extents),
            volume_mm3=float(mesh.volume),
            surface_area_mm2=float(mesh.area),
            input_units="mm",
            unit_scale=1.0,
        ),
        plan=None,
        surface=SurfaceBRepReport(
            recognized_surface_count=6,
            surface_counts={"plane": 6},
            adjacency_count=12,
            brep_face_count=6,
            solid_count=0,
            free_edge_count=3,
            sewing_tolerance_mm=0.01,
            closed=False,
        ),
        score=ScoreReport(
            score=100.0,
            chamfer_rms_mm=0.0,
            chamfer_p95_mm=0.0,
            chamfer_max_mm=0.0,
            volume_error_percent=0.0,
            valid_solid=False,
            candidate_count=1,
            valid_brep=False,
        ),
        warnings=["diagnostic open shell"],
        elapsed_seconds=1.0,
    )

    def invalid_worker(*args, **kwargs):
        (tmp_path / "reconstruction.step").write_text(
            "invalid analytic diagnostic",
            encoding="utf-8",
        )
        (tmp_path / "surface_graph.json").write_text("{}", encoding="utf-8")
        (tmp_path / "report.json").write_text(
            invalid_report.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(surface_reconstruction.subprocess, "run", invalid_worker)

    report = reconstruct_surfaces(
        "invalid-worker-test",
        stl_path,
        "input.stl",
    )

    assert report.surface is not None
    assert report.surface.source_mesh_fallback
    assert report.surface.closed
    assert report.score is not None and report.score.valid_solid
    assert (tmp_path / "analytic_diagnostic.step").read_text(encoding="utf-8") == (
        "invalid analytic diagnostic"
    )
    assert (tmp_path / "analytic_diagnostic_report.json").is_file()
    assert (tmp_path / "analytic_diagnostic_surface_graph.json").is_file()
    roundtrip = cq.importers.importStep(str(tmp_path / "reconstruction.step")).val()
    assert roundtrip.isValid()
    assert len(roundtrip.Solids()) == 1
