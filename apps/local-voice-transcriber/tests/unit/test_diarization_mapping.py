from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from local_voice_transcriber.diarization import (
    AsrSegmentReference,
    DiarizationInterval,
    DiarizationMapper,
    MappingPolicy,
)


def _asr(segment_id: str, start_ms: int, end_ms: int) -> AsrSegmentReference:
    return AsrSegmentReference(segment_id, start_ms, end_ms)


def _interval(
    interval_id: str,
    start_ms: int,
    end_ms: int,
    *,
    raw_label: str = "SPEAKER_00",
    group_id: str = "group-1",
) -> DiarizationInterval:
    return DiarizationInterval(interval_id, start_ms, end_ms, raw_label, group_id)


def test_maps_all_positive_intersections_and_preserves_raw_models() -> None:
    segment = _asr("asr-1", 1_000, 2_000)
    intervals = (
        _interval("d-1", 500, 1_200),
        _interval("d-2", 1_500, 2_500),
        _interval("d-3", 2_000, 2_300),
    )

    result = DiarizationMapper().map((segment,), intervals)

    assert len(result) == 1
    mapping = result[0]
    assert [(link.diarization_interval_id, link.overlap_ms) for link in mapping.links] == [
        ("d-1", 200),
        ("d-2", 500),
    ]
    assert [link.overlap_ratio for link in mapping.links] == [
        pytest.approx(0.2),
        pytest.approx(0.5),
    ]
    assert mapping.primary_speaker_group_id == "group-1"
    assert mapping.requires_review is False
    assert intervals[0].raw_speaker_label == "SPEAKER_00"
    with pytest.raises(FrozenInstanceError):
        intervals[0].raw_speaker_label = "изменять нельзя"  # type: ignore[misc]


def test_returns_empty_links_for_asr_segment_without_diarization() -> None:
    result = DiarizationMapper().map((_asr("asr-1", 1_000, 2_000),), ())

    assert result[0].links == ()
    assert result[0].primary_speaker_group_id is None
    assert result[0].requires_review is False


def test_multiple_speaker_groups_require_review_and_have_no_primary_when_both_significant() -> None:
    result = DiarizationMapper().map(
        (_asr("asr-1", 0, 1_000),),
        (
            _interval("d-1", 0, 500, group_id="group-alex"),
            _interval("d-2", 500, 1_000, raw_label="SPEAKER_01", group_id="group-boris"),
        ),
    )

    mapping = result[0]
    assert mapping.primary_speaker_group_id is None
    assert mapping.requires_review is True
    assert not any(link.is_primary for link in mapping.links)
    assert all(link.requires_review for link in mapping.links)


def test_simultaneous_diarization_intervals_require_review_even_for_one_group() -> None:
    result = DiarizationMapper().map(
        (_asr("asr-1", 0, 1_000),),
        (
            _interval("d-1", 0, 800),
            _interval("d-2", 400, 1_000),
        ),
    )

    mapping = result[0]
    assert mapping.primary_speaker_group_id == "group-1"
    assert mapping.requires_review is True
    assert sum(link.is_primary for link in mapping.links) == 1


def test_short_secondary_intersection_keeps_primary_but_requires_review() -> None:
    result = DiarizationMapper(MappingPolicy(min_significant_overlap_ratio=0.20)).map(
        (_asr("asr-1", 0, 1_000),),
        (
            _interval("d-1", 0, 900, group_id="group-alex"),
            _interval("d-2", 950, 1_000, raw_label="SPEAKER_01", group_id="group-boris"),
        ),
    )

    mapping = result[0]
    assert mapping.primary_speaker_group_id == "group-alex"
    assert mapping.requires_review is True
    assert [link.diarization_interval_id for link in mapping.links if link.is_primary] == ["d-1"]


def test_rejects_duplicate_source_ids() -> None:
    with pytest.raises(ValueError, match="уникальны"):
        DiarizationMapper().map((_asr("same", 0, 100), _asr("same", 100, 200)), ())


def test_rejects_invalid_interval_and_policy() -> None:
    with pytest.raises(ValueError, match="положительную длительность"):
        _asr("asr-1", 100, 100)
    with pytest.raises(ValueError, match="диапазоне"):
        MappingPolicy(min_significant_overlap_ratio=0)
