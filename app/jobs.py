from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from threading import Lock

from . import storage
from .schemas import PlanEditRequest, ReconstructionReport

CONVERSION_TIMEOUT_SECONDS = 3600
# The outer watchdog starts before the analytic worker. Allow time for
# startup and faceted recovery if analytic fitting uses its full budget.
SURFACE_RECOVERY_ALLOWANCE_SECONDS = 240

_ACTIVE_PROCESSES: dict[int, tuple[str | None, subprocess.Popen]] = {}
_ACTIVE_PROCESSES_LOCK = Lock()
_CANCELLED_JOBS: set[str] = set()
_SHUTDOWN = False


class JobCancelledError(RuntimeError):
    pass


def reset_job_system() -> None:
    global _SHUTDOWN
    with _ACTIVE_PROCESSES_LOCK:
        _SHUTDOWN = False
        _CANCELLED_JOBS.clear()


def allow_job(run_id: str) -> None:
    """Permit a newly admitted operation after its predecessor has drained."""

    with _ACTIVE_PROCESSES_LOCK:
        if _SHUTDOWN:
            raise JobCancelledError("The server is shutting down")
        _CANCELLED_JOBS.discard(run_id)


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def terminate_job_processes(run_id: str | None = None) -> None:
    """Terminate active workers and prevent late registration after cancellation."""

    global _SHUTDOWN
    with _ACTIVE_PROCESSES_LOCK:
        if run_id is None:
            _SHUTDOWN = True
        else:
            _CANCELLED_JOBS.add(run_id)
        processes = [
            process
            for owner, process in _ACTIVE_PROCESSES.values()
            if run_id is None or owner == run_id
        ]
    for process in processes:
        _terminate_process(process)


def run_bounded_process(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    stdout_path: Path,
    stderr_path: Path,
    env: dict[str, str] | None = None,
    job_id: str | None = None,
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
        with _ACTIVE_PROCESSES_LOCK:
            _ACTIVE_PROCESSES[process.pid] = (job_id, process)
            cancelled = job_id is not None and (
                _SHUTDOWN or job_id in _CANCELLED_JOBS
            )
        try:
            try:
                if cancelled:
                    raise JobCancelledError(job_id or "Job cancelled during shutdown")
                return process.wait(timeout=timeout)
            except BaseException:
                _terminate_process(process)
                process.wait()
                raise
        finally:
            with _ACTIVE_PROCESSES_LOCK:
                _ACTIVE_PROCESSES.pop(process.pid, None)


def _run_job(directory: Path, request: dict[str, object]) -> ReconstructionReport:
    request_path = directory / f".geometry-job-{uuid.uuid4().hex}.json"
    error_path = request_path.with_suffix(".error.json")
    request_path.write_text(json.dumps(request), encoding="utf-8")
    environment = {**os.environ, "STL_TO_STEP_RUNS_DIR": str(storage.RUNS)}
    timeout = CONVERSION_TIMEOUT_SECONDS
    if request.get("engine") == "surface_brep":
        timeout += SURFACE_RECOVERY_ALLOWANCE_SECONDS
    try:
        returncode = run_bounded_process(
            [sys.executable, "-m", "app.job_worker", str(request_path.resolve())],
            cwd=storage.ROOT,
            timeout=timeout,
            stdout_path=directory / "geometry-worker.stdout.log",
            stderr_path=directory / "geometry-worker.stderr.log",
            env=environment,
            job_id=str(request["run_id"]) if "run_id" in request else None,
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
            f"Geometry work exceeded the {timeout}-second total deadline"
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
