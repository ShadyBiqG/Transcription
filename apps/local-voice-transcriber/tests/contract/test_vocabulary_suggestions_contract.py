"""Контракт приватных словарных предложений без автоматической правки текста."""

from __future__ import annotations

from conftest import login, register
from fastapi.testclient import TestClient

from local_voice_transcriber.editor import SourceSegmentSnapshot


def _headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("local_voice_transcriber_csrf") or ""}


def _create_job(client: TestClient) -> str:
    response = client.post(
        "/api/v1/jobs",
        headers=_headers(client),
        files={
            "media": (
                "meeting.webm",
                b"\x1a\x45\xdf\xa3\x9f"
                + b"\x42\x86\x81\x01\x42\xf7\x81\x01\x42\xf2\x81\x04\x42\xf3\x81\x08"
                + b"\x42\x82\x84webm\x42\x87\x81\x04\x42\x85\x81\x02\x18\x53\x80\x67\xff",
                "video/webm",
            )
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["job"]["id"]


def test_suggestions_are_private_and_never_edit_current_revision(client: TestClient) -> None:
    register(client, "owner")
    login(client, "owner")
    owner_id = client.get("/api/v1/auth/me").json()["user"]["id"]
    job_id = _create_job(client)
    term_response = client.post(
        "/api/v1/vocabulary",
        headers=_headers(client),
        json={"canonical": "ФГИС «Меркурий»", "variants": ["Меркурий"]},
    )
    assert term_response.status_code == 201, term_response.text
    term_id = term_response.json()["term"]["id"]
    client.app.state.editor_repository.create_baseline(
        owner_id=owner_id,
        job_id=job_id,
        job_run_id="run-1",
        groups=(),
        source_segments=(
            SourceSegmentSnapshot(
                source_segment_id="segment-1", ordinal=0, text="Проверьте Меркурий сегодня."
            ),
            SourceSegmentSnapshot(
                source_segment_id="segment-2", ordinal=1, text="Без совпадений."
            ),
        ),
    )
    transcript_before = client.get(f"/api/v1/jobs/{job_id}/transcript").json()

    suggestions = client.get(
        f"/api/v1/jobs/{job_id}/vocabulary/suggestions",
        params={"expected_revision_number": 1},
    )
    assert suggestions.status_code == 200, suggestions.text
    assert suggestions.json() == {
        "revision_number": 1,
        "items": [
            {
                "source_segment_id": "segment-1",
                "ordinal": 0,
                "term_id": term_id,
                "matched_variant": "Меркурий",
                "canonical": "ФГИС «Меркурий»",
                "start_offset": 10,
                "end_offset": 18,
            }
        ],
    }
    assert client.get(f"/api/v1/jobs/{job_id}/transcript").json() == transcript_before
    revisions = client.app.state.editor_repository.list_revisions(owner_id=owner_id, job_id=job_id)
    assert len(revisions) == 1

    other = TestClient(client.app)
    with other:
        register(other, "other")
        login(other, "other")
        private = other.get(
            f"/api/v1/jobs/{job_id}/vocabulary/suggestions",
            params={"expected_revision_number": 1},
        )
        assert private.status_code == 404
        assert "Меркурий" not in private.text


def test_suggestions_report_unavailable_and_stale_revision(logged_in_client: TestClient) -> None:
    client = logged_in_client
    owner_id = client.get("/api/v1/auth/me").json()["user"]["id"]
    job_id = _create_job(client)
    endpoint = f"/api/v1/jobs/{job_id}/vocabulary/suggestions"
    unavailable = client.get(endpoint, params={"expected_revision_number": 1})
    assert unavailable.status_code == 409
    assert unavailable.json()["code"] == "transcript_unavailable"

    client.app.state.editor_repository.create_baseline(
        owner_id=owner_id,
        job_id=job_id,
        job_run_id="run-1",
        groups=(),
        source_segments=(
            SourceSegmentSnapshot(source_segment_id="segment-1", ordinal=0, text="Текст"),
        ),
    )
    client.app.state.editor_repository.create_manual_revision(
        owner_id=owner_id,
        job_id=job_id,
        expected_current_revision=1,
        segment_texts={"segment-1": "Другой текст"},
        created_by=owner_id,
    )
    stale = client.get(endpoint, params={"expected_revision_number": 1})
    assert stale.status_code == 409
    assert stale.json()["code"] == "revision_conflict"
    assert stale.json()["details"] == {
        "expected_revision_number": 1,
        "current_revision_number": 2,
    }
