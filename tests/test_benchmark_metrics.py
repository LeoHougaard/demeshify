from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import trimesh

import tools.benchmark_corpus as benchmark_module
from app.scoring import _point_mesh_distances
from tools.benchmark_corpus import select_cases, strict_metrics


def _fake_bounded_worker(
    monkeypatch: pytest.MonkeyPatch,
    sources: dict[str, str],
) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(benchmark_module, "source_identity", lambda _: dict(sources))
    monkeypatch.setattr(benchmark_module, "runtime_identity", lambda: {"python": "test"})

    def run(command: list[str], **kwargs: object) -> None:
        calls.append(command)
        output = Path(command[command.index("--worker-output") + 1])
        output.write_text(
            json.dumps({"id": "part", "status": "complete", "valid_solid": True}),
            encoding="utf-8",
        )

    monkeypatch.setattr(benchmark_module.subprocess, "run", run)
    return calls


def test_select_cases_starts_strictly_after_frontier() -> None:
    cases = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]

    selected = select_cases(cases, start_after="b", limit=1)

    assert [case["id"] for case in selected] == ["c"]


def test_select_cases_rejects_unknown_frontier() -> None:
    with pytest.raises(ValueError, match="Start-after case ID not found: missing"):
        select_cases([{"id": "a"}], start_after="missing")


def test_bounded_resume_reruns_same_id_when_input_or_reference_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.stl"
    source.write_bytes(b"first mesh")
    reference = tmp_path / "source.step"
    reference.write_bytes(b"first reference")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    case = {"id": "part", "stl": "input.stl", "source_step": "source.step"}
    calls = _fake_bounded_worker(
        monkeypatch,
        {"app/reconstruction.py": "stable", "tools/benchmark_corpus.py": "stable"},
    )

    def run(*, resume: bool = True) -> dict[str, object]:
        return benchmark_module._run_bounded_case(
            case, manifest, tmp_path, 240, resume, None, set()
        )

    first = run()
    assert run() == first
    assert len(calls) == 1

    source.write_bytes(b"changed mesh")
    changed_input = run()
    assert len(calls) == 2
    assert changed_input["evaluation_fingerprint"] != first["evaluation_fingerprint"]

    reference.write_bytes(b"changed reference")
    run()
    assert len(calls) == 3

    run(resume=False)
    assert len(calls) == 4


def test_bounded_resume_reruns_same_id_when_relevant_code_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "input.stl").write_bytes(b"mesh")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    case = {"id": "part", "stl": "input.stl"}
    sources = {
        "app/reconstruction.py": "first implementation",
        "tools/benchmark_corpus.py": "stable",
    }
    calls = _fake_bounded_worker(monkeypatch, sources)

    def run() -> dict[str, object]:
        return benchmark_module._run_bounded_case(
            case, manifest, tmp_path, 240, True, None, set()
        )

    first = run()
    assert run() == first
    assert len(calls) == 1

    sources["app/reconstruction.py"] = "changed implementation"
    changed_code = run()
    assert len(calls) == 2
    assert changed_code["evaluation_fingerprint"] != first["evaluation_fingerprint"]


def test_exact_revolution_satisfies_cylindrical_surface_requirement() -> None:
    metrics = strict_metrics(
        {
            "status": "complete",
            "valid_solid": True,
            "feature_count": 1,
        },
        {
            "feature_count": 1,
            "brep_face_type_counts": {"PLANE": 2, "CYLINDER": 1},
        },
        {
            "output_face_type_counts": {"PLANE": 2, "REVOLUTION": 1},
            "output_edge_type_counts": {"LINE": 2, "CIRCLE": 2},
            "output_face_count": 3,
            "output_edge_count": 4,
        },
    )

    assert metrics["analytic_type_coverage"] == 1.0
    assert metrics["missing_analytic_surface_types"] == []
    assert metrics["strict_complete"] is True


def test_null_planar_bspline_metadata_is_treated_as_zero() -> None:
    metrics = strict_metrics(
        {
            "status": "complete",
            "valid_solid": True,
            "feature_count": 1,
        },
        {
            "feature_count": 1,
            "brep_face_type_counts": {"PLANE": 2, "CYLINDER": 1},
            "effectively_planar_bspline_count": None,
        },
        {
            "output_face_type_counts": {"PLANE": 2, "CYLINDER": 1},
            "output_edge_type_counts": {"LINE": 2, "CIRCLE": 2},
            "output_face_count": 3,
            "output_edge_count": 4,
        },
    )

    assert metrics["analytic_type_coverage"] == 1.0
    assert metrics["strict_complete"] is True


def test_accelerated_point_mesh_distance_matches_triangle_distance() -> None:
    mesh = trimesh.creation.box(extents=(4.0, 6.0, 8.0))
    points = np.asarray(
        [
            [0.0, 0.0, 5.0],
            [3.0, 0.0, 0.0],
            [0.0, -4.0, 0.0],
            [0.25, 0.5, 4.0],
        ],
        dtype=np.float64,
    )

    accelerated = _point_mesh_distances(mesh, points)
    reference = trimesh.proximity.closest_point(mesh, points)[1]

    assert accelerated == pytest.approx(reference, abs=1e-9)
