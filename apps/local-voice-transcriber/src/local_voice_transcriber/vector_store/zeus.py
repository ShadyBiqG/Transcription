"""Fail-closed persistent vector store общего голосового справочника.

ZeusDB — локальная библиотека с persistent-индексами, а не сетевой SaaS-клиент.
Одна настроенная директория является общим индексом экземпляра Transcription
Next. Векторы никогда не дублируются в SQLite: SQLite хранит только профильные
метаданные и задания outbox.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from local_voice_transcriber.voice.index import (
    ImmutableIndexGenerationError,
    IndexGenerationNotFoundError,
    IndexManifest,
    VectorRecord,
    VectorSearchHit,
    VoiceVectorIndexError,
)

_GENERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MANIFEST_FILE_NAME = "transcription-next-manifest.json"
_MANIFEST_SCHEMA_VERSION = 1
_METADATA_PREFIX = "transcription_next_"


class VectorStoreUnavailableError(VoiceVectorIndexError):
    """ZeusDB не готов к безопасной работе; fallback для векторов запрещён."""


class VectorStoreAvailability(StrEnum):
    """Состояние доступности persistent vector store."""

    READY = "ready"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class VectorStoreStatus:
    """Состояние, пригодное для health-ответа без технических деталей векторов."""

    availability: VectorStoreAvailability
    reason: str | None = None

    @property
    def is_ready(self) -> bool:
        """Возвращает true только при доступном и сконфигурированном ZeusDB."""

        return self.availability is VectorStoreAvailability.READY


class ZeusDBIndex(Protocol):
    """Минимальная часть публичного API одного индекса ZeusDB."""

    def add(self, records: Sequence[Mapping[str, object]]) -> object:
        """Добавляет batch записей в новый индекс."""

    def add_metadata(self, metadata: Mapping[str, str]) -> object:
        """Сохраняет служебную метаинформацию индекса."""

    def get_metadata(self, key: str) -> object:
        """Возвращает значение служебной метаинформации индекса."""

    def get_vector_count(self) -> int:
        """Возвращает количество живых записей."""

    def save(self, path: str) -> object:
        """Сохраняет индекс в выделенную директорию."""

    def search(self, vector: Sequence[float], *, top_k: int) -> Sequence[Mapping[str, object]]:
        """Ищет записи с семантикой distance: меньше = ближе."""


class ZeusDBTransport(Protocol):
    """Инъекционная граница к ZeusDB для тестов и контролируемого запуска."""

    def create(self, *, dimension: int, space: str, expected_size: int) -> ZeusDBIndex:
        """Создаёт новый индекс."""

    def load(self, path: Path) -> ZeusDBIndex:
        """Загружает сохранённый индекс."""

    def save(self, index: ZeusDBIndex, path: Path) -> None:
        """Сохраняет индекс в новую директорию."""

    def move_generation(self, source: Path, destination: Path) -> None:
        """Атомарно публикует подготовленное поколение."""


class NativeZeusDBTransport:
    """Тонкая адаптация фактического API zeusdb_vector_database.

    Импорт выполняется лениво. При отсутствии wheel или несовместимой платформе
    вызывающий код получает VectorStoreUnavailableError и не может незаметно
    перейти к SQLite либо in-memory векторному индексу.
    """

    def __init__(self) -> None:
        try:
            from zeusdb_vector_database import VectorDatabase
        except ImportError as error:
            raise VectorStoreUnavailableError(
                "Пакет zeusdb-vector-database не установлен или несовместим с платформой."
            ) from error
        self._vector_database_type: type[Any] = VectorDatabase

    def create(self, *, dimension: int, space: str, expected_size: int) -> ZeusDBIndex:
        try:
            index = self._vector_database_type().create(
                "hnsw",
                dim=dimension,
                space=space,
                expected_size=expected_size,
            )
        except Exception as error:
            raise VectorStoreUnavailableError("ZeusDB не смог создать индекс.") from error
        return cast(ZeusDBIndex, index)

    def load(self, path: Path) -> ZeusDBIndex:
        try:
            index = self._vector_database_type().load(str(path))
        except Exception as error:
            raise VectorStoreUnavailableError(
                "ZeusDB не смог загрузить сохранённое поколение индекса."
            ) from error
        return cast(ZeusDBIndex, index)

    def save(self, index: ZeusDBIndex, path: Path) -> None:
        try:
            index.save(str(path))
        except Exception as error:
            raise VectorStoreUnavailableError("ZeusDB не смог сохранить индекс.") from error

    def move_generation(self, source: Path, destination: Path) -> None:
        try:
            os.replace(source, destination)
        except OSError as error:
            raise VectorStoreUnavailableError(
                "ZeusDB не смог атомарно опубликовать новое поколение."
            ) from error


@dataclass(frozen=True, slots=True)
class _ManifestEnvelope:
    """Файл рядом с .zdb без голосовых векторов и персональных имён."""

    schema_version: int
    generation_id: str
    voice_pack_id: str
    dimension: int
    space: str
    vector_count: int
    digest: str

    @classmethod
    def from_manifest(cls, manifest: IndexManifest) -> _ManifestEnvelope:
        return cls(
            schema_version=_MANIFEST_SCHEMA_VERSION,
            generation_id=manifest.generation_id,
            voice_pack_id=manifest.voice_pack_id,
            dimension=manifest.dimension,
            space=manifest.space,
            vector_count=manifest.vector_count,
            digest=manifest.digest,
        )

    def to_manifest(self) -> IndexManifest:
        if self.schema_version != _MANIFEST_SCHEMA_VERSION:
            raise VectorStoreUnavailableError("Неподдерживаемая версия manifest поколения ZeusDB.")
        _validate_generation_id(self.generation_id)
        if not self.voice_pack_id:
            raise VectorStoreUnavailableError("В manifest отсутствует идентификатор voice pack.")
        if self.dimension < 1 or self.space != "cosine" or self.vector_count < 0:
            raise VectorStoreUnavailableError("Manifest поколения ZeusDB некорректен.")
        if not re.fullmatch(r"[0-9a-f]{64}", self.digest):
            raise VectorStoreUnavailableError("Digest manifest поколения ZeusDB некорректен.")
        return IndexManifest(
            generation_id=self.generation_id,
            voice_pack_id=self.voice_pack_id,
            dimension=self.dimension,
            space=self.space,
            vector_count=self.vector_count,
            digest=self.digest,
        )


class ZeusDBVoiceVectorStore:
    """Persistent VoiceVectorIndex для общих голосовых эмбеддингов.

    Каждое сохранение строит новую директорию поколения. После проверки
    целостности она публикуется одной операцией rename. Идентичный повтор
    идемпотентен, а перезапись уже опубликованного поколения запрещена.
    """

    def __init__(self, root_path: Path | None, *, transport: ZeusDBTransport | None = None) -> None:
        self._root_path = root_path.resolve() if root_path is not None else None
        self._transport = transport

    def status(self) -> VectorStoreStatus:
        """Возвращает явную готовность без fallback для векторов."""

        if self._root_path is None:
            return VectorStoreStatus(
                VectorStoreAvailability.UNAVAILABLE,
                "Не задана директория общего индекса ZeusDB.",
            )
        if self._root_path.exists() and not self._root_path.is_dir():
            return VectorStoreStatus(
                VectorStoreAvailability.UNAVAILABLE,
                "Путь общего индекса ZeusDB не является директорией.",
            )
        try:
            self._get_transport()
        except VectorStoreUnavailableError as error:
            return VectorStoreStatus(VectorStoreAvailability.UNAVAILABLE, str(error))
        return VectorStoreStatus(VectorStoreAvailability.READY)

    def save_generation(
        self,
        *,
        generation_id: str,
        voice_pack_id: str,
        dimension: int,
        space: str,
        records: Iterable[VectorRecord],
    ) -> IndexManifest:
        """Создаёт immutable snapshot в ZeusDB либо подтверждает identical retry."""

        _validate_generation_id(generation_id)
        if not voice_pack_id:
            raise ValueError("Идентификатор voice pack обязателен.")
        if dimension < 1:
            raise ValueError("Размерность эмбеддинга должна быть положительной.")
        if space != "cosine":
            raise ValueError("Голосовой индекс поддерживает только space='cosine'.")

        snapshot_records = tuple(records)
        _validate_records(snapshot_records, dimension)
        manifest = _build_manifest(
            generation_id=generation_id,
            voice_pack_id=voice_pack_id,
            dimension=dimension,
            space=space,
            records=snapshot_records,
        )
        root_path = self._require_root()
        generation_path = self._generation_path(root_path, generation_id)
        transport = self._get_transport()

        if generation_path.exists():
            existing = self._load_existing_manifest(generation_path)
            if existing != manifest:
                raise ImmutableIndexGenerationError(
                    "Immutable generation нельзя перезаписать другими данными."
                )
            self._verify_saved_generation(transport, generation_path, existing)
            return existing

        try:
            root_path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise VectorStoreUnavailableError(
                "Невозможно создать директорию общего индекса ZeusDB."
            ) from error

        temporary_path = root_path / f".{generation_id}.{uuid4().hex}.zdb"
        try:
            index = transport.create(
                dimension=dimension,
                space=space,
                expected_size=max(1, len(snapshot_records)),
            )
            index.add_metadata(_index_metadata(manifest))
            if snapshot_records:
                index.add(_as_zeus_records(snapshot_records))
            transport.save(index, temporary_path)
            self._verify_saved_generation(transport, temporary_path, manifest)
            _write_manifest(temporary_path / _MANIFEST_FILE_NAME, manifest)
            transport.move_generation(temporary_path, generation_path)
        except VectorStoreUnavailableError:
            raise
        except Exception as error:
            raise VectorStoreUnavailableError(
                "ZeusDB не смог подготовить новое поколение голосового индекса."
            ) from error
        return manifest

    def load_generation(self, generation_id: str) -> IndexManifest:
        """Проверяет manifest, метаданные и сохранённый индекс перед использованием."""

        _validate_generation_id(generation_id)
        root_path = self._require_root()
        generation_path = self._generation_path(root_path, generation_id)
        if not generation_path.exists():
            raise IndexGenerationNotFoundError("Поколение индекса не найдено.")
        manifest = self._load_existing_manifest(generation_path)
        self._verify_saved_generation(self._get_transport(), generation_path, manifest)
        return manifest

    def search(
        self,
        *,
        generation_id: str,
        vector: Sequence[float],
        limit: int,
        active_profile_ids: set[str] | None = None,
    ) -> tuple[VectorSearchHit, ...]:
        """Возвращает только технические ID и distance; эмбеддинги не выдаются."""

        if limit < 1:
            raise ValueError("Лимит поиска должен быть положительным.")
        manifest = self.load_generation(generation_id)
        query = _validate_query(vector, manifest.dimension)
        root_path = self._require_root()
        generation_path = self._generation_path(root_path, generation_id)
        transport = self._get_transport()
        index = self._verify_saved_generation(transport, generation_path, manifest)
        try:
            total_records = index.get_vector_count()
            if total_records < 0:
                raise ValueError("ZeusDB вернул отрицательное число записей.")
            raw_hits = index.search(query, top_k=total_records) if total_records else ()
        except VectorStoreUnavailableError:
            raise
        except Exception as error:
            raise VectorStoreUnavailableError(
                "ZeusDB не смог выполнить поиск по голосу."
            ) from error

        hits = _parse_search_hits(raw_hits, active_profile_ids)
        ordered_hits = sorted(
            hits,
            key=lambda item: (item.distance, item.profile_id, item.record_id),
        )
        return tuple(ordered_hits[:limit])

    def _get_transport(self) -> ZeusDBTransport:
        if self._transport is None:
            self._transport = NativeZeusDBTransport()
        return self._transport

    def _require_root(self) -> Path:
        if self._root_path is None:
            raise VectorStoreUnavailableError("Не задана директория общего индекса ZeusDB.")
        return self._root_path

    @staticmethod
    def _generation_path(root_path: Path, generation_id: str) -> Path:
        path = root_path / f"{generation_id}.zdb"
        try:
            path.resolve().relative_to(root_path)
        except ValueError as error:
            raise VectorStoreUnavailableError(
                "Путь поколения ZeusDB выходит за пределы настроенной директории."
            ) from error
        return path

    @staticmethod
    def _load_existing_manifest(generation_path: Path) -> IndexManifest:
        if not generation_path.is_dir() or generation_path.is_symlink():
            raise VectorStoreUnavailableError("Директория поколения ZeusDB повреждена.")
        return _read_manifest(generation_path / _MANIFEST_FILE_NAME)

    @staticmethod
    def _verify_saved_generation(
        transport: ZeusDBTransport,
        generation_path: Path,
        manifest: IndexManifest,
    ) -> ZeusDBIndex:
        try:
            index = transport.load(generation_path)
            metadata = {
                key: index.get_metadata(key)
                for key in (
                    f"{_METADATA_PREFIX}generation_id",
                    f"{_METADATA_PREFIX}voice_pack_id",
                    f"{_METADATA_PREFIX}dimension",
                    f"{_METADATA_PREFIX}space",
                    f"{_METADATA_PREFIX}vector_count",
                    f"{_METADATA_PREFIX}digest",
                )
            }
            expected = _index_metadata(manifest)
            if metadata != expected or index.get_vector_count() != manifest.vector_count:
                raise VectorStoreUnavailableError(
                    "Проверка целостности сохранённого поколения ZeusDB не пройдена."
                )
        except VectorStoreUnavailableError:
            raise
        except Exception as error:
            raise VectorStoreUnavailableError(
                "Не удалось проверить сохранённое поколение ZeusDB."
            ) from error
        return index


def _validate_generation_id(generation_id: str) -> None:
    if not _GENERATION_ID_RE.fullmatch(generation_id) or generation_id in {".", ".."}:
        raise ValueError("Идентификатор поколения имеет небезопасный формат.")


def _validate_records(records: Sequence[VectorRecord], dimension: int) -> None:
    record_ids = [record.record_id for record in records]
    if len(record_ids) != len(set(record_ids)):
        raise ValueError("Идентификаторы vector record должны быть уникальны.")
    if any(len(record.values) != dimension for record in records):
        raise ValueError("Размерность записи не совпадает с поколением.")


def _validate_query(vector: Sequence[float], dimension: int) -> tuple[float, ...]:
    try:
        query = tuple(float(value) for value in vector)
    except (TypeError, ValueError) as error:
        raise ValueError("Query-вектор должен состоять из чисел.") from error
    if len(query) != dimension:
        raise ValueError("Размерность query-вектора не совпадает с поколением.")
    if not all(isfinite(value) for value in query) or not any(value != 0 for value in query):
        raise ValueError("Query-вектор должен быть конечным и ненулевым.")
    return query


def _build_manifest(
    *,
    generation_id: str,
    voice_pack_id: str,
    dimension: int,
    space: str,
    records: Sequence[VectorRecord],
) -> IndexManifest:
    digest = sha256()
    digest.update(f"{generation_id}|{voice_pack_id}|{dimension}|{space}\\n".encode())
    for record in sorted(records, key=lambda item: item.record_id):
        values = ",".join(format(value, ".17g") for value in record.values)
        digest.update(
            (
                f"{record.record_id}|{record.profile_id}|{record.sample_id}|"
                f"{int(record.is_active)}|{values}\\n"
            ).encode()
        )
    return IndexManifest(
        generation_id=generation_id,
        voice_pack_id=voice_pack_id,
        dimension=dimension,
        space=space,
        vector_count=len(records),
        digest=digest.hexdigest(),
    )


def _index_metadata(manifest: IndexManifest) -> dict[str, str]:
    return {
        f"{_METADATA_PREFIX}generation_id": manifest.generation_id,
        f"{_METADATA_PREFIX}voice_pack_id": manifest.voice_pack_id,
        f"{_METADATA_PREFIX}dimension": str(manifest.dimension),
        f"{_METADATA_PREFIX}space": manifest.space,
        f"{_METADATA_PREFIX}vector_count": str(manifest.vector_count),
        f"{_METADATA_PREFIX}digest": manifest.digest,
    }


def _as_zeus_records(records: Sequence[VectorRecord]) -> list[dict[str, object]]:
    return [
        {
            "id": record.record_id,
            "values": list(record.values),
            "metadata": {
                "profile_id": record.profile_id,
                "sample_id": record.sample_id,
                "is_active": record.is_active,
            },
        }
        for record in records
    ]


def _write_manifest(path: Path, manifest: IndexManifest) -> None:
    payload = json.dumps(
        asdict(_ManifestEnvelope.from_manifest(manifest)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)
        os.replace(temporary_path, path)
    except OSError as error:
        raise VectorStoreUnavailableError(
            "Невозможно атомарно записать manifest поколения ZeusDB."
        ) from error


def _read_manifest(path: Path) -> IndexManifest:
    try:
        raw_payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw_payload, dict):
            raise TypeError("Manifest не является объектом.")
        envelope = _ManifestEnvelope(**raw_payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise VectorStoreUnavailableError(
            "Manifest поколения ZeusDB повреждён или отсутствует."
        ) from error
    return envelope.to_manifest()


def _parse_search_hits(
    raw_hits: Sequence[Mapping[str, object]],
    active_profile_ids: set[str] | None,
) -> list[VectorSearchHit]:
    hits: list[VectorSearchHit] = []
    for raw_hit in raw_hits:
        try:
            record_id = raw_hit["id"]
            score = raw_hit["score"]
            metadata = raw_hit["metadata"]
            if not isinstance(record_id, str) or not record_id:
                raise ValueError("ZeusDB вернул запись без корректного ID.")
            if isinstance(score, bool):
                raise ValueError("ZeusDB вернул некорректную distance.")
            distance = float(score)
            if not isfinite(distance) or distance < 0:
                raise ValueError("ZeusDB вернул некорректную distance.")
            if not isinstance(metadata, Mapping):
                raise ValueError("ZeusDB вернул запись без metadata.")
            profile_id = metadata.get("profile_id")
            is_active = metadata.get("is_active")
            if not isinstance(profile_id, str) or not profile_id or is_active is not True:
                continue
        except (KeyError, TypeError, ValueError) as error:
            raise VectorStoreUnavailableError(
                "ZeusDB вернул запись поиска в неожиданном формате."
            ) from error
        if active_profile_ids is None or profile_id in active_profile_ids:
            hits.append(
                VectorSearchHit(
                    record_id=record_id,
                    profile_id=profile_id,
                    distance=distance,
                )
            )
    return hits
