from dataclasses import replace

import cadquery as cq
import pytest
import trimesh

import app.surface_reconstruction as reconstruction
from app.mesh import load_mesh
from app.step import load_step_shape
from app.surface_brep import build_planar_topology_brep, build_surface_brep
from app.surface_graph import detect_surface_graph


@pytest.mark.parametrize("units,scale", [("mm", 1), ("cm", 10)])
def test_failed_planar_fit_recovers_a_hole_without_triangle_faces(
    tmp_path, monkeypatch, units, scale,
):
    source = cq.Workplane("YZ").rect(10, 20).rect(8, 18).extrude(0.05)
    path = tmp_path / "frame.stl"
    cq.exporters.export(source, str(path))
    data = load_mesh(path, path.name, units)
    base = build_surface_brep(data)
    # A valid undersized fitted solid must fail both ordinary trim attempts.
    # Recovery has to construct the full frame from its unchanged source.
    bad_fit = replace(base, shape=base.shape.scale(0.5), warnings=[])
    monkeypatch.setattr(reconstruction, "build_surface_brep", lambda *_a, **_k: bad_fit)
    monkeypatch.setattr(reconstruction, "recover_prismatic_solid", lambda *_: None)
    report = reconstruction._reconstruct_surfaces_direct("planar", path, path.name, units)

    assert report.status == "complete"
    assert report.surface.closed and not report.surface.faceted_fallback
    assert not report.surface.source_mesh_fallback
    assert report.surface.faceted_face_count == 0
    assert report.score.valid_solid and report.score.component_count_match
    assert report.score.volume_error_percent < 1e-6
    shape = load_step_shape(tmp_path / "reconstruction.step")
    assert len(shape.Solids()) == 1 and len(shape.Faces()) == 10
    assert {face.geomType() for face in shape.Faces()} == {"PLANE"}
    assert shape.Volume() == pytest.approx(2.8 * scale**3, rel=1e-6)
    solid = shape.Solids()[0]
    assert not solid.isInside(cq.Vector(0.025 * scale, 0, 0))
    assert solid.isInside(cq.Vector(0.025 * scale, 4.5 * scale, 0))


def test_planar_recovery_does_not_overwrite_with_a_valid_wrong_size(tmp_path, monkeypatch):
    path = tmp_path / "box.stl"
    trimesh.creation.box(extents=(7, 5, 3)).export(path)
    base = build_surface_brep(load_mesh(path, path.name))
    failed_fit = replace(base, closed=False, solid_count=0, warnings=[])
    wrong_recovery = replace(base, shape=base.shape.scale(0.5), warnings=[])
    monkeypatch.setattr(reconstruction, "build_surface_brep", lambda *_a, **_k: failed_fit)
    monkeypatch.setattr(reconstruction, "recover_prismatic_solid", lambda *_: None)
    monkeypatch.setattr(reconstruction, "build_planar_topology_brep", lambda *_: wrong_recovery)
    report = reconstruction._reconstruct_surfaces_direct("wrong-planar", path, path.name)

    assert report.status == "best_effort"
    assert load_step_shape(tmp_path / "reconstruction.step").Volume() == pytest.approx(105)
    assert report.score.volume_error_percent < 0.01


def test_planar_recovery_keeps_a_recognized_cylinder_on_its_analytic_path(tmp_path):
    path = tmp_path / "cylinder.stl"
    trimesh.creation.cylinder(radius=3, height=8, sections=96).export(path)
    data = load_mesh(path, path.name)
    graph = detect_surface_graph(data)
    assert graph.cylindrical_patches
    assert build_planar_topology_brep(data, graph) is None
