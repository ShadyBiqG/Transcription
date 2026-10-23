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
                    "assignment_status": "confirmed",
                }
            },
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"]["number"] == 2
    assert updated.json()["segments"][0]["text"] == "Исправленный текст"

    technical_identifier = client.patch(
        f"/api/v1/jobs/{job_id}/transcript",
        headers=_headers(client),
        json={
            "expected_revision_number": 2,
            "group_updates": {
                "group-a": {
                    "label_snapshot": "Марина",
                    "assignment_status": "confirmed",
                    "profile_id": "нельзя-передавать-клиенту",
                }
            },
        },
    )
    assert technical_identifier.status_code == 422

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


def test_group_identity_reuses_only_exact_normalized_speaker_name(client: TestClient) -> None:
    register(client, "owner-user")
    login(client, "owner-user")
    owner = client.get("/api/v1/auth/me").json()["user"]["id"]
    job_id = _create_job(client)
    _publish_baseline(client, job_id=job_id, owner_id=owner)

    first = client.post(
        f"/api/v1/jobs/{job_id}/transcript/groups/group-a/identity",
        headers=_headers(client),
        json={"display_name": "  Марина   Иванова ", "expected_revision_number": 1},
    )
    assert first.status_code == 200, first.text
    assert first.json()["revision"]["number"] == 2
    assert first.json()["groups"][0]["label"] == "Марина Иванова"
    assert "profile_id" not in first.text
    first_profile_id = next(
        group.profile_id
        for group in client.app.state.editor_repository.get_current_revision(
            owner_id=owner, job_id=job_id
        ).groups
        if group.speaker_group_id == "group-a"
    )
    assert first_profile_id is not None
    with client.app.state.database.connection() as connection:
        audit = connection.execute(
            "SELECT actor_id, target_type, target_id FROM audit_events "
            "WHERE action = 'speaker_group_identity_assigned'"
        ).fetchone()
    assert tuple(audit) == (owner, "speaker", first_profile_id)

    exact = client.post(
        f"/api/v1/jobs/{job_id}/transcript/groups/group-b/identity",
        headers=_headers(client),
        json={"display_name": "марина иванова", "expected_revision_number": 2},
    )
    assert exact.status_code == 200, exact.text
    current = client.app.state.editor_repository.get_current_revision(owner_id=owner, job_id=job_id)
    assert {group.profile_id for group in current.groups} == {first_profile_id}

    partial = client.post(
        f"/api/v1/jobs/{job_id}/transcript/groups/group-a/identity",
        headers=_headers(client),
        json={"display_name": "Марина", "expected_revision_number": 3},
    )
    assert partial.status_code == 200, partial.text
    changed = client.app.state.editor_repository.get_current_revision(owner_id=owner, job_id=job_id)
    profile_ids = {group.speaker_group_id: group.profile_id for group in changed.groups}
    assert profile_ids["group-a"] != profile_ids["group-b"]
    assert changed.info.number == 4


def test_stale_group_identity_does_not_create_an_unlinked_shared_speaker(
    client: TestClient,
) -> None:
    register(client, "owner-user")
    login(client, "owner-user")
    owner = client.get("/api/v1/auth/me").json()["user"]["id"]
    job_id = _create_job(client)
    _publish_baseline(client, job_id=job_id, owner_id=owner)

    current = client.patch(
        f"/api/v1/jobs/{job_id}/transcript",
        headers=_headers(client),
        json={"expected_revision_number": 1, "segment_texts": {"segment-1": "Актуальный текст"}},
    )
    assert current.status_code == 200, current.text

    with client.app.state.database.connection() as connection:
        before_count = connection.execute("SELECT COUNT(*) FROM next_voice_profiles").fetchone()[0]

    stale = client.post(
        f"/api/v1/jobs/{job_id}/transcript/groups/group-a/identity",
        headers=_headers(client),
        json={"display_name": "Новая подпись", "expected_revision_number": 1},
    )

    assert stale.status_code == 409
    assert stale.json()["code"] == "revision_conflict"
    with client.app.state.database.connection() as connection:
        after_count = connection.execute("SELECT COUNT(*) FROM next_voice_profiles").fetchone()[0]
    assert after_count == before_count
    revision = client.app.state.editor_repository.get_current_revision(
        owner_id=owner, job_id=job_id
    )
    assert revision.info.number == 2
    assert all(group.profile_id is None for group in revision.groups)


def test_group_identity_does_not_resolve_a_speaker_alias(client: TestClient) -> None:
    register(client, "owner-user")
    login(client, "owner-user")
    owner = client.get("/api/v1/auth/me").json()["user"]["id"]
    job_id = _create_job(client)
    _publish_baseline(client, job_id=job_id, owner_id=owner)
    client.app.state.voice_directory_service.create_profile(
        profile_id="canonical-speaker",
        display_name="Александр Иванов",
        aliases=("Саша",),
        actor_id=owner,
    )

    response = client.post(
        f"/api/v1/jobs/{job_id}/transcript/groups/group-a/identity",
        headers=_headers(client),
        json={"display_name": "Саша", "expected_revision_number": 1},
    )

    assert response.status_code == 200, response.text
    current = client.app.state.editor_repository.get_current_revision(owner_id=owner, job_id=job_id)
    assigned_profile_id = next(
        group.profile_id for group in current.groups if group.speaker_group_id == "group-a"
    )
    assert assigned_profile_id is not None
    assert assigned_profile_id != "canonical-speaker"


