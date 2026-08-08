from __future__ import annotations

import trimesh
from fastapi.testclient import TestClient

from app import storage
from app.main import app


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
        assert client.get(
            f"/api/runs/{run_id}/files/reconstruction.step"
        ).status_code == 200
        assert client.get(
            f"/api/runs/{run_id}/files/surface_graph.json"
        ).status_code == 200
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
