"""HTTP-контракт приватного редактора и optimistic concurrency."""

from __future__ import annotations

from conftest import login, register
from fastapi.testclient import TestClient

from local_voice_transcriber.editor import GroupSnapshot, SourceSegmentSnapshot


def _headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("local_voice_transcriber_csrf") or ""}


def _create_job(client: TestClient) -> str:
    response = client.post(
        "/api/v1/jobs",
        headers=_headers(client),
        files={"media": ("meeting.webm", b"\x1a\x45\xdf\xa3" + b"content", "video/webm")},
    )
    assert response.status_code == 201, response.text
    return response.json()["job"]["id"]


def _publish_baseline(client: TestClient, *, job_id: str, owner_id: str) -> None:
    client.app.state.editor_repository.create_baseline(
        owner_id=owner_id,
        job_id=job_id,
        job_run_id="run-1",
        groups=(
            GroupSnapshot(speaker_group_id="group-a", label_snapshot="Говорящий 1"),
            GroupSnapshot(speaker_group_id="group-b", label_snapshot="Говорящий 2"),
        ),
        source_segments=(
            SourceSegmentSnapshot(
                source_segment_id="segment-1",
                ordinal=0,
                text="Исходный текст",
                primary_group_id="group-a",
                speaker_group_ids=("group-a",),
            ),
        ),
    )


def test_editor_creates_revision_instead_of_mutating_baseline(client: TestClient) -> None:
    register(client, "owner-user")
    login(client, "owner-user")
    owner = client.get("/api/v1/auth/me").json()["user"]["id"]
    job_id = _create_job(client)
    _publish_baseline(client, job_id=job_id, owner_id=owner)

    initial = client.get(f"/api/v1/jobs/{job_id}/transcript")
    assert initial.status_code == 200, initial.text
    assert initial.json()["revision"]["number"] == 1

    updated = client.patch(
        f"/api/v1/jobs/{job_id}/transcript",
        headers=_headers(client),
        json={
            "expected_revision_number": 1,
            "segment_texts": {"segment-1": "Исправленный текст"},
            "group_updates": {
                "group-a": {
                    "label_snapshot": "Марина",
                    "profile_id": None,
                    "assignment_status": "confirmed",
                }
            },
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"]["number"] == 2
    assert updated.json()["segments"][0]["text"] == "Исправленный текст"

    history = client.get(f"/api/v1/jobs/{job_id}/transcript/revisions")
    assert [item["number"] for item in history.json()["items"]] == [2, 1]

    conflict = client.patch(
        f"/api/v1/jobs/{job_id}/transcript",
        headers=_headers(client),
        json={"expected_revision_number": 1},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "revision_conflict"


def test_editor_remains_private_when_profile_catalog_is_shared(client: TestClient) -> None:
    register(client, "owner-user")
    login(client, "owner-user")
    owner = client.get("/api/v1/auth/me").json()["user"]["id"]
    job_id = _create_job(client)
    _publish_baseline(client, job_id=job_id, owner_id=owner)

    other = TestClient(client.app)
    with other:
        register(other, "other-user")
        login(other, "other-user")

        transcript = other.get(f"/api/v1/jobs/{job_id}/transcript")
        history = other.get(f"/api/v1/jobs/{job_id}/transcript/revisions")

        assert transcript.status_code == 404
        assert history.status_code == 404
