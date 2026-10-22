from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from local_voice_transcriber.asr import AsrRecognition, AsrSegment
from local_voice_transcriber.diarization import DiarizationInterval
from local_voice_transcriber.jobs.media import FfmpegNormalizer, MediaNormalizationError
from local_voice_transcriber.jobs.pipeline import AsrJobProcessor, JobCancelled
from local_voice_transcriber.jobs.repository import JobRepository


class FakeNormalizer:
    def normalize(self, source: Path, destination: Path) -> None:
        assert source.is_file()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"RIFF")


class FakeAsr:
    def transcribe(self, wav_path: Path) -> AsrRecognition:
        assert wav_path.is_file()
        return AsrRecognition(
            language="ru",
            segments=(
                AsrSegment(
                    start_ms=0,
                    end_ms=1000,
                    text="Тест",
                    confidence=None,
                    tokens=(),
                    words=(),
                    word_timing_status="unavailable",
                ),
            ),
        )


class FakePublisher:
    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository
        self.calls: list[tuple[str, str]] = []

    def publish(self, job, run, *, diarization_intervals=()) -> None:  # type: ignore[no-untyped-def]
        assert self._repository.source_segments(run.id)
        self.calls.append((job.id, run.id))


class FakeDiarizer:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def diarize(self, wav_path: Path) -> tuple[DiarizationInterval, ...]:
        self.paths.append(wav_path)
        return (
            DiarizationInterval(
                interval_id="interval-1",
                start_ms=0,
                end_ms=1_000,
                raw_speaker_label="0",
                speaker_group_id="local-speaker:0",
            ),
        )


class CapturingPublisher(FakePublisher):
    def __init__(self, repository: JobRepository) -> None:
        super().__init__(repository)
        self.intervals: tuple[DiarizationInterval, ...] = ()

    def publish(self, job, run, *, diarization_intervals=()) -> None:  # type: ignore[no-untyped-def]
        self.intervals = tuple(diarization_intervals)
        super().publish(job, run, diarization_intervals=diarization_intervals)


def test_pipeline_persists_immutable_asr_source_after_normalization(tmp_path: Path) -> None:
    repository, jobs_root, job, run = claimed_run(tmp_path)
    processor = AsrJobProcessor(repository, FakeNormalizer(), FakeAsr(), jobs_root=jobs_root)

    asyncio.run(processor.process(job, run, lambda: False))

    source = repository.source_segments(run.id)
    assert len(source) == 1
    assert source[0]["raw_text"] == "Тест"
    assert source[0]["word_timing_status"] == "unavailable"
    assert (jobs_root / job.id / "runs" / run.id / "normalized.wav").is_file()


def test_pipeline_checks_cancel_before_calling_local_tools(tmp_path: Path) -> None:
    repository, jobs_root, job, run = claimed_run(tmp_path)
    processor = AsrJobProcessor(repository, FakeNormalizer(), FakeAsr(), jobs_root=jobs_root)

    with pytest.raises(JobCancelled):
        asyncio.run(processor.process(job, run, lambda: True))

    assert not repository.source_segments(run.id)


def test_pipeline_publishes_editor_baseline_only_after_source_is_immutable(tmp_path: Path) -> None:
    repository, jobs_root, job, run = claimed_run(tmp_path)
    publisher = FakePublisher(repository)
    processor = AsrJobProcessor(
        repository,
        FakeNormalizer(),
        FakeAsr(),
        jobs_root=jobs_root,
        publisher=publisher,
    )

    asyncio.run(processor.process(job, run, lambda: False))

    assert publisher.calls == [(job.id, run.id)]


def test_pipeline_passes_local_diarization_groups_to_publisher(tmp_path: Path) -> None:
    repository, jobs_root, job, run = claimed_run(tmp_path)
    publisher = CapturingPublisher(repository)
    diarizer = FakeDiarizer()
    processor = AsrJobProcessor(
        repository,
        FakeNormalizer(),
        FakeAsr(),
        jobs_root=jobs_root,
        publisher=publisher,
        diarizer=diarizer,
    )

    asyncio.run(processor.process(job, run, lambda: False))

    assert diarizer.paths == [jobs_root / job.id / "runs" / run.id / "normalized.wav"]
    assert [item.speaker_group_id for item in publisher.intervals] == ["local-speaker:0"]


def test_ffmpeg_normalizer_rejects_media_outside_candidate_jobs_root(tmp_path: Path) -> None:
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"not-executed")
    outside = tmp_path / "outside.webm"
    outside.write_bytes(b"webm")
    normalizer = FfmpegNormalizer(executable, jobs_root=jobs_root)

    with pytest.raises(MediaNormalizationError):
        normalizer.normalize(outside, jobs_root / "job-1" / "normalized.wav")


def claimed_run(tmp_path: Path):
    jobs_root = tmp_path / "jobs"
    source = jobs_root / "job-1" / "source.webm"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"webm")
    repository = JobRepository(tmp_path / "next.sqlite3")
    repository.migrate()
    repository.create_job(
        job_id="job-1",
        owner_id="owner-1",
        source_name="source.webm",
        source_path=str(source),
    )
    repository.create_run(run_id="run-1", job_id="job-1", request_id="r1", asr_pack_id="asr-1")
    claimed = repository.claim_next_run(worker_id="worker", lease_seconds=60)
    assert claimed is not None
    return repository, jobs_root, *claimed
