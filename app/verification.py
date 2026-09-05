from __future__ import annotations

import math

from .schemas import ScoreReport


def acceptance_threshold(diagonal: float) -> float:
    return max(0.12, diagonal * 0.003)


def passes_geometry_gate(score: ScoreReport, threshold: float) -> bool:
    """Accept only geometry measured from the delivered STEP, including small regions."""

    values = (score.chamfer_p95_mm, score.chamfer_max_mm, score.volume_error_percent)
    return bool(
        score.step_geometry_verified
        and score.valid_brep
        and score.valid_solid
        and score.volume_comparable
        and score.component_count_match is True
        and all(math.isfinite(value) for value in values)
        and score.local_max_mm is not None
        and math.isfinite(score.local_max_mm)
        and score.chamfer_p95_mm <= threshold
        and score.chamfer_max_mm <= threshold
        and score.local_max_mm <= threshold
        and score.volume_error_percent <= 2.0
    )


def verification_warnings(score: ScoreReport, threshold: float) -> list[str]:
    warnings = []
    if score.component_count_match is False:
        warnings.append(
            "Verification: the input has "
            f"{score.source_component_count} connected surface components, but the STEP "
            f"contains {score.step_shell_count} shells."
        )
    maximum = max(score.chamfer_max_mm, score.local_max_mm or 0.0)
    if maximum > threshold:
        warnings.append(
            f"Verification: sampled local deviation {maximum:.6g} mm exceeds "
            f"the {threshold:.6g} mm limit."
        )
    if not score.volume_comparable:
        warnings.append("Verification: closed-volume comparison is unavailable.")
    elif score.volume_error_percent > 2.0:
        warnings.append(
            f"Verification: volume error {score.volume_error_percent:.6g}% exceeds 2%."
        )
    return warnings
