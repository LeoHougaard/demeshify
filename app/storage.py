from __future__ import annotations

import json
import os
from pathlib import Path

from .schemas import ReconstructionReport

ROOT = Path(__file__).resolve().parents[1]
RUNS = Path(os.environ.get("STL_TO_STEP_RUNS_DIR", ROOT / "runs")).expanduser().resolve()
RUNS.mkdir(parents=True, exist_ok=True)


def run_dir(run_id: str, *, create: bool = True) -> Path:
    path = RUNS / run_id
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, content: str) -> None:
    """Replace a text artifact only after its full contents reach disk."""

    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def save_report(report: ReconstructionReport) -> None:
    path = run_dir(report.id) / "report.json"
    atomic_write_text(path, report.model_dump_json(indent=2))


def load_report(run_id: str) -> ReconstructionReport:
    data = json.loads(
        (run_dir(run_id, create=False) / "report.json").read_text(encoding="utf-8")
    )
    return ReconstructionReport.model_validate(data)
