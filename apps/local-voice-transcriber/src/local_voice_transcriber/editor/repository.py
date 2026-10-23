"""SQLite repository для append-only ревизий редактора Transcription Next."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ..voice.repository import SharedVoiceProfile, VoiceDirectoryRepository
from .models import (
    GroupMergeOperation,
    GroupSnapshot,
    GroupUpdate,
    RevisionOrigin,
    RevisionSegment,
    SourceSegmentSnapshot,
    TranscriptRevision,
    TranscriptRevisionInfo,
)


class EditorRepositoryError(RuntimeError):
    """Базовая ошибка устойчивого editor domain."""


class RevisionConflictError(EditorRepositoryError):
    """Клиент пытался изменить неактуальную текущую ревизию."""

    def __init__(self, *, expected_number: int, current_number: int) -> None:
        self.expected_number = expected_number
        self.current_number = current_number
        super().__init__(
            f"Текущая ревизия изменилась: ожидалась {expected_number}, сейчас {current_number}"
        )


class BaselineAlreadyExistsError(EditorRepositoryError):
    """Для job уже опубликована отличающаяся baseline-ревизия."""


class InvalidGroupOperationError(EditorRepositoryError):
    """Merge или unmerge не соответствует текущему снимку."""


class EditorRepository:
    """Хранит только снимки редактирования, но не source ASR-данные.

    Каждый публичный lookup требует одновременно ``owner_id`` и ``job_id``.
    Это делает проверку владения частью repository, а не необязательной
    обязанностью будущего HTTP-слоя.
    """

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)

    def migrate(self) -> None:
        """Создаёт независимые таблицы append-only домена редактора."""

        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS next_editor_jobs (
                    job_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS next_transcript_revisions (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES next_editor_jobs(job_id) ON DELETE CASCADE,
                    owner_id TEXT NOT NULL,
                    job_run_id TEXT NOT NULL,
                    number INTEGER NOT NULL CHECK(number >= 1),
                    parent_id TEXT REFERENCES next_transcript_revisions(id),
                    origin TEXT NOT NULL CHECK(
                        origin IN ('baseline', 'automatic', 'manual', 'restore')
                    ),
                    created_by TEXT,
                    expected_parent_number INTEGER,
                    comment TEXT,
                    restored_from_id TEXT REFERENCES next_transcript_revisions(id),
                    baseline_digest TEXT,
                    is_current INTEGER NOT NULL CHECK(is_current IN (0, 1)),
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, number)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS next_transcript_revisions_current_idx
                    ON next_transcript_revisions(job_id) WHERE is_current = 1;
                CREATE INDEX IF NOT EXISTS next_transcript_revisions_owner_idx
                    ON next_transcript_revisions(job_id, owner_id, number DESC);

                CREATE TABLE IF NOT EXISTS next_revision_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    revision_id TEXT NOT NULL
                        REFERENCES next_transcript_revisions(id) ON DELETE CASCADE,
                    speaker_group_id TEXT NOT NULL,
                    profile_id TEXT,
                    label_snapshot TEXT NOT NULL,
                    assignment_status TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    merged_into_group_id TEXT,
                    UNIQUE(revision_id, speaker_group_id),
                    CHECK(merged_into_group_id IS NULL OR merged_into_group_id != speaker_group_id)
                );

                CREATE TABLE IF NOT EXISTS next_revision_segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    revision_id TEXT NOT NULL
                        REFERENCES next_transcript_revisions(id) ON DELETE CASCADE,
                    source_segment_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                    text TEXT NOT NULL,
                    primary_group_id TEXT,
                    requires_review INTEGER NOT NULL CHECK(requires_review IN (0, 1)),
                    UNIQUE(revision_id, source_segment_id),
                    UNIQUE(revision_id, ordinal)
                );

                CREATE TABLE IF NOT EXISTS next_revision_segment_speakers (
                    revision_segment_id INTEGER NOT NULL
                        REFERENCES next_revision_segments(id) ON DELETE CASCADE,
                    speaker_group_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('primary', 'secondary')),
                    PRIMARY KEY(revision_segment_id, speaker_group_id)
                );

                CREATE TABLE IF NOT EXISTS next_group_merge_operations (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES next_editor_jobs(job_id) ON DELETE CASCADE,
                    owner_id TEXT NOT NULL,
                    source_group_id TEXT NOT NULL,
                    target_group_id TEXT NOT NULL,
                    operation TEXT NOT NULL CHECK(operation IN ('merge', 'unmerge')),
                    revision_id TEXT NOT NULL REFERENCES next_transcript_revisions(id),
                    reverses_operation_id TEXT REFERENCES next_group_merge_operations(id),
                    reversed_by TEXT REFERENCES next_group_merge_operations(id),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    CHECK(source_group_id != target_group_id)
                );
                CREATE INDEX IF NOT EXISTS next_group_merge_operations_job_idx
                    ON next_group_merge_operations(job_id, owner_id, created_at, id);
                """
            )

    def create_baseline(
        self,
        *,
        owner_id: str,
        job_id: str,
        job_run_id: str,
        groups: Sequence[GroupSnapshot],
        source_segments: Sequence[SourceSegmentSnapshot],
        created_by: str | None = None,
        comment: str | None = None,
    ) -> TranscriptRevision:
        """Публикует baseline один раз; одинаковый retry возвращает её же."""

        _required(owner_id, "Владелец")
        _required(job_id, "Задание")
        _required(job_run_id, "Запуск задания")
        if created_by is not None and created_by != owner_id:
            raise ValueError("Создатель ревизии должен быть владельцем задания")
        normalized_groups = _validate_groups(groups)
        normalized_segments = _validate_segments(source_segments, normalized_groups)
        digest = _baseline_digest(job_run_id, normalized_groups, normalized_segments)
        with self._transaction() as connection:
            existing_owner = connection.execute(
                "SELECT owner_id FROM next_editor_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing_owner is not None and existing_owner["owner_id"] != owner_id:
                raise KeyError("Задание не найдено")
            if existing_owner is None:
                connection.execute(
                    "INSERT INTO next_editor_jobs(job_id, owner_id, created_at) VALUES (?, ?, ?)",
                    (job_id, owner_id, _utc_now()),
                )
            existing = connection.execute(
                """
                SELECT id, baseline_digest FROM next_transcript_revisions
                WHERE job_id = ? AND owner_id = ? AND origin = 'baseline'
                """,
                (job_id, owner_id),
            ).fetchone()
            if existing is not None:
                if existing["baseline_digest"] == digest:
                    return self._load_revision(connection, str(existing["id"]))
                raise BaselineAlreadyExistsError(
                    "Исходный результат уже имеет другую baseline-ревизию"
                )
            revision_id = str(uuid4())
            now = _utc_now()
            connection.execute(
                """
                INSERT INTO next_transcript_revisions(
                    id, job_id, owner_id, job_run_id, number, parent_id, origin, created_by,
                    expected_parent_number, comment, restored_from_id, baseline_digest, is_current,
                    created_at
                ) VALUES (?, ?, ?, ?, 1, NULL, 'baseline', ?, NULL, ?, NULL, ?, 1, ?)
                """,
                (revision_id, job_id, owner_id, job_run_id, created_by, comment, digest, now),
            )
            self._insert_snapshot(connection, revision_id, normalized_groups, normalized_segments)
            return self._load_revision(connection, revision_id)

    def get_current_revision(self, *, owner_id: str, job_id: str) -> TranscriptRevision:
        """Возвращает единственную текущую ревизию принадлежащего пользователю job."""

        with self._connection() as connection:
            row = self._current_row(connection, owner_id=owner_id, job_id=job_id)
            return self._load_revision(connection, str(row["id"]))

    def get_revision(
        self, *, owner_id: str, job_id: str, revision_id: str
    ) -> TranscriptRevision:
        """Возвращает конкретную ревизию только внутри owner-scoped job."""

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM next_transcript_revisions
                WHERE id = ? AND job_id = ? AND owner_id = ?
                """,
                (revision_id, job_id, owner_id),
            ).fetchone()
            if row is None:
                raise KeyError("Ревизия не найдена")
            return self._load_revision(connection, revision_id)

    def list_revisions(self, *, owner_id: str, job_id: str) -> list[TranscriptRevisionInfo]:
        """Возвращает историю без чужих записей и без скрытого содержимого."""

        with self._connection() as connection:
            self._ensure_owned_job(connection, owner_id=owner_id, job_id=job_id)
            rows = connection.execute(
                """
                SELECT * FROM next_transcript_revisions
                WHERE job_id = ? AND owner_id = ? ORDER BY number DESC
                """,
                (job_id, owner_id),
            ).fetchall()
        return [_to_revision_info(row) for row in rows]

    def create_manual_revision(
        self,
        *,
        owner_id: str,
        job_id: str,
        expected_current_revision: int,
        segment_texts: Mapping[str, str] | None = None,
        group_updates: Mapping[str, GroupUpdate] | None = None,
        created_by: str | None = None,
        comment: str | None = None,
    ) -> TranscriptRevision:
        """Сохраняет текстовые и групповые правки как новую current revision."""

        self._validate_actor(owner_id, created_by)
        requested_texts = dict(segment_texts or {})
        requested_groups = dict(group_updates or {})
        with self._transaction() as connection:
            current = self._require_current(
                connection,
                owner_id=owner_id,
                job_id=job_id,
                expected_current_revision=expected_current_revision,
            )
            segments = list(current.segments)
            groups = list(current.groups)
            known_segment_ids = {segment.source_segment_id for segment in segments}
            unknown_segments = set(requested_texts).difference(known_segment_ids)
            if unknown_segments:
                raise KeyError("Изменяемый исходный отрезок не найден")
            known_group_ids = {group.speaker_group_id for group in groups}
            unknown_groups = set(requested_groups).difference(known_group_ids)
            if unknown_groups:
                raise KeyError("Изменяемая группа не найдена")
            segments = [
                RevisionSegment(
                    source_segment_id=segment.source_segment_id,
                    ordinal=segment.ordinal,
                    text=requested_texts.get(segment.source_segment_id, segment.text),
                    primary_group_id=segment.primary_group_id,
                    speaker_group_ids=segment.speaker_group_ids,
                    requires_review=segment.requires_review,
                )
                for segment in segments
            ]
            groups = [
                _replace_group(group, requested_groups.get(group.speaker_group_id))
                for group in groups
            ]
            return self._append_revision(
                connection,
                current=current,
                origin=RevisionOrigin.MANUAL,
                created_by=created_by,
                comment=comment,
                groups=groups,
                segments=segments,
            )

    def assign_speaker_group_identity(
        self,
        *,
        owner_id: str,
        job_id: str,
        speaker_group_id: str,
        display_name: str,
        expected_current_revision: int,
        created_by: str,
        voice_directory_repository: VoiceDirectoryRepository,
    ) -> tuple[SharedVoiceProfile, TranscriptRevision]:
        """Атомарно связывает группу с общей карточкой и новой ревизией.

        Проверка optimistic concurrency выполняется до поиска или создания
        карточки. Поэтому устаревший запрос не оставляет общую карточку без
        связанной ревизии, а любая ошибка записи снимка откатывает и карточку.
        """

        self._validate_actor(owner_id, created_by)
        with self._transaction() as connection:
            current = self._require_current(
                connection,
                owner_id=owner_id,
                job_id=job_id,
                expected_current_revision=expected_current_revision,
            )
            group = next(
                (item for item in current.groups if item.speaker_group_id == speaker_group_id),
                None,
            )
            if group is None:
                raise KeyError("Группа говорящего не найдена")
            if group.merged_into_group_id is not None:
                raise InvalidGroupOperationError("Назначьте подпись итоговой группе")
            speaker = voice_directory_repository.resolve_or_create_active_speaker_in_transaction(
                connection,
                profile_id=str(uuid4()),
                display_name=display_name,
                created_by=created_by,
            )
            revision = self._append_revision(
                connection,
                current=current,
                origin=RevisionOrigin.MANUAL,
                created_by=created_by,
                comment="Назначена подпись говорящего",
                groups=[
                    _replace_group(
                        item,
                        GroupUpdate(
                            label_snapshot=speaker.display_name,
                            profile_id=speaker.id,
                            assignment_status="confirmed",
                            provenance="manual",
                        ),
                    )
                    if item.speaker_group_id == speaker_group_id
                    else item
                    for item in current.groups
                ],
                segments=current.segments,
            )
            return speaker, revision

    def restore_revision(
        self,
        *,
        owner_id: str,
        job_id: str,
        revision_id: str,
        expected_current_revision: int,
        created_by: str | None = None,
        comment: str | None = None,
    ) -> TranscriptRevision:
        """Клонирует исторический снимок в новую current revision, ничего не перезаписывая."""

        self._validate_actor(owner_id, created_by)
        with self._transaction() as connection:
            current = self._require_current(
                connection,
                owner_id=owner_id,
                job_id=job_id,
                expected_current_revision=expected_current_revision,
            )
            target = self._load_owned_revision(
                connection, owner_id=owner_id, job_id=job_id, revision_id=revision_id
            )
            return self._append_revision(
                connection,
                current=current,
                origin=RevisionOrigin.RESTORE,
                created_by=created_by,
                comment=comment or f"Восстановлена ревизия {target.info.number}",
                groups=target.groups,
                segments=target.segments,
                restored_from_id=target.info.id,
            )

    def merge_groups(
        self,
        *,
        owner_id: str,
        job_id: str,
        source_group_id: str,
        target_group_id: str,
        expected_current_revision: int,
        created_by: str | None = None,
        comment: str | None = None,
    ) -> TranscriptRevision:
        """Объединяет две локальные группы только в новом редакторском снимке."""

        self._validate_actor(owner_id, created_by)
        if source_group_id == target_group_id:
            raise InvalidGroupOperationError("Нельзя объединить группу с самой собой")
        with self._transaction() as connection:
            current = self._require_current(
                connection,
                owner_id=owner_id,
                job_id=job_id,
                expected_current_revision=expected_current_revision,
            )
            groups_by_id = {group.speaker_group_id: group for group in current.groups}
            source = groups_by_id.get(source_group_id)
            target = groups_by_id.get(target_group_id)
            if source is None or target is None:
                raise KeyError("Группа не найдена")
            if source.merged_into_group_id is not None:
                raise InvalidGroupOperationError("Исходная группа уже объединена")
            if target.merged_into_group_id is not None:
                raise InvalidGroupOperationError("Целевая группа уже объединена")
            groups = [
                GroupSnapshot(
                    speaker_group_id=group.speaker_group_id,
                    label_snapshot=group.label_snapshot,
                    profile_id=group.profile_id,
                    assignment_status=group.assignment_status,
                    provenance=group.provenance,
                    merged_into_group_id=(
                        target_group_id
                        if group.speaker_group_id == source_group_id
                        else group.merged_into_group_id
                    ),
                )
                for group in current.groups
            ]
            revision = self._append_revision(
                connection,
                current=current,
                origin=RevisionOrigin.MANUAL,
                created_by=created_by,
                comment=comment or f"Объединены группы {source_group_id} и {target_group_id}",
                groups=groups,
                segments=current.segments,
            )
            self._insert_merge_operation(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                source_group_id=source_group_id,
                target_group_id=target_group_id,
                operation="merge",
                revision_id=revision.info.id,
                created_by=created_by or owner_id,
            )
            return revision

    def unmerge_group(
        self,
        *,
        owner_id: str,
        job_id: str,
        source_group_id: str,
        expected_current_revision: int,
        created_by: str | None = None,
        comment: str | None = None,
    ) -> TranscriptRevision:
        """Отменяет видимое объединение через ещё одну неизменяемую ревизию."""

        self._validate_actor(owner_id, created_by)
        with self._transaction() as connection:
            current = self._require_current(
                connection,
                owner_id=owner_id,
                job_id=job_id,
                expected_current_revision=expected_current_revision,
            )
            groups_by_id = {group.speaker_group_id: group for group in current.groups}
            source = groups_by_id.get(source_group_id)
            if source is None:
                raise KeyError("Группа не найдена")
            target_group_id = source.merged_into_group_id
            if target_group_id is None:
                raise InvalidGroupOperationError("Группа не находится в объединении")
            groups = [
                GroupSnapshot(
                    speaker_group_id=group.speaker_group_id,
                    label_snapshot=group.label_snapshot,
                    profile_id=group.profile_id,
                    assignment_status=group.assignment_status,
                    provenance=group.provenance,
                    merged_into_group_id=(
                        None
                        if group.speaker_group_id == source_group_id
                        else group.merged_into_group_id
                    ),
                )
                for group in current.groups
            ]
            revision = self._append_revision(
                connection,
                current=current,
                origin=RevisionOrigin.MANUAL,
                created_by=created_by,
                comment=comment or f"Разъединена группа {source_group_id}",
                groups=groups,
                segments=current.segments,
            )
            previous = connection.execute(
                """
                SELECT operation.id FROM next_group_merge_operations AS operation
                JOIN next_transcript_revisions AS revision ON revision.id = operation.revision_id
                WHERE operation.job_id = ? AND operation.owner_id = ?
                  AND operation.source_group_id = ? AND operation.target_group_id = ?
                  AND operation.operation = 'merge' AND operation.reversed_by IS NULL
                ORDER BY revision.number DESC, operation.id DESC LIMIT 1
                """,
                (job_id, owner_id, source_group_id, target_group_id),
            ).fetchone()
            operation_id = self._insert_merge_operation(
                connection,
                job_id=job_id,
                owner_id=owner_id,
                source_group_id=source_group_id,
                target_group_id=target_group_id,
                operation="unmerge",
                revision_id=revision.info.id,
                reverses_operation_id=str(previous["id"]) if previous else None,
                created_by=created_by or owner_id,
            )
            if previous is not None:
                connection.execute(
                    "UPDATE next_group_merge_operations SET reversed_by = ? WHERE id = ?",
                    (operation_id, previous["id"]),
                )
            return revision

    def list_merge_operations(self, *, owner_id: str, job_id: str) -> list[GroupMergeOperation]:
        """Возвращает append-only журнал merge/unmerge только владельцу job."""

        with self._connection() as connection:
            self._ensure_owned_job(connection, owner_id=owner_id, job_id=job_id)
            rows = connection.execute(
                """
                SELECT operation.* FROM next_group_merge_operations AS operation
                JOIN next_transcript_revisions AS revision ON revision.id = operation.revision_id
                WHERE operation.job_id = ? AND operation.owner_id = ?
                ORDER BY revision.number, operation.id
                """,
                (job_id, owner_id),
            ).fetchall()
        return [_to_merge_operation(row) for row in rows]

    def _append_revision(
        self,
        connection: sqlite3.Connection,
        *,
        current: TranscriptRevision,
        origin: RevisionOrigin,
        created_by: str | None,
        comment: str | None,
        groups: Sequence[GroupSnapshot],
        segments: Sequence[RevisionSegment],
        restored_from_id: str | None = None,
    ) -> TranscriptRevision:
        groups = _validate_groups(groups)
        segments = _validate_revision_segments(segments, groups)
        revision_id = str(uuid4())
        now = _utc_now()
        connection.execute(
            "UPDATE next_transcript_revisions SET is_current = 0 WHERE id = ?",
            (current.info.id,),
        )
        connection.execute(
            """
            INSERT INTO next_transcript_revisions(
                id, job_id, owner_id, job_run_id, number, parent_id, origin, created_by,
                expected_parent_number, comment, restored_from_id, baseline_digest, is_current,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?)
            """,
            (
                revision_id,
                current.info.job_id,
                self._owner_for_current(connection, current.info.id),
                current.info.job_run_id,
                current.info.number + 1,
                current.info.id,
                origin.value,
                created_by,
                current.info.number,
                comment,
                restored_from_id,
                now,
            ),
        )
        self._insert_snapshot(connection, revision_id, groups, segments)
        return self._load_revision(connection, revision_id)

    @staticmethod
    def _insert_snapshot(
        connection: sqlite3.Connection,
        revision_id: str,
        groups: Sequence[GroupSnapshot],
        segments: Sequence[SourceSegmentSnapshot | RevisionSegment],
    ) -> None:
        connection.executemany(
            """
            INSERT INTO next_revision_groups(
                revision_id, speaker_group_id, profile_id, label_snapshot, assignment_status,
                provenance, merged_into_group_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    revision_id,
                    group.speaker_group_id,
                    group.profile_id,
                    group.label_snapshot,
                    group.assignment_status,
                    group.provenance,
                    group.merged_into_group_id,
                )
                for group in groups
            ],
        )
        for segment in segments:
            cursor = connection.execute(
                """
                INSERT INTO next_revision_segments(
                    revision_id, source_segment_id, ordinal, text, primary_group_id, requires_review
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    segment.source_segment_id,
                    segment.ordinal,
                    segment.text,
                    segment.primary_group_id,
                    int(segment.requires_review),
                ),
            )
            segment_id = cursor.lastrowid
            assert segment_id is not None
            connection.executemany(
                """
                INSERT INTO next_revision_segment_speakers(
                    revision_segment_id, speaker_group_id, role
                ) VALUES (?, ?, ?)
                """,
                [
                    (
                        segment_id,
                        group_id,
                        "primary" if group_id == segment.primary_group_id else "secondary",
                    )
                    for group_id in segment.speaker_group_ids
                ],
            )

    def _require_current(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        job_id: str,
        expected_current_revision: int,
    ) -> TranscriptRevision:
        row = self._current_row(connection, owner_id=owner_id, job_id=job_id)
        current_number = int(row["number"])
        if expected_current_revision != current_number:
            raise RevisionConflictError(
                expected_number=expected_current_revision, current_number=current_number
            )
        return self._load_revision(connection, str(row["id"]))

    def _current_row(
        self, connection: sqlite3.Connection, *, owner_id: str, job_id: str
    ) -> sqlite3.Row:
        self._ensure_owned_job(connection, owner_id=owner_id, job_id=job_id)
        row = connection.execute(
            """
            SELECT * FROM next_transcript_revisions
            WHERE job_id = ? AND owner_id = ? AND is_current = 1
            """,
            (job_id, owner_id),
        ).fetchone()
        if row is None:
            raise KeyError("Текущая ревизия не найдена")
        return row

    @staticmethod
    def _ensure_owned_job(
        connection: sqlite3.Connection, *, owner_id: str, job_id: str
    ) -> None:
        row = connection.execute(
            "SELECT 1 FROM next_editor_jobs WHERE job_id = ? AND owner_id = ?",
            (job_id, owner_id),
        ).fetchone()
        if row is None:
            raise KeyError("Задание не найдено")

    def _load_owned_revision(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        job_id: str,
        revision_id: str,
    ) -> TranscriptRevision:
        row = connection.execute(
            """
            SELECT id FROM next_transcript_revisions
            WHERE id = ? AND job_id = ? AND owner_id = ?
            """,
            (revision_id, job_id, owner_id),
        ).fetchone()
        if row is None:
            raise KeyError("Ревизия не найдена")
        return self._load_revision(connection, revision_id)

    @staticmethod
    def _owner_for_current(
        connection: sqlite3.Connection, revision_id: str
    ) -> str:
        row = connection.execute(
            "SELECT owner_id FROM next_transcript_revisions WHERE id = ?", (revision_id,)
        ).fetchone()
        if row is None:
            raise KeyError("Ревизия не найдена")
        return str(row["owner_id"])

    @staticmethod
    def _validate_actor(owner_id: str, created_by: str | None) -> None:
        _required(owner_id, "Владелец")
        if created_by is not None and created_by != owner_id:
            raise ValueError("Создатель ревизии должен быть владельцем задания")

    def _load_revision(
        self, connection: sqlite3.Connection, revision_id: str
    ) -> TranscriptRevision:
        revision_row = connection.execute(
            "SELECT * FROM next_transcript_revisions WHERE id = ?", (revision_id,)
        ).fetchone()
        if revision_row is None:
            raise KeyError("Ревизия не найдена")
        groups = tuple(
            GroupSnapshot(
                speaker_group_id=str(row["speaker_group_id"]),
                label_snapshot=str(row["label_snapshot"]),
                profile_id=row["profile_id"],
                assignment_status=str(row["assignment_status"]),
                provenance=str(row["provenance"]),
                merged_into_group_id=row["merged_into_group_id"],
            )
            for row in connection.execute(
                """
                SELECT * FROM next_revision_groups WHERE revision_id = ?
                ORDER BY speaker_group_id
                """,
                (revision_id,),
            ).fetchall()
        )
        segment_rows = connection.execute(
            """
            SELECT * FROM next_revision_segments WHERE revision_id = ? ORDER BY ordinal
            """,
            (revision_id,),
        ).fetchall()
        segments = tuple(
            RevisionSegment(
                source_segment_id=str(row["source_segment_id"]),
                ordinal=int(row["ordinal"]),
                text=str(row["text"]),
                primary_group_id=row["primary_group_id"],
                speaker_group_ids=tuple(
                    str(speaker_row["speaker_group_id"])
                    for speaker_row in connection.execute(
                        """
                        SELECT speaker_group_id FROM next_revision_segment_speakers
                        WHERE revision_segment_id = ?
                        ORDER BY speaker_group_id
                        """,
                        (row["id"],),
                    ).fetchall()
                ),
                requires_review=bool(row["requires_review"]),
            )
            for row in segment_rows
        )
        return TranscriptRevision(
            info=_to_revision_info(revision_row), groups=groups, segments=segments
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    @staticmethod
    def _insert_merge_operation(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        owner_id: str,
        source_group_id: str,
        target_group_id: str,
        operation: str,
        revision_id: str,
        created_by: str,
        reverses_operation_id: str | None = None,
    ) -> str:
        operation_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO next_group_merge_operations(
                id, job_id, owner_id, source_group_id, target_group_id, operation, revision_id,
                reverses_operation_id, reversed_by, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                operation_id,
                job_id,
                owner_id,
                source_group_id,
                target_group_id,
                operation,
                revision_id,
                reverses_operation_id,
                created_by,
                _utc_now(),
            ),
        )
        return operation_id


def _to_revision_info(row: sqlite3.Row) -> TranscriptRevisionInfo:
    return TranscriptRevisionInfo(
        id=str(row["id"]),
        job_id=str(row["job_id"]),
        job_run_id=str(row["job_run_id"]),
        number=int(row["number"]),
        parent_id=row["parent_id"],
        origin=RevisionOrigin(row["origin"]),
        created_by=row["created_by"],
        expected_parent_number=row["expected_parent_number"],
        comment=row["comment"],
        restored_from_id=row["restored_from_id"],
        is_current=bool(row["is_current"]),
        created_at=str(row["created_at"]),
    )


def _to_merge_operation(row: sqlite3.Row) -> GroupMergeOperation:
    return GroupMergeOperation(
        id=str(row["id"]),
        job_id=str(row["job_id"]),
        source_group_id=str(row["source_group_id"]),
        target_group_id=str(row["target_group_id"]),
        operation=str(row["operation"]),
        revision_id=str(row["revision_id"]),
        reverses_operation_id=row["reverses_operation_id"],
        reversed_by=row["reversed_by"],
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
    )


def _replace_group(group: GroupSnapshot, update: GroupUpdate | None) -> GroupSnapshot:
    if update is None:
        return group
    return GroupSnapshot(
        speaker_group_id=group.speaker_group_id,
        label_snapshot=update.label_snapshot,
        profile_id=update.profile_id,
        assignment_status=update.assignment_status,
        provenance=update.provenance,
        merged_into_group_id=group.merged_into_group_id,
    )


def _validate_groups(groups: Sequence[GroupSnapshot]) -> tuple[GroupSnapshot, ...]:
    normalized = tuple(groups)
    group_ids = tuple(group.speaker_group_id for group in normalized)
    if len(set(group_ids)) != len(group_ids):
        raise ValueError("Идентификаторы групп ревизии должны быть уникальны")
    all_ids = set(group_ids)
    for group in normalized:
        if group.merged_into_group_id is not None and group.merged_into_group_id not in all_ids:
            raise ValueError("Целевая группа объединения отсутствует в ревизии")
    for group in normalized:
        seen: set[str] = set()
        target = group.merged_into_group_id
        while target is not None:
            if target in seen or target == group.speaker_group_id:
                raise ValueError("В ревизии нельзя создавать цикл объединений")
            seen.add(target)
            next_group = next(item for item in normalized if item.speaker_group_id == target)
            target = next_group.merged_into_group_id
    return normalized


def _validate_segments(
    segments: Sequence[SourceSegmentSnapshot], groups: Sequence[GroupSnapshot]
) -> tuple[SourceSegmentSnapshot, ...]:
    normalized = tuple(segments)
    source_ids = tuple(segment.source_segment_id for segment in normalized)
    ordinals = tuple(segment.ordinal for segment in normalized)
    if len(set(source_ids)) != len(source_ids) or len(set(ordinals)) != len(ordinals):
        raise ValueError(
            "Идентификаторы и порядковые номера исходных отрезков должны быть уникальны"
        )
    group_ids = {group.speaker_group_id for group in groups}
    for segment in normalized:
        if not set(segment.speaker_group_ids).issubset(group_ids):
            raise ValueError("Отрезок ссылается на отсутствующую группу")
    return tuple(sorted(normalized, key=lambda item: item.ordinal))


def _validate_revision_segments(
    segments: Sequence[RevisionSegment], groups: Sequence[GroupSnapshot]
) -> tuple[RevisionSegment, ...]:
    converted = tuple(
        SourceSegmentSnapshot(
            source_segment_id=segment.source_segment_id,
            ordinal=segment.ordinal,
            text=segment.text,
            primary_group_id=segment.primary_group_id,
            speaker_group_ids=segment.speaker_group_ids,
            requires_review=segment.requires_review,
        )
        for segment in segments
    )
    _validate_segments(converted, groups)
    return tuple(sorted(segments, key=lambda item: item.ordinal))


def _baseline_digest(
    job_run_id: str,
    groups: Sequence[GroupSnapshot],
    segments: Sequence[SourceSegmentSnapshot],
) -> str:
    """Создаёт стабильный digest без сериализации векторных или медиа-данных."""

    import hashlib
    import json

    payload = {
        "job_run_id": job_run_id,
        "groups": [
            {
                "id": item.speaker_group_id,
                "label": item.label_snapshot,
                "profile": item.profile_id,
                "status": item.assignment_status,
                "provenance": item.provenance,
                "merged_into": item.merged_into_group_id,
            }
            for item in groups
        ],
        "segments": [
            {
                "id": item.source_segment_id,
                "ordinal": item.ordinal,
                "text": item.text,
                "primary": item.primary_group_id,
                "groups": item.speaker_group_ids,
                "requires_review": item.requires_review,
            }
            for item in segments
        ],
    }
    encoded_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded_payload).hexdigest()


def _required(value: str, field_name: str) -> None:
    if not value:
        raise ValueError(f"{field_name} обязателен")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
