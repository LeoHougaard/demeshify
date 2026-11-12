import math

import cadquery as cq
import pytest

from app.mesh import load_mesh
from app.scoring import score_exported_shape
from app.surface_brep import _nest_closed_solids, build_faceted_brep, export_surface_brep
from app.surface_reconstruction import reconstruct_surfaces
from app.verification import acceptance_threshold, passes_geometry_gate


def _hollow_box():
    return cq.Workplane("XY").box(10, 10, 10).cut(cq.Workplane("XY").box(8, 8, 8))


@pytest.mark.parametrize("reverse", [False, True])
def test_enclosed_shell_subtracts_volume_even_with_reversed_input(tmp_path, reverse):
    source = tmp_path / "input.stl"
    cq.exporters.export(_hollow_box(), str(source))
    if reverse:
        import trimesh

        mesh = trimesh.load(source, process=False)
        mesh.invert()
        mesh.export(source)

    data = load_mesh(source, source.name)

    assert data.report.watertight
    assert data.report.body_count == 2
    assert data.report.solid_count == 1
    assert data.report.cavity_shell_count == 1
    assert data.report.volume_mm3 == pytest.approx(488)
    assert data.mesh.volume == pytest.approx(488)


@pytest.mark.parametrize("correct", [True, False])
def test_verification_distinguishes_hollow_solid_from_nested_positive_solids(tmp_path, correct):
    hollow = _hollow_box()
    source = tmp_path / "input.stl"
    cq.exporters.export(hollow, str(source))
    candidate = hollow if correct else cq.Compound.makeCompound([
        cq.Workplane("XY").box(10, 10, 10).val(),
        cq.Workplane("XY").box(8, 8, 8).val(),
    ])
    cq.exporters.export(candidate, str(tmp_path / "reconstruction.step"))
    data = load_mesh(source, source.name)

    score = score_exported_shape(data, tmp_path)

    assert score.valid_solid
    assert score.chamfer_max_mm < 1e-8
    assert score.local_max_mm < 1e-8
    assert score.source_solid_count == 1
    assert score.step_solid_count == (1 if correct else 2)
    assert score.component_count_match is correct
    assert passes_geometry_gate(score, acceptance_threshold(data.diagonal)) is correct
    assert score.volume_error_percent == pytest.approx(0 if correct else 1024 / 488 * 100)


@pytest.mark.parametrize("island", [False, True])
def test_faceted_fallback_preserves_cavity_and_nested_island(tmp_path, island):
    hollow = _hollow_box().val()
    source_shape = cq.Compound.makeCompound([
        hollow, cq.Workplane("XY").box(2, 2, 2).val(),
    ]) if island else hollow
    source = tmp_path / "input.stl"
    cq.exporters.export(source_shape, str(source))
    data = load_mesh(source, source.name)

    result = build_faceted_brep(data)
    export_surface_brep(result, tmp_path, source)
    delivered = cq.importers.importStep(str(tmp_path / "reconstruction.step")).val()
    score = score_exported_shape(data, tmp_path)

    assert result.faceted_fallback
    assert result.solid_count == (2 if island else 1)
    assert delivered.isValid()
    assert len(delivered.Solids()) == (2 if island else 1)
    assert sum(solid.Volume() for solid in delivered.Solids()) == pytest.approx(
        496 if island else 488,
    )
    assert passes_geometry_gate(score, acceptance_threshold(data.diagonal))


def test_overlapping_bounds_do_not_turn_body_in_through_hole_into_cavity(tmp_path):
    ring = cq.Workplane("XY").box(10, 10, 10).cut(cq.Workplane("XY").box(8, 8, 20))
    island = cq.Workplane("XY").box(2, 2, 2)
    source = tmp_path / "input.stl"
    cq.exporters.export(cq.Compound.makeCompound([ring.val(), island.val()]), str(source))

    data = load_mesh(source, source.name)

    assert data.report.body_count == 2
    assert data.report.solid_count == 2
    assert data.report.cavity_shell_count == 0
    assert data.report.volume_mm3 == pytest.approx(368)


@pytest.mark.parametrize("island", [False, True])
def test_surface_conversion_delivers_analytic_hollow_step(tmp_path, island):
    shape = _hollow_box().val()
    if island:
        shape = cq.Compound.makeCompound([shape, cq.Workplane("XY").box(2, 2, 2).val()])
    source = tmp_path / "input.stl"
    cq.exporters.export(shape, str(source))

    report = reconstruct_surfaces("enclosed-cavity", source, source.name)
    delivered = cq.importers.importStep(str(tmp_path / "reconstruction.step")).val()

    assert report.status == "complete"
    assert not report.surface.faceted_fallback
    assert report.surface.solid_count == (2 if island else 1)
    assert report.score.valid_solid
    assert report.score.component_count_match
    assert delivered.isValid()
    assert sum(solid.Volume() for solid in delivered.Solids()) == pytest.approx(
        496 if island else 488,
    )


def test_existing_hollow_solid_keeps_inner_shell_when_assembled_with_an_island(tmp_path):
    solids = [_hollow_box().val(), cq.Workplane("XY").box(2, 2, 2).val()]
    source = tmp_path / "input.stl"
    cq.exporters.export(cq.Compound.makeCompound(solids), str(source))
    data = load_mesh(source, source.name)

    assembled = _nest_closed_solids(solids, 1e-6)
    cq.exporters.export(assembled, str(tmp_path / "reconstruction.step"))
    score = score_exported_shape(data, tmp_path)

    assert len(assembled.Solids()) == 2
    assert len(assembled.Shells()) == 3
    assert sum(solid.Volume() for solid in assembled.Solids()) == pytest.approx(496)
    assert passes_geometry_gate(score, acceptance_threshold(data.diagonal))


def test_surface_conversion_preserves_analytic_spherical_cavity(tmp_path):
    shape = cq.Workplane("XY").sphere(5).cut(cq.Workplane("XY").sphere(4))
    source = tmp_path / "input.stl"
    cq.exporters.export(shape, str(source), tolerance=0.01, angularTolerance=0.15)

    report = reconstruct_surfaces("spherical-cavity", source, source.name)
    delivered = cq.importers.importStep(str(tmp_path / "reconstruction.step")).val()

    assert report.status == "complete"
    assert not report.surface.faceted_fallback
    assert report.surface.solid_count == 1
    assert len(delivered.Shells()) == 2
    assert {face.geomType() for face in delivered.Faces()} == {"SPHERE"}
    assert delivered.Volume() == pytest.approx(4 / 3 * math.pi * (5**3 - 4**3), rel=0.001)
