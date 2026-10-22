"""Контрактные тесты fail-closed адаптера ZeusDB без установки реального wheel."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from local_voice_transcriber.vector_store.zeus import (
    VectorStoreAvailability,
    VectorStoreUnavailableError,
    ZeusDBVoiceVectorStore,
)
from local_voice_transcriber.voice.index import (
    ImmutableIndexGenerationError,
    IndexGenerationNotFoundError,
    VectorRecord,
)


class FakeZeusIndex:
    """Минимальный fake ZeusDB с теми же lower-distance-first результатами."""

    def __init__(self) -> None:
        self.metadata: dict[str, str] = {}
        self.records: list[dict[str, object]] = []

    def add(self, records: Sequence[Mapping[str, object]]) -> None:
        self.records.extend(copy.deepcopy(list(records)))

    def add_metadata(self, metadata: Mapping[str, str]) -> None:
        self.metadata.update(metadata)

    def get_metadata(self, key: str) -> object:
        return self.metadata.get(key)

    def get_vector_count(self) -> int:
        return len(self.records)

    def search(self, vector: Sequence[float], *, top_k: int) -> Sequence[Mapping[str, object]]:
        def distance(record: Mapping[str, object]) -> float:
            values = record["values"]
            assert isinstance(values, list)
            return sum(
                (float(left) - float(right)) ** 2
                for left, right in zip(vector, values, strict=True)
            )

        return [
            {
                "id": record["id"],
                "score": distance(record),
                "metadata": record["metadata"],
            }
            for record in sorted(self.records, key=distance)[:top_k]
        ]


class FakeZeusTransport:
    """Fake transport имитирует durable snapshot без реального ZeusDB."""

    def __init__(self) -> None:
        self._saved: dict[Path, FakeZeusIndex] = {}
        self.created: list[tuple[int, str, int]] = []
        self.fail_load = False

    def create(self, *, dimension: int, space: str, expected_size: int) -> FakeZeusIndex:
        self.created.append((dimension, space, expected_size))
        return FakeZeusIndex()

    def load(self, path: Path) -> FakeZeusIndex:
        if self.fail_load:
            raise RuntimeError("storage unavailable")
        return copy.deepcopy(self._saved[path.resolve()])

    def save(self, index: FakeZeusIndex, path: Path) -> None:
        path.mkdir()
        self._saved[path.resolve()] = copy.deepcopy(index)

    def move_generation(self, source: Path, destination: Path) -> None:
        source.replace(destination)
        self._saved[destination.resolve()] = self._saved.pop(source.resolve())


def _records() -> tuple[VectorRecord, ...]:
    return (
        VectorRecord("record-alex", "profile-alex", "sample-alex", (1.0, 0.0)),
        VectorRecord("record-boris", "profile-boris", "sample-boris", (0.0, 1.0)),
        VectorRecord(
            "record-archived",
            "profile-archived",
            "sample-archived",
            (0.9, 0.1),
            is_active=False,
        ),
    )


def test_status_is_explicitly_unavailable_without_configuration() -> None:
    store = ZeusDBVoiceVectorStore(None)

    assert store.status().availability is VectorStoreAvailability.UNAVAILABLE
    with pytest.raises(VectorStoreUnavailableError, match="директория"):
        store.save_generation(
            generation_id="generation-1",
            voice_pack_id="voice-pack-1",
            dimension=2,
            space="cosine",
            records=_records(),
        )


def test_saves_verified_immutable_generation_with_injected_zeus_transport(tmp_path: Path) -> None:
    transport = FakeZeusTransport()
    store = ZeusDBVoiceVectorStore(tmp_path / "shared-zeus", transport=transport)

    first = store.save_generation(
        generation_id="generation-1",
        voice_pack_id="voice-pack-1",
        dimension=2,
        space="cosine",
        records=_records(),
    )
    second = store.save_generation(
        generation_id="generation-1",
        voice_pack_id="voice-pack-1",
        dimension=2,
        space="cosine",
        records=_records(),
    )

    assert store.status().availability is VectorStoreAvailability.READY
    assert first == second == store.load_generation("generation-1")
    assert transport.created == [(2, "cosine", 3)]
    assert (tmp_path / "shared-zeus" / "generation-1.zdb").is_dir()
    with pytest.raises(ImmutableIndexGenerationError):
        store.save_generation(
            generation_id="generation-1",
            voice_pack_id="voice-pack-1",
            dimension=2,
            space="cosine",
            records=(VectorRecord("record-other", "profile-other", "sample", (1.0, 0.0)),),
        )


def test_search_does_not_return_vectors_or_inactive_or_archived_profiles(tmp_path: Path) -> None:
    store = ZeusDBVoiceVectorStore(tmp_path / "shared-zeus", transport=FakeZeusTransport())
    store.save_generation(
        generation_id="generation-1",
        voice_pack_id="voice-pack-1",
        dimension=2,
        space="cosine",
        records=_records(),
    )

    hits = store.search(
        generation_id="generation-1",
        vector=(1.0, 0.0),
        limit=5,
        active_profile_ids={"profile-boris"},
    )

    assert [(hit.profile_id, hit.record_id) for hit in hits] == [
        ("profile-boris", "record-boris")
    ]
    assert not hasattr(hits[0], "values")


def test_corrupt_or_unavailable_zeus_never_falls_back_to_sqlite_or_memory(tmp_path: Path) -> None:
    transport = FakeZeusTransport()
    store = ZeusDBVoiceVectorStore(tmp_path / "shared-zeus", transport=transport)
    store.save_generation(
        generation_id="generation-1",
        voice_pack_id="voice-pack-1",
        dimension=2,
        space="cosine",
        records=_records(),
    )
    transport.fail_load = True

    with pytest.raises(VectorStoreUnavailableError, match="сохранённое поколение"):
        store.load_generation("generation-1")
    with pytest.raises(VectorStoreUnavailableError, match="сохранённое поколение"):
        store.search(generation_id="generation-1", vector=(1.0, 0.0), limit=1)


def test_refuses_invalid_generation_path_and_missing_generation(tmp_path: Path) -> None:
    store = ZeusDBVoiceVectorStore(tmp_path / "shared-zeus", transport=FakeZeusTransport())

    with pytest.raises(ValueError, match="небезопасный"):
        store.save_generation(
            generation_id="../escape",
            voice_pack_id="voice-pack-1",
            dimension=2,
            space="cosine",
            records=_records(),
        )
    with pytest.raises(IndexGenerationNotFoundError):
        store.load_generation("missing")


def test_native_zeusdb_package_creates_persists_and_searches(tmp_path: Path) -> None:
    """Проверяет реальный wheel, а не только injected fake transport."""

    store = ZeusDBVoiceVectorStore(tmp_path / "shared-zeus")
    store.save_generation(
        generation_id="native-1",
        voice_pack_id="voice-pack-1",
        dimension=2,
        space="cosine",
        records=(VectorRecord("record-1", "profile-1", "sample-1", (1.0, 0.0)),),
    )

    hits = store.search(generation_id="native-1", vector=(1.0, 0.0), limit=1)

    assert [(hit.profile_id, hit.distance) for hit in hits] == [("profile-1", 0.0)]
