from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from threading import Lock
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .ai import is_configured
from .editor import RevisionConflictError, apply_plan_edit
from .progress import apply_stage, new_progress, public_progress
from .reconstruction import reconstruct
from .schemas import PlanEditRequest, ReconstructionReport
from .storage import ROOT, load_report, run_dir

app = FastAPI(title="MeshMind CAD", version="0.1.0")
MAX_UPLOAD_BYTES = 250 * 1024 * 1024
DOWNLOADS = {
    "input.stl",
    "reconstruction.step",
    "reconstruction.stl",
    "reconstruction.py",
    "plan.json",
    "report.json",
}
EDIT_LOCKS: dict[str, asyncio.Lock] = {}
RUN_PROGRESS: dict[str, dict[str, object]] = {}
RUN_PROGRESS_LOCK = Lock()
RUN_TASKS: set[asyncio.Task[None]] = set()


@app.get("/api/health")
def health() -> dict[str, object]:
    return {"status": "ok", "ai_configured": is_configured()}


async def _receive_upload(
    file: Annotated[UploadFile, File()],
    input_units: str,
) -> tuple[str, Path, str]:
    if input_units not in {"mm", "cm", "m", "in"}:
        raise HTTPException(400, "Unsupported input units")
    safe_name = Path(file.filename or "input.stl").name
    if Path(safe_name).suffix.lower() != ".stl":
        raise HTTPException(400, "Upload an STL file")

    run_id = uuid.uuid4().hex[:12]
    directory = run_dir(run_id)
    target = directory / "input.stl"
    size = 0
    with target.open("wb") as handle:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                target.unlink(missing_ok=True)
                raise HTTPException(413, "The STL exceeds the 250 MB upload limit")
            handle.write(chunk)

    return run_id, target, safe_name


@app.post("/api/reconstruct", response_model=ReconstructionReport)
async def reconstruct_endpoint(
    file: Annotated[UploadFile, File()],
    input_units: Annotated[str, Form()] = "mm",
    prompt: Annotated[str, Form()] = "",
) -> ReconstructionReport:
    run_id, target, safe_name = await _receive_upload(file, input_units)
    try:
        return await asyncio.to_thread(
            reconstruct,
            run_id,
            target,
            safe_name,
            input_units,
            prompt[:4000],
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"Reconstruction failed: {exc}") from exc


async def _run_reconstruction_job(
    run_id: str,
    target: Path,
    safe_name: str,
    input_units: str,
    prompt: str,
) -> None:
    def update(label: str) -> None:
        with RUN_PROGRESS_LOCK:
            progress = RUN_PROGRESS.get(run_id)
            if progress is not None:
                apply_stage(progress, label)

    try:
        report = await asyncio.to_thread(
            reconstruct,
            run_id,
            target,
            safe_name,
            input_units,
            prompt[:4000],
            update,
        )
        with RUN_PROGRESS_LOCK:
            progress = RUN_PROGRESS[run_id]
            progress.update(
                status="complete",
                stage="Editable model ready",
                detail=(
                    f"Completed with {1 + len(report.plan.operations)} features"
                    if report.plan is not None
                    else "Reconstruction finished"
                ),
                percent=100.0,
                current_features=(
                    1 + len(report.plan.operations) if report.plan is not None else 0
                ),
                preview_path=(
                    "reconstruction.stl" if report.plan is not None else None
                ),
            )
    except Exception as exc:
        with RUN_PROGRESS_LOCK:
            progress = RUN_PROGRESS[run_id]
            progress.update(
                status="failed",
                stage="Reconstruction stopped",
                detail="The geometry engine could not complete this model",
                error=str(exc),
            )


