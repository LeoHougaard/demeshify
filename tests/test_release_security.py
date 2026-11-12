import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main as main_module
from app import storage
from app.security import LocalRequestMiddleware


@pytest.mark.parametrize("path", ["/api/reconstruct", "/api/reconstruct/start"])
def test_full_queue_rejects_both_conversion_routes(path, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "RUNS", tmp_path)
    monkeypatch.setattr(main_module, "RUN_TASKS", {object() for _ in range(4)})
    with TestClient(main_module.app, base_url="http://127.0.0.1") as client:
        response = client.post(path, files={"file": ("model.stl", b"mesh")})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "10"
    assert not list(tmp_path.iterdir())
    assert not main_module.REQUEST_TASKS


@pytest.mark.parametrize("origin", ["https://unrelated.example", "null", "http://localhost"])
def test_cross_origin_upload_is_rejected_before_creating_a_run(origin, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "RUNS", tmp_path)
    with TestClient(main_module.app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/reconstruct/start", headers={"Origin": origin},
            files={"file": ("model.stl", b"mesh")},
        )
    assert response.status_code == 403
    assert not list(tmp_path.iterdir())


def test_request_admission_is_released_after_invalid_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "RUNS", tmp_path)
    with TestClient(main_module.app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/reconstruct", headers={"Origin": "http://127.0.0.1"},
            files={"file": ("model.stl", b"")},
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "The STL is empty"
    assert not main_module.REQUEST_TASKS
    assert not list(tmp_path.iterdir())


def test_dns_rebinding_hostname_is_rejected():
    with TestClient(main_module.app, base_url="http://unrelated.example") as client:
        response = client.get("/api/health")
    assert response.status_code == 400


def test_codespace_origin_survives_forwarded_local_host(monkeypatch):
    monkeypatch.setenv("CODESPACE_NAME", "release-test")
    monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
    app = FastAPI()
    app.add_middleware(LocalRequestMiddleware)

    @app.post("/check")
    def check():
        return {"ok": True}

    with TestClient(app, base_url="http://localhost:8421") as client:
        response = client.post("/check", headers={
            "Origin": "https://release-test-8421.app.github.dev",
            "Sec-Fetch-Site": "same-origin",
        })
    assert response.status_code == 200
