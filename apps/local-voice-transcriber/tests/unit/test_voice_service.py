from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from local_voice_transcriber.voice.repository import ProfileState, VoiceDirectoryRepository
from local_voice_transcriber.voice.service import (
    ExplicitSampleConfirmationRequiredError,
    VoiceDirectoryService,
)


def test_directory_is_shared_but_projection_has_no_private_sample_provenance(
    tmp_path: Path,
) -> None:
    service = prepared_service(tmp_path)
    service.create_profile(
        profile_id="alex",
        display_name="Александр",
        actor_id="user-a",
        aliases=("Саша",),
    )

    visible_to_another_user = service.list_profiles(query="саш")

    assert len(visible_to_another_user) == 1
    projection = asdict(visible_to_another_user[0])
    assert projection["display_name"] == "Александр"
    assert "created_by" not in projection
    assert "source_job_id" not in projection
    assert "vector_id" not in projection
    assert "protected_path" not in projection


def test_sample_requires_explicit_confirmation_and_cannot_be_implicit_auto_data(
    tmp_path: Path,
) -> None:
    service = prepared_service(tmp_path)
    service.create_profile(profile_id="alex", display_name="Александр", actor_id="user-a")

    with pytest.raises(ExplicitSampleConfirmationRequiredError):
        service.add_explicit_sample(
            explicit_confirm=False,
            sample_id="sample-1",
            operation_id="operation-1",
            profile_id="alex",
            voice_pack_id="voice-pack-1",
            protected_artifact_id="artifact-1",
            artifact_content_sha256="a" * 64,
            protected_path="protected/sample-1.wav",
            quality={"score": 0.9},
            actor_id="user-a",
            source_group_id="group-1",
        )

    assert service._repository.list_samples_for_index(voice_pack_id="voice-pack-1") == []

    enrollment = service.add_explicit_sample(
        explicit_confirm=True,
        sample_id="sample-1",
        operation_id="operation-1",
        profile_id="alex",
        voice_pack_id="voice-pack-1",
        protected_artifact_id="artifact-1",
        artifact_content_sha256="a" * 64,
        protected_path="protected/sample-1.wav",
        quality={"score": 0.9},
        actor_id="user-a",
        source_group_id="group-1",
    )

    assert enrollment.profile_id == "alex"
    assert enrollment.operation_id == "operation-1"
    assert enrollment.state.value == "pending"


def test_archive_and_restore_are_reversible_directory_operations(tmp_path: Path) -> None:
    service = prepared_service(tmp_path)
    service.create_profile(profile_id="alex", display_name="Александр", actor_id="user-a")

    archived = service.archive_profile(profile_id="alex", actor_id="user-b", reason="ошибка имени")

    assert archived.profile.state is ProfileState.ARCHIVED
    assert service.list_profiles() == []
    assert service.list_profiles(state=ProfileState.ARCHIVED)[0].id == "alex"

    restored = service.restore_profile(profile_id="alex", actor_id="admin", reason="подтверждено")

    assert restored.profile.state is ProfileState.ACTIVE
    assert service.require_active_profile("alex").id == "alex"


def test_explicit_sample_requires_local_group_or_interval(tmp_path: Path) -> None:
    service = prepared_service(tmp_path)
    service.create_profile(profile_id="alex", display_name="Александр", actor_id="user-a")

    with pytest.raises(ValueError, match="группа или diarization-интервал"):
        service.add_explicit_sample(
            explicit_confirm=True,
            sample_id="sample-1",
            operation_id="operation-1",
            profile_id="alex",
            voice_pack_id="voice-pack-1",
            protected_artifact_id="artifact-1",
            artifact_content_sha256="a" * 64,
            protected_path="protected/sample-1.wav",
            quality={"score": 0.9},
            actor_id="user-a",
        )


def prepared_service(tmp_path: Path) -> VoiceDirectoryService:
    repository = VoiceDirectoryRepository(tmp_path / "next.sqlite3")
    repository.migrate()
    return VoiceDirectoryService(repository)
