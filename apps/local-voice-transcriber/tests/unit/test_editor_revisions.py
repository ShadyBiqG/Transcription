from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from local_voice_transcriber.cleaning import CleanableSegment, CleanDraftCandidate
from local_voice_transcriber.editor import (
    BaselineAlreadyExistsError,
    EditorRepository,
    GroupSnapshot,
    GroupUpdate,
    InvalidGroupOperationError,
    RevisionConflictError,
    RevisionOrigin,
    SourceSegmentSnapshot,
)


def test_baseline_is_immutable_and_same_retry_is_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    baseline = _baseline(repository)

    same = _baseline(repository)
    current = repository.get_current_revision(owner_id="owner-a", job_id="job-a")

    assert same.info.id == baseline.info.id
    assert current.info.number == 1
    assert current.segments[1].speaker_group_ids == ("group-a", "group-b")
    assert current.segments[1].requires_review is True
    with pytest.raises(BaselineAlreadyExistsError):
        repository.create_baseline(
            owner_id="owner-a",
            job_id="job-a",
            job_run_id="run-a",
            groups=_groups(),
            source_segments=[SourceSegmentSnapshot("source-1", 0, "Другой текст")],
        )


def test_manual_text_and_group_edit_create_new_snapshot(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    baseline = _baseline(repository)

    edited = repository.create_manual_revision(
        owner_id="owner-a",
        job_id="job-a",
        expected_current_revision=baseline.info.number,
        segment_texts={"source-1": "Исправленный текст"},
        group_updates={
            "group-a": GroupUpdate(
                label_snapshot="Александр",
                profile_id="profile-alex",
                assignment_status="confirmed",
            )
        },
        created_by="owner-a",
        comment="Исправлена подпись",
    )

    assert edited.info.number == 2
    assert edited.info.parent_id == baseline.info.id
    assert edited.info.origin is RevisionOrigin.MANUAL
    assert edited.segments[0].text == "Исправленный текст"
    assert edited.groups[0].label_snapshot == "Александр"
    assert edited.groups[0].profile_id == "profile-alex"
    assert baseline.segments[0].text == "Привет"
    assert baseline.groups[0].label_snapshot == "SPEAKER_00"


def test_stale_expected_revision_returns_optimistic_conflict(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    baseline = _baseline(repository)
    repository.create_manual_revision(
        owner_id="owner-a",
        job_id="job-a",
        expected_current_revision=baseline.info.number,
        segment_texts={"source-1": "Первая правка"},
    )

    with pytest.raises(RevisionConflictError) as error:
        repository.create_manual_revision(
            owner_id="owner-a",
            job_id="job-a",
            expected_current_revision=baseline.info.number,
            segment_texts={"source-2": "Запоздалая правка"},
        )

    assert error.value.expected_number == 1
    assert error.value.current_number == 2
    current = repository.get_current_revision(owner_id="owner-a", job_id="job-a")
    assert current.segments[1].text == "Речь"


def test_restore_creates_new_snapshot_without_mutating_history(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    baseline = _baseline(repository)
    edited = repository.create_manual_revision(
        owner_id="owner-a",
        job_id="job-a",
        expected_current_revision=1,
        segment_texts={"source-1": "Новая формулировка"},
    )

    restored = repository.restore_revision(
        owner_id="owner-a",
        job_id="job-a",
        revision_id=baseline.info.id,
        expected_current_revision=edited.info.number,
        created_by="owner-a",
    )

    assert restored.info.number == 3
    assert restored.info.origin is RevisionOrigin.RESTORE
    assert restored.info.restored_from_id == baseline.info.id
    assert restored.segments[0].text == "Привет"
    edited_revision = repository.get_revision(
        owner_id="owner-a", job_id="job-a", revision_id=edited.info.id
    )
    assert edited_revision.segments[0].text == "Новая формулировка"


def test_merge_and_unmerge_are_reversible_without_mutating_raw_group_ids(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    baseline = _baseline(repository)

    merged = repository.merge_groups(
        owner_id="owner-a",
        job_id="job-a",
        source_group_id="group-b",
        target_group_id="group-a",
        expected_current_revision=baseline.info.number,
        created_by="owner-a",
    )
    source_group = next(group for group in merged.groups if group.speaker_group_id == "group-b")

    assert source_group.merged_into_group_id == "group-a"
    assert merged.segments[1].primary_group_id == "group-b"
    assert merged.effective_primary_group_id(merged.segments[1]) == "group-a"
    assert baseline.effective_primary_group_id(baseline.segments[1]) == "group-b"

    unmerged = repository.unmerge_group(
        owner_id="owner-a",
        job_id="job-a",
        source_group_id="group-b",
        expected_current_revision=merged.info.number,
        created_by="owner-a",
    )
    operations = repository.list_merge_operations(owner_id="owner-a", job_id="job-a")

    assert unmerged.info.number == 3
    assert unmerged.effective_primary_group_id(unmerged.segments[1]) == "group-b"
    assert [operation.operation for operation in operations] == ["merge", "unmerge"]
    assert operations[0].reversed_by == operations[1].id
    assert operations[1].reverses_operation_id == operations[0].id
    with pytest.raises(InvalidGroupOperationError):
        repository.unmerge_group(
            owner_id="owner-a",
            job_id="job-a",
            source_group_id="group-b",
            expected_current_revision=unmerged.info.number,
        )


def test_owner_scoping_hides_other_users_revisions_and_operations(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    baseline = _baseline(repository)
    repository.merge_groups(
        owner_id="owner-a",
        job_id="job-a",
        source_group_id="group-b",
        target_group_id="group-a",
        expected_current_revision=baseline.info.number,
    )

    with pytest.raises(KeyError):
        repository.get_current_revision(owner_id="owner-b", job_id="job-a")
    with pytest.raises(KeyError):
        repository.get_revision(
            owner_id="owner-b", job_id="job-a", revision_id=baseline.info.id
        )
    with pytest.raises(KeyError):
        repository.list_revisions(owner_id="owner-b", job_id="job-a")
    with pytest.raises(KeyError):
        repository.list_merge_operations(owner_id="owner-b", job_id="job-a")


def test_rejects_unknown_snapshot_references_and_actor_other_than_owner(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _baseline(repository)

    with pytest.raises(KeyError):
        repository.create_manual_revision(
            owner_id="owner-a",
            job_id="job-a",
            expected_current_revision=1,
            segment_texts={"unknown": "Текст"},
        )
    with pytest.raises(KeyError):
        repository.create_manual_revision(
            owner_id="owner-a",
            job_id="job-a",
            expected_current_revision=1,
            group_updates={
                "unknown": GroupUpdate("Неизвестный", None, "unknown"),
            },
        )
    with pytest.raises(ValueError, match="Создатель"):
        repository.create_manual_revision(
            owner_id="owner-a",
            job_id="job-a",
            expected_current_revision=1,
            created_by="owner-b",
        )


def test_clean_draft_is_a_new_revision_and_preserves_immutable_baseline(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    baseline = _cleanable_baseline(repository)

    result = repository.create_clean_draft(
        owner_id="owner-a",
        job_id="clean-job",
        cleaner=FakeCleaner({"clean-segment": "Речь"}),
    )

    assert result.info.status.value == "applied"
    assert result.revision.info.number == 2
    assert result.revision.info.origin.value == "automatic"
    assert result.revision.segments[0].text == "Речь"
    raw = repository.get_baseline_revision(owner_id="owner-a", job_id="clean-job")
    assert raw.info.id == baseline.info.id
    assert raw.segments[0].text == "Эм, Речь"


def test_clean_draft_falls_back_to_baseline_when_candidate_is_not_deletion_only(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    baseline = _baseline(repository)

    result = repository.create_clean_draft(
        owner_id="owner-a",
        job_id="job-a",
        cleaner=FakeCleaner({"source-1": "Новый текст"}),
    )

    assert result.info.status.value == "failed"
    assert result.revision.info.id == baseline.info.id
    current = repository.get_current_revision(owner_id="owner-a", job_id="job-a")
    assert current.info.id == baseline.info.id


def test_cleaner_does_not_hold_immediate_transaction_while_it_runs(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _cleanable_baseline(repository)

    result = repository.create_clean_draft(
        owner_id="owner-a",
        job_id="clean-job",
        cleaner=TransactionProbeCleaner(tmp_path / "next.sqlite3"),
    )

    assert result.info.status.value == "applied"
    assert result.revision.info.number == 2


def test_segment_speaker_change_only_accepts_an_unmerged_local_group(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    baseline = _baseline(repository)
    merged = repository.merge_groups(
        owner_id="owner-a",
        job_id="job-a",
        source_group_id="group-b",
        target_group_id="group-a",
        expected_current_revision=baseline.info.number,
    )

    with pytest.raises(InvalidGroupOperationError):
        repository.assign_segment_speaker(
            owner_id="owner-a",
            job_id="job-a",
            source_segment_id="source-1",
            speaker_group_id="group-b",
            expected_current_revision=merged.info.number,
        )

    changed = repository.assign_segment_speaker(
        owner_id="owner-a",
        job_id="job-a",
        source_segment_id="source-2",
        speaker_group_id="group-a",
        expected_current_revision=merged.info.number,
    )
    assert changed.info.number == 3
    assert changed.segments[1].primary_group_id == "group-a"
    assert repository.get_baseline_revision(owner_id="owner-a", job_id="job-a").segments[
        1
    ].primary_group_id == "group-b"


def _repository(tmp_path: Path) -> EditorRepository:
    repository = EditorRepository(tmp_path / "next.sqlite3")
    repository.migrate()
    return repository


def _baseline(repository: EditorRepository):
    return repository.create_baseline(
        owner_id="owner-a",
        job_id="job-a",
        job_run_id="run-a",
        groups=_groups(),
        source_segments=[
            SourceSegmentSnapshot(
                source_segment_id="source-1",
                ordinal=0,
                text="Привет",
                primary_group_id="group-a",
                speaker_group_ids=("group-a",),
            ),
            SourceSegmentSnapshot(
                source_segment_id="source-2",
                ordinal=1,
                text="Речь",
                primary_group_id="group-b",
                speaker_group_ids=("group-a", "group-b"),
                requires_review=True,
            ),
        ],
        created_by="owner-a",
    )


def _cleanable_baseline(repository: EditorRepository):
    return repository.create_baseline(
        owner_id="owner-a",
        job_id="clean-job",
        job_run_id="clean-run",
        groups=_groups(),
        source_segments=[
            SourceSegmentSnapshot(
                source_segment_id="clean-segment",
                ordinal=0,
                text="Эм, Речь",
                primary_group_id="group-a",
                speaker_group_ids=("group-a",),
            )
        ],
        created_by="owner-a",
    )


def _groups() -> list[GroupSnapshot]:
    return [
        GroupSnapshot("group-a", "SPEAKER_00"),
        GroupSnapshot("group-b", "SPEAKER_01"),
    ]


class FakeCleaner:
    def __init__(self, changes: dict[str, str]) -> None:
        self._changes = changes

    def clean(self, segments: tuple[CleanableSegment, ...]) -> CleanDraftCandidate:
        del segments
        return CleanDraftCandidate(self._changes)


class TransactionProbeCleaner:
    """Проверяет, что во время внешней очистки SQLite не удерживает write-lock."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def clean(self, segments: tuple[CleanableSegment, ...]) -> CleanDraftCandidate:
        connection = sqlite3.connect(self._database_path, timeout=0.05)
        try:
            connection.execute("BEGIN IMMEDIATE")
        finally:
            connection.rollback()
            connection.close()
        return CleanDraftCandidate({"clean-segment": "Речь"})
