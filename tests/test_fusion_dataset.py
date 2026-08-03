from __future__ import annotations

import json
from pathlib import Path

from tools.benchmark_corpus import multiset_precision_recall, refresh_result_metrics
from tools.profile_fusion_dataset import metadata_record, stratified_sample, summarize


def test_profiles_operation_family_and_complexity(tmp_path: Path) -> None:
    metadata = {
        "metadata": {"component_name": "Bracket"},
        "body": {
            "properties": {
                "face_count": 24,
                "edge_count": 48,
                "shell_count": 1,
            },
            "faces": [],
        },
        "features": {
            "base": {
                "type": "ExtrudeFeature",
                "operation": "NewBodyFeatureOperation",
            },
            "pocket": {
                "type": "ExtrudeFeature",
                "operation": "CutFeatureOperation",
            },
            "rounds": {"type": "FilletFeature"},
        },
    }
    path = tmp_path / "part.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")

    record = metadata_record(path, "test")

    assert record["family"] == "fillet"
    assert record["complexity"] == "medium"
    assert record["feature_type_counts"]["ExtrudeFeature"] == 2
    assert record["operation_counts"]["CutFeatureOperation"] == 1


def test_stratified_sample_is_repeatable_and_spans_families() -> None:
    records = [
        {
            "id": f"part-{index}",
            "split": "test",
            "family": "fillet" if index % 2 else "multi_extrude",
            "complexity": "simple" if index % 3 else "complex",
            "feature_type_counts": {"ExtrudeFeature": 1},
            "operation_counts": {},
        }
        for index in range(20)
    ]

    first = stratified_sample(records, 8, "test", 12)
    second = stratified_sample(records, 8, "test", 12)

    assert [item["id"] for item in first] == [item["id"] for item in second]
    assert {item["family"] for item in first} == {"fillet", "multi_extrude"}
    assert summarize(records)["model_count"] == 20


def test_surface_histogram_metric_penalizes_missing_curves() -> None:
    precision, recall, f1 = multiset_precision_recall(
        {"PLANE": 6, "CYLINDER": 2, "TORUS": 4},
        {"PLANE": 6, "CYLINDER": 1},
    )

    assert precision == 1.0
    assert recall == 7 / 12
    assert f1 < 0.75


def test_manual_scope_override_is_explicitly_honored() -> None:
    result = refresh_result_metrics(
        {
            "id": "decorative-spline",
            "status": "complete",
            "family": "fillet_chamfer",
            "ground_truth": {
                "scope_override": "out_of_scope",
                "scope_override_reason": "Dense decorative spline relief.",
            },
        }
    )

    assert result["scope_status"] == "out_of_scope"
    assert result["scope_reason"] == "Dense decorative spline relief."


def test_strict_metric_rejects_feature_explosion() -> None:
    result = refresh_result_metrics(
        {
            "id": "slabbed-part",
            "status": "complete",
            "valid_solid": True,
            "feature_count": 25,
            "family": "fillet",
            "output_face_type_counts": {"PLANE": 8, "CYLINDER": 2},
            "output_edge_type_counts": {"LINE": 16, "CIRCLE": 4},
            "output_face_count": 10,
            "output_edge_count": 20,
            "ground_truth": {
                "feature_count": 4,
                "brep_face_type_counts": {"PLANE": 8, "CYLINDER": 2},
            },
        }
    )

    assert result["feature_count_ratio"] == 6.25
    assert result["feature_count_limit"] == 12
    assert result["feature_count_overage"] == 13
    assert result["feature_count_clean"] is False
    assert result["strict_complete"] is False
