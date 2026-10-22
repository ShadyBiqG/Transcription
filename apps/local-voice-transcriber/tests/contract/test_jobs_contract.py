from __future__ import annotations

import pytest
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
    assert listed.json()["next_cursor"] is None
    assert listed.json()["items"][0]["current_run"] is None

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


def test_job_list_uses_private_stable_cursor_and_safe_current_run(
    logged_in_client: TestClient,
) -> None:
    client = logged_in_client
    owner_id = client.get("/api/v1/auth/me").json()["user"]["id"]
    repository = client.app.state.jobs_repository
    for number in range(53):
        repository.create_job(
            job_id=f"job-{number:03}",
            owner_id=owner_id,
            source_name=f"Запись {number}.webm",
            source_path=f"/private/secret-{number}.webm",
        )
    run = repository.create_run(
        run_id="run-visible", job_id="job-052", request_id="first", asr_pack_id="fake-pack"
    )

    first = client.get("/api/v1/jobs?limit=20")
    assert first.status_code == 200, first.text
    first_payload = first.json()
    assert len(first_payload["items"]) == 20
    assert first_payload["next_cursor"]
    current_run = first_payload["items"][0]["current_run"]
    assert current_run == {
        "id": run.id,
        "status": "queued",
        "stage": None,
        "attempt": 0,
        "max_attempts": 3,
        "cancel_requested": False,
        "error_code": None,
        "created_at": run.created_at,
        "started_at": None,
        "completed_at": None,
    }
    assert "/private/" not in first.text
    assert "fake-pack" not in first.text

    repository.create_job(
        job_id="newest", owner_id=owner_id, source_name="Новая.webm", source_path="/private/new"
    )
    seen = [item["id"] for item in first_payload["items"]]
    cursor = first_payload["next_cursor"]
    while cursor is not None:
        response = client.get("/api/v1/jobs", params={"limit": 20, "cursor": cursor})
        assert response.status_code == 200, response.text
        payload = response.json()
        seen.extend(item["id"] for item in payload["items"])
        cursor = payload["next_cursor"]
    assert len(seen) == len(set(seen)) == 53
    assert "newest" not in seen

    other = TestClient(client.app)
    with other:
        register(other, "other")
        login(other, "other")
        private = other.get("/api/v1/jobs", params={"cursor": first_payload["next_cursor"]})
        assert private.status_code == 200
        assert private.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"cursor": "bad!"}])
def test_job_list_rejects_invalid_pagination(
    logged_in_client: TestClient, params: dict[str, object]
) -> None:
    response = logged_in_client.get("/api/v1/jobs", params=params)
    assert response.status_code == 422


def test_run_requires_running_worker_even_with_ready_pack(logged_in_client: TestClient) -> None:
    client = logged_in_client
    job_id = _create_job(client)
    client.app.state.asr_ready = True
    client.app.state.asr_pack_id = "local-fake-pack"
    client.app.state.dispatcher_status = lambda: "stopped"
    response = client.post(
        f"/api/v1/jobs/{job_id}/runs",
        headers={**_headers(client), "Idempotency-Key": "first-request"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "worker_unavailable"


@pytest.mark.parametrize("processing", [False, True])
def test_active_run_rejects_new_key_and_retries_same_key(
    logged_in_client: TestClient, processing: bool
) -> None:
    client = logged_in_client
    job_id = _create_job(client)
    assert client.get(f"/api/v1/jobs/{job_id}").json()["job"]["status"] == "uploaded"
    client.app.state.asr_ready = True
    client.app.state.asr_pack_id = "local-fake-pack"
    client.app.state.dispatcher_status = lambda: "running"
    endpoint = f"/api/v1/jobs/{job_id}/runs"
    first_headers = {**_headers(client), "Idempotency-Key": "first-request"}

    first = client.post(endpoint, headers=first_headers)
    assert first.status_code == 202
    run_id = first.json()["run"]["id"]
    repository = client.app.state.jobs_repository
    if processing:
        assert repository.claim_next_run(worker_id="fake-worker", lease_seconds=30) is not None
    retry = client.post(endpoint, headers=first_headers)
    assert retry.status_code == 202
    assert retry.json()["run"]["id"] == run_id
    duplicate = client.post(
        endpoint, headers={**_headers(client), "Idempotency-Key": "second-request"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "active_run_exists"
    assert "message" in duplicate.json()
    owner_id = client.get("/api/v1/auth/me").json()["user"]["id"]
    assert len(repository.list_runs(job_id=job_id, owner_id=owner_id)) == 1
    if processing:
        repository.mark_ready(run_id=run_id, worker_id="fake-worker")
    else:
        repository.request_cancel(run_id)
    next_run = client.post(
        endpoint, headers={**_headers(client), "Idempotency-Key": "second-request"}
    )
    assert next_run.status_code == 202
    assert next_run.json()["run"]["id"] != run_id
