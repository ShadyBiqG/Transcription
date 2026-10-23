"""Контракт явного обучения профиля из локальной группы."""

from __future__ import annotations

from pathlib import Path

from conftest import login, register
from fastapi.testclient import TestClient

from local_voice_transcriber.editor import GroupSnapshot, SourceSegmentSnapshot


def _headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("local_voice_transcriber_csrf") or ""}


def test_group_sample_is_rejected_until_verified_local_voice_pack_is_configured(
    client: TestClient,
) -> None:
    register(client, "owner")
    login(client, "owner")

    response = client.post(
        "/api/v1/jobs/unknown/transcript/groups/local-speaker:0/samples",
        headers=_headers(client),
        json={"profile_id": "profile-1"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "voice_pack_unavailable"


class _Artifact:
    artifact_path = Path("sample.wav")
    protected_path = "sample.wav"
    content_sha256 = "a" * 64
    quality_metadata = {"duration_ms": 1_000, "non_silent_ratio": 1.0}


class _ArtifactWriter:
    def create_from_interval(self, *args: object, **kwargs: object) -> _Artifact:
        del args, kwargs
        return _Artifact()


class _IndexWriter:
    def reconcile(self) -> None:
        return None


def _create_job_with_group(client: TestClient, *, owner_id: str) -> str:
    job = client.app.state.jobs_repository.create_job(
        job_id="job-with-group",
        owner_id=owner_id,
        source_name="meeting.webm",
        source_path="jobs/job-with-group/source.webm",
    )
    run = client.app.state.jobs_repository.create_run(
        run_id="run-with-group",
        job_id=job.id,
        request_id="request-with-group",
        asr_pack_id="asr-pack",
        voice_pack_id="voice-pack",
    )
    with client.app.state.database.connection() as connection:
        source_segment_id = connection.execute(
            """
            INSERT INTO next_asr_segments(
                job_run_id, ordinal, start_ms, end_ms, raw_text, language, confidence,
                word_timing_status
            ) VALUES (?, 0, 0, 1500, 'Текст', 'ru', NULL, 'unavailable')
            """,
            (run.id,),
        ).lastrowid
    assert source_segment_id is not None
    client.app.state.editor_repository.create_baseline(
        owner_id=owner_id,
        job_id=job.id,
        job_run_id=run.id,
        groups=(
            GroupSnapshot(speaker_group_id="group-a", label_snapshot="Говорящий 1"),
            GroupSnapshot(speaker_group_id="group-b", label_snapshot="Говорящий 2"),
        ),
        source_segments=(
            SourceSegmentSnapshot(
                source_segment_id=str(source_segment_id),
                ordinal=0,
                text="Текст",
                primary_group_id="group-a",
                speaker_group_ids=("group-a",),
            ),
        ),
    )
    return job.id


def test_sample_uses_hidden_group_identity_and_ignores_submitted_profile_id(
    client: TestClient,
) -> None:
    register(client, "owner")
    login(client, "owner")
    owner = client.get("/api/v1/auth/me").json()["user"]["id"]
    job_id = _create_job_with_group(client, owner_id=owner)
    identity = client.post(
        f"/api/v1/jobs/{job_id}/transcript/groups/group-a/identity",
        headers=_headers(client),
        json={"display_name": "Марина Иванова", "expected_revision_number": 1},
    )
    assert identity.status_code == 200, identity.text
    assigned_profile_id = next(
        group.profile_id
        for group in client.app.state.editor_repository.get_current_revision(
            owner_id=owner, job_id=job_id
        ).groups
        if group.speaker_group_id == "group-a"
    )
    assert assigned_profile_id is not None
    client.app.state.voice_diarization_ready = True
    client.app.state.voice_pack_id = "voice-pack"
    client.app.state.voice_sample_artifact_writer = _ArtifactWriter()
    client.app.state.voice_index_writer = _IndexWriter()

    unassigned = client.post(
        f"/api/v1/jobs/{job_id}/transcript/groups/group-b/samples",
        headers=_headers(client),
        json={"profile_id": "arbitrary-id"},
    )
    enrolled = client.post(
        f"/api/v1/jobs/{job_id}/transcript/groups/group-a/samples",
        headers=_headers(client),
        json={"profile_id": "arbitrary-id"},
    )

    assert unassigned.status_code == 422
    assert unassigned.json()["code"] == "speaker_identity_required"
    assert enrolled.status_code == 202, enrolled.text
    assert "profile_id" not in enrolled.text
    samples = client.app.state.voice_directory_repository.list_samples_for_index(
        voice_pack_id="voice-pack"
    )
    assert len(samples) == 1
    assert samples[0].profile_id == assigned_profile_id
