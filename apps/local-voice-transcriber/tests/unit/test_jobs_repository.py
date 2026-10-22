from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from local_voice_transcriber.asr import AsrRecognition, AsrSegment, AsrToken, AsrWord
from local_voice_transcriber.jobs import ImmutableSourceError, JobRepository, JobStatus


def test_run_is_idempotent_and_single_worker_lease_is_atomic(tmp_path: Path) -> None:
    repository = prepared_repository(tmp_path)

    first = repository.create_run(
        run_id="run-1", job_id="job-1", request_id="client-request", asr_pack_id="asr-1"
    )
    second = repository.create_run(
        run_id="ignored", job_id="job-1", request_id="client-request", asr_pack_id="asr-1"
    )
    claimed = repository.claim_next_run(worker_id="worker-a", lease_seconds=30)

    assert first.id == second.id == "run-1"
    assert claimed is not None
    assert claimed[1].status is JobStatus.PROCESSING
    assert claimed[1].attempt == 1
    assert repository.claim_next_run(worker_id="worker-b", lease_seconds=30) is None
    assert repository.heartbeat(run_id="run-1", worker_id="worker-a", lease_seconds=30)
    assert not repository.heartbeat(run_id="run-1", worker_id="worker-b", lease_seconds=30)


def test_expired_run_becomes_stalled_and_retries_without_duplicate_attempt(tmp_path: Path) -> None:
    repository = prepared_repository(tmp_path)
    repository.create_run(run_id="run-1", job_id="job-1", request_id="r1", asr_pack_id="asr-1")
    repository.claim_next_run(worker_id="worker-a", lease_seconds=1)

    stalled = repository.mark_expired_as_stalled(now=datetime.now(UTC) + timedelta(seconds=2))
    requeued, exhausted = repository.requeue_stalled()
    claimed = repository.claim_next_run(worker_id="worker-b", lease_seconds=30)

    assert stalled == ["run-1"]
    assert requeued == ["run-1"]
    assert exhausted == []
    assert claimed is not None and claimed[1].attempt == 2


def test_cancelled_queued_run_is_never_claimed(tmp_path: Path) -> None:
    repository = prepared_repository(tmp_path)
    repository.create_run(run_id="run-1", job_id="job-1", request_id="r1", asr_pack_id="asr-1")

    cancelled = repository.request_cancel("run-1")

    assert cancelled.status is JobStatus.CANCELLED
    assert repository.get_job("job-1").status is JobStatus.CANCELLED
    assert repository.claim_next_run(worker_id="worker", lease_seconds=30) is None


def test_owner_scoped_job_and_run_lookups_do_not_leak_other_owner(tmp_path: Path) -> None:
    repository = prepared_repository(tmp_path)
    repository.create_run(run_id="run-1", job_id="job-1", request_id="r1", asr_pack_id="asr-1")
    repository.create_job(
        job_id="job-2",
        owner_id="owner-2",
        source_name="other.webm",
        source_path=str(tmp_path / "jobs" / "job-2" / "source.webm"),
    )

    assert [job.id for job in repository.list_jobs("owner-1")] == ["job-1"]
    assert [run.id for run in repository.list_runs(job_id="job-1", owner_id="owner-1")] == ["run-1"]
    run = repository.get_run_for_job(run_id="run-1", job_id="job-1", owner_id="owner-1")
    assert run.id == "run-1"
    assert repository.list_runs(job_id="job-1", owner_id="owner-2") == []
    with pytest.raises(KeyError):
        repository.get_run_for_job(run_id="run-1", job_id="job-1", owner_id="owner-2")


def test_source_result_is_immutable_but_same_retry_is_idempotent(tmp_path: Path) -> None:
    repository = prepared_repository(tmp_path)
    repository.create_run(run_id="run-1", job_id="job-1", request_id="r1", asr_pack_id="asr-1")
    recognition = sample_recognition()

    assert repository.store_asr_recognition(run_id="run-1", recognition=recognition)
    assert not repository.store_asr_recognition(run_id="run-1", recognition=recognition)
    assert len(repository.source_segments("run-1")) == 1
    changed = AsrRecognition(language="ru", segments=())
    with pytest.raises(ImmutableSourceError):
        repository.store_asr_recognition(run_id="run-1", recognition=changed)


def prepared_repository(tmp_path: Path) -> JobRepository:
    repository = JobRepository(tmp_path / "next.sqlite3")
    repository.migrate()
    repository.create_job(
        job_id="job-1",
        owner_id="owner-1",
        source_name="meeting.webm",
        source_path=str(tmp_path / "jobs" / "job-1" / "source.webm"),
    )
    return repository


def sample_recognition() -> AsrRecognition:
    return AsrRecognition(
        language="ru",
        segments=(
            AsrSegment(
                start_ms=1000,
                end_ms=1800,
                text="Привет",
                confidence=-0.1,
                tokens=(AsrToken("▁Привет", 1000, 1800, -0.1),),
                words=(AsrWord("Привет", 1000, 1800),),
                word_timing_status="available",
            ),
        ),
    )