def test_archived_speaker_collision_is_not_enumerated_from_identity_endpoint(
    client: TestClient,
) -> None:
    register(client, "owner-user")
    login(client, "owner-user")
    owner = client.get("/api/v1/auth/me").json()["user"]["id"]
    job_id = _create_job(client)
    _publish_baseline(client, job_id=job_id, owner_id=owner)
    assigned = client.post(
        f"/api/v1/jobs/{job_id}/transcript/groups/group-a/identity",
        headers=_headers(client),
        json={"display_name": "Марина Иванова", "expected_revision_number": 1},
    )
    assert assigned.status_code == 200, assigned.text
    profile_id = next(
        group.profile_id
        for group in client.app.state.editor_repository.get_current_revision(
            owner_id=owner, job_id=job_id
        ).groups
        if group.speaker_group_id == "group-a"
    )
    assert profile_id is not None
    client.app.state.voice_directory_repository.archive_profile(
        profile_id=profile_id,
        actor_id=owner,
        reason="проверка",
    )

    collision = client.post(
        f"/api/v1/jobs/{job_id}/transcript/groups/group-b/identity",
        headers=_headers(client),
        json={"display_name": "марина   иванова", "expected_revision_number": 2},
    )

    assert collision.status_code == 422
    assert collision.json()["code"] == "speaker_identity_unavailable"
    assert "профил" not in collision.text.casefold()
    assert "марина" not in collision.text.casefold()


def test_transcript_export_is_current_and_owner_scoped(client: TestClient) -> None:
    register(client, "owner-user")
    login(client, "owner-user")
    owner = client.get("/api/v1/auth/me").json()["user"]["id"]
    job_id = _create_job(client)
    _publish_baseline(client, job_id=job_id, owner_id=owner)
    assigned = client.post(
        f"/api/v1/jobs/{job_id}/transcript/groups/group-a/identity",
        headers=_headers(client),
        json={"display_name": "Марина Иванова", "expected_revision_number": 1},
    )
    assert assigned.status_code == 200, assigned.text

    exported = client.get(f"/api/v1/jobs/{job_id}/transcript/export?format=txt")

    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"].startswith("text/plain")
    assert exported.headers["content-disposition"] == 'attachment; filename="transcription.txt"'
    assert "Марина Иванова: Исходный текст" in exported.text
    assert client.get(f"/api/v1/jobs/{job_id}/transcript/export?format=html").status_code == 422

    other = TestClient(client.app)
    with other:
        register(other, "other-user")
        login(other, "other-user")
        denied = other.get(f"/api/v1/jobs/{job_id}/transcript/export?format=txt")

    assert denied.status_code == 404


def test_editor_projects_legacy_generic_labels_as_speakers(client: TestClient) -> None:
    register(client, "owner-user")
    login(client, "owner-user")
    owner = client.get("/api/v1/auth/me").json()["user"]["id"]
    job_id = _create_job(client)
    _publish_baseline(client, job_id=job_id, owner_id=owner)

    transcript = client.get(f"/api/v1/jobs/{job_id}/transcript")

    assert transcript.status_code == 200, transcript.text
    assert [group["label"] for group in transcript.json()["groups"]] == [
        "Спикер 1",
        "Спикер 2",
    ]
    assert "Спикер 1: Исходный текст" in client.get(
        f"/api/v1/jobs/{job_id}/transcript/export"
    ).text


def test_owner_can_choose_raw_clean_or_last_selected_view(client: TestClient) -> None:
    register(client, "owner-user")
    login(client, "owner-user")
    owner = client.get("/api/v1/auth/me").json()["user"]["id"]
    job_id = _create_job(client)
    _publish_baseline(client, job_id=job_id, owner_id=owner)

    initial = client.get(f"/api/v1/jobs/{job_id}/transcript/view-preference")
    assert initial.json() == {"preference": "clean", "selected_view": "clean"}

    updated = client.patch(
        f"/api/v1/jobs/{job_id}/transcript/view-preference",
        headers=_headers(client),
        json={"preference": "last_selected", "selected_view": "raw"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json() == {"preference": "last_selected", "selected_view": "raw"}

    transcript = client.get(f"/api/v1/jobs/{job_id}/transcript")
    assert transcript.status_code == 200, transcript.text
    assert transcript.json()["view"]["resolved"] == "raw"
    assert transcript.json()["view"]["preference"] == "last_selected"


def test_segment_speaker_assignment_is_a_new_local_revision(client: TestClient) -> None:
    register(client, "owner-user")
    login(client, "owner-user")
    owner = client.get("/api/v1/auth/me").json()["user"]["id"]
    job_id = _create_job(client)
    _publish_baseline(client, job_id=job_id, owner_id=owner)

    changed = client.put(
        f"/api/v1/jobs/{job_id}/transcript/segments/segment-1/speaker",
        headers=_headers(client),
        json={"speaker_group_id": "group-b", "expected_revision_number": 1},
    )

    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"]["number"] == 2
    assert changed.json()["segments"][0]["primary_group_id"] == "group-b"
    raw = client.get(f"/api/v1/jobs/{job_id}/transcript?view=raw")
    assert raw.status_code == 200, raw.text
    assert raw.json()["segments"][0]["primary_group_id"] == "group-a"
