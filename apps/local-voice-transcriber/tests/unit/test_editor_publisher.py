"""Публикация baseline из immutable ASR и локальной диаризации."""

from __future__ import annotations

from pathlib import Path

from local_voice_transcriber.asr import AsrRecognition, AsrSegment
from local_voice_transcriber.diarization import DiarizationInterval
from local_voice_transcriber.editor import AsrBaselinePublisher, EditorRepository
from local_voice_transcriber.jobs.repository import JobRepository
from local_voice_transcriber.voice.automatic_matching import AutomaticSpeakerGroupMatch
from local_voice_transcriber.voice.models import (
    DecisionProvenance,
    VoiceMatchDecision,
    VoiceMatchStatus,
)


def test_publisher_creates_local_speaker_groups_without_profile_assignment(tmp_path: Path) -> None:
    jobs_root = tmp_path / "jobs"
    source_path = jobs_root / "job-1" / "source.webm"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    jobs = JobRepository(tmp_path / "next.sqlite3")
    editor = EditorRepository(tmp_path / "next.sqlite3")
    jobs.migrate()
    editor.migrate()
    job = jobs.create_job(
        job_id="job-1",
        owner_id="owner-1",
        source_name="source.webm",
        source_path=str(source_path),
    )
    run = jobs.create_run(
        run_id="run-1",
        job_id=job.id,
        request_id="request-1",
        asr_pack_id="asr-local",
        voice_pack_id="voice-local",
    )
    jobs.store_asr_recognition(
        run_id=run.id,
        recognition=AsrRecognition(
            language="ru",
            segments=(
                AsrSegment(
                    start_ms=0,
                    end_ms=1_000,
                    text="Первая реплика",
                    confidence=None,
                    tokens=(),
                    words=(),
                    word_timing_status="unavailable",
                ),
                AsrSegment(
                    start_ms=1_000,
                    end_ms=2_000,
                    text="Вторая реплика",
                    confidence=None,
                    tokens=(),
                    words=(),
                    word_timing_status="unavailable",
                ),
            ),
        ),
    )

    revision = AsrBaselinePublisher(jobs, editor).publish(
        job,
        run,
        diarization_intervals=(
            DiarizationInterval(
                interval_id="speaker-0-first",
                start_ms=0,
                end_ms=1_000,
                raw_speaker_label="0",
                speaker_group_id="local-speaker:0",
            ),
            DiarizationInterval(
                interval_id="speaker-1-second",
                start_ms=1_000,
                end_ms=2_000,
                raw_speaker_label="1",
                speaker_group_id="local-speaker:1",
            ),
        ),
    )

    published_groups = [
        (group.speaker_group_id, group.label_snapshot, group.profile_id)
        for group in revision.groups
    ]
    assert published_groups == [
        ("local-speaker:0", "Говорящий 1", None),
        ("local-speaker:1", "Говорящий 2", None),
    ]
    assert [
        (segment.primary_group_id, segment.speaker_group_ids, segment.requires_review)
        for segment in revision.segments
    ] == [
        ("local-speaker:0", ("local-speaker:0",), False),
        ("local-speaker:1", ("local-speaker:1",), False),
    ]


def test_publisher_projects_automatic_group_match_without_vector_data(
    tmp_path: Path,
) -> None:
    jobs, editor, job, run = _prepared_job_and_run(tmp_path)
    jobs.store_asr_recognition(
        run_id=run.id,
        recognition=AsrRecognition(
            language="ru",
            segments=(
                AsrSegment(
                    start_ms=0,
                    end_ms=1_000,
                    text="Реплика",
                    confidence=None,
                    tokens=(),
                    words=(),
                    word_timing_status="unavailable",
                ),
            ),
        ),
    )
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
        reason="Пройдены калиброванные условия.",
    )

    revision = AsrBaselinePublisher(jobs, editor).publish(
        job,
        run,
        diarization_intervals=(
            DiarizationInterval(
                interval_id="speaker-0",
                start_ms=0,
                end_ms=1_000,
                raw_speaker_label="0",
                speaker_group_id="local-speaker:0",
            ),
        ),
        voice_matches=(
            AutomaticSpeakerGroupMatch(
                speaker_group_id="local-speaker:0",
                decision=decision,
                profile_id="profile-alex",
                confidence=None,
                provenance=DecisionProvenance.AUTOMATIC,
            ),
        ),
    )

    group = revision.groups[0]
    assert (group.profile_id, group.assignment_status, group.provenance) == (
        "profile-alex",
        "auto_assigned",
        "automatic",
    )


def _prepared_job_and_run(tmp_path: Path):
    jobs_root = tmp_path / "jobs"
    source_path = jobs_root / "job-1" / "source.webm"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    jobs = JobRepository(tmp_path / "next.sqlite3")
    editor = EditorRepository(tmp_path / "next.sqlite3")
    jobs.migrate()
    editor.migrate()
    job = jobs.create_job(
        job_id="job-1",
        owner_id="owner-1",
        source_name="source.webm",
        source_path=str(source_path),
    )
    run = jobs.create_run(
        run_id="run-1",
        job_id=job.id,
        request_id="request-1",
        asr_pack_id="asr-local",
        voice_pack_id="voice-local",
    )
    return jobs, editor, job, run