@app.post("/api/reconstruct/start")
async def start_reconstruction_endpoint(
    file: Annotated[UploadFile, File()],
    input_units: Annotated[str, Form()] = "mm",
    prompt: Annotated[str, Form()] = "",
) -> dict[str, object]:
    run_id, target, safe_name = await _receive_upload(file, input_units)
    with RUN_PROGRESS_LOCK:
        RUN_PROGRESS[run_id] = new_progress(run_id)
    task = asyncio.create_task(
        _run_reconstruction_job(
            run_id,
            target,
            safe_name,
            input_units,
            prompt,
        )
    )
    RUN_TASKS.add(task)
    task.add_done_callback(RUN_TASKS.discard)
    with RUN_PROGRESS_LOCK:
        return public_progress(RUN_PROGRESS[run_id])


@app.get("/api/runs/{run_id}/progress")
def progress_endpoint(run_id: str) -> dict[str, object]:
    if not re.fullmatch(r"[a-f0-9]{12}", run_id):
        raise HTTPException(404, "Run not found")
    with RUN_PROGRESS_LOCK:
        progress = RUN_PROGRESS.get(run_id)
        if progress is not None:
            return public_progress(progress)
    try:
        report = load_report(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Run not found") from exc
    feature_count = 1 + len(report.plan.operations) if report.plan is not None else 0
    return {
        "id": run_id,
        "status": "complete",
        "stage": "Editable model ready",
        "detail": f"Completed with {feature_count} features",
        "percent": 100.0,
        "candidates_done": report.score.candidate_count if report.score else 0,
        "candidates_total": report.score.candidate_count if report.score else 0,
        "current_features": feature_count,
        "refinements_done": 0,
        "preview_revision": 1,
        "preview_url": f"/api/runs/{run_id}/live-preview?revision=1",
        "elapsed_seconds": report.elapsed_seconds,
        "seconds_since_update": 0.0,
        "error": None,
    }


@app.get("/api/runs/{run_id}/live-preview")
def live_preview_endpoint(run_id: str, revision: int = 0) -> FileResponse:
    del revision  # The query value is a browser cache key, not a file selector.
    if not re.fullmatch(r"[a-f0-9]{12}", run_id):
        raise HTTPException(404, "Run not found")
    with RUN_PROGRESS_LOCK:
        progress = RUN_PROGRESS.get(run_id)
        relative = progress.get("preview_path") if progress is not None else None
    if not isinstance(relative, str):
        completed = run_dir(run_id) / "reconstruction.stl"
        if not completed.is_file():
            raise HTTPException(404, "Live preview is not ready")
        relative = "reconstruction.stl"
    root = run_dir(run_id).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.suffix != ".stl":
        raise HTTPException(404, "Live preview is not ready")
    return FileResponse(
        path,
        media_type="model/stl",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/api/runs/{run_id}", response_model=ReconstructionReport)
def report_endpoint(run_id: str) -> ReconstructionReport:
    if not re.fullmatch(r"[a-f0-9]{12}", run_id):
        raise HTTPException(404, "Run not found")
    try:
        return load_report(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Run not found") from exc


@app.put("/api/runs/{run_id}/plan", response_model=ReconstructionReport)
async def edit_plan_endpoint(
    run_id: str,
    request: PlanEditRequest,
) -> ReconstructionReport:
    if not re.fullmatch(r"[a-f0-9]{12}", run_id):
        raise HTTPException(404, "Run not found")
    lock = EDIT_LOCKS.setdefault(run_id, asyncio.Lock())
    async with lock:
        try:
            return await asyncio.to_thread(apply_plan_edit, run_id, request)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Run not found") from exc
        except RevisionConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(500, f"Feature tree rebuild failed: {exc}") from exc


@app.get("/api/runs/{run_id}/files/{file_name}")
def download_endpoint(run_id: str, file_name: str) -> FileResponse:
    if not re.fullmatch(r"[a-f0-9]{12}", run_id) or file_name not in DOWNLOADS:
        raise HTTPException(404, "File not found")
    path = run_dir(run_id) / file_name
    if not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=file_name)


DIST = ROOT / "web" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="web")
else:

    @app.get("/", response_class=HTMLResponse)
    def missing_frontend() -> str:
        return "<h1>MeshMind CAD</h1><p>Run npm install and npm run build in web/.</p>"
