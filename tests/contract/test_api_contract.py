from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import register_user
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
        register_user(client)
        response = client.post(
            "/api/v1/jobs",
            files={"file": ("встреча.webm", b"webm-data", "video/webm")},
            data={"language": "ru", "model": "precise", "speaker_detection": "2"},
        )
    assert response.status_code == 202
    body = response.json()
    assert body["original_filename"] == "встреча.webm"
    assert body["status"] == "queued"
    assert body["speaker_detection"] == "2"
    assert body["created_at"] is not None
    assert body["started_at"] is None
    assert body["completed_at"] is None
    assert body["transcript_url"] is None
    assert body["manifest_url"].endswith("/manifest")


def test_upload_rejects_invalid_extension_and_empty_file(settings, fake_runner):
    with TestClient(create_app(settings, fake_runner, start_worker=False)) as client:
        register_user(client)
        wrong_type = client.post(
            "/api/v1/jobs", files={"file": ("meeting.mp3", b"audio", "audio/mpeg")}
        )
        empty = client.post("/api/v1/jobs", files={"file": ("meeting.webm", b"", "video/webm")})
    assert wrong_type.status_code == 400
    assert empty.status_code == 400


def test_upload_rejects_invalid_speaker_count(settings, fake_runner):
    with TestClient(create_app(settings, fake_runner, start_worker=False)) as client:
        register_user(client)
        response = client.post(
            "/api/v1/jobs",
            files={"file": ("meeting.webm", b"audio", "video/webm")},
            data={"speaker_detection": "11"},
        )
    assert response.status_code == 422


def test_cookie_session_is_enforced(settings, fake_runner):
    with TestClient(create_app(settings, fake_runner, start_worker=False)) as client:
        denied = client.get("/api/v1/jobs")
        register_user(client)
        allowed = client.get("/api/v1/jobs")
    assert denied.status_code == 401
    assert allowed.status_code == 200


def test_registration_login_logout_contract(settings, fake_runner):
    with TestClient(create_app(settings, fake_runner, start_worker=False)) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": " User@Example.com ", "password": "password123"},
        )
        me = client.get("/api/v1/auth/me")
        duplicate = client.post(
            "/api/v1/auth/register",
            json={"email": "user@example.com", "password": "password123"},
        )
        logout = client.post("/api/v1/auth/logout")
        denied = client.get("/api/v1/auth/me")
        bad_login = client.post(
            "/api/v1/auth/login",
            json={"email": "user@example.com", "password": "wrong-password"},
        )
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "user@example.com", "password": "password123"},
        )

    assert registered.status_code == 201
    assert registered.json()["email"] == "user@example.com"
    assert "httponly" in registered.headers["set-cookie"].lower()
    assert me.status_code == 200
    assert duplicate.status_code == 409
    assert logout.status_code == 204
    assert denied.status_code == 401
    assert bad_login.status_code == 401
    assert login.status_code == 200


def test_users_cannot_see_each_others_jobs(settings, fake_runner):
    with TestClient(create_app(settings, fake_runner, start_worker=False)) as client:
        register_user(client, "first@example.com")
        first_job = client.post(
            "/api/v1/jobs",
            files={"file": ("private.webm", b"video", "video/webm")},
        ).json()
        client.post("/api/v1/auth/logout")

        register_user(client, "second@example.com")
        second_list = client.get("/api/v1/jobs")
        second_get = client.get(f"/api/v1/jobs/{first_job['id']}")

        client.post("/api/v1/auth/logout")
        client.post(
            "/api/v1/auth/login",
            json={"email": "first@example.com", "password": "password123"},
        )
        first_list = client.get("/api/v1/jobs")

    assert second_list.json() == []
    assert second_get.status_code == 404
    assert [job["id"] for job in first_list.json()] == [first_job["id"]]
