from pathlib import Path
from types import SimpleNamespace

from tools.benchmark_surface_corpus import (
    _write_output,
    analytic_quality_metrics,
    benchmark_summary,
    case_has_analytic_curves,
    is_clean_analytic_reconstruction,
)


def test_surface_benchmark_summary_tracks_watertight_step_acceptance() -> None:
    summary = benchmark_summary(
        [
            {
                "status": "complete",
                "closed": True,
                "valid_brep": True,
                "step_roundtrip_valid": True,
                "accepted": True,
                "free_edge_count": 0,
            },
            {
                "status": "best_effort",
                "closed": False,
                "valid_brep": True,
                "step_roundtrip_valid": True,
                "accepted": False,
                "free_edge_count": 7,
            },
        ]
    )

    assert summary["case_count"] == 2
    assert summary["watertight_rate"] == 0.5
    assert summary["accepted_rate"] == 0.5
    assert summary["total_free_edges"] == 7


def test_faceted_geometric_match_is_not_an_analytic_success() -> None:
    summary = benchmark_summary(
        [
            {
                "status": "complete",
                "closed": True,
                "valid_brep": True,
                "step_roundtrip_valid": True,
                "geometric_accepted": True,
                "clean_analytic": False,
                "accepted": False,
                "faceted_fallback": True,
                "free_edge_count": 0,
            }
        ]
    )

    assert summary["geometric_accepted_rate"] == 1.0
    assert summary["clean_analytic_rate"] == 0.0
    assert summary["accepted_rate"] == 0.0


def test_curved_case_selection_uses_ground_truth_surface_types() -> None:
    assert case_has_analytic_curves(
        {"ground_truth": {"brep_face_type_counts": {"PLANE": 4, "CYLINDER": 1}}}
    )
    assert not case_has_analytic_curves(
        {"ground_truth": {"brep_face_type_counts": {"PLANE": 6}}}
    )


def test_analytic_area_gate_detects_missing_cylindrical_area() -> None:
    metrics = analytic_quality_metrics(
        {
            "face_areas_mm2": {"PLANE": 20.0, "CYLINDER": 10.0},
            "edge_lengths_mm": {"CIRCLE": 12.0},
        },
        {
            "face_areas_mm2": {"PLANE": 30.0, "CYLINDER": 4.0},
            "edge_lengths_mm": {"CIRCLE": 5.0},
        },
    )

    assert metrics["missing_analytic_surface_types"] == []
    assert metrics["minimum_analytic_area_recall"] == 0.4
    assert metrics["circular_edge_length_recall"] == 5 / 12


def test_analytic_gate_rejects_unexpected_bspline_faces() -> None:
    metrics = analytic_quality_metrics(
        {
            "face_counts": {"PLANE": 2, "CYLINDER": 1},
            "face_areas_mm2": {"PLANE": 20.0, "CYLINDER": 10.0},
            "edge_lengths_mm": {},
        },
        {
            "face_counts": {"PLANE": 2, "CYLINDER": 1, "BSPLINE": 1},
            "face_areas_mm2": {
                "PLANE": 20.0,
                "CYLINDER": 10.0,
                "BSPLINE": 2.0,
            },
            "edge_lengths_mm": {},
        },
    )

    assert metrics["unexpected_nonanalytic_surface_types"] == ["BSPLINE"]


def test_analytic_gate_allows_bspline_when_source_is_nonanalytic() -> None:
    metrics = analytic_quality_metrics(
        {
            "face_counts": {"PLANE": 2, "BSPLINE": 1},
            "face_areas_mm2": {"PLANE": 20.0, "BSPLINE": 2.0},
            "edge_lengths_mm": {},
        },
        {
            "face_counts": {"PLANE": 2, "BSPLINE": 1},
            "face_areas_mm2": {"PLANE": 20.0, "BSPLINE": 2.0},
            "edge_lengths_mm": {},
        },
    )

    assert metrics["unexpected_nonanalytic_surface_types"] == []


def test_analytic_gate_allows_exact_swept_output_surfaces() -> None:
    metrics = analytic_quality_metrics(
        {
            "face_counts": {"PLANE": 2, "CONE": 1},
            "face_areas_mm2": {"PLANE": 20.0, "CONE": 10.0},
            "edge_lengths_mm": {},
        },
        {
            "face_counts": {"PLANE": 2, "CONE": 1, "EXTRUSION": 1},
            "face_areas_mm2": {
                "PLANE": 20.0,
                "CONE": 10.0,
                "EXTRUSION": 2.0,
            },
            "edge_lengths_mm": {},
        },
    )

    assert metrics["unexpected_nonanalytic_surface_types"] == []


def test_clean_analytic_gate_rejects_c0_and_faceted_fallbacks() -> None:
    metrics = {
        "minimum_analytic_area_recall": 1.0,
        "minimum_analytic_area_precision": 1.0,
        "missing_analytic_surface_types": [],
        "unexpected_nonanalytic_surface_types": [],
    }
    surface = SimpleNamespace(
        faceted_fallback=False,
        source_mesh_fallback=False,
        faceted_patch_count=0,
        faceted_face_count=0,
    )

    assert is_clean_analytic_reconstruction(surface, metrics, [])
    assert not is_clean_analytic_reconstruction(
        surface,
        metrics,
        ["Filled 1 closed residual boundary loop with C0 surfaces."],
    )
    surface.faceted_patch_count = 1
    assert not is_clean_analytic_reconstruction(surface, metrics, [])


def test_surface_benchmark_writes_failure_ledger(tmp_path: Path) -> None:
    output = tmp_path / "surface.json"

    _write_output(
        output,
        [
            {
                "id": "failed-case",
                "status": "best_effort",
                "accepted": False,
                "closed": False,
                "free_edge_count": 2,
            }
        ],
    )

    assert output.is_file()
    assert (tmp_path / "surface_failures.json").is_file()
    assert (tmp_path / "surface_failures.md").is_file()
