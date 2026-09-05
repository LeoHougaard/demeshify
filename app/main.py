from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Callable
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .editor import RevisionConflictError
from .jobs import edit_in_worker, reconstruct_in_worker
from .progress import apply_stage, new_progress, public_progress
from .schemas import PlanEditRequest, ReconstructionReport
from .storage import ROOT, load_report, run_dir

app = FastAPI(title="STL to STEP Converter", version="0.1.0")
LOGGER = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 250 * 1024 * 1024
MAX_PENDING_JOBS = 4
MAX_CACHED_PROGRESS = 100
RUN_ID_PATTERN = re.compile(r"(?:[a-f0-9]{12}|[a-f0-9]{32})")
DOWNLOADS = {
    "input.stl",
    "reconstruction.step",
    "reconstruction.stl",
    "reconstruction.py",
    "plan.json",
    "report.json",
    "surface_graph.json",
    "joined_surfaces.step",
}
EDIT_LOCKS: dict[str, asyncio.Lock] = {}
RUN_PROGRESS: dict[str, dict[str, object]] = {}
RUN_PROGRESS_LOCK = Lock()
RUN_TASKS: set[asyncio.Task[None]] = set()
RECONSTRUCTION_SLOT = asyncio.Semaphore(1)
ADMISSION_LOCK = asyncio.Lock()


def _valid_run_id(run_id: str) -> bool:
    return RUN_ID_PATTERN.fullmatch(run_id) is not None


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'"
    )
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def _download_name(run_id: str, file_name: str) -> str:
    """Give exported CAD files a recognizable source-derived name."""

    if file_name not in {"reconstruction.step", "joined_surfaces.step"}:
        return file_name
    try:
        source_name = load_report(run_id).mesh.file_name
    except (FileNotFoundError, ValueError):
        return file_name
    stem = Path(source_name).stem.strip() or "model"
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "model"
    suffix = "reconstructed" if file_name == "reconstruction.step" else "joined_surfaces"
    return f"{safe_stem}_{suffix}.step"


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "engines": ["surface_brep", "feature_tree"],
        "max_concurrent_jobs": 1,
        "max_pending_jobs": MAX_PENDING_JOBS,
        "deployment_mode": "local_single_user",
    }


def _reconstructor(engine: str):
    if engine in {"surface_brep", "feature_tree"}:
        return partial(reconstruct_in_worker, engine=engine)
    raise HTTPException(400, "Unsupported reconstruction engine")


async def _receive_upload(
    file: Annotated[UploadFile, File()],
    input_units: str,
) -> tuple[str, Path, str]:
    directory: Path | None = None
    target: Path | None = None
    try:
        if input_units not in {"mm", "cm", "m", "in"}:
            raise HTTPException(400, "Unsupported input units")
        safe_name = Path(file.filename or "input.stl").name
        if Path(safe_name).suffix.lower() != ".stl":
            raise HTTPException(400, "Upload an STL file")

        run_id = uuid.uuid4().hex
        directory = run_dir(run_id)
        target = directory / "input.stl"
        size = 0
        with target.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "The STL exceeds the 250 MB upload limit")
                handle.write(chunk)
        if size == 0:
            raise HTTPException(400, "The STL is empty")
        return run_id, target, safe_name
    except BaseException:
        if target is not None:
            target.unlink(missing_ok=True)
        if directory is not None:
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    finally:
        await file.close()


@app.post("/api/reconstruct", response_model=ReconstructionReport)
async def reconstruct_endpoint(
    file: Annotated[UploadFile, File()],
    input_units: Annotated[str, Form()] = "mm",
    engine: Annotated[str, Form()] = "surface_brep",
) -> ReconstructionReport:
    reconstructor = _reconstructor(engine)
    run_id, target, safe_name = await _receive_upload(file, input_units)
    try:
        async with RECONSTRUCTION_SLOT:
            return await asyncio.to_thread(
                reconstructor,
                run_id,
                target,
                safe_name,
                input_units,
            )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        LOGGER.exception("Synchronous reconstruction failed for run %s", run_id)
        raise HTTPException(
            500,
            "Reconstruction failed. Check the server log for details.",
        ) from exc


