"""Run the surface engine inside a dedicated browser worker."""

import json
from collections.abc import Callable
from pathlib import Path

from .progress import apply_stage, new_progress, public_progress
from .surface_reconstruction import (
    _preserve_analytic_diagnostic,
    _reconstruct_faceted_only,
    _report_has_valid_solid,
    reconstruct_surfaces,
)


def convert(
    input_path: str,
    filename: str,
    units: str,
    run_id: str,
    send_progress: Callable[[str], None],
    recovery_reason: str = "",
) -> str:
    path = Path(input_path)
    progress = new_progress(run_id)

    def update(label: str) -> None:
        apply_stage(progress, label)
        # Browser artifacts become downloadable together at job completion.
        progress["preview_path"] = None
        send_progress(json.dumps(public_progress(progress)))

    pending_recovery = ""
    if recovery_reason:
        report = _reconstruct_faceted_only(run_id, path, filename, units, recovery_reason, update)
    else:
        report = reconstruct_surfaces(run_id, path, filename, units, update, isolate=False)
        if report.mesh.watertight and not _report_has_valid_solid(report):
            _preserve_analytic_diagnostic(path.parent)
            pending_recovery = "the analytic STEP did not pass the solid and geometry checks"
    return json.dumps({
        "report": json.loads(report.model_dump_json()),
        "pending_recovery": pending_recovery,
    })
