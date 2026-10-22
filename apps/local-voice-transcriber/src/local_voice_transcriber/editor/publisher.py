"""Публикация immutable ASR source в исходную ревизию редактора."""

from __future__ import annotations

from ..jobs.models import Job, JobRun
from ..jobs.repository import JobRepository
from .models import SourceSegmentSnapshot, TranscriptRevision
from .repository import EditorRepository


class AsrBaselinePublisher:
    """Создаёт baseline только из immutable ASR и не меняет raw source."""

    def __init__(self, jobs: JobRepository, editor: EditorRepository) -> None:
        self._jobs = jobs
        self._editor = editor

    def publish(self, job: Job, run: JobRun) -> TranscriptRevision:
        """Публикует идемпотентный baseline без выдуманного speaker-назначения."""

        source_segments = tuple(
            SourceSegmentSnapshot(
                source_segment_id=str(row["id"]),
                ordinal=int(row["ordinal"]),
                text=str(row["raw_text"]),
            )
            for row in self._jobs.source_segments(run.id)
        )
        return self._editor.create_baseline(
            owner_id=job.owner_id,
            job_id=job.id,
            job_run_id=run.id,
            groups=(),
            source_segments=source_segments,
        )
