from pathlib import Path

from tools.benchmark_surface_corpus import _write_output, benchmark_summary


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
