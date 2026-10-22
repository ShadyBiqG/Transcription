"""Публикация immutable ASR source в исходную ревизию редактора."""

from __future__ import annotations

from ..diarization import AsrSegmentReference, DiarizationInterval, DiarizationMapper
from ..jobs.models import Job, JobRun
from ..jobs.repository import JobRepository
from .models import GroupSnapshot, SourceSegmentSnapshot, TranscriptRevision
from .repository import EditorRepository


class AsrBaselinePublisher:
    """Создаёт baseline только из immutable ASR и не меняет raw source."""

    def __init__(self, jobs: JobRepository, editor: EditorRepository) -> None:
        self._jobs = jobs
        self._editor = editor

    def publish(
        self,
        job: Job,
        run: JobRun,
        *,
        diarization_intervals: tuple[DiarizationInterval, ...] = (),
    ) -> TranscriptRevision:
        """Публикует baseline из ASR и локальных групп без назначения профиля."""

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
        groups = tuple(
            GroupSnapshot(
                speaker_group_id=group_id,
                label_snapshot=f"Говорящий {index}",
                assignment_status="unknown",
                provenance="diarization",
            )
            for index, group_id in enumerate(group_order, start=1)
        )
        return self._editor.create_baseline(
            owner_id=job.owner_id,
            job_id=job.id,
            job_run_id=run.id,
            groups=groups,
            source_segments=source_segments,
        )
