import json
import sys

import cadquery as cq

from app import editor, job_worker, storage
from app.cad import export_plan
from app.mesh import load_mesh
from app.schemas import (
    Axis,
    ExtrudeFeature,
    PlanEditRequest,
    PolygonProfile,
    ReconstructionPlan,
    ReconstructionReport,
)


def test_failed_report_save_rolls_back_every_published_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "RUNS", tmp_path)
    run_id = "b" * 32
    directory = storage.run_dir(run_id)
    plan = ReconstructionPlan(
        name="box",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=0,
            depth=2,
            outer=PolygonProfile(points=[(-5, -5), (5, -5), (5, 5), (-5, 5)]),
        ),
    )
    cq.exporters.export(cq.Workplane("XY").box(10, 10, 2), str(directory / "input.stl"))
    export_plan(plan, directory)
    data = load_mesh(directory / "input.stl", "box.stl")
    storage.save_report(
        ReconstructionReport(
            id=run_id,
            status="complete",
            mesh=data.report,
            plan=plan,
            score=None,
            warnings=[],
            elapsed_seconds=0,
        )
    )
    edited = plan.model_copy(deep=True)
    edited.base.depth = 3
    monkeypatch.setattr(
        editor,
        "save_report",
        lambda _: (_ for _ in ()).throw(OSError("disk full")),
    )

    try:
        editor.apply_plan_edit(run_id, PlanEditRequest(plan=edited, expected_revision=0))
    except OSError as exc:
        assert str(exc) == "disk full"
    else:
        raise AssertionError("the injected save failure did not propagate")

    report = json.loads((directory / "report.json").read_text())
    published_plan = json.loads((directory / "plan.json").read_text())
    step = cq.importers.importStep(str(directory / "reconstruction.step")).val()
    assert report["plan"]["revision"] == 0
    assert report["plan"]["base"]["depth"] == 2
    assert published_plan["revision"] == 0
    assert published_plan["base"]["depth"] == 2
    assert step.BoundingBox().zlen == 2


def test_edit_worker_does_not_rewrite_transactional_report(tmp_path, monkeypatch):
    plan = ReconstructionPlan(
        name="box",
        base=ExtrudeFeature(
            axis=Axis.Z,
            start=0,
            depth=2,
            outer=PolygonProfile(points=[(-5, -5), (5, -5), (5, 5), (-5, 5)]),
        ),
    )
    request = tmp_path / "edit.json"
    request.write_text(json.dumps({
        "mode": "edit",
        "run_id": "b" * 32,
        "edit": {"plan": plan.model_dump(mode="json"), "expected_revision": 0},
    }))
    monkeypatch.setattr(sys, "argv", ["job_worker", str(request)])
    calls = []
    monkeypatch.setattr(editor, "apply_plan_edit", lambda *_: calls.append("edit"))

    def unexpected_write(*_args):
        raise AssertionError("job_worker rewrote the published report")

    monkeypatch.setattr(storage, "atomic_write_text", unexpected_write)

    assert job_worker.main() == 0
    assert calls == ["edit"]
