from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .mesh import load_mesh
from .schemas import PlanEditRequest, ReconstructionPlan, ReconstructionReport
from .scoring import score_exported_shape, score_plan
from .storage import load_report, run_dir, save_report
from .verification import acceptance_threshold, passes_geometry_gate, verification_warnings

EDITABLE_ARTIFACTS = (
    "reconstruction.step",
    "reconstruction.stl",
    "reconstruction.py",
    "plan.json",
)


class RevisionConflictError(ValueError):
    pass


def _numeric_values(plan: ReconstructionPlan) -> dict[str, float]:
    values: dict[str, float] = {}

    def collect(value: Any, path: str) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            values[path] = float(value)
            return
        if isinstance(value, BaseModel):
            for field_name in type(value).model_fields:
                if field_name in {"tree_index", "confidence", "feature_index"}:
                    continue
                collect(getattr(value, field_name), f"{path}.{field_name}")
            return
        if isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                collect(item, f"{path}.{index}")

    for feature in [plan.base, *plan.operations]:
        collect(feature, feature.feature_id)
    return values


def _snapshot(directory: Path, revision: int) -> None:
    snapshot = directory / "history" / f"revision-{revision:04d}"
    snapshot.mkdir(parents=True, exist_ok=True)
    for file_name in (*EDITABLE_ARTIFACTS, "report.json"):
        source = directory / file_name
        target = snapshot / file_name
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)


def apply_plan_edit(run_id: str, request: PlanEditRequest) -> ReconstructionReport:
    started = time.perf_counter()
    current = load_report(run_id)
    if current.plan is None:
        raise ValueError("This run has no editable feature plan")
    if request.expected_revision != current.plan.revision:
        raise RevisionConflictError(
            "The feature tree changed in another editor. Reload before applying this edit."
        )

    edited = ReconstructionPlan.model_validate(
        request.plan.model_dump(mode="python")
    )
    edited.revision = current.plan.revision + 1
    previous_values = _numeric_values(current.plan)
    edited_values = _numeric_values(edited)
    edited.parameter_sources = {
        path: (
            "user"
            if path not in previous_values
            or abs(value - previous_values[path]) > 1e-12
            else current.plan.parameter_sources.get(path, "measured")
        )
        for path, value in edited_values.items()
    }

    directory = run_dir(run_id)
    input_path = directory / "input.stl"
    if not input_path.is_file():
        raise ValueError("The original STL is no longer available for verification")
    data = load_mesh(
        input_path,
        current.mesh.file_name,
        current.mesh.input_units,
    )

    scratch_root = directory / "editor-candidates"
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"revision-{edited.revision:04d}-",
        dir=scratch_root,
    ) as scratch_name:
        candidate = score_plan(
            data,
            edited,
            Path(scratch_name),
            candidate_count=(current.score.candidate_count + 1 if current.score else 1),
        )
        candidate.report = score_exported_shape(
            data, candidate.directory, candidate_count=candidate.report.candidate_count
        )
        _snapshot(directory, current.plan.revision)
        for file_name in EDITABLE_ARTIFACTS:
            shutil.copy2(candidate.directory / file_name, directory / file_name)

    passed = (
        passes_geometry_gate(candidate.report, acceptance_threshold(data.diagonal))
        and candidate.plan.representation == "semantic"
    )
    warnings = [
        warning
        for warning in current.warnings
        if not warning.startswith(("Feature tree revision ", "Verification:"))
    ]
    warnings.append(
        f"Feature tree revision {edited.revision} was edited by the user and "
        "rebuilt against the original STL."
    )
    warnings.extend(verification_warnings(candidate.report, acceptance_threshold(data.diagonal)))
    report = ReconstructionReport(
        id=run_id,
        status="complete" if passed else "best_effort",
        mesh=data.report,
        plan=candidate.plan,
        score=candidate.report,
        warnings=warnings,
        elapsed_seconds=time.perf_counter() - started,
    )
    save_report(report)
    return report


def load_plan_revision(run_id: str, revision: int) -> ReconstructionPlan:
    current = load_report(run_id)
    if current.plan is not None and current.plan.revision == revision:
        return current.plan
    path = run_dir(run_id) / "history" / f"revision-{revision:04d}" / "plan.json"
    if not path.is_file():
        raise FileNotFoundError(revision)
    return ReconstructionPlan.model_validate(
        json.loads(path.read_text(encoding="utf-8"))
    )
