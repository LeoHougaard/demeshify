from __future__ import annotations

import json
from pathlib import Path

from .schemas import ReconstructionReport

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
RUNS.mkdir(exist_ok=True)


def run_dir(run_id: str) -> Path:
    path = RUNS / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_report(report: ReconstructionReport) -> None:
    path = run_dir(report.id) / "report.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")


def load_report(run_id: str) -> ReconstructionReport:
    data = json.loads((run_dir(run_id) / "report.json").read_text(encoding="utf-8"))
    return ReconstructionReport.model_validate(data)
