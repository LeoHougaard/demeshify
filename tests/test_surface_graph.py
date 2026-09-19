from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import cadquery as cq
import numpy as np
import pytest
import trimesh

from app.mesh import load_mesh
from app.surface_graph import (
    PlanarPatch,
    _edge_chains,
    _extract_local_analytic_regions,
    _fit_component,
    _regularize_plane_relations,
    _validated_seeded_cylinder_fit,
    detect_surface_graph,
)


def _mesh_data(mesh: trimesh.Trimesh) -> SimpleNamespace:
    return SimpleNamespace(
        mesh=mesh,
        diagonal=float(np.linalg.norm(mesh.extents)),
    )


def _assert_complete_partition(graph: object, face_count: int) -> None:
    assigned = np.concatenate([patch.face_indices for patch in graph.patches])
    assert len(assigned) == face_count
    assert np.array_equal(np.sort(assigned), np.arange(face_count))
    assert len(set(graph.face_patch_ids)) == len(graph.patches)


def test_edge_chains_split_self_touching_cycles_at_repeated_vertices() -> None:
    edges = np.asarray(
        [
            (0, 1),
            (1, 2),
            (2, 0),
            (0, 3),
            (3, 4),
            (4, 0),
        ],
        dtype=np.int64,
    )

    chains = _edge_chains(edges)

    assert len(chains) == 2
    assert {frozenset(chain[:-1]) for chain in chains} == {
        frozenset((0, 1, 2)),
        frozenset((0, 3, 4)),
    }
    assert all(chain[0] == chain[-1] for chain in chains)


def test_surface_graph_recognizes_box_planes_and_shared_boundaries() -> None:
    mesh = trimesh.creation.box(extents=(10.0, 8.0, 6.0))

    graph = detect_surface_graph(_mesh_data(mesh))

    assert len(graph.planar_patches) == 6
    assert len(graph.adjacency) == 12
    assert not graph.freeform_patches
    _assert_complete_partition(graph, len(mesh.faces))


def test_surface_graph_recognizes_arbitrary_axis_cylinder() -> None:
    mesh = trimesh.creation.cylinder(radius=3.0, height=8.0, sections=96)
    rotation = trimesh.transformations.rotation_matrix(
        math.radians(37.0),
        [1.0, 2.0, 0.5],
    )
    mesh.apply_transform(rotation)
    expected_axis = rotation[:3, :3] @ np.asarray([0.0, 0.0, 1.0])

    graph = detect_surface_graph(_mesh_data(mesh))

    assert len(graph.planar_patches) == 2
    assert len(graph.cylindrical_patches) == 1
    patch = graph.cylindrical_patches[0]
    assert patch.radius == pytest.approx(3.0, abs=1e-6)
    assert abs(float(patch.axis @ expected_axis)) == pytest.approx(1.0, abs=1e-6)
    assert patch.end - patch.start == pytest.approx(8.0, abs=1e-6)
    assert not graph.freeform_patches
    _assert_complete_partition(graph, len(mesh.faces))


def test_coarse_cylinder_facets_compete_as_one_point_fitted_surface() -> None:
    mesh = trimesh.creation.cylinder(radius=3.0, height=8.0, sections=16)

    graph = detect_surface_graph(_mesh_data(mesh))

    assert len(graph.planar_patches) == 2
    assert len(graph.cylindrical_patches) == 1
    assert len(graph.patches) == 3
    assert graph.cylindrical_patches[0].vertex_indices.size == 32
    _assert_complete_partition(graph, len(mesh.faces))


def test_surface_graph_recognizes_conical_surface() -> None:
    mesh = trimesh.creation.cone(radius=4.0, height=9.0, sections=96)

    graph = detect_surface_graph(_mesh_data(mesh))

    assert len(graph.planar_patches) == 1
    assert len(graph.conical_patches) == 1
    patch = graph.conical_patches[0]
    assert patch.semi_angle == pytest.approx(math.atan2(4.0, 9.0), abs=1e-6)
    assert not graph.freeform_patches
    _assert_complete_partition(graph, len(mesh.faces))


def test_narrow_cone_sectors_do_not_collapse_to_cylinders(tmp_path: Path) -> None:
    source = cq.Workplane(obj=cq.Solid.makeCone(10.0, 9.3, 2.0)).intersect(
        cq.Workplane("XY").box(3.0, 24.0, 4.0, centered=(False, True, True))
    )
    stl_path = tmp_path / "narrow-cone-sectors.stl"
    cq.exporters.export(
        source,
        str(stl_path),
        tolerance=0.02,
        angularTolerance=0.06,
    )
    data = load_mesh(stl_path, stl_path.name, "mm")

    graph = detect_surface_graph(data)

    expected_angle = math.atan2(0.7, 2.0)
    assert len(graph.conical_patches) == 2
    assert not graph.cylindrical_patches
    assert all(
        patch.semi_angle == pytest.approx(expected_angle, abs=math.radians(0.1))
        for patch in graph.conical_patches
    )
    _assert_complete_partition(graph, len(data.mesh.faces))


