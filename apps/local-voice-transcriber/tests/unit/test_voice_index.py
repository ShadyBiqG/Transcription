from __future__ import annotations

import pytest

from local_voice_transcriber.voice.index import (
    ImmutableIndexGenerationError,
    IndexGenerationNotFoundError,
    InMemoryVoiceVectorIndex,
    VectorRecord,
    VoiceVectorIndex,
)


def test_fake_index_uses_zeus_lower_distance_semantics_and_never_returns_vectors() -> None:
    index = InMemoryVoiceVectorIndex()
    index.save_generation(
        generation_id="gen-1",
        voice_pack_id="pack-1",
        dimension=2,
        space="cosine",
        records=(
            VectorRecord("record-alex", "alex", "sample-a", (1.0, 0.0)),
            VectorRecord("record-boris", "boris", "sample-b", (0.0, 1.0)),
        ),
    )

    hits = index.search(generation_id="gen-1", vector=(0.9, 0.1), limit=2)

    assert isinstance(index, VoiceVectorIndex)
    assert [hit.profile_id for hit in hits] == ["alex", "boris"]
    assert hits[0].distance < hits[1].distance
    assert not hasattr(hits[0], "values")


def test_generation_is_immutable_but_identical_retry_is_idempotent() -> None:
    index = InMemoryVoiceVectorIndex()
    records = (VectorRecord("record-a", "alex", "sample-a", (1.0, 0.0)),)

    first = index.save_generation(
        generation_id="gen-1",
        voice_pack_id="pack-1",
        dimension=2,
        space="cosine",
        records=records,
    )
    second = index.save_generation(
        generation_id="gen-1",
        voice_pack_id="pack-1",
        dimension=2,
        space="cosine",
        records=records,
    )

    assert first == second == index.load_generation("gen-1")
    with pytest.raises(ImmutableIndexGenerationError):
        index.save_generation(
            generation_id="gen-1",
            voice_pack_id="pack-1",
            dimension=2,
            space="cosine",
            records=(VectorRecord("record-a", "alex", "sample-a", (0.0, 1.0)),),
        )


def test_search_filters_archived_profile_and_validates_dimension() -> None:
    index = InMemoryVoiceVectorIndex()
    index.save_generation(
        generation_id="gen-1",
        voice_pack_id="pack-1",
        dimension=2,
        space="cosine",
        records=(
            VectorRecord("record-alex", "alex", "sample-a", (1.0, 0.0)),
            VectorRecord("record-boris", "boris", "sample-b", (0.8, 0.2)),
        ),
    )

    hits = index.search(
        generation_id="gen-1", vector=(1.0, 0.0), limit=3, active_profile_ids={"boris"}
    )

    assert [hit.profile_id for hit in hits] == ["boris"]
    with pytest.raises(ValueError, match="Размерность"):
        index.search(generation_id="gen-1", vector=(1.0,), limit=1)
    with pytest.raises(IndexGenerationNotFoundError):
        index.load_generation("missing")


def test_fake_rejects_invalid_vectors_and_record_duplicates() -> None:
    with pytest.raises(ValueError, match="Нулевой"):
        VectorRecord("record", "alex", "sample", (0.0, 0.0))

    index = InMemoryVoiceVectorIndex()
    with pytest.raises(ValueError, match="уникальны"):
        index.save_generation(
            generation_id="gen-1",
            voice_pack_id="pack-1",
            dimension=2,
            space="cosine",
            records=(
                VectorRecord("same", "alex", "sample-a", (1.0, 0.0)),
                VectorRecord("same", "boris", "sample-b", (0.0, 1.0)),
            ),
        )
