import json

import cadquery as cq

from app.mesh import load_mesh
from app.prismatic_recovery import _cut_conical_bores, recover_prismatic_solid
from app.profiles import PlanCandidate
from app.schemas import Axis, ExtrudeFeature, PolygonProfile, ReconstructionPlan
from app.surface_graph import detect_surface_graph


def test_prismatic_recovery_retains_holes_and_exports_verified_cad(tmp_path):
    source = cq.Workplane("XY").box(20, 12, 2).faces(">Z").workplane().hole(4)
    path = tmp_path / "input.stl"
    cq.exporters.export(source, str(path), tolerance=0.02, angularTolerance=0.06)
    data = load_mesh(path, "plate.stl")

    report = recover_prismatic_solid(data, "test", tmp_path)

    assert report is not None
    assert report.status == "complete"
    assert report.score.step_geometry_verified
    assert report.surface.surface_counts["cylinder"] == 1
    assert not report.surface.faceted_fallback
    solid = cq.importers.importStep(str(tmp_path / "reconstruction.step")).val()
    assert solid.isValid()
    assert abs(solid.Volume() - source.val().Volume()) < 0.1


def test_prismatic_recovery_rejects_loss_of_small_rounds_even_with_close_geometry(
    tmp_path, monkeypatch,
):
    source = cq.Workplane("XY").box(10, 8, 2).edges("|Z").fillet(0.2)
    path = tmp_path / "input.stl"
    cq.exporters.export(source, str(path), tolerance=0.001, angularTolerance=0.06)
    data = load_mesh(path, "rounded.stl")
    polygon = PolygonProfile(points=[(-5, -4), (5, -4), (5, 4), (-5, 4)])
    plan = ReconstructionPlan(
        name="rounded", base=ExtrudeFeature(axis="Z", start=-1, depth=2, outer=polygon),
    )
    candidate = PlanCandidate(plan=plan, heuristic_score=0, section_consistency=1)
    monkeypatch.setattr(
        "app.prismatic_recovery.generate_prismatic_candidates", lambda *_: [candidate],
    )
    monkeypatch.setattr("app.prismatic_recovery.generate_layered_candidates", lambda *_: [])

    assert recover_prismatic_solid(data, "test", tmp_path) is None
    assert not (tmp_path / "reconstruction.step").exists()


def test_layered_recovery_keeps_a_circular_boss_on_a_rectangular_plate(tmp_path):
    source = cq.Workplane("XY").box(10, 8, 2).faces(">Z").workplane().circle(2).extrude(3)
    path = tmp_path / "input.stl"
    cq.exporters.export(source, str(path), tolerance=0.01, angularTolerance=0.06)

    report = recover_prismatic_solid(load_mesh(path, path.name), "layers", tmp_path)

    assert report is not None and report.status == "complete"
    assert report.surface.surface_counts["cylinder"] == 1
    assert report.score.local_max_mm < 0.01
    graph = json.loads((tmp_path / "surface_graph.json").read_text())
    assert graph["recovery_method"] == "layered_extrusion"


def test_layered_recovery_restores_both_ends_of_counterbores(tmp_path):
    source = cq.Workplane("XY").box(80, 60, 4, centered=(True, True, False))
    source = source.faces(">Z").workplane().pushPoints([(-15, 0), (15, 0)]).cboreHole(4, 8, 1.5)
    shape = source.val()
    for x in (-15, 15):
        shape = shape.cut(
            cq.Solid.makeCone(2.3, 2, 0.3, cq.Vector(x, 0, 0)),
            cq.Solid.makeCone(4, 4.3, 0.3, cq.Vector(x, 0, 3.7)),
        )
    path = tmp_path / "counterbores.stl"
    shape.exportStl(str(path), tolerance=0.005, angularTolerance=0.06, relative=False)
    data = load_mesh(path, path.name)

    report = recover_prismatic_solid(data, "bores", tmp_path)

    assert report is not None and report.status == "complete"
    assert report.surface.surface_counts["cone"] == 4
    assert not report.surface.faceted_fallback
    assert report.score.step_geometry_verified and report.score.component_count_match
    assert report.score.volume_error_percent < 0.1
    graph = json.loads((tmp_path / "surface_graph.json").read_text())
    assert graph["recovery_method"] == "layered_extrusion_with_conical_bores"


def test_conical_bore_recovery_does_not_cut_a_convex_boss(tmp_path):
    plate = cq.Workplane("XY").box(20, 20, 1, centered=(True, True, False)).val()
    source = plate.fuse(cq.Solid.makeCone(3, 1, 2, cq.Vector(0, 0, 1)))
    path = tmp_path / "boss.stl"
    source.exportStl(str(path), tolerance=0.005, angularTolerance=0.06, relative=False)
    data = load_mesh(path, path.name)
    graph = detect_surface_graph(data)

    assert graph.conical_patches
    assert _cut_conical_bores(plate, data, graph, Axis.Z) is None
