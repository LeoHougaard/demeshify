from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

from . import storage
from .schemas import PlanEditRequest, ReconstructionReport

JOB_TIMEOUT_SECONDS = 240


def run_bounded_process(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    stdout_path: Path,
    stderr_path: Path,
    env: dict[str, str] | None = None,
) -> int:
    """Supervise native work and terminate its descendants when the deadline expires."""

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=stdout,
            stderr=stderr,
            creationflags=flags,
            start_new_session=sys.platform != "win32",
        )
        try:
            return process.wait(timeout=timeout)
        except BaseException:
            if sys.platform == "win32":
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=flags,
                        timeout=10,
                        check=False,
                    )
                finally:
                    if process.poll() is None:
                        process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            raise


def _run_job(directory: Path, request: dict[str, object]) -> ReconstructionReport:
    request_path = directory / f".geometry-job-{uuid.uuid4().hex}.json"
    error_path = request_path.with_suffix(".error.json")
    request_path.write_text(json.dumps(request), encoding="utf-8")
    environment = {**os.environ, "STL_TO_STEP_RUNS_DIR": str(storage.RUNS)}
    try:
        returncode = run_bounded_process(
            [sys.executable, "-m", "app.job_worker", str(request_path.resolve())],
            cwd=storage.ROOT,
            timeout=JOB_TIMEOUT_SECONDS,
            stdout_path=directory / "geometry-worker.stdout.log",
            stderr_path=directory / "geometry-worker.stderr.log",
            env=environment,
        )
        if returncode:
            if error_path.is_file():
                error = json.loads(error_path.read_text(encoding="utf-8"))
                if error["type"] == "RevisionConflictError":
                    from .editor import RevisionConflictError

                    raise RevisionConflictError(error["message"])
                if error["type"] == "FileNotFoundError":
                    raise FileNotFoundError(error["message"])
                raise ValueError(error["message"])
            raise RuntimeError(f"Geometry worker terminated with exit code {returncode}")
        return ReconstructionReport.model_validate_json(
            (directory / "report.json").read_text(encoding="utf-8")
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"Geometry work exceeded the {JOB_TIMEOUT_SECONDS}-second total deadline"
        ) from exc
    finally:
        request_path.unlink(missing_ok=True)
        error_path.unlink(missing_ok=True)


def reconstruct_in_worker(
    run_id: str,
    stl_path: Path,
    original_name: str,
    input_units: str = "mm",
    progress_callback=None,
    *,
    engine: str,
) -> ReconstructionReport:
    report = _run_job(
        stl_path.parent,
        {
            "mode": "reconstruct",
            "engine": engine,
            "run_id": run_id,
            "stl_path": str(stl_path.resolve()),
            "original_name": original_name,
            "input_units": input_units,
        },
    )
    if progress_callback:
        progress_callback("geometry_worker_done")
    return report


def edit_in_worker(run_id: str, request: PlanEditRequest) -> ReconstructionReport:
    directory = storage.run_dir(run_id, create=False)
    if not (directory / "report.json").is_file():
        raise FileNotFoundError(run_id)
    return _run_job(
        directory,
        {"mode": "edit", "run_id": run_id, "edit": request.model_dump(mode="json")},
    )