def _consume_worker_progress(
    path: Path,
    offset: int,
    update: Callable[[str], None],
) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(offset)
            for line in stream:
                label = line.strip()
                if label:
                    update(label)
            return stream.tell()
    except (FileNotFoundError, OSError):
        return offset


async def _run_reconstruction_job(
    run_id: str,
    target: Path,
    safe_name: str,
    input_units: str,
    engine: str,
) -> None:
    def update(label: str) -> None:
        with RUN_PROGRESS_LOCK:
            progress = RUN_PROGRESS.get(run_id)
            if progress is not None:
                apply_stage(progress, label)

    worker_progress_path = target.parent / "surface-worker-progress.log"
    try:
        async with RECONSTRUCTION_SLOT:
            with RUN_PROGRESS_LOCK:
                progress = RUN_PROGRESS[run_id]
                progress.update(
                    status="running",
                    stage="Starting reconstruction",
                    detail="Preparing the local geometry engine",
                )
            reconstruction_task = asyncio.create_task(
                asyncio.to_thread(
                    _reconstructor(engine),
                    run_id,
                    target,
                    safe_name,
                    input_units,
                    update,
                )
            )
            progress_offset = 0
            while not reconstruction_task.done():
                progress_offset = _consume_worker_progress(
                    worker_progress_path,
                    progress_offset,
                    update,
                )
                await asyncio.wait({reconstruction_task}, timeout=0.2)
            _consume_worker_progress(worker_progress_path, progress_offset, update)
            report = await reconstruction_task
        with RUN_PROGRESS_LOCK:
            progress = RUN_PROGRESS[run_id]
            if report.status == "failed":
                progress.update(
                    status="failed",
                    stage="Reconstruction stopped",
                    detail="No CAD model could be built from this mesh",
                    percent=min(float(progress["percent"]), 99.0),
                    preview_path=None,
                    error="Reconstruction failed. Review the verification report for details.",
                )
            else:
                feature_count = (
                    1 + len(report.plan.operations)
                    if report.plan is not None
                    else (
                        report.surface.recognized_surface_count
                        if report.surface is not None
                        else 0
                    )
                )
                unit_name = "features" if report.plan is not None else "surfaces"
                progress.update(
                    status="complete",
                    stage=(
                        "Editable model ready"
                        if report.plan is not None
                        else "Surface B-rep ready"
                    ),
                    detail=f"Completed with {feature_count} {unit_name}",
                    percent=100.0,
                    current_features=feature_count,
                    preview_path=(
                        "reconstruction.stl"
                        if (target.parent / "reconstruction.stl").is_file()
                        else None
                    ),
                )
    except Exception:
        LOGGER.exception("Background reconstruction failed for run %s", run_id)
        with RUN_PROGRESS_LOCK:
            progress = RUN_PROGRESS[run_id]
            progress.update(
                status="failed",
                stage="Reconstruction stopped",
                detail="The geometry engine could not complete this model",
                error="Reconstruction failed. Check the server log for details.",
            )
    finally:
        worker_progress_path.unlink(missing_ok=True)


@app.post("/api/reconstruct/start")
async def start_reconstruction_endpoint(
    file: Annotated[UploadFile, File()],
    input_units: Annotated[str, Form()] = "mm",
    engine: Annotated[str, Form()] = "surface_brep",
) -> dict[str, object]:
    _reconstructor(engine)
    async with ADMISSION_LOCK:
        if len(RUN_TASKS) >= MAX_PENDING_JOBS:
            await file.close()
            raise HTTPException(
                429,
                "The local reconstruction queue is full. Try again after a job finishes.",
                headers={"Retry-After": "10"},
            )
        run_id, target, safe_name = await _receive_upload(file, input_units)
        with RUN_PROGRESS_LOCK:
            RUN_PROGRESS[run_id] = new_progress(run_id)
        task = asyncio.create_task(
            _run_reconstruction_job(
                run_id,
                target,
                safe_name,
                input_units,
                engine,
            )
        )
        RUN_TASKS.add(task)

    def finish_task(completed_task: asyncio.Task[None]) -> None:
        RUN_TASKS.discard(completed_task)
        with RUN_PROGRESS_LOCK:
            terminal_ids = [
                item_id
                for item_id, item in RUN_PROGRESS.items()
                if item.get("status") in {"complete", "failed"}
            ]
            for old_id in terminal_ids[:-MAX_CACHED_PROGRESS]:
                RUN_PROGRESS.pop(old_id, None)
                EDIT_LOCKS.pop(old_id, None)

    task.add_done_callback(finish_task)
    with RUN_PROGRESS_LOCK:
        return public_progress(RUN_PROGRESS[run_id])


