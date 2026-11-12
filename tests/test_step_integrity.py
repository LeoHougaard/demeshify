import cadquery as cq
import pytest
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

from app.mesh import load_mesh
from app.scoring import score_exported_shape
from app.surface_brep import build_faceted_brep, export_surface_brep
from app.verification import (
    acceptance_threshold,
    passes_geometry_gate,
    verification_warnings,
)
from tools.benchmark_surface_corpus import step_surface_metrics


@pytest.fixture
def box():
    return cq.Workplane("XY").box(10, 10, 10).val()


def _source(shape, directory):
    path = directory / "input.stl"
    cq.exporters.export(shape, str(path))
    return load_mesh(path, path.name)


def _write_roots(roots, directory):
    writer = STEPControl_Writer()
    for shape in roots:
        assert writer.Transfer(shape.wrapped, STEPControl_AsIs) == IFSelect_RetDone
    path = directory / "reconstruction.step"
    assert writer.Write(str(path)) == IFSelect_RetDone
    return path


@pytest.mark.parametrize("kind", ["edge", "wire", "vertex", "face"])
def test_free_step_geometry_cannot_hide_beside_a_matching_solid(tmp_path, box, kind):
    data = _source(box, tmp_path)
    edge = cq.Edge.makeLine(cq.Vector(20, 0, 0), cq.Vector(100, 0, 0))
    extra = {
        "edge": edge,
        "wire": cq.Wire.assembleEdges([edge]),
        "vertex": cq.Vertex.makeVertex(100, 100, 100),
        "face": cq.Face.makePlane(10, 10, cq.Vector(50, 0, 0)),
    }[kind]
    cq.exporters.export(
        cq.Compound.makeCompound([box, extra]),
        str(tmp_path / "reconstruction.step"),
    )

    score = score_exported_shape(data, tmp_path)

    assert score.valid_brep
    assert not score.valid_solid
    assert score.unowned_geometry_counts
    assert not passes_geometry_gate(score, acceptance_threshold(data.diagonal))
    assert any("outside" in warning for warning in verification_warnings(score, 0.12))


def test_single_root_matching_solid_still_passes(tmp_path, box):
    data = _source(box, tmp_path)
    _write_roots([box], tmp_path)

    score = score_exported_shape(data, tmp_path)

    assert passes_geometry_gate(score, acceptance_threshold(data.diagonal))
    assert score.unowned_geometry_counts == {}


@pytest.mark.parametrize("reverse", [False, True])
def test_extra_step_root_fails_regardless_of_root_order(tmp_path, box, reverse):
    data = _source(box, tmp_path)
    roots = [box, box.translate((50, 0, 0))]
    path = _write_roots(roots[::-1] if reverse else roots, tmp_path)
    assert len(cq.importers.importStep(str(path)).vals()) == 2

    score = score_exported_shape(data, tmp_path)

    assert score.step_solid_count == 2
    assert score.step_shell_count == 2
    assert score.component_count_match is False
    assert score.volume_error_percent == pytest.approx(100)
    assert score.local_max_mm > 40
    assert not passes_geometry_gate(score, acceptance_threshold(data.diagonal))


@pytest.mark.parametrize("compound", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_matching_multiple_step_roots_and_compound_are_equivalent(
    tmp_path, box, compound, reverse
):
    roots = [box, box.translate((50, 0, 0))]
    if reverse:
        roots.reverse()
    data = _source(cq.Compound.makeCompound(roots), tmp_path)
    path = _write_roots([cq.Compound.makeCompound(roots)] if compound else roots, tmp_path)
    assert len(cq.importers.importStep(str(path)).vals()) == (1 if compound else 2)

    score = score_exported_shape(data, tmp_path)

    assert score.step_solid_count == 2
    assert score.step_shell_count == 2
    assert score.unowned_geometry_counts == {}
    assert score.volume_error_percent < 1e-8
    assert score.local_max_mm < 1e-8
    assert passes_geometry_gate(score, acceptance_threshold(data.diagonal))


@pytest.mark.parametrize("reverse", [False, True])
def test_reference_metrics_cover_all_transfer_roots(tmp_path, box, reverse):
    roots = [box, box.translate((50, 0, 0))]
    path = _write_roots(roots[::-1] if reverse else roots, tmp_path)

    metrics = step_surface_metrics(path)

    assert metrics["face_counts"] == {"PLANE": 12}
    assert metrics["face_areas_mm2"]["PLANE"] == pytest.approx(1200)
    assert len(metrics["shape"].Solids()) == 2


@pytest.mark.parametrize("kind", ["edge", "vertex"])
def test_export_roundtrip_downgrades_geometry_outside_solids(tmp_path, box, kind):
    data = _source(box, tmp_path)
    result = build_faceted_brep(data, reason="STEP integrity regression")
    assert result.closed
    extra = (
        cq.Edge.makeLine(cq.Vector(20, 0, 0), cq.Vector(100, 0, 0))
        if kind == "edge"
        else cq.Vertex.makeVertex(100, 100, 100)
    )
    result.shape = cq.Compound.makeCompound([result.shape, extra])

    export_surface_brep(result, tmp_path)

    assert not result.closed
    assert result.solid_count == 0
    assert result.joined_shape is not None
    assert any("downgraded" in warning for warning in result.warnings)
