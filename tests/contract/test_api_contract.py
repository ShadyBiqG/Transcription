from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from transcription_service.api import create_app


def test_health_contract(settings, fake_runner):
    with TestClient(create_app(settings, fake_runner, start_worker=False)) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {
        "service": "ok",
        "noscribe": "ready",
        "models": ["fast", "precise"],
        "worker": "stopped",
    }


def test_upload_contract_returns_accepted_job(settings, fake_runner):
    with TestClient(create_app(settings, fake_runner, start_worker=False)) as client:
        response = client.post(
            "/api/v1/jobs",
            files={"file": ("встреча.webm", b"webm-data", "video/webm")},
            data={"language": "ru", "model": "precise"},
        )
    assert response.status_code == 202
    body = response.json()
    assert body["original_filename"] == "встреча.webm"
    assert body["status"] == "queued"
    assert body["transcript_url"] is None
    assert body["manifest_url"].endswith("/manifest")


def test_upload_rejects_invalid_extension_and_empty_file(settings, fake_runner):
    with TestClient(create_app(settings, fake_runner, start_worker=False)) as client:
        wrong_type = client.post(
            "/api/v1/jobs", files={"file": ("meeting.mp3", b"audio", "audio/mpeg")}
        )
        empty = client.post("/api/v1/jobs", files={"file": ("meeting.webm", b"", "video/webm")})
    assert wrong_type.status_code == 400
    assert empty.status_code == 400


def test_bearer_token_is_enforced(settings, fake_runner):
    protected = replace(settings, api_token="correct-token")
    with TestClient(create_app(protected, fake_runner, start_worker=False)) as client:
        denied = client.get("/api/v1/jobs")
        allowed = client.get("/api/v1/jobs", headers={"Authorization": "Bearer correct-token"})
    assert denied.status_code == 401
    assert allowed.status_code == 200