@app.get("/api/runs/{run_id}/progress")
def progress_endpoint(run_id: str) -> dict[str, object]:
    if not _valid_run_id(run_id):
        raise HTTPException(404, "Run not found")
    with RUN_PROGRESS_LOCK:
        progress = RUN_PROGRESS.get(run_id)
        if progress is not None:
            return public_progress(progress)
    try:
        report = load_report(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Run not found") from exc
    feature_count = (
        1 + len(report.plan.operations)
        if report.plan is not None
        else (
            report.surface.recognized_surface_count
            if report.surface is not None
            else 0
        )
    )
    unit_name = "features" if report.plan is not None else "surfaces"
    failed = report.status == "failed"
    preview_exists = (
        run_dir(run_id, create=False) / "reconstruction.stl"
    ).is_file()
    return {
        "id": run_id,
        "status": "failed" if failed else "complete",
        "stage": (
            "Reconstruction stopped"
            if failed
            else (
                "Editable model ready"
                if report.plan is not None
                else "Surface B-rep ready"
            )
        ),
        "detail": (
            "No CAD model was produced"
            if failed
            else f"Completed with {feature_count} {unit_name}"
        ),
        "percent": 99.0 if failed else 100.0,
        "candidates_done": report.score.candidate_count if report.score else 0,
        "candidates_total": report.score.candidate_count if report.score else 0,
        "current_features": feature_count,
        "refinements_done": 0,
        "preview_revision": 1 if preview_exists else 0,
        "preview_url": (
            f"/api/runs/{run_id}/live-preview?revision=1"
            if preview_exists
            else None
        ),
        "elapsed_seconds": report.elapsed_seconds,
        "seconds_since_update": 0.0,
        "error": (
            report.warnings[0]
            if failed and report.warnings
            else ("Reconstruction failed" if failed else None)
        ),
    }


@app.get("/api/runs/{run_id}/live-preview")
def live_preview_endpoint(run_id: str, revision: int = 0) -> FileResponse:
    del revision  # The query value is a browser cache key, not a file selector.
    if not _valid_run_id(run_id):
        raise HTTPException(404, "Run not found")
    with RUN_PROGRESS_LOCK:
        progress = RUN_PROGRESS.get(run_id)
        relative = progress.get("preview_path") if progress is not None else None
    if not isinstance(relative, str):
        completed = run_dir(run_id, create=False) / "reconstruction.stl"
        if not completed.is_file():
            raise HTTPException(404, "Live preview is not ready")
        relative = "reconstruction.stl"
    root = run_dir(run_id, create=False).resolve()
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
    if not _valid_run_id(run_id):
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
    if not _valid_run_id(run_id):
        raise HTTPException(404, "Run not found")
    lock = EDIT_LOCKS.setdefault(run_id, asyncio.Lock())
    async with lock:
        try:
            async with RECONSTRUCTION_SLOT:
                return await asyncio.to_thread(edit_in_worker, run_id, request)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Run not found") from exc
        except RevisionConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("Feature tree rebuild failed for run %s", run_id)
            raise HTTPException(
                500,
                "Feature tree rebuild failed. Check the server log for details.",
            ) from exc


@app.get("/api/runs/{run_id}/files/{file_name}")
def download_endpoint(run_id: str, file_name: str) -> FileResponse:
    if not _valid_run_id(run_id) or file_name not in DOWNLOADS:
        raise HTTPException(404, "File not found")
    path = run_dir(run_id, create=False) / file_name
    if not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=_download_name(run_id, file_name))


DIST = ROOT / "web" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="web")
else:

    @app.get("/", response_class=HTMLResponse)
    def missing_frontend() -> str:
        return "<h1>STL to STEP Converter</h1><p>Run npm install and npm run build in web/.</p>"
