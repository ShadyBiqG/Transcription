"""Граница persistent vector index и детерминированная test-double реализация.

Настоящий адаптер ZeusDB будет реализовывать этот протокол отдельно. Этот модуль
не пишет SQLite и не знает о HTTP: его единственная ответственность — сохранить
неизменяемое поколение векторов и вернуть технические результаты поиска с
семантикой ZeusDB ``distance: меньше = ближе``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite, sqrt
from typing import Protocol, runtime_checkable


class VoiceVectorIndexError(RuntimeError):
    """Базовая ошибка технического vector index."""


class IndexGenerationNotFoundError(VoiceVectorIndexError):
    """Запрошенное сохранённое поколение отсутствует."""


class ImmutableIndexGenerationError(VoiceVectorIndexError):
    """Попытка заменить уже сохранённое поколение другими данными."""


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """Внутренняя запись, которую writer передаёт в индекс.

    ``values`` существуют только в процессе writer-а или в ZeusDB. Этот класс
    никогда не используется в API-проекции и не сохраняется в SQLite.
    """

    record_id: str
    profile_id: str
    sample_id: str
    values: tuple[float, ...]
    is_active: bool = True

    def __post_init__(self) -> None:
        if not self.record_id or not self.profile_id or not self.sample_id:
            raise ValueError("Идентификаторы vector record обязательны")
        if not self.values:
            raise ValueError("Вектор не может быть пустым")
        if any(not isfinite(value) for value in self.values):
            raise ValueError("Вектор должен состоять из конечных чисел")
        if not any(value != 0 for value in self.values):
            raise ValueError("Нулевой вектор не допускается")


@dataclass(frozen=True, slots=True)
class IndexManifest:
    """Проверяемый public-to-worker manifest сохранённого поколения."""

    generation_id: str
    voice_pack_id: str
    dimension: int
    space: str
    vector_count: int
    digest: str


@dataclass(frozen=True, slots=True)
class VectorSearchHit:
    """Технический hit без vector bytes и protected artifact metadata."""

    record_id: str
    profile_id: str
    distance: float


@runtime_checkable
class VoiceVectorIndex(Protocol):
    """Контракт для pinned ZeusDB adapter-а.

    ``save_generation`` принимает полный immutable snapshot. Это предотвращает
    изменение active-индекса «на месте»: writer строит новое поколение, валидирует
    его через ``load_generation`` и лишь затем SQLite переключает pointer.
    """

    def save_generation(
        self,
        *,
        generation_id: str,
        voice_pack_id: str,
        dimension: int,
        space: str,
        records: Iterable[VectorRecord],
    ) -> IndexManifest:
        """Атомарно сохраняет либо идемпотентно подтверждает immutable snapshot."""

    def load_generation(self, generation_id: str) -> IndexManifest:
        """Проверяет, что сохранённый snapshot можно загрузить после restart."""

    def search(
        self,
        *,
        generation_id: str,
        vector: Sequence[float],
        limit: int,
        active_profile_ids: set[str] | None = None,
    ) -> tuple[VectorSearchHit, ...]:
        """Ищет nearest records; ``distance`` меньше для более похожего голоса."""


@dataclass(frozen=True, slots=True)
class _Snapshot:
    manifest: IndexManifest
    records: tuple[VectorRecord, ...]


class InMemoryVoiceVectorIndex:
    """Детерминированный in-memory fake для unit-тестов и offline конвейера.

    В отличие от настоящего ZeusDB он не переживает restart, но строго проверяет
    dimension, immutable generation и lower-distance-first порядок. Это делает
    его подходящим contract-double без установки или запуска реальной БД.
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, _Snapshot] = {}

    def save_generation(
        self,
        *,
        generation_id: str,
        voice_pack_id: str,
        dimension: int,
        space: str,
        records: Iterable[VectorRecord],
    ) -> IndexManifest:
        if not generation_id or not voice_pack_id:
            raise ValueError("Идентификаторы поколения и voice pack обязательны")
        if dimension < 1:
            raise ValueError("Размерность эмбеддинга должна быть положительной")
        if space != "cosine":
            raise ValueError("Fake поддерживает только space='cosine'")
        snapshot_records = tuple(records)
        record_ids = [record.record_id for record in snapshot_records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("Идентификаторы vector record должны быть уникальны")
        if any(len(record.values) != dimension for record in snapshot_records):
            raise ValueError("Размерность записи не совпадает с поколением")
        manifest = IndexManifest(
            generation_id=generation_id,
            voice_pack_id=voice_pack_id,
            dimension=dimension,
            space=space,
            vector_count=len(snapshot_records),
            digest=_snapshot_digest(
                generation_id=generation_id,
                voice_pack_id=voice_pack_id,
                dimension=dimension,
                space=space,
                records=snapshot_records,
            ),
        )
        existing = self._snapshots.get(generation_id)
        if existing is not None:
            if existing.manifest != manifest:
                raise ImmutableIndexGenerationError(
                    "Immutable generation нельзя перезаписать другими данными"
                )
            return existing.manifest
        self._snapshots[generation_id] = _Snapshot(manifest=manifest, records=snapshot_records)
        return manifest

    def load_generation(self, generation_id: str) -> IndexManifest:
        snapshot = self._snapshots.get(generation_id)
        if snapshot is None:
            raise IndexGenerationNotFoundError("Поколение индекса не найдено")
        expected_digest = _snapshot_digest(
            generation_id=snapshot.manifest.generation_id,
            voice_pack_id=snapshot.manifest.voice_pack_id,
            dimension=snapshot.manifest.dimension,
            space=snapshot.manifest.space,
            records=snapshot.records,
        )
        if expected_digest != snapshot.manifest.digest:
            raise VoiceVectorIndexError("Контрольная сумма in-memory snapshot не совпала")
        return snapshot.manifest

    def search(
        self,
        *,
        generation_id: str,
        vector: Sequence[float],
        limit: int,
        active_profile_ids: set[str] | None = None,
    ) -> tuple[VectorSearchHit, ...]:
        if limit < 1:
            raise ValueError("Лимит поиска должен быть положительным")
        snapshot = self._snapshots.get(generation_id)
        if snapshot is None:
            raise IndexGenerationNotFoundError("Поколение индекса не найдено")
        query = tuple(float(value) for value in vector)
        if len(query) != snapshot.manifest.dimension:
            raise ValueError("Размерность query-вектора не совпадает с поколением")
        if not all(isfinite(value) for value in query) or not any(value != 0 for value in query):
            raise ValueError("Query-вектор должен быть конечным и ненулевым")
        hits = [
            VectorSearchHit(
                record_id=record.record_id,
                profile_id=record.profile_id,
                distance=_cosine_distance(query, record.values),
            )
            for record in snapshot.records
            if record.is_active
            and (active_profile_ids is None or record.profile_id in active_profile_ids)
        ]
        hits.sort(key=lambda hit: (hit.distance, hit.profile_id, hit.record_id))
        return tuple(hits[:limit])

    def record_ids(self, generation_id: str) -> tuple[str, ...]:
        """Тестовый inspection hook; production API его не использует."""

        snapshot = self._snapshots.get(generation_id)
        if snapshot is None:
            raise IndexGenerationNotFoundError("Поколение индекса не найдено")
        return tuple(record.record_id for record in snapshot.records)


def _cosine_distance(left: Sequence[float], right: Sequence[float]) -> float:
    dot_product = sum(first * second for first, second in zip(left, right, strict=True))
    left_norm = sqrt(sum(value * value for value in left))
    right_norm = sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("Нулевой вектор не допускается")
    # Numerical noise at the boundary should not make an exact match negative.
    similarity = max(-1.0, min(1.0, dot_product / (left_norm * right_norm)))
    return 1.0 - similarity


def _snapshot_digest(
    *,
    generation_id: str,
    voice_pack_id: str,
    dimension: int,
    space: str,
    records: Sequence[VectorRecord],
) -> str:
    digest = sha256()
    digest.update(f"{generation_id}|{voice_pack_id}|{dimension}|{space}\n".encode())
    for record in sorted(records, key=lambda item: item.record_id):
        values = ",".join(format(value, ".17g") for value in record.values)
        digest.update(
            (
                f"{record.record_id}|{record.profile_id}|{record.sample_id}|"
                f"{int(record.is_active)}|{values}\n"
            ).encode()
        )
    return digest.hexdigest()
