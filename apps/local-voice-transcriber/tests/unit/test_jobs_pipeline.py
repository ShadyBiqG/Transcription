from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import pytest

from local_voice_transcriber.asr import AsrRecognition, AsrSegment
from local_voice_transcriber.diarization import DiarizationInterval
from local_voice_transcriber.jobs.media import FfmpegNormalizer, MediaNormalizationError
from local_voice_transcriber.jobs.pipeline import AsrJobProcessor, JobCancelled
from local_voice_transcriber.jobs.repository import JobRepository
from local_voice_transcriber.voice.automatic_matching import AutomaticSpeakerGroupMatch
from local_voice_transcriber.voice.models import (
    DecisionProvenance,
    VoiceMatchDecision,
    VoiceMatchStatus,
)


class FakeNormalizer:
    def normalize(self, source: Path, destination: Path) -> None:
        assert source.is_file()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"RIFF")


class ValidWavNormalizer:
    def normalize(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16_000)
            audio.writeframes(b"\x00\x00" * 16_000)


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

    def publish(  # type: ignore[no-untyped-def]
        self,
        job,
        run,
        *,
        diarization_intervals=(),
        voice_matches=(),
    ) -> None:
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
        self.voice_matches: tuple[AutomaticSpeakerGroupMatch, ...] = ()

    def publish(  # type: ignore[no-untyped-def]
        self,
        job,
        run,
        *,
        diarization_intervals=(),
        voice_matches=(),
    ) -> None:
        self.intervals = tuple(diarization_intervals)
        self.voice_matches = tuple(voice_matches)
        super().publish(
            job,
            run,
            diarization_intervals=diarization_intervals,
            voice_matches=voice_matches,
        )


class FakeVoiceMatcher:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def match_groups(
        self,
        *,
        normalized_wav_path: Path,
        intervals: tuple[DiarizationInterval, ...],
    ) -> tuple[AutomaticSpeakerGroupMatch, ...]:
        self.paths.append(normalized_wav_path)
        assert intervals
        decision = VoiceMatchDecision(
            status=VoiceMatchStatus.AUTO_ASSIGNED,
            provenance=DecisionProvenance.AUTOMATIC,
            selected_profile_id="profile-alex",
            candidates=(),
            best_distance=0.1,
            second_distance=None,
            margin=None,
            evidence_count=1,
            coverage=1.0,
            has_overlap=False,
            reason="Тестовое калиброванное совпадение.",
        )
        return (
            AutomaticSpeakerGroupMatch(
                speaker_group_id=intervals[0].speaker_group_id,
                decision=decision,
                profile_id="profile-alex",
                confidence=None,
                provenance=DecisionProvenance.AUTOMATIC,
            ),
        )


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


def test_pipeline_passes_safe_voice_match_to_publisher_after_diarization(tmp_path: Path) -> None:
    repository, jobs_root, job, run = claimed_run(tmp_path)
    publisher = CapturingPublisher(repository)
    matcher = FakeVoiceMatcher()
    processor = AsrJobProcessor(
        repository,
        FakeNormalizer(),
        FakeAsr(),
        jobs_root=jobs_root,
        publisher=publisher,
        diarizer=FakeDiarizer(),
        voice_matcher=matcher,
    )

    asyncio.run(processor.process(job, run, lambda: False))

    assert matcher.paths == [jobs_root / job.id / "runs" / run.id / "normalized.wav"]
    assert publisher.voice_matches[0].profile_id == "profile-alex"


def test_pipeline_records_measured_audio_duration_and_stage_events(tmp_path: Path) -> None:
    repository, jobs_root, job, run = claimed_run(tmp_path)
    processor = AsrJobProcessor(
        repository,
        ValidWavNormalizer(),
        FakeAsr(),
        jobs_root=jobs_root,
        publisher=FakePublisher(repository),
        diarizer=FakeDiarizer(),
        voice_matcher=FakeVoiceMatcher(),
    )

    asyncio.run(processor.process(job, run, lambda: False))
    repository.mark_ready(run_id=run.id, worker_id="worker")
    data = repository.get_run(run.id).diagnostics_data
    assert data is not None
    assert data["audio_duration_ms"] == 1_000
    assert data["stages"]["publishing"]["local_speaker_group_count"] == 1
    events = data["stage_events"]
    assert [event["stage"] for event in events] == [
        "extracting_audio", "asr", "diarizing", "matching", "publishing"
    ]
    assert all(event["completed_at"] is not None for event in events)


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


@pytest.mark.parametrize(
    "suffix", [".webm", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".mp4", ".mov"]
)
def test_ffmpeg_normalizer_accepts_every_verified_upload_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    jobs_root = tmp_path / "jobs"
    source = jobs_root / "job-1" / f"source{suffix}"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"verified-container")
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"not-executed")
    destination = jobs_root / "job-1" / "runs" / "run-1" / "normalized.wav"

    def fake_run(command: list[str], **kwargs: object) -> None:
        assert command[0] == str(executable)
        assert command[command.index("-i") + 1] == str(source)
        Path(command[-1]).write_bytes(b"RIFF")

    monkeypatch.setattr("local_voice_transcriber.jobs.media.subprocess.run", fake_run)
    FfmpegNormalizer(executable, jobs_root=jobs_root).normalize(source, destination)

    assert destination.read_bytes() == b"RIFF"


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
