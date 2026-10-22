"""Проверки durable writer-а общих голосовых эмбеддингов."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from local_voice_transcriber.voice.index import (
    IndexManifest,
    InMemoryVoiceVectorIndex,
    VectorRecord,
    VectorSearchHit,
    VoiceVectorIndexError,
)
from local_voice_transcriber.voice.index_writer import (
    VoiceIndexWriter,
    VoiceIndexWriterError,
)
from local_voice_transcriber.voice.repository import (
    IndexOperationState,
    VoiceDirectoryRepository,
)


class FlakyVoiceIndex:
    """Локальный fake с одним временным сбоем сохранения."""

    def __init__(self) -> None:
        self._delegate = InMemoryVoiceVectorIndex()
        self.save_calls = 0
        self.fail_next_save = False

    def save_generation(
        self,
        *,
        generation_id: str,
        voice_pack_id: str,
        dimension: int,
        space: str,
        records: Iterable[VectorRecord],
    ) -> IndexManifest:
        self.save_calls += 1
        if self.fail_next_save:
            self.fail_next_save = False
            raise VoiceVectorIndexError("temporary local index fault")
        return self._delegate.save_generation(
            generation_id=generation_id,
            voice_pack_id=voice_pack_id,
            dimension=dimension,
            space=space,
            records=records,
        )

    def load_generation(self, generation_id: str) -> IndexManifest:
        return self._delegate.load_generation(generation_id)

    def search(
        self,
        *,
        generation_id: str,
        vector: Sequence[float],
        limit: int,
        active_profile_ids: set[str] | None = None,
    ) -> tuple[VectorSearchHit, ...]:
        return self._delegate.search(
            generation_id=generation_id,
            vector=vector,
            limit=limit,
            active_profile_ids=active_profile_ids,
        )

    def record_ids(self, generation_id: str) -> tuple[str, ...]:
        return self._delegate.record_ids(generation_id)


def test_writer_builds_full_snapshot_and_activates_explicit_sample(tmp_path: Path) -> None:
    repository, protected_root = prepared_repository(tmp_path)
    add_sample(repository, protected_root, sample_id="sample-a", profile_id="alex")
    index = FlakyVoiceIndex()
    extractor_calls: list[Path] = []
    writer = make_writer(
        repository,
        index,
        protected_root,
        extractor=lambda path: capture_vector(extractor_calls, path, (1.0, 0.0)),
    )

    result = writer.reconcile()

    assert result.did_work
    assert result.generation is not None
    assert result.generation.vector_count == 1
    assert result.generation.state.value == "active"
    assert extractor_calls == [(protected_root / "sample-a.wav").resolve()]
    assert index.record_ids(result.generation.id) == ("voice-sample:sample-a",)
    assert repository.list_reconcilable_operations(voice_pack_id="voice-pack-1") == []
    with sqlite3.connect(tmp_path / "next.sqlite3") as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(next_voice_samples)").fetchall()
        }
    assert "embedding" not in columns
    assert "vector" not in columns


def test_writer_retries_after_temporary_index_failure_without_terminalizing_outbox(
    tmp_path: Path,
) -> None:
    repository, protected_root = prepared_repository(tmp_path)
    enrollment = add_sample(repository, protected_root, sample_id="sample-a", profile_id="alex")
    index = FlakyVoiceIndex()
    index.fail_next_save = True
    writer = make_writer(repository, index, protected_root, extractor=lambda _path: (1.0, 0.0))

    with pytest.raises(VoiceIndexWriterError, match="можно повторить"):
        writer.reconcile()

    pending = repository.list_reconcilable_operations(voice_pack_id="voice-pack-1")
    assert [(item.id, item.state) for item in pending] == [
        (enrollment.operation_id, IndexOperationState.PENDING)
    ]
    assert repository.active_generation("voice-pack-1") is None

    completed = writer.reconcile()

    assert completed.generation is not None
    assert completed.generation.state.value == "active"
    assert index.save_calls == 2
    assert repository.list_reconcilable_operations(voice_pack_id="voice-pack-1") == []


def test_writer_resumes_after_index_save_before_sqlite_validation(tmp_path: Path) -> None:
    repository, protected_root = prepared_repository(tmp_path)
    enrollment = add_sample(repository, protected_root, sample_id="sample-a", profile_id="alex")
    index = FlakyVoiceIndex()
    writer = make_writer(repository, index, protected_root, extractor=lambda _path: (1.0, 0.0))

    # Имитация процесса, остановленного точно после atomic Zeus save. Outbox уже
    # указывает на проверяемое поколение и должен завершиться без нового save.
    generation_id = "crash-after-save"
    records = (
        VectorRecord("voice-sample:sample-a", "alex", "sample-a", (1.0, 0.0)),
    )
    manifest = index.save_generation(
        generation_id=generation_id,
        voice_pack_id="voice-pack-1",
        dimension=2,
        space="cosine",
        records=records,
    )
    generation = repository.create_generation(
        generation_id=generation_id,
        voice_pack_id="voice-pack-1",
        generation_no=1,
        directory=f"generations/{generation_id}.zdb",
        manifest_sha256=manifest.digest,
        embedding_dimension=2,
        space="cosine",
        pack_sha256="a" * 64,
        vector_count=1,
    )
    repository.mark_operations_index_saved(
        generation_id=generation.id,
        operation_ids=(enrollment.operation_id,),
    )

    result = writer.reconcile()

    assert result.generation is not None
    assert result.generation.id == generation_id
    assert result.generation.state.value == "active"
    assert index.save_calls == 1
    assert repository.list_reconcilable_operations(voice_pack_id="voice-pack-1") == []


def test_writer_does_not_pass_path_outside_protected_root_to_extractor(tmp_path: Path) -> None:
    repository, protected_root = prepared_repository(tmp_path)
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"private")
    repository.create_explicit_sample(
        sample_id="sample-a",
        profile_id="alex",
        voice_pack_id="voice-pack-1",
        protected_artifact_id="artifact-a",
        artifact_content_sha256="c" * 64,
        protected_path=str(outside),
        quality={"score": 1},
        created_by="owner",
        operation_id="op-a",
        source_group_id="group-a",
    )
    index = FlakyVoiceIndex()
    extractor_calls: list[Path] = []
    writer = make_writer(
        repository,
        index,
        protected_root,
        extractor=lambda path: capture_vector(extractor_calls, path, (1.0, 0.0)),
    )

    with pytest.raises(VoiceIndexWriterError, match="недоступен"):
        writer.reconcile()

    assert extractor_calls == []
    assert index.save_calls == 0
    assert repository.list_reconcilable_operations(voice_pack_id="voice-pack-1")[0].state is (
        IndexOperationState.PENDING
    )


def test_writer_rebuilds_active_and_pending_samples_in_one_snapshot(tmp_path: Path) -> None:
    repository, protected_root = prepared_repository(tmp_path)
    add_sample(repository, protected_root, sample_id="sample-a", profile_id="alex")
    index = FlakyVoiceIndex()
    writer = make_writer(repository, index, protected_root, extractor=lambda _path: (1.0, 0.0))
    first = writer.reconcile()
    assert first.generation is not None

    repository.create_profile(profile_id="boris", display_name="Борис", created_by="owner")
    add_sample(repository, protected_root, sample_id="sample-b", profile_id="boris")
    second = writer.reconcile()

    assert second.generation is not None
    assert second.generation.id != first.generation.id
    assert index.record_ids(second.generation.id) == (
        "voice-sample:sample-a",
        "voice-sample:sample-b",
    )


def test_writer_creates_new_generation_when_archive_restore_returns_same_records(
    tmp_path: Path,
) -> None:
    repository, protected_root = prepared_repository(tmp_path)
    add_sample(repository, protected_root, sample_id="sample-a", profile_id="alex")
    index = FlakyVoiceIndex()
    writer = make_writer(repository, index, protected_root, extractor=lambda _path: (1.0, 0.0))

    initial = writer.reconcile()
    assert initial.generation is not None
    repository.archive_profile(profile_id="alex", actor_id="admin", reason="проверка")
    archived = writer.reconcile()
    assert archived.generation is not None
    assert archived.generation.vector_count == 0
    repository.restore_profile(profile_id="alex", actor_id="admin", reason="возврат")
    restored = writer.reconcile()

    assert restored.generation is not None
    assert len({initial.generation.id, archived.generation.id, restored.generation.id}) == 3
    assert index.record_ids(restored.generation.id) == ("voice-sample:sample-a",)


def prepared_repository(tmp_path: Path) -> tuple[VoiceDirectoryRepository, Path]:
    repository = VoiceDirectoryRepository(tmp_path / "next.sqlite3")
    repository.migrate()
    protected_root = tmp_path / "protected"
    protected_root.mkdir()
    repository.create_profile(profile_id="alex", display_name="Александр", created_by="owner")
    return repository, protected_root


def add_sample(
    repository: VoiceDirectoryRepository,
    protected_root: Path,
    *,
    sample_id: str,
    profile_id: str,
) -> object:
    (protected_root / f"{sample_id}.wav").write_bytes(b"local-only audio")
    return repository.create_explicit_sample(
        sample_id=sample_id,
        profile_id=profile_id,
        voice_pack_id="voice-pack-1",
        protected_artifact_id=f"artifact-{sample_id}",
        artifact_content_sha256="c" * 64,
        protected_path=f"{sample_id}.wav",
        quality={"score": 1},
        created_by="owner",
        operation_id=f"op-{sample_id}",
        source_group_id="group-a",
    )


def make_writer(
    repository: VoiceDirectoryRepository,
    index: FlakyVoiceIndex,
    protected_root: Path,
    *,
    extractor: object,
) -> VoiceIndexWriter:
    assert callable(extractor)
    return VoiceIndexWriter(
        repository,
        index,
        voice_pack_id="voice-pack-1",
        voice_pack_sha256="a" * 64,
        embedding_dimension=2,
        protected_root=protected_root,
        extractor=extractor,
    )


def capture_vector(calls: list[Path], path: Path, vector: tuple[float, ...]) -> tuple[float, ...]:
    calls.append(path)
    return vector
