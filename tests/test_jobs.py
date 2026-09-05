import subprocess
import sys
import time

import pytest
import trimesh
from fastapi.testclient import TestClient

from app import storage
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
    with TestClient(app) as client:
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
