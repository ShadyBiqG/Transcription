from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from local_voice_transcriber.voice.repository import (
    IndexGenerationState,
    IndexOperationState,
    ProfileState,
    SampleState,
    VoiceDirectoryRepository,
)


def test_shared_projection_has_no_source_or_vector_identifiers(tmp_path: Path) -> None:
    repository = prepared_repository(tmp_path)
    repository.create_profile(
        profile_id="alex",
        display_name="  Александр  ",
        created_by="owner-a",
        aliases=("Саша", "Алекс"),
    )
    add_pending_sample(repository)

    profile = repository.get_shared_profile("alex")

    assert profile.display_name == "Александр"
    assert profile.aliases == ("Алекс", "Саша")
    assert profile.active_sample_count == 0
    assert set(profile.__dataclass_fields__) == {
        "id",
        "display_name",
        "aliases",
        "state",
        "active_sample_count",
        "created_at",
        "archived_at",
    }
    with sqlite3.connect(tmp_path / "next.sqlite3") as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(next_voice_samples)").fetchall()
        }
    assert "embedding" not in columns
    assert "vector" not in columns


def test_aliases_are_global_and_profile_search_is_safe(tmp_path: Path) -> None:
    repository = prepared_repository(tmp_path)
    repository.create_profile(
        profile_id="alex", display_name="Александр", created_by="owner-a", aliases=("Саша",)
    )
    repository.create_profile(profile_id="boris", display_name="Борис", created_by="owner-b")

    assert [profile.id for profile in repository.list_shared_profiles(query="саш")] == ["alex"]
    with pytest.raises(sqlite3.IntegrityError):
        repository.add_alias(profile_id="boris", alias="САША", actor_id="owner-b")


def test_sample_outbox_moves_through_immutable_generation_and_becomes_active(
    tmp_path: Path,
) -> None:
    repository = prepared_repository(tmp_path)
    repository.create_profile(profile_id="alex", display_name="Александр", created_by="owner-a")
    enrollment = add_pending_sample(repository)
    manifest_hash = "a" * 64
    pack_hash = "b" * 64

    generation = repository.create_generation(
        generation_id="gen-1",
        voice_pack_id="voice-pack-1",
        generation_no=1,
        directory="generations/gen-1.zdb",
        manifest_sha256=manifest_hash,
        embedding_dimension=192,
        space="cosine",
        pack_sha256=pack_hash,
        vector_count=1,
    )
    repository.mark_operations_index_saved(
        generation_id=generation.id, operation_ids=(enrollment.operation_id,)
    )
    repository.validate_generation(
        generation_id=generation.id, operation_ids=(enrollment.operation_id,)
    )
    active = repository.activate_generation(
        generation_id=generation.id, operation_ids=(enrollment.operation_id,)
    )

    assert active.state is IndexGenerationState.ACTIVE
    assert repository.active_generation("voice-pack-1") == active
    assert repository.get_shared_profile("alex").active_sample_count == 1
    operation = repository.list_reconcilable_operations(voice_pack_id="voice-pack-1")
    assert operation == []
    with sqlite3.connect(tmp_path / "next.sqlite3") as connection:
        row = connection.execute(
            "SELECT state FROM next_voice_index_operations WHERE id = ?", (enrollment.operation_id,)
        ).fetchone()
    assert row == (IndexOperationState.COMMITTED.value,)


def test_archive_excludes_profile_immediately_and_restore_enqueues_new_generation_work(
    tmp_path: Path,
) -> None:
    repository = prepared_repository(tmp_path)
    repository.create_profile(profile_id="alex", display_name="Александр", created_by="owner-a")
    enrollment = add_pending_sample(repository)
    activate_sample(repository, enrollment.operation_id)

    archived = repository.archive_profile(profile_id="alex", actor_id="owner-b", reason="ошибка")

    assert archived.profile.state is ProfileState.ARCHIVED
    assert repository.list_shared_profiles() == []
    assert len(archived.operations) == 1
    assert archived.operations[0].operation_type == "archive"
    assert archived.operations[0].state is IndexOperationState.PENDING
    assert repository.list_samples_for_index(voice_pack_id="voice-pack-1") == []

    restored = repository.restore_profile(profile_id="alex", actor_id="admin", reason="проверено")

    assert restored.profile.state is ProfileState.ACTIVE
    assert restored.profile.active_sample_count == 1
    assert [operation.operation_type for operation in restored.operations] == ["restore"]
    assert [operation.operation_type for operation in repository.list_reconcilable_operations(
        voice_pack_id="voice-pack-1"
    )] == ["archive", "restore"]


def test_pending_sample_stays_pending_after_archive_restore(tmp_path: Path) -> None:
    repository = prepared_repository(tmp_path)
    repository.create_profile(profile_id="alex", display_name="Александр", created_by="owner-a")
    add_pending_sample(repository)

    archived = repository.archive_profile(profile_id="alex", actor_id="owner-a", reason="пауза")
    restored = repository.restore_profile(profile_id="alex", actor_id="admin", reason="возврат")

    assert archived.operations == ()
    assert restored.operations == ()
    pending = repository.list_samples_for_index(voice_pack_id="voice-pack-1")
    assert len(pending) == 1
    assert pending[0].state is SampleState.PENDING


def test_generation_rejects_out_of_order_outbox_transition(tmp_path: Path) -> None:
    repository = prepared_repository(tmp_path)
    repository.create_profile(profile_id="alex", display_name="Александр", created_by="owner-a")
    enrollment = add_pending_sample(repository)
    generation = repository.create_generation(
        generation_id="gen-1",
        voice_pack_id="voice-pack-1",
        generation_no=1,
        directory="generations/gen-1.zdb",
        manifest_sha256="a" * 64,
        embedding_dimension=192,
        space="cosine",
        pack_sha256="b" * 64,
        vector_count=1,
    )

    with pytest.raises(RuntimeError, match="порядок"):
        repository.validate_generation(
            generation_id=generation.id, operation_ids=(enrollment.operation_id,)
        )


def prepared_repository(tmp_path: Path) -> VoiceDirectoryRepository:
    repository = VoiceDirectoryRepository(tmp_path / "next.sqlite3")
    repository.migrate()
    return repository


def add_pending_sample(repository: VoiceDirectoryRepository):  # type: ignore[no-untyped-def]
    return repository.create_explicit_sample(
        sample_id="sample-1",
        profile_id="alex",
        voice_pack_id="voice-pack-1",
        protected_artifact_id="artifact-1",
        artifact_content_sha256="c" * 64,
        protected_path="protected/voice/artifact-1.wav",
        quality={"score": 0.94},
        created_by="owner-a",
        operation_id="op-upsert-1",
        source_group_id="group-1",
    )


def activate_sample(repository: VoiceDirectoryRepository, operation_id: str) -> None:
    generation = repository.create_generation(
        generation_id="gen-1",
        voice_pack_id="voice-pack-1",
        generation_no=1,
        directory="generations/gen-1.zdb",
        manifest_sha256="a" * 64,
        embedding_dimension=192,
        space="cosine",
        pack_sha256="b" * 64,
        vector_count=1,
    )
    repository.mark_operations_index_saved(
        generation_id=generation.id, operation_ids=(operation_id,)
    )
    repository.validate_generation(generation_id=generation.id, operation_ids=(operation_id,))
    repository.activate_generation(generation_id=generation.id, operation_ids=(operation_id,))
