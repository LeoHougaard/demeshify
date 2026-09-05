from types import SimpleNamespace

import cadquery as cq
import numpy as np
import pytest
import trimesh

from app.mesh import load_mesh
from app.scoring import _step_mesh, score_exported_shape
from app.surface_reconstruction import (
    _acceptance_threshold,
    _passes_surface_gate,
    reconstruct_surfaces,
)


def _accepted(data, score):
    result = SimpleNamespace(
        valid=True,
        closed=True,
        faceted_fallback=False,
        source_mesh_fallback=False,
        free_edge_count=0,
    )
    return _passes_surface_gate(result, score, _acceptance_threshold(data.diagonal))


def test_matching_preview_cannot_hide_wrong_step_dimensions(tmp_path):
    source = cq.Workplane("XY").box(100, 100, 10)
    cq.exporters.export(source, str(tmp_path / "input.stl"))
    cq.exporters.export(source, str(tmp_path / "reconstruction.stl"))
    cq.exporters.export(
        cq.Workplane("XY").box(10, 10, 1),
        str(tmp_path / "reconstruction.step"),
    )
    data = load_mesh(tmp_path / "input.stl", "plate.stl")

    score = score_exported_shape(data, tmp_path)

    assert score.valid_solid
    assert not _accepted(data, score)
    assert score.chamfer_max_mm > 40


def test_missing_small_through_hole_fails_even_when_p95_is_small(tmp_path):
    plate = cq.Workplane("XY").box(100, 100, 10)
    source = plate.faces(">Z").workplane().hole(1)
    cq.exporters.export(
        source, str(tmp_path / "input.stl"), tolerance=0.001, angularTolerance=0.04
    )
    cq.exporters.export(plate, str(tmp_path / "reconstruction.stl"))
    cq.exporters.export(plate, str(tmp_path / "reconstruction.step"))
    data = load_mesh(tmp_path / "input.stl", "small-hole.stl")

    score = score_exported_shape(data, tmp_path)

    assert score.valid_solid
    assert score.chamfer_p95_mm < _acceptance_threshold(data.diagonal)
    assert not _accepted(data, score)


def test_surface_conversion_accounts_for_tiny_disconnected_body(tmp_path):
    large = trimesh.creation.box(extents=(10, 10, 10))
    small = trimesh.creation.box(extents=(0.02, 0.02, 0.02))
    small.apply_translation((8, 0, 0))
    source = tmp_path / "input.stl"
    trimesh.util.concatenate((large, small)).export(source)

    report = reconstruct_surfaces("tiny-component", source, "two-bodies.stl")

    assert report.mesh.body_count == 2
    assert report.surface.solid_count == 2
    imported = cq.importers.importStep(str(tmp_path / "reconstruction.step")).val()
    assert len(imported.Solids()) == 2
    assert report.score.valid_solid


def test_verification_mesh_deflection_is_absolute_on_large_curved_faces():
    tolerance = 0.002
    mesh = _step_mesh(cq.Solid.makeCylinder(100, 1), tolerance)
    side_centers = mesh.triangles_center[np.abs(mesh.face_normals[:, 2]) < 0.5]
    radial_error = 100 - np.linalg.norm(side_centers[:, :2], axis=1)
    assert np.max(radial_error) <= tolerance * 1.1


@pytest.mark.parametrize("size", [0.02, 0.002])
def test_disjoint_bodies_use_their_own_fitting_scale(tmp_path, size):
    large = trimesh.creation.box(extents=(10, 10, 10))
    small = trimesh.creation.box(extents=(size, size, size))
    small.apply_translation((8, 0, 0))
    source = tmp_path / "input.stl"
    trimesh.util.concatenate((large, small)).export(source)

    report = reconstruct_surfaces("component-scales", source, "two-bodies.stl")

    assert report.status == "complete"
    assert report.surface.solid_count == 2
    assert not report.surface.faceted_fallback
    assert report.surface.brep_face_count == 12
