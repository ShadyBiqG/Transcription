"""Изолированный extracting-audio -> ASR pipeline для одного job run."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..asr.models import AsrRecognition
from ..diarization import DiarizationInterval
from .media import FfmpegNormalizer
from .models import Job, JobRun, JobStage
from .repository import JobRepository


class AsrAdapter(Protocol):
    def transcribe(self, wav_path: Path) -> AsrRecognition: ...


class SourcePublisher(Protocol):
    """Публикует уже сохранённый source как immutable редакторскую ревизию."""

    def publish(
        self,
        job: Job,
        run: JobRun,
        *,
        diarization_intervals: tuple[DiarizationInterval, ...] = (),
    ) -> object: ...


class DiarizationAdapter(Protocol):
    """Строго локальный шаг, строящий группы для текущего job run."""

    def diarize(self, wav_path: Path) -> tuple[DiarizationInterval, ...]: ...


class JobCancelled(RuntimeError):
    """Worker увидел явную отмену на безопасной границе стадии."""


class AsrJobProcessor:
    """Не публикует транскрипт: только создаёт immutable ASR source result."""

    def __init__(
        self,
        repository: JobRepository,
        normalizer: FfmpegNormalizer,
        asr: AsrAdapter,
        *,
        jobs_root: Path,
        publisher: SourcePublisher | None = None,
        diarizer: DiarizationAdapter | None = None,
    ) -> None:
        self._repository = repository
        self._normalizer = normalizer
        self._asr = asr
        self._jobs_root = jobs_root
        self._publisher = publisher
        self._diarizer = diarizer

    async def process(
        self, job: Job, run: JobRun, cancellation_requested: Callable[[], bool]
    ) -> None:
        self._raise_if_cancelled(cancellation_requested)
        source = Path(job.source_path)
        normalized = self._jobs_root / job.id / "runs" / run.id / "normalized.wav"
        self._repository.stage_manifest(
            run_id=run.id,
            worker_id=run.lease_owner or "",
            stage=JobStage.EXTRACTING_AUDIO,
            manifest={"normalized_audio": str(normalized.relative_to(self._jobs_root))},
        )
        await asyncio.to_thread(self._normalizer.normalize, source, normalized)
        self._raise_if_cancelled(cancellation_requested)
        self._repository.stage_manifest(
            run_id=run.id,
            worker_id=run.lease_owner or "",
            stage=JobStage.ASR,
            manifest={"input": str(normalized.relative_to(self._jobs_root))},
        )
        recognition = await asyncio.to_thread(self._asr.transcribe, normalized)
        self._raise_if_cancelled(cancellation_requested)
        self._repository.store_asr_recognition(run_id=run.id, recognition=recognition)
        diarization_intervals: tuple[DiarizationInterval, ...] = ()
        if self._diarizer is not None:
            self._repository.stage_manifest(
                run_id=run.id,
                worker_id=run.lease_owner or "",
                stage=JobStage.DIARIZING,
                manifest={"input": str(normalized.relative_to(self._jobs_root))},
            )
            diarization_intervals = await asyncio.to_thread(self._diarizer.diarize, normalized)
            self._raise_if_cancelled(cancellation_requested)
        if self._publisher is not None:
            self._repository.stage_manifest(
                run_id=run.id,
                worker_id=run.lease_owner or "",
                stage=JobStage.PUBLISHING,
                manifest={"editor_baseline": "pending"},
            )
            await asyncio.to_thread(
                self._publisher.publish,
                job,
                run,
                diarization_intervals=diarization_intervals,
            )
        self._repository.stage_manifest(
            run_id=run.id,
            worker_id=run.lease_owner or "",
            stage=JobStage.PUBLISHING if self._publisher is not None else JobStage.ASR,
            manifest={
                "source_records_saved": True,
                "segment_count": len(recognition.segments),
                "diarization_interval_count": len(diarization_intervals),
                "editor_baseline": self._publisher is not None,
            },
        )

    @staticmethod
    def _raise_if_cancelled(cancellation_requested: Callable[[], bool]) -> None:
        if cancellation_requested():
            raise JobCancelled("Задание отменено владельцем")
