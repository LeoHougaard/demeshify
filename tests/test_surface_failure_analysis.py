from __future__ import annotations

import json

from tools.analyze_surface_failures import analyze, classify_failure, markdown_report


def test_classify_topology_and_deviation_failure() -> None:
    categories = classify_failure(
        {
            "status": "best_effort",
            "closed": False,
            "free_edge_count": 0,
            "valid_brep": False,
            "valid_solid": False,
            "step_roundtrip_valid": False,
            "p95_mm": 0.2,
            "p95_limit_mm": 0.12,
            "warnings": ["Analytic trimming failed for 1 torus patch"],
        }
    )

    assert "edge_closed_invalid_shell" in categories
    assert "step_roundtrip_invalid" in categories
    assert "surface_deviation" in categories
    assert "analytic_trim_failure" in categories


def test_analyzer_records_each_failed_case(tmp_path) -> None:
    source = tmp_path / "benchmark.json"
    source.write_text(
        json.dumps(
            {
                "results": [
                    {"id": "ok", "status": "complete", "accepted": True},
                    {
                        "id": "open",
                        "status": "best_effort",
                        "accepted": False,
                        "free_edge_count": 3,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = analyze([source])

    assert result["failure_count"] == 1
    assert result["failures"][0]["id"] == "open"
    assert result["category_counts"]["open_boundaries"] == 1
    assert "Individual failures" in markdown_report(result)
