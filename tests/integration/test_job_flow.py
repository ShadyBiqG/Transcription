from __future__ import annotations

import time

from fastapi.testclient import TestClient

from tests.conftest import register_user
from transcription_service.api import create_app


def wait_for_terminal(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        body = response.json()
        if body["status"] in {"completed", "failed"}:
            return body
        time.sleep(0.02)
    raise AssertionError("Задание не перешло в terminal state")


def test_worker_completes_job_and_list_exposes_it(settings, fake_runner):
    with TestClient(create_app(settings, fake_runner)) as client:
        register_user(client)
        created = client.post(
            "/api/v1/jobs",
            files={"file": ("meeting.webm", b"video", "video/webm")},
        ).json()
        terminal = wait_for_terminal(client, created["id"])
        listed = client.get("/api/v1/jobs").json()
    assert terminal["status"] == "completed"
    assert terminal["started_at"] is not None
    assert terminal["completed_at"] is not None
    assert terminal["transcript_url"].endswith("/transcript")
    assert listed[0]["id"] == created["id"]


def test_worker_records_safe_failure(settings):
    from tests.conftest import FakeNoScribeRunner

    with TestClient(create_app(settings, FakeNoScribeRunner(fail=True))) as client:
        register_user(client)
        created = client.post(
            "/api/v1/jobs",
            files={"file": ("meeting.webm", b"video", "video/webm")},
        ).json()
        terminal = wait_for_terminal(client, created["id"])
    assert terminal["status"] == "failed"
    assert terminal["error_code"] == "noscribe_failed"
    assert terminal["error_message"] == "Тестовая ошибка"
