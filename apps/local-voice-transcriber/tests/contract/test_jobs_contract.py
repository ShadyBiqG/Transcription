from __future__ import annotations

from conftest import login, register
from fastapi.testclient import TestClient


def _headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("local_voice_transcriber_csrf") or ""}


def _webm() -> bytes:
    return b"\x1a\x45\xdf\xa3" + b"test-webm"


def _create_job(client: TestClient) -> str:
    response = client.post(
        "/api/v1/jobs",
        headers=_headers(client),
        files={"media": ("meeting.webm", _webm(), "video/webm")},
    )
    assert response.status_code == 201, response.text
    return response.json()["job"]["id"]


def test_upload_and_list_are_private(client: TestClient) -> None:
    register(client, "owner")
    login(client, "owner")
    job_id = _create_job(client)

    listed = client.get("/api/v1/jobs")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [job_id]

    other = TestClient(client.app)
    with other:
        register(other, "other")
        login(other, "other")
        assert other.get(f"/api/v1/jobs/{job_id}").status_code == 404
        assert other.get(f"/api/v1/jobs/{job_id}/audio").status_code == 404
        assert other.get(f"/api/v1/jobs/{job_id}/source").status_code == 404


def test_upload_requires_csrf_and_valid_webm(client: TestClient) -> None:
    register(client)
    login(client)

    no_csrf = client.post("/api/v1/jobs", files={"media": ("meeting.webm", _webm())})
    assert no_csrf.status_code == 403

    invalid = client.post(
        "/api/v1/jobs",
        headers=_headers(client),
        files={"media": ("meeting.webm", b"not-webm")},
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_media"


def test_run_is_safely_blocked_without_verified_local_pack(client: TestClient) -> None:
    register(client)
    login(client)
    job_id = _create_job(client)

    response = client.post(
        f"/api/v1/jobs/{job_id}/runs",
        headers={**_headers(client), "Idempotency-Key": "client-request-1"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "asr_pack_unavailable"
