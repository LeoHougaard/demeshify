from __future__ import annotations

from pathlib import Path

import cadquery as cq
import pytest
import trimesh

import app.reconstruction as reconstruction_module
from app.cad import build_plan
from app.mesh import load_mesh
from app.profiles import generate_prismatic_candidates
from app.reconstruction import reconstruct
from app.schemas import CylinderFeature, ExtrudeFeature


def export_stl(shape: cq.Workplane, path: Path) -> Path:
    cq.exporters.export(shape, str(path), tolerance=0.02, angularTolerance=0.1)
    return path


@pytest.fixture
def plate_stl(tmp_path: Path) -> Path:
    plate = cq.Workplane("XY").box(40, 30, 8)
    plate = plate.faces(">Z").workplane().rect(30, 20, forConstruction=True).vertices().hole(4)
    return export_stl(plate, tmp_path / "mounting_plate.stl")


@pytest.fixture
def tube_stl(tmp_path: Path) -> Path:
    tube = cq.Workplane("XY").circle(12).circle(7).extrude(28)
    return export_stl(tube, tmp_path / "spacer_tube.stl")


def test_plate_recovers_extrusion_and_openings(plate_stl: Path) -> None:
    data = load_mesh(plate_stl, plate_stl.name)
    candidates = generate_prismatic_candidates(data, "Mounting plate")
    z_candidates = [candidate for candidate in candidates if candidate.plan.base.axis.value == "Z"]
    assert z_candidates
    base = z_candidates[0].plan.base
    assert isinstance(base, ExtrudeFeature)
    assert base.depth == pytest.approx(8, abs=0.05)
    assert len(base.holes) == 4
    assert build_plan(z_candidates[0].plan).val().isValid()


def test_tube_recovers_analytic_cylinder(tube_stl: Path) -> None:
    data = load_mesh(tube_stl, tube_stl.name)
    candidates = generate_prismatic_candidates(data, "Tube")
    cylinders = [
        candidate.plan.base
        for candidate in candidates
        if isinstance(candidate.plan.base, CylinderFeature)
    ]
    assert cylinders
    base = cylinders[0]
    assert base.radius == pytest.approx(12, abs=0.08)
    assert base.inner_radius == pytest.approx(7, abs=0.08)
    assert base.depth == pytest.approx(28, abs=0.05)


def test_end_to_end_exports_valid_step(
    plate_stl: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconstruction_module, "save_report", lambda report: None)
    report = reconstruct("123456789abc", plate_stl, plate_stl.name)
    assert report.status == "complete"
    assert report.score is not None and report.score.valid_solid
    assert report.score.chamfer_p95_mm < 0.2
    assert (tmp_path / "reconstruction.step").is_file()
    assert (tmp_path / "reconstruction.py").is_file()
    output = trimesh.load(tmp_path / "reconstruction.stl", force="mesh")
    assert output.is_watertight
