"""Локальный durable writer неизменяемых поколений общего голосового индекса.

Writer читает из SQLite только outbox и private-пути явно подтверждённых образцов.
Эмбеддинги существуют исключительно в памяти процесса и в ZeusDB: они не
сериализуются в SQLite, ошибки или возвращаемый результат. Каждая мутация строит
полный immutable snapshot, проверяет сохранённое поколение и лишь затем
переключает durable pointer через repository.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from pathlib import Path
from threading import Lock

from .index import IndexManifest, VectorRecord, VoiceVectorIndex
from .repository import (
    IndexGenerationState,
    IndexOperationState,
    IndexSampleInput,
    VoiceDirectoryRepository,
    VoiceIndexGeneration,
    VoiceIndexOperation,
)

_SPACE = "cosine"


class VoiceIndexWriterError(RuntimeError):
    """Безопасная причина, по которой локальная пересборка не завершилась.

    Оригинальная ошибка остаётся exception cause для локального журнала, но в
    текст этого исключения не попадают путь к фрагменту, вектор или данные
    говорящего.
    """


class VoiceIndexWriterBusyError(VoiceIndexWriterError):
    """В одном процессе уже выполняется запись поколения для этого writer-а."""


VoiceEmbeddingExtractor = Callable[[Path], Sequence[float]]


@dataclass(frozen=True, slots=True)
class VoiceIndexWriteResult:
    """Безопасный итог одного запуска reconciliation writer-а."""

    generation: VoiceIndexGeneration | None
    operation_ids: tuple[str, ...]

    @property
    def did_work(self) -> bool:
        """Возвращает true, если writer обработал хотя бы одну outbox-операцию."""

        return bool(self.operation_ids)


class VoiceIndexWriter:
    """Последовательно материализует outbox одного voice pack в ZeusDB.

    protected_root образует обязательную границу путей: даже ошибочная строка в
    SQLite не может заставить extractor прочитать файл за её пределами. Класс
    намеренно не имеет HTTP-клиента и не делает сетевых вызовов.

    При временной ошибке extractor или ZeusDB операции остаются в исходном
    durable состоянии. Это важно: fail_generation терминализирует outbox, поэтому
    его нельзя применять для обычной повторяемой технической ошибки. Следующий
    запуск заново использует тот же deterministic generation id.
    """

    def __init__(
        self,
        repository: VoiceDirectoryRepository,
        index: VoiceVectorIndex,
        *,
        voice_pack_id: str,
        voice_pack_sha256: str,
        embedding_dimension: int,
        protected_root: Path,
        extractor: VoiceEmbeddingExtractor,
    ) -> None:
        if not voice_pack_id:
            raise ValueError("Идентификатор voice pack обязателен.")
        if not _is_sha256(voice_pack_sha256):
            raise ValueError("Контрольная сумма voice pack должна быть SHA-256.")
        if embedding_dimension < 1:
            raise ValueError("Размерность голосового эмбеддинга должна быть положительной.")

        self._repository = repository
        self._index = index
        self._voice_pack_id = voice_pack_id
        self._voice_pack_sha256 = voice_pack_sha256.lower()
        self._embedding_dimension = embedding_dimension
        self._protected_root = protected_root
        self._extractor = extractor
        self._lock = Lock()

    def reconcile(self) -> VoiceIndexWriteResult:
        """Продолжает незавершённую фазу либо создаёт следующее поколение.

        Операции в index_saved или validated завершаются до чтения новых
        pending-записей. Поэтому перезапуск после любой durable-фазы не строит
        другой snapshot и не меняет active pointer до проверки load_generation.
        """

        if not self._lock.acquire(blocking=False):
            raise VoiceIndexWriterBusyError("Запись голосового индекса уже выполняется.")
        try:
            return self._reconcile_locked()
        except VoiceIndexWriterError:
            raise
        except Exception as error:
            raise VoiceIndexWriterError(
                "Локальная пересборка голосового индекса не выполнена; её можно повторить."
            ) from error
        finally:
            self._lock.release()

    def _reconcile_locked(self) -> VoiceIndexWriteResult:
        operations = tuple(
            self._repository.list_reconcilable_operations(voice_pack_id=self._voice_pack_id)
        )
        if not operations:
            return VoiceIndexWriteResult(
                generation=self._repository.active_generation(self._voice_pack_id),
                operation_ids=(),
            )

        resumable = tuple(
            operation
            for operation in operations
            if operation.state
            in {
                IndexOperationState.INDEX_SAVED,
                IndexOperationState.VALIDATED,
            }
        )
        if resumable:
            return self._resume_saved_generation(resumable)

        pending = tuple(
            operation
            for operation in operations
            if operation.state is IndexOperationState.PENDING
        )
        if len(pending) != len(operations):
            raise VoiceIndexWriterError(
                "Очередь голосового индекса содержит недопустимое состояние операции."
            )
        return self._build_pending_snapshot(pending)

    def _resume_saved_generation(
        self, operations: tuple[VoiceIndexOperation, ...]
    ) -> VoiceIndexWriteResult:
        target_ids = {operation.target_generation_id for operation in operations}
        if len(target_ids) != 1 or None in target_ids:
            raise VoiceIndexWriterError(
                "Незавершённые операции голосового индекса ссылаются на разные поколения."
            )
        generation_id = next(iter(target_ids))
        assert generation_id is not None
        generation = self._repository.get_generation(generation_id)
        if generation.voice_pack_id != self._voice_pack_id:
            raise VoiceIndexWriterError(
                "Незавершённое поколение относится к другому голосовому пакету."
            )
        operation_ids = tuple(operation.id for operation in operations)

        # load_generation повторно проверяет manifest и сам index до каких-либо
        # переходов SQLite. При повреждённом ZeusDB остаётся последний active
        # pointer, а операции можно безопасно повторить после исправления storage.
        manifest = self._index.load_generation(generation_id)
        _validate_generation_against_manifest(
            generation,
            manifest,
            expected_pack_sha256=self._voice_pack_sha256,
        )
        states = {operation.state for operation in operations}
        if states == {IndexOperationState.INDEX_SAVED}:
            self._repository.validate_generation(
                generation_id=generation_id,
                operation_ids=operation_ids,
            )
        elif states != {IndexOperationState.VALIDATED}:
            raise VoiceIndexWriterError(
                "Незавершённое поколение голосового индекса имеет несовместимые фазы."
            )

        active = self._repository.activate_generation(
            generation_id=generation_id,
            operation_ids=operation_ids,
        )
        return VoiceIndexWriteResult(generation=active, operation_ids=operation_ids)

    def _build_pending_snapshot(
        self, pending_operations: tuple[VoiceIndexOperation, ...]
    ) -> VoiceIndexWriteResult:
        samples = tuple(
            self._repository.list_index_inputs(voice_pack_id=self._voice_pack_id)
        )
        records = tuple(self._records_from_samples(samples))
        content_digest = _content_digest(
            voice_pack_id=self._voice_pack_id,
            dimension=self._embedding_dimension,
            records=records,
            operations=pending_operations,
        )
        generation_id = _generation_id(
            voice_pack_id=self._voice_pack_id,
            pack_sha256=self._voice_pack_sha256,
            content_digest=content_digest,
        )
        generation_no = _generation_number(
            voice_pack_id=self._voice_pack_id,
            pack_sha256=self._voice_pack_sha256,
            content_digest=content_digest,
        )
        manifest_digest = _manifest_digest(
            generation_id=generation_id,
            voice_pack_id=self._voice_pack_id,
            dimension=self._embedding_dimension,
            records=records,
        )

        generation = self._repository.create_generation(
            generation_id=generation_id,
            voice_pack_id=self._voice_pack_id,
            generation_no=generation_no,
            directory=f"generations/{generation_id}.zdb",
            manifest_sha256=manifest_digest,
            embedding_dimension=self._embedding_dimension,
            space=_SPACE,
            pack_sha256=self._voice_pack_sha256,
            vector_count=len(records),
        )
        operation_ids = tuple(operation.id for operation in pending_operations)
        manifest = self._index.save_generation(
            generation_id=generation_id,
            voice_pack_id=self._voice_pack_id,
            dimension=self._embedding_dimension,
            space=_SPACE,
            records=records,
        )
        _validate_generation_against_manifest(
            generation,
            manifest,
            expected_pack_sha256=self._voice_pack_sha256,
        )
        self._repository.mark_operations_index_saved(
            generation_id=generation_id,
            operation_ids=operation_ids,
        )
        self._repository.validate_generation(
            generation_id=generation_id,
            operation_ids=operation_ids,
        )
        active = self._repository.activate_generation(
            generation_id=generation_id,
            operation_ids=operation_ids,
        )
        return VoiceIndexWriteResult(generation=active, operation_ids=operation_ids)

    def _records_from_samples(self, samples: Sequence[IndexSampleInput]) -> Sequence[VectorRecord]:
        record_ids = set[str]()
        records: list[VectorRecord] = []
        for sample in samples:
            if sample.voice_pack_id != self._voice_pack_id:
                raise VoiceIndexWriterError(
                    "Во входе голосового индекса обнаружен другой голосовой пакет."
                )
            if sample.deterministic_record_id in record_ids:
                raise VoiceIndexWriterError(
                    "Во входе голосового индекса обнаружен повторный идентификатор образца."
                )
            record_ids.add(sample.deterministic_record_id)
            protected_path = self._resolve_protected_path(sample.protected_path)
            vector = _to_vector(
                self._extractor(protected_path),
                expected_dimension=self._embedding_dimension,
            )
            records.append(
                VectorRecord(
                    record_id=sample.deterministic_record_id,
                    profile_id=sample.profile_id,
                    sample_id=sample.sample_id,
                    values=vector,
                )
            )
        return records

    def _resolve_protected_path(self, raw_path: str) -> Path:
        if not raw_path:
            raise VoiceIndexWriterError("Не задан защищённый путь голосового образца.")
        try:
            root = self._protected_root.resolve(strict=True)
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = root / candidate
            if candidate.is_symlink():
                raise ValueError("symbolic link")
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            if not resolved.is_file():
                raise ValueError("not a file")
        except (OSError, ValueError) as error:
            raise VoiceIndexWriterError(
                "Защищённый голосовой образец недоступен для локальной обработки."
            ) from error
        return resolved


def _to_vector(raw_vector: Sequence[float], *, expected_dimension: int) -> tuple[float, ...]:
    try:
        vector = tuple(float(value) for value in raw_vector)
    except (TypeError, ValueError) as error:
        raise VoiceIndexWriterError("Локальный extractor вернул некорректный эмбеддинг.") from error
    if len(vector) != expected_dimension:
        raise VoiceIndexWriterError(
            "Размерность локального эмбеддинга не совпадает с voice pack."
        )
    if not all(isfinite(value) for value in vector) or not any(value != 0 for value in vector):
        raise VoiceIndexWriterError("Локальный extractor вернул недопустимый эмбеддинг.")
    return vector


def _validate_generation_against_manifest(
    generation: VoiceIndexGeneration,
    manifest: IndexManifest,
    *,
    expected_pack_sha256: str,
) -> None:
    if (
        generation.id != manifest.generation_id
        or generation.voice_pack_id != manifest.voice_pack_id
        or generation.embedding_dimension != manifest.dimension
        or generation.space != manifest.space
        or generation.vector_count != manifest.vector_count
        or generation.manifest_sha256 != manifest.digest
        or generation.pack_sha256 != expected_pack_sha256
    ):
        raise VoiceIndexWriterError(
            "Проверка согласованности поколения голосового индекса не пройдена."
        )
    if generation.state not in {
        IndexGenerationState.BUILDING,
        IndexGenerationState.VALIDATED,
        IndexGenerationState.ACTIVE,
    }:
        raise VoiceIndexWriterError("Поколение голосового индекса недоступно для активации.")


def _content_digest(
    *,
    voice_pack_id: str,
    dimension: int,
    records: Sequence[VectorRecord],
    operations: Sequence[VoiceIndexOperation],
) -> str:
    """Даёт stable key reconciliation до выделения generation id.

    Одинаковый набор записей может повториться после archive и restore. В таком
    случае новая операция обязана получить другое immutable generation, иначе
    retired generation нельзя будет безопасно активировать повторно.
    """

    digest = sha256()
    digest.update(f"{voice_pack_id}|{dimension}|{_SPACE}\n".encode())
    for record in sorted(records, key=lambda item: item.record_id):
        values = ",".join(format(value, ".17g") for value in record.values)
        digest.update(
            (
                f"{record.record_id}|{record.profile_id}|{record.sample_id}|"
                f"{int(record.is_active)}|{values}\n"
            ).encode()
        )
    for operation in sorted(operations, key=lambda item: item.id):
        digest.update(
            (
                f"{operation.id}|{operation.operation_type}|"
                f"{operation.deterministic_record_id}\n"
            ).encode()
        )
    return digest.hexdigest()


def _manifest_digest(
    *,
    generation_id: str,
    voice_pack_id: str,
    dimension: int,
    records: Sequence[VectorRecord],
) -> str:
    """Повторяет public checksum-контракт VoiceVectorIndex."""

    digest = sha256()
    digest.update(f"{generation_id}|{voice_pack_id}|{dimension}|{_SPACE}\n".encode())
    for record in sorted(records, key=lambda item: item.record_id):
        values = ",".join(format(value, ".17g") for value in record.values)
        digest.update(
            (
                f"{record.record_id}|{record.profile_id}|{record.sample_id}|"
                f"{int(record.is_active)}|{values}\n"
            ).encode()
        )
    return digest.hexdigest()


def _generation_id(*, voice_pack_id: str, pack_sha256: str, content_digest: str) -> str:
    digest = sha256(f"{voice_pack_id}|{pack_sha256}|{content_digest}".encode()).hexdigest()
    return f"voice-{digest[:48]}"


def _generation_number(*, voice_pack_id: str, pack_sha256: str, content_digest: str) -> int:
    """Строит стабильный положительный номер без SQLite-счётчика нового API.

    15 шестнадцатеричных символов укладываются в signed SQLite INTEGER. Вход
    включает pack и полный snapshot, поэтому повтор после аварии получает тот
    же номер, а изменённый snapshot практически не может столкнуться с ним.
    """

    digest = sha256(f"{voice_pack_id}|{pack_sha256}|{content_digest}".encode()).hexdigest()
    return int(digest[:15], 16) + 1


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)
