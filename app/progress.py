from __future__ import annotations

import re
import time
from typing import Any

FINAL_REFINEMENT_COUNT = 13


def new_progress(run_id: str) -> dict[str, Any]:
    return {
        "id": run_id,
        "status": "queued",
        "stage": "Uploading mesh",
        "detail": "Saving the STL locally",
        "percent": 2.0,
        "candidates_done": 0,
        "candidates_total": 0,
        "current_features": 0,
        "refinements_done": 0,
        "preview_revision": 0,
        "preview_path": None,
        "started_at": time.monotonic(),
        "updated_at": time.monotonic(),
        "error": None,
    }


def public_progress(progress: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value
        for key, value in progress.items()
        if key not in {"started_at", "updated_at", "generated_steps", "preview_path"}
    }
    result["elapsed_seconds"] = max(
        0.0,
        time.monotonic() - float(progress["started_at"]),
    )
    result["seconds_since_update"] = max(
        0.0,
        time.monotonic() - float(progress["updated_at"]),
    )
    result["preview_url"] = (
        f"/api/runs/{progress['id']}/live-preview?revision="
        f"{progress['preview_revision']}"
        if progress.get("preview_path")
        else None
    )
    return result


def _number(label: str, name: str) -> int | None:
    match = re.search(rf"(?:^|\s){re.escape(name)}=(\d+)", label)
    return int(match.group(1)) if match else None


def apply_stage(progress: dict[str, Any], label: str) -> None:
    """Translate reconstruction trace events into stable user-facing progress."""

    progress["status"] = "running"
    progress["updated_at"] = time.monotonic()
    operations = _number(label, "operations")
    if operations is not None:
        progress["current_features"] = operations + 1
    preview_match = re.search(r"(?:^|\s)preview=([^\s]+)", label)
    if preview_match:
        preview_path = preview_match.group(1)
        if preview_path != progress.get("preview_path"):
            progress["preview_path"] = preview_path
            progress["preview_revision"] = int(progress["preview_revision"]) + 1

    if label == "reconstruction_started":
        progress.update(
            stage="Starting reconstruction",
            detail="Preparing the local geometry engine",
            percent=max(progress["percent"], 5.0),
        )
    elif label.startswith("mesh_loaded"):
        triangles = _number(label, "triangles") or 0
        progress.update(
            stage="Inspecting mesh topology",
            detail=f"Loaded {triangles:,} triangles",
            percent=max(progress["percent"], 10.0),
        )
    elif label.endswith("_generated") or "_generated count=" in label:
        count = _number(label, "count") or 0
        generated_steps = int(progress.get("generated_steps", 0)) + 1
        progress["generated_steps"] = generated_steps
        family = label.split("_generated", 1)[0].replace("_", " ")
        progress.update(
            stage="Finding editable features",
            detail=f"{family.title()}: {count} candidate plans",
            percent=max(progress["percent"], min(22.0, 10.0 + generated_steps * 1.7)),
        )
    elif label.startswith("initial_shortlist_ready"):
        total = _number(label, "count") or 0
        progress.update(
            stage="Candidate feature tree ready",
            detail=f"Testing {total} candidate constructions",
            percent=max(progress["percent"], 25.0),
            candidates_total=total,
        )
    elif label.startswith("initial_score_start"):
        index = _number(label, "index") or 0
        total = max(int(progress["candidates_total"]), index + 1)
        base_match = re.search(r"(?:^|\s)base=([^\s]+)", label)
        base = base_match.group(1).replace("_", " ") if base_match else "CAD"
        progress.update(
            stage="Testing CAD candidates",
            detail=f"Building {base} candidate {index + 1} of {total}",
            percent=max(progress["percent"], 25.0 + 28.0 * index / max(total, 1)),
        )
    elif label.startswith(("initial_score_done", "initial_score_failed")):
        index = _number(label, "index") or 0
        done = max(int(progress["candidates_done"]), index + 1)
        total = max(int(progress["candidates_total"]), done)
        progress.update(
            candidates_done=done,
            percent=max(progress["percent"], 25.0 + 28.0 * done / max(total, 1)),
        )
    elif label.startswith("initial_scoring_complete"):
        progress.update(
            stage="Selecting the closest construction",
            detail="Comparing geometric fit and editable feature count",
            percent=max(progress["percent"], 55.0),
        )
    elif "torus" in label:
        progress.update(
            stage="Preserving tangent rounds",
            detail="Checking analytic fillets and curved transitions",
            percent=max(progress["percent"], 61.0),
        )
    elif "cone" in label:
        progress.update(
            stage="Preserving tapered features",
            detail="Checking chamfers, countersinks, and conical faces",
            percent=max(progress["percent"], 65.0),
        )
    elif label.startswith("weighted_generation"):
        progress.update(
            stage="Recovering changing profiles",
            detail="Placing layers where the cross-section changes",
            percent=max(progress["percent"], 68.0),
        )
    elif label.startswith("weighted_score_start"):
        index = _number(label, "index") or 0
        progress.update(
            stage="Testing detailed curved candidates",
            detail=f"Candidate {index + 1} · {progress['current_features']} features",
            percent=max(progress["percent"], min(77.0, 69.0 + index * 0.7)),
        )
    elif label.startswith("final_refinement_start"):
        done = int(progress["refinements_done"])
        name_match = re.search(r"(?:^|\s)name=([^\s]+)", label)
        name = (
            name_match.group(1).replace("_", " ")
            if name_match
            else "feature tree"
        )
        progress.update(
            stage="Finalizing the feature tree",
            detail=f"{name.title()} · pass {done + 1} of {FINAL_REFINEMENT_COUNT}",
            percent=max(
                progress["percent"],
                78.0 + 15.0 * done / FINAL_REFINEMENT_COUNT,
            ),
        )
    elif label.startswith("final_refinement_done"):
        done = min(FINAL_REFINEMENT_COUNT, int(progress["refinements_done"]) + 1)
        progress.update(
            refinements_done=done,
            percent=max(
                progress["percent"],
                78.0 + 15.0 * done / FINAL_REFINEMENT_COUNT,
            ),
        )
    elif label.startswith("export_start"):
        progress.update(
            stage="Exporting editable CAD",
            detail=f"Writing STEP and CadQuery · {progress['current_features']} features",
            percent=max(progress["percent"], 96.0),
        )
    elif label.startswith("reconstruction_complete"):
        progress.update(
            status="complete",
            stage="Editable model ready",
            detail=f"Completed with {progress['current_features']} features",
            percent=100.0,
        )
