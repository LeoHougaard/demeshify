from __future__ import annotations

import numpy as np
import pytest
import trimesh

from app.scoring import _point_mesh_distances
from tools.benchmark_corpus import select_cases, strict_metrics


def test_select_cases_starts_strictly_after_frontier() -> None:
    cases = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]

    selected = select_cases(cases, start_after="b", limit=1)

    assert [case["id"] for case in selected] == ["c"]


def test_select_cases_rejects_unknown_frontier() -> None:
    with pytest.raises(ValueError, match="Start-after case ID not found: missing"):
        select_cases([{"id": "a"}], start_after="missing")


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
