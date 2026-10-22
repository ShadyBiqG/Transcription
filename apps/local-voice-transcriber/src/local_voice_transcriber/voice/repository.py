"""Durable SQLite-слой общего справочника голосов.

Векторы намеренно отсутствуют из этой схемы: единственным persistent-хранилищем
vector bytes будет ZeusDB. SQLite хранит права, жизненный цикл образцов и
указатель на проверенное поколение индекса, чтобы операцию можно было безопасно
продолжить после остановки процесса.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5


class VoiceDirectoryError(RuntimeError):
    """Базовая ошибка состояния общего голосового справочника."""


class VoiceProfileNotFoundError(VoiceDirectoryError):
    """Запрошенный профиль отсутствует или был физически очищен."""


class VoiceProfileStateError(VoiceDirectoryError):
    """Переход состояния профиля или образца недопустим."""


class VoiceIndexStateError(VoiceDirectoryError):
    """Нарушен порядок durable-операции с поколением индекса."""


class ProfileState(StrEnum):
    """Жизненный цикл общей карточки говорящего."""

    ACTIVE = "active"
    ARCHIVED = "archived"
    PURGE_PENDING = "purge_pending"
    PURGED = "purged"


class SampleState(StrEnum):
    """Жизненный цикл разрешённого защищённого эталонного фрагмента."""

    PENDING = "pending"
    ACTIVE = "active"
    ARCHIVED = "archived"
    PURGE_PENDING = "purge_pending"
    PURGED = "purged"


class IndexGenerationState(StrEnum):
    """Состояние неизменяемой сохранённой копии vector index."""

    BUILDING = "building"
    VALIDATED = "validated"
    ACTIVE = "active"
    FAILED = "failed"
    RETIRED = "retired"


class IndexOperationState(StrEnum):
    """Монотонное состояние outbox-операции записи индекса."""

    PENDING = "pending"
    INDEX_SAVED = "index_saved"
    VALIDATED = "validated"
    COMMITTED = "committed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SharedVoiceProfile:
    """Безопасная проекция профиля для любого авторизованного пользователя.

    Здесь принципиально нет автора, source job, пути protected artifact и
    технического идентификатора вектора.
    """

    id: str
    display_name: str
    aliases: tuple[str, ...]
    state: ProfileState
    active_sample_count: int
    created_at: str
    archived_at: str | None


@dataclass(frozen=True, slots=True)
class VoiceSampleEnrollment:
    """Безопасный результат явного добавления эталонного образца."""

    id: str
    profile_id: str
    voice_pack_id: str
    state: SampleState
    operation_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class VoiceIndexGeneration:
    """Метаданные поколения, достаточные для загрузки ZeusDB без vector bytes."""

    id: str
    voice_pack_id: str
    generation_no: int
    directory: str
    manifest_sha256: str
    embedding_dimension: int
    space: str
    pack_sha256: str
    vector_count: int
    state: IndexGenerationState
    validated_at: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class VoiceIndexOperation:
    """Outbox-задание для единственного writer-а голосового индекса.

    ``input_json`` хранит только идентификаторы и безопасную техническую
    метаинформацию. Ни эмбеддинг, ни путь protected-файла сюда не попадают.
    """

    id: str
    operation_type: str
    deterministic_record_id: str
    voice_pack_id: str
    sample_id: str | None
    input_json: str
    target_generation_id: str | None
    state: IndexOperationState
    error_code: str | None
    created_at: str
    committed_at: str | None


@dataclass(frozen=True, slots=True)
class IndexSample:
    """Внутренняя метаинформация для index writer-а, без vector bytes."""

    sample_id: str
    profile_id: str
    voice_pack_id: str
    deterministic_record_id: str
    protected_artifact_id: str
    quality_json: str
    state: SampleState


@dataclass(frozen=True, slots=True)
class IndexSampleInput:
    """Приватный вход index writer-а с путём к уже подтверждённому образцу.

    Эта проекция не используется HTTP-маршрутами и не содержит vector bytes.
    Она существует только внутри локального worker-а, которому необходимо
    передать защищённый WAV в extractor перед записью результата в ZeusDB.
    """

    sample_id: str
    profile_id: str
    voice_pack_id: str
    deterministic_record_id: str
    protected_artifact_id: str
    protected_path: str
    state: SampleState


@dataclass(frozen=True, slots=True)
class _ProfileTransition:
    profile: SharedVoiceProfile
    samples: tuple[IndexSample, ...]
    operations: tuple[VoiceIndexOperation, ...]


class VoiceDirectoryRepository:
    """SQLite repository shared by all users of Transcription Next.

    Соединение живёт только внутри одного вызова. ``BEGIN IMMEDIATE`` в
    write-методах делает переход состояния и outbox атомарными для SQLite.
    """

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)

    def migrate(self) -> None:
        """Создаёт только таблицы voice-directory нового приложения."""

        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS next_voice_profiles (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    state TEXT NOT NULL CHECK(state IN (
                        'active', 'archived', 'purge_pending', 'purged'
                    )),
                    auto_assign_enabled INTEGER NOT NULL DEFAULT 1
                        CHECK(auto_assign_enabled IN (0, 1)),
                    created_by TEXT NOT NULL,
                    archived_at TEXT,
                    archive_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS next_voice_profile_aliases (
                    id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL REFERENCES next_voice_profiles(id),
                    value TEXT NOT NULL,
                    normalized_value TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    created_at TEXT NOT NULL,
                    UNIQUE(profile_id, normalized_value)
                );
                CREATE INDEX IF NOT EXISTS next_voice_profile_aliases_profile_idx
                    ON next_voice_profile_aliases(profile_id);

                CREATE TABLE IF NOT EXISTS next_protected_sample_artifacts (
                    id TEXT PRIMARY KEY,
                    content_sha256 TEXT NOT NULL,
                    protected_path TEXT NOT NULL,
                    source_job_deleted_at TEXT,
                    retention_until TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(content_sha256, protected_path)
                );

                CREATE TABLE IF NOT EXISTS next_voice_index_generations (
                    id TEXT PRIMARY KEY,
                    voice_pack_id TEXT NOT NULL,
                    generation_no INTEGER NOT NULL CHECK(generation_no >= 1),
                    directory TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    embedding_dimension INTEGER NOT NULL CHECK(embedding_dimension > 0),
                    space TEXT NOT NULL,
                    pack_sha256 TEXT NOT NULL,
                    vector_count INTEGER NOT NULL CHECK(vector_count >= 0),
                    state TEXT NOT NULL CHECK(state IN (
                        'building', 'validated', 'active', 'failed', 'retired'
                    )),
                    validated_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(voice_pack_id, generation_no),
                    UNIQUE(voice_pack_id, directory)
                );

                CREATE TABLE IF NOT EXISTS next_voice_index_state (
                    voice_pack_id TEXT PRIMARY KEY,
                    active_generation_id TEXT REFERENCES next_voice_index_generations(id),
                    last_valid_generation_id TEXT REFERENCES next_voice_index_generations(id),
                    writer_lease_id TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS next_voice_samples (
                    id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL REFERENCES next_voice_profiles(id),
                    voice_pack_id TEXT NOT NULL,
                    source_group_id TEXT,
                    source_interval_id TEXT,
                    protected_artifact_id TEXT NOT NULL
                        REFERENCES next_protected_sample_artifacts(id),
                    quality_json TEXT NOT NULL,
                    index_record_id TEXT NOT NULL UNIQUE,
                    index_generation_id TEXT REFERENCES next_voice_index_generations(id),
                    state TEXT NOT NULL CHECK(state IN (
                        'pending', 'active', 'archived', 'purge_pending', 'purged'
                    )),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    archived_at TEXT
                );
                CREATE INDEX IF NOT EXISTS next_voice_samples_profile_idx
                    ON next_voice_samples(profile_id, state);
                CREATE INDEX IF NOT EXISTS next_voice_samples_pack_idx
                    ON next_voice_samples(voice_pack_id, state);

                CREATE TABLE IF NOT EXISTS next_voice_index_operations (
                    id TEXT PRIMARY KEY,
                    operation_type TEXT NOT NULL CHECK(operation_type IN (
                        'upsert', 'archive', 'restore', 'purge'
                    )),
                    deterministic_record_id TEXT NOT NULL,
                    voice_pack_id TEXT NOT NULL,
                    sample_id TEXT REFERENCES next_voice_samples(id),
                    input_json TEXT NOT NULL,
                    target_generation_id TEXT REFERENCES next_voice_index_generations(id),
                    state TEXT NOT NULL CHECK(state IN (
                        'pending', 'index_saved', 'validated', 'committed', 'failed'
                    )),
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    committed_at TEXT,
                    UNIQUE(operation_type, deterministic_record_id, voice_pack_id, state)
                );
                CREATE INDEX IF NOT EXISTS next_voice_index_operations_pending_idx
                    ON next_voice_index_operations(voice_pack_id, state, created_at);

                CREATE TABLE IF NOT EXISTS next_voice_profile_events (
                    id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL REFERENCES next_voice_profiles(id),
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    sample_id TEXT REFERENCES next_voice_samples(id),
                    reason TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS next_voice_profile_events_profile_idx
                    ON next_voice_profile_events(profile_id, created_at);
                """
            )

    def create_profile(
        self,
        *,
        profile_id: str,
        display_name: str,
        created_by: str,
        aliases: Sequence[str] = (),
    ) -> SharedVoiceProfile:
        """Создаёт глобальную карточку с глобально уникальными псевдонимами."""

        clean_name = _display_value(display_name, "Имя профиля")
        clean_aliases = _unique_values(aliases, "Псевдоним")
        now = _utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO next_voice_profiles(
                    id, display_name, normalized_name, state, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    clean_name,
                    _normalized(clean_name),
                    ProfileState.ACTIVE,
                    created_by,
                    now,
                    now,
                ),
            )
            self._insert_aliases(connection, profile_id=profile_id, aliases=clean_aliases, now=now)
            self._event(
                connection,
                profile_id=profile_id,
                actor_id=created_by,
                action="profile_created",
                reason=None,
            )
        return self.get_shared_profile(profile_id)

    def add_alias(
        self, *, profile_id: str, alias: str, actor_id: str
    ) -> SharedVoiceProfile:
        """Добавляет псевдоним без раскрытия provenance другим пользователям."""

        clean_alias = _display_value(alias, "Псевдоним")
        now = _utc_now()
        with self._transaction() as connection:
            self._require_profile(connection, profile_id, allowed_states={ProfileState.ACTIVE})
            self._insert_aliases(connection, profile_id=profile_id, aliases=(clean_alias,), now=now)
            connection.execute(
                "UPDATE next_voice_profiles SET updated_at = ? WHERE id = ?", (now, profile_id)
            )
            self._event(
                connection,
                profile_id=profile_id,
                actor_id=actor_id,
                action="alias_added",
                reason=None,
            )
        return self.get_shared_profile(profile_id)

    def rename_profile(
        self, *, profile_id: str, display_name: str, actor_id: str
    ) -> SharedVoiceProfile:
        """Переименовывает active-профиль и оставляет исторические samples неизменными."""

        clean_name = _display_value(display_name, "Имя профиля")
        now = _utc_now()
        with self._transaction() as connection:
            self._require_profile(connection, profile_id, allowed_states={ProfileState.ACTIVE})
            connection.execute(
                """
                UPDATE next_voice_profiles
                SET display_name = ?, normalized_name = ?, updated_at = ?
                WHERE id = ?
                """,
                (clean_name, _normalized(clean_name), now, profile_id),
            )
            self._event(
                connection,
                profile_id=profile_id,
                actor_id=actor_id,
                action="profile_renamed",
                reason=None,
            )
        return self.get_shared_profile(profile_id)

    def get_shared_profile(self, profile_id: str) -> SharedVoiceProfile:
        """Возвращает только безопасную shared-проекцию профиля."""

        with self._connection() as connection:
            row = connection.execute(
                self._shared_profile_query() + " WHERE profiles.id = ?", (profile_id,)
            ).fetchone()
        if row is None or ProfileState(row["state"]) is ProfileState.PURGED:
            raise VoiceProfileNotFoundError("Голосовой профиль не найден")
        return _to_shared_profile(row)

    def list_shared_profiles(
        self,
        *,
        query: str = "",
        state: ProfileState | None = ProfileState.ACTIVE,
    ) -> list[SharedVoiceProfile]:
        """Ищет карточки по имени или псевдониму без раскрытия источников samples."""

        conditions: list[str] = []
        values: list[object] = []
        if state is not None:
            conditions.append("profiles.state = ?")
            values.append(state.value)
        if query.strip():
            conditions.append(
                """(
                    profiles.normalized_name LIKE ?
                    OR EXISTS (
                        SELECT 1 FROM next_voice_profile_aliases AS matching_aliases
                        WHERE matching_aliases.profile_id = profiles.id
                          AND matching_aliases.normalized_value LIKE ?
                    )
                )"""
            )
            pattern = f"%{_normalized(query)}%"
            values.extend((pattern, pattern))
        statement = self._shared_profile_query()
        if conditions:
            statement += " WHERE " + " AND ".join(conditions)
        statement += " ORDER BY profiles.normalized_name, profiles.id"
        with self._connection() as connection:
            rows = connection.execute(statement, values).fetchall()
        return [_to_shared_profile(row) for row in rows]

    def create_explicit_sample(
        self,
        *,
        sample_id: str,
        profile_id: str,
        voice_pack_id: str,
        protected_artifact_id: str,
        artifact_content_sha256: str,
        protected_path: str,
        quality: Mapping[str, object],
        created_by: str,
        operation_id: str,
        source_group_id: str | None = None,
        source_interval_id: str | None = None,
        retention_until: str | None = None,
    ) -> VoiceSampleEnrollment:
        """Фиксирует только явно подтверждённый sample и pending outbox-запись.

        Вызов не принимает эмбеддинг. Его сформирует локальный writer позже из
        protected artifact, а vector bytes попадут только в ZeusDB.
        """

        if not voice_pack_id:
            raise ValueError("Идентификатор voice pack обязателен")
        if not sample_id or not operation_id or not protected_artifact_id:
            raise ValueError("Идентификаторы sample, операции и protected artifact обязательны")
        if not _is_sha256(artifact_content_sha256):
            raise ValueError("content_sha256 должен быть SHA-256 в шестнадцатеричном виде")
        if not protected_path:
            raise ValueError("Защищённый путь к образцу обязателен")
        quality_json = _safe_json(quality)
        record_id = deterministic_record_id(sample_id)
        now = _utc_now()
        with self._transaction() as connection:
            self._require_profile(connection, profile_id, allowed_states={ProfileState.ACTIVE})
            existing = connection.execute(
                "SELECT * FROM next_voice_samples WHERE id = ?", (sample_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["profile_id"] != profile_id
                    or existing["voice_pack_id"] != voice_pack_id
                    or existing["index_record_id"] != record_id
                ):
                    raise VoiceDirectoryError("Повторный sample_id передан с другими данными")
                operation = self._require_operation(connection, operation_id)
                return _to_enrollment(existing, operation)
            connection.execute(
                """
                INSERT INTO next_protected_sample_artifacts(
                    id, content_sha256, protected_path, source_job_deleted_at,
                    retention_until, created_at
                ) VALUES (?, ?, ?, NULL, ?, ?)
                """,
                (
                    protected_artifact_id,
                    artifact_content_sha256,
                    protected_path,
                    retention_until,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO next_voice_samples(
                    id, profile_id, voice_pack_id, source_group_id, source_interval_id,
                    protected_artifact_id, quality_json, index_record_id, index_generation_id,
                    state, created_by, created_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL)
                """,
                (
                    sample_id,
                    profile_id,
                    voice_pack_id,
                    source_group_id,
                    source_interval_id,
                    protected_artifact_id,
                    quality_json,
                    record_id,
                    SampleState.PENDING,
                    created_by,
                    now,
                ),
            )
            operation = self._insert_operation(
                connection,
                operation_id=operation_id,
                operation_type="upsert",
                record_id=record_id,
                voice_pack_id=voice_pack_id,
                sample_id=sample_id,
                input_payload={"sample_id": sample_id, "profile_id": profile_id},
                now=now,
            )
            self._event(
                connection,
                profile_id=profile_id,
                actor_id=created_by,
                action="sample_explicitly_added",
                sample_id=sample_id,
                reason=None,
            )
        return VoiceSampleEnrollment(
            id=sample_id,
            profile_id=profile_id,
            voice_pack_id=voice_pack_id,
            state=SampleState.PENDING,
            operation_id=operation.id,
            created_at=now,
        )

    def archive_profile(
        self, *, profile_id: str, actor_id: str, reason: str
    ) -> _ProfileTransition:
        """Сразу исключает profile из read-model и ставит archive в durable outbox."""

        return self._transition_profile(
            profile_id=profile_id,
            actor_id=actor_id,
            reason=_display_value(reason, "Причина архивации"),
            from_state=ProfileState.ACTIVE,
            to_state=ProfileState.ARCHIVED,
            sample_from_states=(SampleState.PENDING, SampleState.ACTIVE),
            sample_to_state=SampleState.ARCHIVED,
            operation_type="archive",
            event_action="profile_archived",
        )

    def restore_profile(self, *, profile_id: str, actor_id: str, reason: str) -> _ProfileTransition:
        """Восстанавливает reversible archive через новое поколение индекса."""

        return self._transition_profile(
            profile_id=profile_id,
            actor_id=actor_id,
            reason=_display_value(reason, "Причина восстановления"),
            from_state=ProfileState.ARCHIVED,
            to_state=ProfileState.ACTIVE,
            sample_from_states=(SampleState.ARCHIVED,),
            sample_to_state=SampleState.ACTIVE,
            operation_type="restore",
            event_action="profile_restored",
        )

    def list_samples_for_index(
        self, *, voice_pack_id: str, states: Sequence[SampleState] = (SampleState.PENDING,)
    ) -> list[IndexSample]:
        """Возвращает writer-метаданные, но никогда не embedding и не путь к аудио."""

        if not states:
            return []
        placeholders = ", ".join("?" for _ in states)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT id, profile_id, voice_pack_id, index_record_id, protected_artifact_id,
                       quality_json, state, index_generation_id
                FROM next_voice_samples
                WHERE voice_pack_id = ? AND state IN ({placeholders})
                ORDER BY created_at, id
                """,
                (voice_pack_id, *(state.value for state in states)),
            ).fetchall()
        return [_to_index_sample(row) for row in rows]

    def list_index_inputs(
        self,
        *,
        voice_pack_id: str,
        states: Sequence[SampleState] = (SampleState.PENDING, SampleState.ACTIVE),
    ) -> list[IndexSampleInput]:
        """Возвращает private inputs только для локального index writer-а.

        Путь защищённого WAV никогда не включается в shared-проекцию профиля,
        API-ответ или outbox JSON. При пересборке необходим полный snapshot
        активных и новых образцов, поэтому по умолчанию включены оба состояния.
        """

        if not states:
            return []
        placeholders = ", ".join("?" for _ in states)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT samples.id, samples.profile_id, samples.voice_pack_id,
                       samples.index_record_id, samples.protected_artifact_id,
                       artifacts.protected_path, samples.state
                FROM next_voice_samples AS samples
                JOIN next_protected_sample_artifacts AS artifacts
                  ON artifacts.id = samples.protected_artifact_id
                WHERE samples.voice_pack_id = ? AND samples.state IN ({placeholders})
                ORDER BY samples.created_at, samples.id
                """,
                (voice_pack_id, *(state.value for state in states)),
            ).fetchall()
        return [_to_index_sample_input(row) for row in rows]

    def list_reconcilable_operations(self, *, voice_pack_id: str) -> list[VoiceIndexOperation]:
        """Возвращает незавершённый outbox в хронологическом порядке для одного writer-а."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM next_voice_index_operations
                WHERE voice_pack_id = ? AND state IN ('pending', 'index_saved', 'validated')
                ORDER BY created_at, id
                """,
                (voice_pack_id,),
            ).fetchall()
        return [_to_operation(row) for row in rows]

    def create_generation(
        self,
        *,
        generation_id: str,
        voice_pack_id: str,
        generation_no: int,
        directory: str,
        manifest_sha256: str,
        embedding_dimension: int,
        space: str,
        pack_sha256: str,
        vector_count: int,
    ) -> VoiceIndexGeneration:
        """Регистрирует building-поколение до его атомарного ``Zeus.save``."""

        if not all((generation_id, voice_pack_id, directory, space)):
            raise ValueError("Идентификаторы поколения, pack, каталог и space обязательны")
        if generation_no < 1 or embedding_dimension < 1 or vector_count < 0:
            raise ValueError("Некорректные числовые параметры поколения")
        if not _is_sha256(manifest_sha256) or not _is_sha256(pack_sha256):
            raise ValueError("Хеши manifest и pack должны быть SHA-256")
        now = _utc_now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM next_voice_index_generations WHERE id = ?", (generation_id,)
            ).fetchone()
            if existing is not None:
                generation = _to_generation(existing)
                expected = (
                    generation.voice_pack_id,
                    generation.generation_no,
                    generation.directory,
                    generation.manifest_sha256,
                    generation.embedding_dimension,
                    generation.space,
                    generation.pack_sha256,
                    generation.vector_count,
                )
                actual = (
                    voice_pack_id,
                    generation_no,
                    directory,
                    manifest_sha256,
                    embedding_dimension,
                    space,
                    pack_sha256,
                    vector_count,
                )
                if expected != actual:
                    raise VoiceIndexStateError(
                        "generation_id повторён с другой immutable-конфигурацией"
                    )
                return generation
            connection.execute(
                """
                INSERT INTO next_voice_index_generations(
                    id, voice_pack_id, generation_no, directory, manifest_sha256,
                    embedding_dimension, space, pack_sha256, vector_count, state, validated_at,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    generation_id,
                    voice_pack_id,
                    generation_no,
                    directory,
                    manifest_sha256,
                    embedding_dimension,
                    space,
                    pack_sha256,
                    vector_count,
                    IndexGenerationState.BUILDING,
                    now,
                ),
            )
        return self.get_generation(generation_id)

    def mark_operations_index_saved(
        self, *, generation_id: str, operation_ids: Sequence[str]
    ) -> None:
        """Фиксирует факт atomic index save; повтор идентичен исходному шагу."""

        self._transition_operations(
            generation_id=generation_id,
            operation_ids=operation_ids,
            expected_states=(IndexOperationState.PENDING,),
            next_state=IndexOperationState.INDEX_SAVED,
        )

    def validate_generation(self, *, generation_id: str, operation_ids: Sequence[str]) -> None:
        """Отмечает положительную ``load + probe``-проверку сохранённого поколения."""

        now = _utc_now()
        with self._transaction() as connection:
            generation = self._require_generation(connection, generation_id)
            if generation.state is IndexGenerationState.BUILDING:
                connection.execute(
                    """
                    UPDATE next_voice_index_generations
                    SET state = ?, validated_at = ? WHERE id = ?
                    """,
                    (IndexGenerationState.VALIDATED, now, generation_id),
                )
            elif generation.state not in {
                IndexGenerationState.VALIDATED,
                IndexGenerationState.ACTIVE,
            }:
                raise VoiceIndexStateError("Нельзя проверять failed или retired поколение")
            self._transition_operations_in_connection(
                connection,
                generation_id=generation_id,
                operation_ids=operation_ids,
                expected_states=(IndexOperationState.INDEX_SAVED,),
                next_state=IndexOperationState.VALIDATED,
                now=now,
            )

    def activate_generation(
        self, *, generation_id: str, operation_ids: Sequence[str]
    ) -> VoiceIndexGeneration:
        """Атомарно переключает pointer только на validated immutable generation."""

        now = _utc_now()
        with self._transaction() as connection:
            generation = self._require_generation(connection, generation_id)
            if generation.state not in {
                IndexGenerationState.VALIDATED,
                IndexGenerationState.ACTIVE,
            }:
                raise VoiceIndexStateError("Активировать можно только проверенное поколение")
            state_row = connection.execute(
                "SELECT * FROM next_voice_index_state WHERE voice_pack_id = ?",
                (generation.voice_pack_id,),
            ).fetchone()
            if state_row is not None and state_row["active_generation_id"] not in {
                None,
                generation_id,
            }:
                connection.execute(
                    """
                    UPDATE next_voice_index_generations
                    SET state = ?
                    WHERE id = ? AND state = ?
                    """,
                    (
                        IndexGenerationState.RETIRED,
                        state_row["active_generation_id"],
                        IndexGenerationState.ACTIVE,
                    ),
                )
            connection.execute(
                "UPDATE next_voice_index_generations SET state = ? WHERE id = ?",
                (IndexGenerationState.ACTIVE, generation_id),
            )
            connection.execute(
                """
                INSERT INTO next_voice_index_state(
                    voice_pack_id, active_generation_id, last_valid_generation_id,
                    writer_lease_id, updated_at
                ) VALUES (?, ?, ?, NULL, ?)
                ON CONFLICT(voice_pack_id) DO UPDATE SET
                    active_generation_id = excluded.active_generation_id,
                    last_valid_generation_id = excluded.last_valid_generation_id,
                    writer_lease_id = NULL,
                    updated_at = excluded.updated_at
                """,
                (generation.voice_pack_id, generation_id, generation_id, now),
            )
            self._transition_operations_in_connection(
                connection,
                generation_id=generation_id,
                operation_ids=operation_ids,
                expected_states=(IndexOperationState.VALIDATED,),
                next_state=IndexOperationState.COMMITTED,
                now=now,
            )
            connection.execute(
                """
                UPDATE next_voice_samples
                SET index_generation_id = ?,
                    state = CASE WHEN state = 'pending' THEN 'active' ELSE state END
                WHERE id IN (
                    SELECT sample_id FROM next_voice_index_operations
                    WHERE target_generation_id = ? AND operation_type = 'upsert'
                )
                """,
                (generation_id, generation_id),
            )
        return self.get_generation(generation_id)

    def fail_generation(self, *, generation_id: str, error_code: str) -> None:
        """Помечает неактивное поколение и его незавершённые операции как failed."""

        if not error_code:
            raise ValueError("Код ошибки обязателен")
        with self._transaction() as connection:
            generation = self._require_generation(connection, generation_id)
            if generation.state is IndexGenerationState.ACTIVE:
                raise VoiceIndexStateError("Активное поколение нельзя пометить failed")
            connection.execute(
                "UPDATE next_voice_index_generations SET state = ? WHERE id = ?",
                (IndexGenerationState.FAILED, generation_id),
            )
            connection.execute(
                """
                UPDATE next_voice_index_operations
                SET state = ?, error_code = ?
                WHERE target_generation_id = ? AND state != ?
                """,
                (
                    IndexOperationState.FAILED,
                    error_code,
                    generation_id,
                    IndexOperationState.COMMITTED,
                ),
            )

    def get_generation(self, generation_id: str) -> VoiceIndexGeneration:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM next_voice_index_generations WHERE id = ?", (generation_id,)
            ).fetchone()
        if row is None:
            raise VoiceIndexStateError("Поколение голосового индекса не найдено")
        return _to_generation(row)

    def active_generation(self, voice_pack_id: str) -> VoiceIndexGeneration | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT generations.*
                FROM next_voice_index_state AS state
                JOIN next_voice_index_generations AS generations
                    ON generations.id = state.active_generation_id
                WHERE state.voice_pack_id = ?
                """,
                (voice_pack_id,),
            ).fetchone()
        return _to_generation(row) if row else None

    def _transition_profile(
        self,
        *,
        profile_id: str,
        actor_id: str,
        reason: str,
        from_state: ProfileState,
        to_state: ProfileState,
        sample_from_states: Sequence[SampleState],
        sample_to_state: SampleState,
        operation_type: str,
        event_action: str,
    ) -> _ProfileTransition:
        now = _utc_now()
        with self._transaction() as connection:
            profile_row = self._require_profile(
                connection, profile_id, allowed_states={from_state, to_state}
            )
            current_state = ProfileState(profile_row["state"])
            sample_placeholders = ", ".join("?" for _ in sample_from_states)
            samples = connection.execute(
                f"""
                SELECT id, profile_id, voice_pack_id, index_record_id, protected_artifact_id,
                       quality_json, state, index_generation_id
                FROM next_voice_samples
                WHERE profile_id = ? AND state IN ({sample_placeholders})
                ORDER BY created_at, id
                """,
                (profile_id, *(state.value for state in sample_from_states)),
            ).fetchall()
            if current_state is from_state:
                archived_at = now if to_state is ProfileState.ARCHIVED else None
                connection.execute(
                    """
                    UPDATE next_voice_profiles
                    SET state = ?, archived_at = ?, archive_reason = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (to_state, archived_at, reason, now, profile_id),
                )
                if samples and to_state is ProfileState.ARCHIVED:
                    connection.execute(
                        f"""
                        UPDATE next_voice_samples
                        SET state = ?, archived_at = ?
                        WHERE profile_id = ? AND state IN ({sample_placeholders})
                        """,
                        (
                            sample_to_state,
                            archived_at,
                            profile_id,
                            *(state.value for state in sample_from_states),
                        ),
                    )
                elif samples and to_state is ProfileState.ACTIVE:
                    # Pending sample ещё не попал в сохранённый index и после
                    # restore должен дождаться исходного upsert, а не считаться active.
                    connection.execute(
                        f"""
                        UPDATE next_voice_samples
                        SET state = CASE
                            WHEN index_generation_id IS NULL THEN 'pending' ELSE 'active'
                        END,
                            archived_at = NULL
                        WHERE profile_id = ? AND state IN ({sample_placeholders})
                        """,
                        (profile_id, *(state.value for state in sample_from_states)),
                    )
            operations: list[VoiceIndexOperation] = []
            for sample_row in samples:
                sample = _to_index_sample(sample_row)
                # Archive изменяет метаданные уже индексированного record. Pending
                # reference записи в индекс ещё нет; её исходный upsert остаётся
                # outbox-предпосылкой и не нуждается в archive/restore операции.
                was_indexed = sample_row["index_generation_id"] is not None
                if not was_indexed:
                    continue
                operation_id = deterministic_operation_id(operation_type, sample.sample_id)
                operations.append(
                    self._insert_operation(
                        connection,
                        operation_id=operation_id,
                        operation_type=operation_type,
                        record_id=sample.deterministic_record_id,
                        voice_pack_id=sample.voice_pack_id,
                        sample_id=sample.sample_id,
                        input_payload={
                            "sample_id": sample.sample_id,
                            "profile_id": sample.profile_id,
                            "state": sample_to_state.value,
                        },
                        now=now,
                    )
                )
            self._event(
                connection,
                profile_id=profile_id,
                actor_id=actor_id,
                action=event_action,
                reason=reason,
            )
        profile = self.get_shared_profile(profile_id)
        return _ProfileTransition(
            profile=profile,
            samples=tuple(_to_index_sample(row) for row in samples),
            operations=tuple(operations),
        )

    def _transition_operations(
        self,
        *,
        generation_id: str,
        operation_ids: Sequence[str],
        expected_states: Sequence[IndexOperationState],
        next_state: IndexOperationState,
    ) -> None:
        now = _utc_now()
        with self._transaction() as connection:
            self._require_generation(connection, generation_id)
            self._transition_operations_in_connection(
                connection,
                generation_id=generation_id,
                operation_ids=operation_ids,
                expected_states=expected_states,
                next_state=next_state,
                now=now,
            )

    def _transition_operations_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        generation_id: str,
        operation_ids: Sequence[str],
        expected_states: Sequence[IndexOperationState],
        next_state: IndexOperationState,
        now: str,
    ) -> None:
        if not operation_ids:
            return
        for operation_id in operation_ids:
            operation = self._require_operation(connection, operation_id)
            if operation.target_generation_id not in {None, generation_id}:
                raise VoiceIndexStateError("Операция уже принадлежит другому поколению")
            if operation.state is next_state:
                continue
            if operation.state not in expected_states:
                raise VoiceIndexStateError("Нарушен порядок состояния index operation")
            committed_at = now if next_state is IndexOperationState.COMMITTED else None
            updated = connection.execute(
                """
                UPDATE next_voice_index_operations
                SET target_generation_id = ?, state = ?, committed_at = COALESCE(?, committed_at),
                    error_code = NULL
                WHERE id = ?
                """,
                (generation_id, next_state, committed_at, operation_id),
            ).rowcount
            if updated != 1:
                raise VoiceIndexStateError("Не удалось изменить состояние index operation")

    def _insert_operation(
        self,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        operation_type: str,
        record_id: str,
        voice_pack_id: str,
        sample_id: str | None,
        input_payload: Mapping[str, object],
        now: str,
    ) -> VoiceIndexOperation:
        payload = _safe_json(input_payload)
        existing = connection.execute(
            "SELECT * FROM next_voice_index_operations WHERE id = ?", (operation_id,)
        ).fetchone()
        if existing is not None:
            operation = _to_operation(existing)
            if (
                operation.operation_type != operation_type
                or operation.deterministic_record_id != record_id
                or operation.voice_pack_id != voice_pack_id
                or operation.sample_id != sample_id
                or operation.input_json != payload
            ):
                raise VoiceIndexStateError("operation_id повторён с другими данными")
            return operation
        connection.execute(
            """
            INSERT INTO next_voice_index_operations(
                id, operation_type, deterministic_record_id, voice_pack_id, sample_id, input_json,
                target_generation_id, state, error_code, created_at, committed_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, NULL)
            """,
            (
                operation_id,
                operation_type,
                record_id,
                voice_pack_id,
                sample_id,
                payload,
                IndexOperationState.PENDING,
                now,
            ),
        )
        return VoiceIndexOperation(
            id=operation_id,
            operation_type=operation_type,
            deterministic_record_id=record_id,
            voice_pack_id=voice_pack_id,
            sample_id=sample_id,
            input_json=payload,
            target_generation_id=None,
            state=IndexOperationState.PENDING,
            error_code=None,
            created_at=now,
            committed_at=None,
        )

    def _require_profile(
        self,
        connection: sqlite3.Connection,
        profile_id: str,
        *,
        allowed_states: set[ProfileState] | None = None,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM next_voice_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None or ProfileState(row["state"]) is ProfileState.PURGED:
            raise VoiceProfileNotFoundError("Голосовой профиль не найден")
        if allowed_states is not None and ProfileState(row["state"]) not in allowed_states:
            raise VoiceProfileStateError("Операция недопустима для текущего состояния профиля")
        return row

    def _require_generation(
        self, connection: sqlite3.Connection, generation_id: str
    ) -> VoiceIndexGeneration:
        row = connection.execute(
            "SELECT * FROM next_voice_index_generations WHERE id = ?", (generation_id,)
        ).fetchone()
        if row is None:
            raise VoiceIndexStateError("Поколение голосового индекса не найдено")
        return _to_generation(row)

    def _require_operation(
        self, connection: sqlite3.Connection, operation_id: str
    ) -> VoiceIndexOperation:
        row = connection.execute(
            "SELECT * FROM next_voice_index_operations WHERE id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise VoiceIndexStateError("Index operation не найдена")
        return _to_operation(row)

    def _insert_aliases(
        self,
        connection: sqlite3.Connection,
        *,
        profile_id: str,
        aliases: Sequence[str],
        now: str,
    ) -> None:
        for alias in aliases:
            connection.execute(
                """
                INSERT INTO next_voice_profile_aliases(
                    id, profile_id, value, normalized_value, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(uuid5(NAMESPACE_URL, f"voice-alias:{profile_id}:{_normalized(alias)}")),
                    profile_id,
                    alias,
                    _normalized(alias),
                    now,
                ),
            )

    def _event(
        self,
        connection: sqlite3.Connection,
        *,
        profile_id: str,
        actor_id: str,
        action: str,
        sample_id: str | None = None,
        reason: str | None,
    ) -> None:
        event_seed = f"voice-event:{profile_id}:{action}:{sample_id or ''}:{_utc_now()}"
        connection.execute(
            """
            INSERT INTO next_voice_profile_events(
                id, profile_id, actor_id, action, sample_id, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid5(NAMESPACE_URL, event_seed)),
                profile_id,
                actor_id,
                action,
                sample_id,
                reason,
                _utc_now(),
            ),
        )

    @staticmethod
    def _shared_profile_query() -> str:
        return """
            SELECT profiles.id, profiles.display_name, profiles.state, profiles.created_at,
                   profiles.archived_at,
                   COALESCE((
                       SELECT COUNT(*) FROM next_voice_samples AS samples
                       WHERE samples.profile_id = profiles.id AND samples.state = 'active'
                   ), 0) AS active_sample_count,
                   COALESCE((
                       SELECT group_concat(value, char(31)) FROM (
                           SELECT value FROM next_voice_profile_aliases
                           WHERE profile_id = profiles.id
                           ORDER BY normalized_value, id
                       )
                   ), '') AS aliases_text
            FROM next_voice_profiles AS profiles
        """

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


def deterministic_record_id(sample_id: str) -> str:
    """Даёт стабильный Zeus record id для retry и crash-recovery."""

    if not sample_id:
        raise ValueError("Идентификатор sample обязателен")
    return f"voice-sample:{sample_id}"


def deterministic_operation_id(operation_type: str, sample_id: str) -> str:
    """Строит повторяемый UUID операции смены visibility одного sample."""

    if not operation_type or not sample_id:
        raise ValueError("Тип операции и sample_id обязательны")
    return str(uuid5(NAMESPACE_URL, f"voice-index:{operation_type}:{sample_id}"))


def _to_shared_profile(row: sqlite3.Row) -> SharedVoiceProfile:
    aliases_text = str(row["aliases_text"])
    return SharedVoiceProfile(
        id=str(row["id"]),
        display_name=str(row["display_name"]),
        aliases=tuple(value for value in aliases_text.split(chr(31)) if value),
        state=ProfileState(row["state"]),
        active_sample_count=int(row["active_sample_count"]),
        created_at=str(row["created_at"]),
        archived_at=row["archived_at"],
    )


def _to_enrollment(row: sqlite3.Row, operation: VoiceIndexOperation) -> VoiceSampleEnrollment:
    return VoiceSampleEnrollment(
        id=str(row["id"]),
        profile_id=str(row["profile_id"]),
        voice_pack_id=str(row["voice_pack_id"]),
        state=SampleState(row["state"]),
        operation_id=operation.id,
        created_at=str(row["created_at"]),
    )


def _to_generation(row: sqlite3.Row) -> VoiceIndexGeneration:
    return VoiceIndexGeneration(
        id=str(row["id"]),
        voice_pack_id=str(row["voice_pack_id"]),
        generation_no=int(row["generation_no"]),
        directory=str(row["directory"]),
        manifest_sha256=str(row["manifest_sha256"]),
        embedding_dimension=int(row["embedding_dimension"]),
        space=str(row["space"]),
        pack_sha256=str(row["pack_sha256"]),
        vector_count=int(row["vector_count"]),
        state=IndexGenerationState(row["state"]),
        validated_at=row["validated_at"],
        created_at=str(row["created_at"]),
    )


def _to_operation(row: sqlite3.Row) -> VoiceIndexOperation:
    return VoiceIndexOperation(
        id=str(row["id"]),
        operation_type=str(row["operation_type"]),
        deterministic_record_id=str(row["deterministic_record_id"]),
        voice_pack_id=str(row["voice_pack_id"]),
        sample_id=row["sample_id"],
        input_json=str(row["input_json"]),
        target_generation_id=row["target_generation_id"],
        state=IndexOperationState(row["state"]),
        error_code=row["error_code"],
        created_at=str(row["created_at"]),
        committed_at=row["committed_at"],
    )


def _to_index_sample(row: sqlite3.Row) -> IndexSample:
    return IndexSample(
        sample_id=str(row["id"]),
        profile_id=str(row["profile_id"]),
        voice_pack_id=str(row["voice_pack_id"]),
        deterministic_record_id=str(row["index_record_id"]),
        protected_artifact_id=str(row["protected_artifact_id"]),
        quality_json=str(row["quality_json"]),
        state=SampleState(row["state"]),
    )


def _to_index_sample_input(row: sqlite3.Row) -> IndexSampleInput:
    return IndexSampleInput(
        sample_id=str(row["id"]),
        profile_id=str(row["profile_id"]),
        voice_pack_id=str(row["voice_pack_id"]),
        deterministic_record_id=str(row["index_record_id"]),
        protected_artifact_id=str(row["protected_artifact_id"]),
        protected_path=str(row["protected_path"]),
        state=SampleState(row["state"]),
    )


def _display_value(value: str, label: str) -> str:
    clean = " ".join(value.split())
    if not clean:
        raise ValueError(f"{label} обязателен")
    if len(clean) > 160:
        raise ValueError(f"{label} слишком длинный")
    return clean


def _unique_values(values: Sequence[str], label: str) -> tuple[str, ...]:
    result: list[str] = []
    normalized_values: set[str] = set()
    for raw_value in values:
        value = _display_value(raw_value, label)
        normalized = _normalized(value)
        if normalized not in normalized_values:
            normalized_values.add(normalized)
            result.append(value)
    return tuple(result)


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _safe_json(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("Технические метаданные должны быть JSON-совместимы") from error


def _is_sha256(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return sha256(bytes.fromhex(value)).digest() is not None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
