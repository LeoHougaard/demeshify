import asyncio
import subprocess
import sys
import time

import pytest
import trimesh
from fastapi.testclient import TestClient

from app import jobs, storage
from app import main as main_module
from app.jobs import run_bounded_process
from app.main import app


def test_job_deadline_terminates_descendant_processes(tmp_path):
    marker = tmp_path / "orphan.txt"
    child = (
        "import sys,time; from pathlib import Path; time.sleep(2); "
        "Path(sys.argv[1]).write_text('orphan')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}, {str(marker)!r}]); "
        "print('child started', flush=True); time.sleep(60)"
    )
    stdout = tmp_path / "stdout.log"
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded_process(
            [sys.executable, "-c", parent],
            cwd=tmp_path,
            timeout=1,
            stdout_path=stdout,
            stderr_path=tmp_path / "stderr.log",
        )
    assert "child started" in stdout.read_text()
    time.sleep(2.2)
    assert not marker.exists()


@pytest.mark.parametrize(
    ("request_data", "expected_timeout"),
    [
        ({"mode": "edit"}, 3600),
        ({"mode": "reconstruct", "engine": "feature_tree"}, 3600),
        ({"mode": "reconstruct", "engine": "surface_brep"}, 3840),
    ],
)
def test_geometry_job_deadline_leaves_time_for_surface_recovery(
    tmp_path, monkeypatch, request_data, expected_timeout
):
    timeouts = []

    def fake_run_bounded_process(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired("geometry-worker", kwargs["timeout"])

    monkeypatch.setattr(jobs, "run_bounded_process", fake_run_bounded_process)

    with pytest.raises(TimeoutError, match=f"{expected_timeout}-second total deadline"):
        jobs._run_job(tmp_path, request_data)
    assert timeouts == [expected_timeout]


def test_job_retains_native_error_output_and_exit_code(tmp_path):
    stderr = tmp_path / "stderr.log"
    code = run_bounded_process(
        [sys.executable, "-c", "import sys; print('native failure',file=sys.stderr); sys.exit(7)"],
        cwd=tmp_path,
        timeout=10,
        stdout_path=tmp_path / "stdout.log",
        stderr_path=stderr,
    )
    assert code == 7
    assert "native failure" in stderr.read_text()


def test_feature_conversion_and_revision_edit_use_isolated_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "RUNS", tmp_path)
    source = trimesh.creation.box(extents=(10, 8, 6)).export(file_type="stl")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/reconstruct",
            files={"file": ("box.stl", source, "model/stl")},
            data={"engine": "feature_tree"},
        )
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["status"] == "complete"
        assert report["score"]["step_geometry_verified"]
        run_id = report["id"]
        request = {"plan": report["plan"], "expected_revision": report["plan"]["revision"]}
        edited = client.put(f"/api/runs/{run_id}/plan", json=request)
        assert edited.status_code == 200, edited.text
        assert edited.json()["plan"]["revision"] == request["expected_revision"] + 1
        assert edited.json()["score"]["step_geometry_verified"]
        stale = client.put(f"/api/runs/{run_id}/plan", json=request)
        assert stale.status_code == 409
    assert (tmp_path / run_id / "geometry-worker.stdout.log").is_file()


def test_background_cancellation_terminates_worker_and_descendants(tmp_path, monkeypatch):
    marker = tmp_path / "orphan.txt"
    stdout = tmp_path / "stdout.log"
    child = (
        "import sys,time; from pathlib import Path; time.sleep(2); "
        "Path(sys.argv[1]).write_text('orphan')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}, {str(marker)!r}]); "
        "print('child started', flush=True); time.sleep(60)"
    )

    def reconstruct(run_id, *_args, **_kwargs):
        code = run_bounded_process(
            [sys.executable, "-c", parent],
            cwd=tmp_path,
            timeout=60,
            stdout_path=stdout,
            stderr_path=tmp_path / "stderr.log",
            job_id=run_id,
        )
        raise RuntimeError(f"worker stopped with {code}")

    monkeypatch.setattr(main_module, "_reconstructor", lambda _engine: reconstruct)

    async def run():
        run_id = "c" * 32
        target = tmp_path / "input.stl"
        main_module.RUN_PROGRESS[run_id] = main_module.new_progress(run_id)
        task = asyncio.create_task(
            main_module._run_reconstruction_job(
                run_id, target, "input.stl", "mm", "surface_brep"
            )
        )
        for _ in range(100):
            await asyncio.sleep(0.05)
            if stdout.is_file() and "child started" in stdout.read_text():
                break
        else:
            raise AssertionError("worker did not start")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("background task did not retain cancellation")

    jobs.reset_job_system()
    asyncio.run(run())
    time.sleep(2.2)
    assert not marker.exists()
    jobs.reset_job_system()


@pytest.mark.parametrize("shutdown", [False, True])
def test_cancelled_work_cannot_register_after_the_termination_sweep(tmp_path, shutdown):
    marker = tmp_path / "late-worker.txt"
    job_id = "late-registration"
    if shutdown:
        jobs.terminate_job_processes()
    else:
        jobs.terminate_job_processes(job_id)
    try:
        with pytest.raises(jobs.JobCancelledError):
            run_bounded_process(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys,time; from pathlib import Path; time.sleep(1); "
                        "Path(sys.argv[1]).write_text('late')"
                    ),
                    str(marker),
                ],
                cwd=tmp_path,
                timeout=30,
                stdout_path=tmp_path / "late.stdout.log",
                stderr_path=tmp_path / "late.stderr.log",
                job_id=job_id,
            )
        time.sleep(1.2)
        assert not marker.exists()
    finally:
        jobs.reset_job_system()
