"""Детерминированное связывание ASR-отрезков с диаризационными интервалами."""

from __future__ import annotations

from collections.abc import Iterable

from .models import (
    AsrSegmentReference,
    DiarizationInterval,
    DiarizationLink,
    MappingPolicy,
    SegmentDiarizationMapping,
)


class DiarizationMapper:
    """Строит связи 0..N, не нормализуя и не объединяя исходные интервалы.

    Интервалы трактуются как полуоткрытые ``[start_ms, end_ms)``. Поэтому две
    реплики, касающиеся только границей, не считаются пересекающимися. Любая
    одновременная речь или несколько групп внутри одного ASR-отрезка помечаются
    как требующие проверки; автоматическое решение о говорящем над ними не
    строится в этом модуле.
    """

    def __init__(self, policy: MappingPolicy | None = None) -> None:
        self._policy = policy or MappingPolicy()

    def map(
        self,
        asr_segments: Iterable[AsrSegmentReference],
        diarization_intervals: Iterable[DiarizationInterval],
    ) -> tuple[SegmentDiarizationMapping, ...]:
        """Возвращает ровно одну проекцию на каждый ASR-отрезок в порядке входа."""

        segments = tuple(asr_segments)
        intervals = tuple(diarization_intervals)
        self._ensure_unique_asr_ids(segments)
        self._ensure_unique_interval_ids(intervals)
        return tuple(self._map_segment(segment, intervals) for segment in segments)

    def _map_segment(
        self,
        segment: AsrSegmentReference,
        intervals: tuple[DiarizationInterval, ...],
    ) -> SegmentDiarizationMapping:
        overlaps = tuple(
            (interval, self._overlap_ms(segment, interval))
            for interval in intervals
            if self._overlap_ms(segment, interval) > 0
        )
        if not overlaps:
            return SegmentDiarizationMapping(
                asr_segment_id=segment.segment_id,
                links=(),
                primary_speaker_group_id=None,
                requires_review=False,
            )

        significant_groups = {
            interval.speaker_group_id
            for interval, overlap_ms in overlaps
            if overlap_ms / self._duration_ms(segment) >= self._policy.min_significant_overlap_ratio
        }
        all_groups = {interval.speaker_group_id for interval, _ in overlaps}
        has_interval_overlap = self._has_interval_overlap(overlaps)
        requires_review = has_interval_overlap or len(all_groups) > 1
        primary_group_id = (
            next(iter(significant_groups)) if len(significant_groups) == 1 else None
        )
        primary_interval_id = self._primary_interval_id(primary_group_id, overlaps)

        links = tuple(
            DiarizationLink(
                asr_segment_id=segment.segment_id,
                diarization_interval_id=interval.interval_id,
                speaker_group_id=interval.speaker_group_id,
                overlap_ms=overlap_ms,
                overlap_ratio=overlap_ms / self._duration_ms(segment),
                is_primary=interval.interval_id == primary_interval_id,
                requires_review=requires_review,
            )
            for interval, overlap_ms in overlaps
        )
        return SegmentDiarizationMapping(
            asr_segment_id=segment.segment_id,
            links=links,
            primary_speaker_group_id=primary_group_id,
            requires_review=requires_review,
        )

    @staticmethod
    def _ensure_unique_asr_ids(items: Iterable[AsrSegmentReference]) -> None:
        seen_ids: set[str] = set()
        for item in items:
            if item.segment_id in seen_ids:
                raise ValueError("Идентификаторы ASR-отрезков должны быть уникальны.")
            seen_ids.add(item.segment_id)

    @staticmethod
    def _ensure_unique_interval_ids(items: Iterable[DiarizationInterval]) -> None:
        seen_ids: set[str] = set()
        for item in items:
            if item.interval_id in seen_ids:
                raise ValueError("Идентификаторы диаризационных интервалов должны быть уникальны.")
            seen_ids.add(item.interval_id)

    @staticmethod
    def _duration_ms(segment: AsrSegmentReference) -> int:
        return segment.end_ms - segment.start_ms

    @staticmethod
    def _overlap_ms(segment: AsrSegmentReference, interval: DiarizationInterval) -> int:
        start_ms = max(segment.start_ms, interval.start_ms)
        end_ms = min(segment.end_ms, interval.end_ms)
        return max(0, end_ms - start_ms)

    @staticmethod
    def _has_interval_overlap(overlaps: tuple[tuple[DiarizationInterval, int], ...]) -> bool:
        for index, (left, _) in enumerate(overlaps):
            for right, _ in overlaps[index + 1 :]:
                if min(left.end_ms, right.end_ms) > max(left.start_ms, right.start_ms):
                    return True
        return False

    @staticmethod
    def _primary_interval_id(
        primary_group_id: str | None,
        overlaps: tuple[tuple[DiarizationInterval, int], ...],
    ) -> str | None:
        if primary_group_id is None:
            return None
        candidates = (
            (overlap_ms, interval.interval_id)
            for interval, overlap_ms in overlaps
            if interval.speaker_group_id == primary_group_id
        )
        return max(candidates, key=lambda item: (item[0], item[1]))[1]
