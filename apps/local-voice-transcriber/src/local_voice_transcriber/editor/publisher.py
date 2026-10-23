"""Публикация immutable ASR source в исходную ревизию редактора."""

from __future__ import annotations

from collections.abc import Callable

from ..cleaning import RuleBasedDeletionCleaner, TranscriptCleaner
from ..diarization import AsrSegmentReference, DiarizationInterval, DiarizationMapper
from ..jobs.models import Job, JobRun
from ..jobs.repository import JobRepository
from ..voice.automatic_matching import AutomaticSpeakerGroupMatch
from ..voice.models import VoiceMatchStatus
from .models import GroupSnapshot, SourceSegmentSnapshot, TranscriptRevision
from .repository import EditorRepository


class AsrBaselinePublisher:
    """Создаёт baseline только из immutable ASR и не меняет raw source."""

    def __init__(
        self,
        jobs: JobRepository,
        editor: EditorRepository,
        *,
        active_profile_label: Callable[[str], str | None] | None = None,
        cleaner: TranscriptCleaner | None = None,
    ) -> None:
        self._jobs = jobs
        self._editor = editor
        self._active_profile_label = active_profile_label
        self._cleaner = cleaner or RuleBasedDeletionCleaner()

    def publish(
        self,
        job: Job,
        run: JobRun,
        *,
        diarization_intervals: tuple[DiarizationInterval, ...] = (),
        voice_matches: tuple[AutomaticSpeakerGroupMatch, ...] = (),
    ) -> TranscriptRevision:
        """Публикует baseline из ASR, групп диаризации и безопасных auto-assignment."""

        source_rows = self._jobs.source_segments(run.id)
        mappings = DiarizationMapper().map(
            (
                AsrSegmentReference(
                    segment_id=str(row["id"]),
                    start_ms=int(row["start_ms"]),
                    end_ms=int(row["end_ms"]),
                )
                for row in source_rows
            ),
            diarization_intervals,
        )
        mapping_by_segment = {item.asr_segment_id: item for item in mappings}
        source_segments = tuple(
            SourceSegmentSnapshot(
                source_segment_id=str(row["id"]),
                ordinal=int(row["ordinal"]),
                text=str(row["raw_text"]),
                primary_group_id=mapping_by_segment[str(row["id"])].primary_speaker_group_id,
                speaker_group_ids=tuple(
                    sorted(
                        {
                            link.speaker_group_id
                            for link in mapping_by_segment[str(row["id"])].links
                        }
                    )
                ),
                requires_review=mapping_by_segment[str(row["id"])].requires_review,
            )
            for row in source_rows
        )
        group_order = tuple(
            dict.fromkeys(interval.speaker_group_id for interval in diarization_intervals)
        )
        matches_by_group = {item.speaker_group_id: item for item in voice_matches}
        groups = tuple(
            self._group_snapshot(
                group_id=group_id,
                generic_label=f"Спикер {index}",
                match=matches_by_group.get(group_id),
            )
            for index, group_id in enumerate(group_order, start=1)
        )
        self._editor.create_baseline(
            owner_id=job.owner_id,
            job_id=job.id,
            job_run_id=run.id,
            groups=groups,
            source_segments=source_segments,
        )
        return self._editor.create_clean_draft(
            owner_id=job.owner_id,
            job_id=job.id,
            cleaner=self._cleaner,
        ).revision

    def _group_snapshot(
        self,
        *,
        group_id: str,
        generic_label: str,
        match: AutomaticSpeakerGroupMatch | None,
    ) -> GroupSnapshot:
        """Проецирует auto-assignment в подпись без раскрытия технических данных."""

        if match is None:
            return GroupSnapshot(
                speaker_group_id=group_id,
                label_snapshot=generic_label,
                profile_id=None,
                assignment_status="unknown",
                provenance="diarization",
            )
        return GroupSnapshot(
            speaker_group_id=group_id,
            label_snapshot=self._label_for_match(match, fallback=generic_label),
            profile_id=match.profile_id,
            assignment_status=match.decision.status.value,
            provenance=match.provenance.value,
        )

    def _label_for_match(self, match: AutomaticSpeakerGroupMatch, *, fallback: str) -> str:
        """Возвращает только каноническое имя активного auto-assignment.

        Сбой directory не должен срывать публикацию личной транскрипции и не
        может превратить технический ``profile_id`` в пользовательскую подпись.
        """

        if (
            match.decision.status is not VoiceMatchStatus.AUTO_ASSIGNED
            or not match.profile_id
            or self._active_profile_label is None
        ):
            return fallback
        try:
            label = self._active_profile_label(match.profile_id)
        except Exception:
            return fallback
        clean_label = " ".join(label.split()) if label else ""
        return clean_label or fallback
