from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path


def main() -> int:
    started = time.perf_counter()
    request_path = Path(sys.argv[1])
    try:
        from .editor import apply_plan_edit
        from .reconstruction import reconstruct
        from .schemas import PlanEditRequest
        from .storage import atomic_write_text
        from .surface_reconstruction import reconstruct_surfaces

        request = json.loads(request_path.read_text(encoding="utf-8"))

        def update(label: str) -> None:
            with (request_path.parent / "surface-worker-progress.log").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(label.replace("\n", " ") + "\n")

        if request["mode"] == "edit":
            report = apply_plan_edit(
                request["run_id"], PlanEditRequest.model_validate(request["edit"])
            )
        else:
            operation = (
                reconstruct_surfaces if request["engine"] == "surface_brep" else reconstruct
            )
            report = operation(
                request["run_id"], Path(request["stl_path"]), request["original_name"],
                request["input_units"], update,
            )
        report.elapsed_seconds = time.perf_counter() - started
        atomic_write_text(request_path.parent / "report.json", report.model_dump_json(indent=2))
        return 0
    except Exception as exc:
        request_path.with_suffix(".error.json").write_text(
            json.dumps({"type": type(exc).__name__, "message": str(exc)}), encoding="utf-8"
        )
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