def test_surface_graph_recognizes_spherical_surface() -> None:
    mesh = trimesh.creation.icosphere(subdivisions=3, radius=5.0)
    mesh.apply_translation([2.5, -3.0, 7.0])

    graph = detect_surface_graph(_mesh_data(mesh))

    assert len(graph.spherical_patches) == 1
    patch = graph.spherical_patches[0]
    assert patch.radius == pytest.approx(5.0, abs=1e-6)
    assert patch.center == pytest.approx((2.5, -3.0, 7.0), abs=1e-6)
    assert not graph.freeform_patches
    _assert_complete_partition(graph, len(mesh.faces))


def test_surface_graph_recognizes_toroidal_surface() -> None:
    mesh = trimesh.creation.torus(
        major_radius=6.0,
        minor_radius=1.5,
        major_sections=72,
        minor_sections=36,
    )

    graph = detect_surface_graph(_mesh_data(mesh))

    assert len(graph.toroidal_patches) == 1
    patch = graph.toroidal_patches[0]
    assert patch.major_radius == pytest.approx(6.0, abs=1e-6)
    assert patch.minor_radius == pytest.approx(1.5, abs=1e-6)
    assert not graph.freeform_patches
    _assert_complete_partition(graph, len(mesh.faces))


def test_surface_graph_separates_tangent_prism_fillets(tmp_path: Path) -> None:
    stl_path = tmp_path / "filleted-prism.stl"
    model = cq.Workplane("XY").box(20.0, 16.0, 8.0).edges("|Z").fillet(2.0)
    cq.exporters.export(
        model,
        str(stl_path),
        tolerance=0.025,
        angularTolerance=0.07,
    )
    data = load_mesh(stl_path, stl_path.name, "mm")

    graph = detect_surface_graph(data)

    assert len(graph.planar_patches) == 6
    assert len(graph.cylindrical_patches) == 4
    assert all(
        patch.radius == pytest.approx(2.0, abs=0.01)
        for patch in graph.cylindrical_patches
    )
    assert not graph.freeform_patches
    _assert_complete_partition(graph, len(data.mesh.faces))
    assert detect_surface_graph(data) is graph


@pytest.mark.parametrize("width,height,radius", [(20, 36, 0.5), (10, 8, 1.5)])
def test_sparse_flat_walls_between_small_fillets_do_not_become_bowed_cylinders(
    tmp_path, width, height, radius,
):
    stl_path = tmp_path / "thin-filleted-plate.stl"
    model = cq.Workplane("XY").box(width, height, 1.6).edges("|Z").fillet(radius)
    cq.exporters.export(model, str(stl_path), tolerance=0.02, angularTolerance=0.06)
    data = load_mesh(stl_path, stl_path.name, "mm")

    graph = detect_surface_graph(data)

    assert len(graph.planar_patches) == 6
    assert len(graph.cylindrical_patches) == 4
    assert all(patch.radius == pytest.approx(radius, abs=0.001)
               for patch in graph.cylindrical_patches)
    _assert_complete_partition(graph, len(data.mesh.faces))


def test_tiny_transition_strip_does_not_hide_a_supported_cylinder() -> None:
    radius = 40.0
    angles = np.linspace(0, math.radians(20), 12)
    profile = radius * np.column_stack((np.cos(angles), np.sin(angles)))
    tangent = math.radians(115)
    profile = np.vstack((profile, profile[-1] + 0.02 * np.array([
        math.cos(tangent), math.sin(tangent),
    ])))
    vertices = np.array([[x, y, z] for x, y in profile for z in (0.0, 4.0)])
    faces = []
    for index in range(len(profile) - 1):
        first = 2 * index
        faces.extend(((first, first + 2, first + 3), (first, first + 3, first + 1)))
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)

    seeded = _validated_seeded_cylinder_fit(
        mesh, np.arange(len(faces)), np.array([0.0, 0.0, 1.0]),
        np.zeros(3), radius, 0.01, 8.0,
    )
    assert seeded is None
    data = _mesh_data(mesh)
    assert _fit_component(
        data, np.arange(len(faces)), 0.01, 8.0, allowed_kinds=("cylinder",),
    ) is None
    fit = _fit_component(
        data, np.arange(len(faces)), 0.01, 8.0, allowed_kinds=("cylinder",),
        allow_transition_slivers=True,
    )

    assert fit is not None
    assert fit.kind == "cylinder"
    assert fit.parameters["radius"] == pytest.approx(radius, rel=0.001)
    assert fit.max_error < 0.01


