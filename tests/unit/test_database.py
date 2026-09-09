from __future__ import annotations

from datetime import UTC, datetime

import pytest

from transcription_service.database import InvalidStateTransition, JobRepository
from transcription_service.models import JobStatus, TranscriptionJob


def make_job(job_id: str = "00000000-0000-0000-0000-000000000001") -> TranscriptionJob:
    now = datetime.now(UTC)
    return TranscriptionJob(
        id=job_id,
        original_filename="meeting.webm",
        status=JobStatus.QUEUED,
        language="ru",
        model="precise",
        media_type="video/webm",
        size_bytes=3,
        sha256="a" * 64,
        created_at=now,
        updated_at=now,
    )


def test_repository_claims_and_completes_job(tmp_path):
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    repository.create(make_job())
    running = repository.claim_next()
    assert running is not None and running.status is JobStatus.RUNNING
    completed = repository.mark_completed(running.id)
    assert completed.status is JobStatus.COMPLETED
    with pytest.raises(InvalidStateTransition):
        repository.mark_completed(running.id)


def test_list_is_newest_first(tmp_path):
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    first = make_job()
    second = make_job("00000000-0000-0000-0000-000000000002").model_copy(
        update={"created_at": datetime.now(UTC), "updated_at": datetime.now(UTC)}
    )
    repository.create(first)
    repository.create(second)
    assert [job.id for job in repository.list()] == [second.id, first.id]
