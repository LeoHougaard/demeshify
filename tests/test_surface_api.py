from __future__ import annotations

import time

import trimesh
from fastapi.testclient import TestClient

import app.main as main_module
from app import storage
from app.main import app
from app.schemas import MeshReport, ReconstructionReport


def test_surface_engine_runs_through_api_and_exposes_downloads(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(storage, "RUNS", tmp_path)
    stl = trimesh.creation.box(extents=(10, 8, 6)).export(file_type="stl")

    with TestClient(app) as client:
        response = client.post(
            "/api/reconstruct",
            files={"file": ("box.stl", stl, "model/stl")},
            data={"input_units": "mm", "engine": "surface_brep"},
        )

        assert response.status_code == 200, response.text
        report = response.json()
        assert report["engine"] == "surface_brep"
        assert report["status"] == "complete"
        assert report["plan"] is None
        assert report["surface"]["surface_counts"]["plane"] == 6
        assert report["surface"]["free_edge_count"] == 0

        run_id = report["id"]
        assert client.get(f"/api/runs/{run_id}").status_code == 200
        step_response = client.get(f"/api/runs/{run_id}/files/reconstruction.step")
        assert step_response.status_code == 200
        assert 'filename="box_reconstructed.step"' in step_response.headers[
            "content-disposition"
        ]
        graph_response = client.get(
            f"/api/runs/{run_id}/files/surface_graph.json"
        )
        assert graph_response.status_code == 200
        visualization = graph_response.json()["visualization"]
        assert not visualization["global_faceted_fallback"]
        assert not visualization["faceted_source_face_indices"]
        assert visualization["surface_boundaries"]
        assert client.get(f"/api/runs/{run_id}/live-preview").status_code == 200


def test_api_rejects_unknown_engine_before_creating_a_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "RUNS", tmp_path)
    stl = trimesh.creation.box().export(file_type="stl")

    with TestClient(app) as client:
        response = client.post(
            "/api/reconstruct/start",
            files={"file": ("box.stl", stl, "model/stl")},
            data={"engine": "unknown"},
        )

    assert response.status_code == 400
    assert not list(tmp_path.iterdir())


def test_background_surface_job_can_be_polled_reopened_and_downloaded(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(storage, "RUNS", tmp_path)
    stl = trimesh.creation.box(extents=(10, 8, 6)).export(file_type="stl")

    with TestClient(app) as client:
        start = client.post(
            "/api/reconstruct/start",
            files={"file": ("box.stl", stl, "model/stl")},
            data={"input_units": "mm", "engine": "surface_brep"},
        )
        assert start.status_code == 200, start.text
        run_id = start.json()["id"]
        assert len(run_id) == 32

        stages = []
        for _ in range(200):
            progress_response = client.get(f"/api/runs/{run_id}/progress")
            assert progress_response.status_code == 200
            progress = progress_response.json()
            stages.append(progress["stage"])
            if progress["status"] == "complete":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("background reconstruction did not finish")

        assert progress["stage"] == "Surface B-rep ready"
        assert any(stage != "Uploading mesh" for stage in stages)
        assert client.get(f"/api/runs/{run_id}").status_code == 200
        assert client.get(f"/api/runs/{run_id}/files/reconstruction.step").status_code == 200
        assert start.headers["x-content-type-options"] == "nosniff"


def test_empty_upload_is_rejected_without_leaving_a_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "RUNS", tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/reconstruct/start",
            files={"file": ("empty.stl", b"", "model/stl")},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "The STL is empty"
    assert not list(tmp_path.iterdir())


def test_missing_run_read_does_not_create_a_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "RUNS", tmp_path)
    missing_id = "d" * 32

    with TestClient(app) as client:
        assert client.get(f"/api/runs/{missing_id}").status_code == 404

    assert not (tmp_path / missing_id).exists()


def test_failed_report_is_not_advertised_as_a_completed_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "RUNS", tmp_path)

    def failed_reconstructor(
        run_id,
        _target,
        safe_name,
        input_units,
        _update,
    ) -> ReconstructionReport:
        return ReconstructionReport(
            id=run_id,
            status="failed",
            engine="feature_tree",
            mesh=MeshReport(
                file_name=safe_name,
                triangle_count=12,
                vertex_count=8,
                watertight=True,
                body_count=1,
                dimensions_mm=(1, 1, 1),
                volume_mm3=1,
                surface_area_mm2=6,
                input_units=input_units,
                unit_scale=1,
            ),
            plan=None,
            score=None,
            warnings=["No valid candidate could be built."],
            elapsed_seconds=0.01,
        )

    monkeypatch.setattr(main_module, "_reconstructor", lambda _engine: failed_reconstructor)
    stl = trimesh.creation.box().export(file_type="stl")

    with TestClient(app) as client:
        start = client.post(
            "/api/reconstruct/start",
            files={"file": ("box.stl", stl, "model/stl")},
        )
        run_id = start.json()["id"]
        for _ in range(50):
            progress = client.get(f"/api/runs/{run_id}/progress").json()
            if progress["status"] == "failed":
                break
            time.sleep(0.01)

    assert progress["status"] == "failed"
    assert progress["stage"] == "Reconstruction stopped"
    assert progress["preview_url"] is None


def test_full_background_queue_rejects_before_storing_upload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(storage, "RUNS", tmp_path)
    monkeypatch.setattr(
        main_module,
        "RUN_TASKS",
        {object() for _ in range(main_module.MAX_PENDING_JOBS)},
    )
    stl = trimesh.creation.box().export(file_type="stl")

    with TestClient(app) as client:
        response = client.post(
            "/api/reconstruct/start",
            files={"file": ("box.stl", stl, "model/stl")},
        )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "10"
    assert not list(tmp_path.iterdir())