def test_surface_graph_retains_nonanalytic_surface_as_freeform() -> None:
    mesh = trimesh.creation.icosphere(subdivisions=3, radius=1.0)
    mesh.apply_scale([7.0, 4.0, 2.0])

    graph = detect_surface_graph(_mesh_data(mesh))

    assert len(graph.freeform_patches) == 1
    assert not graph.spherical_patches
    assert not graph.toroidal_patches
    _assert_complete_partition(graph, len(mesh.faces))


def test_shallow_cone_with_distant_apex_does_not_abort_fitting() -> None:
    angles = np.linspace(0, 2 * math.pi, 97)[:-1]
    vertices = np.array([
        [(5 + 0.02 * z) * math.cos(angle),
         (5 + 0.02 * z) * math.sin(angle), z]
        for angle in angles for z in (0.0, 1.0)
    ])
    faces = []
    for index in range(len(angles)):
        first, following = 2 * index, 2 * ((index + 1) % len(angles))
        faces.extend(((first, following, following + 1),
                      (first, following + 1, first + 1)))
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)

    fit = _fit_component(_mesh_data(mesh), np.arange(len(faces)), 0.001, 5.0,
                         allowed_kinds=("cone",))

    assert fit is not None
    assert fit.kind == "cone"
    assert fit.parameters["apex"][2] == pytest.approx(-250.0, abs=0.01)
    assert fit.max_error < 0.001


def test_local_sampling_separates_two_sparse_tangent_cylinders() -> None:
    # Two quarter-circle bends joined by a straight wall. Each bend contains
    # fewer triangles than the old minimum neighborhood of 64.
    angles = np.linspace(0, math.pi / 2, 16)
    first = np.column_stack((5 * np.cos(angles), 5 * np.sin(angles)))
    second = np.column_stack((-20 + 5 * np.cos(angles + math.pi / 2),
                              5 * np.sin(angles + math.pi / 2)))
    profile = np.vstack((first, second))
    vertices = np.array([[x, y, z] for x, y in profile for z in (0., 4.)])
    faces = []
    for index in range(len(profile) - 1):
        start = 2 * index
        faces.extend(((start, start + 2, start + 3),
                      (start, start + 3, start + 1)))
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)

    detected, residuals = _extract_local_analytic_regions(
        _mesh_data(mesh), np.arange(len(faces)), 0.001, 5.0, 0.01,
    )

    assert len(detected) == 2
    assert all(fit.kind == "cylinder" for fit, _ in detected)
    assert all(fit.parameters["radius"] == pytest.approx(5.0, abs=0.001)
               for fit, _ in detected)
    assert sum(len(indices) for _, indices in detected) == 60
    assert sum(len(indices) for indices in residuals) == 2


def test_plane_relations_use_tolerance_gated_exact_fraction_snap() -> None:
    offsets = [0.0, 1.0 / 3.0 + 2e-5, 1.0]
    vertices: list[list[float]] = []
    faces: list[list[int]] = []
    patches: list[PlanarPatch] = []
    tilted_normals = [
        np.asarray([5e-5, 0.0, 1.0]),
        np.asarray([-5e-5, 0.0, 1.0]),
        np.asarray([0.0, 0.0, 1.0]),
    ]
    for index, (offset, normal) in enumerate(zip(offsets, tilted_normals, strict=True)):
        first = len(vertices)
        vertices.extend(
            [
                [0.0, 0.0, offset],
                [1.0, 0.0, offset],
                [1.0, 1.0, offset],
                [0.0, 1.0, offset],
            ]
        )
        face_indices = np.asarray([len(faces), len(faces) + 1], dtype=np.int64)
        faces.extend([[first, first + 1, first + 2], [first, first + 2, first + 3]])
        normal = normal / np.linalg.norm(normal)
        patches.append(
            PlanarPatch(
                patch_id=f"plane-{index}",
                face_indices=face_indices,
                vertex_indices=np.arange(first, first + 4, dtype=np.int64),
                origin=np.asarray([0.0, 0.0, offset]),
                normal=normal,
                x_direction=np.asarray([1.0, 0.0, 0.0]),
                y_direction=np.asarray([0.0, 1.0, 0.0]),
                area=1.0,
                boundary_loops=[],
                polygon=None,
            )
        )
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)

    relation_count = _regularize_plane_relations(mesh, patches, 1e-3, 5.0)

    assert relation_count >= 3
    assert abs(float(patches[0].normal @ patches[1].normal)) == pytest.approx(1.0)
    positions = [float(patch.origin @ patches[0].normal) for patch in patches]
    ratio = (positions[1] - positions[0]) / (positions[2] - positions[0])
    assert ratio == pytest.approx(1.0 / 3.0, abs=1e-12)
    assert all(patch.max_error <= 1e-3 for patch in patches)
