from __future__ import annotations

from app.progress import apply_stage, new_progress, public_progress


def test_reconstruction_events_report_real_candidate_and_feature_counts() -> None:
    progress = new_progress("abc123abc123")
    events = (
        "reconstruction_started",
        "mesh_loaded triangles=4200 watertight=True",
        "prismatic_generated count=3",
        "initial_shortlist_ready count=4",
        "initial_score_start index=0 base=extrude operations=5",
        "initial_score_done index=0 p95=0.02 valid=True "
        "preview=candidates/00/reconstruction.stl",
        "final_refinement_start name=preserve_detected_cylinders operations=6",
        "final_refinement_done name=preserve_detected_cylinders operations=7",
        "export_start operations=7",
        "reconstruction_complete operations=7",
    )
    percentages = []
    for event in events:
        apply_stage(progress, event)
        percentages.append(progress["percent"])

    public = public_progress(progress)
    assert percentages == sorted(percentages)
    assert public["status"] == "complete"
    assert public["percent"] == 100.0
    assert public["candidates_done"] == 1
    assert public["candidates_total"] == 4
    assert public["current_features"] == 8
    assert public["preview_revision"] == 1
    assert public["preview_url"].endswith("/live-preview?revision=1")
    assert public["elapsed_seconds"] >= 0


def test_slow_stage_exposes_time_since_the_last_real_event() -> None:
    progress = new_progress("abc123abc123")
    apply_stage(progress, "weighted_generation_start axis=Y layers=18")

    public = public_progress(progress)

    assert public["stage"] == "Recovering changing profiles"
    assert public["seconds_since_update"] >= 0
