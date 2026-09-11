from __future__ import annotations

import sqlite3
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


def test_initialize_migrates_existing_database_with_speaker_detection(tmp_path):
    database_path = tmp_path / "jobs.sqlite3"
    repository = JobRepository(database_path)
    repository.initialize()
    repository.create(make_job())
    with repository._connect() as connection:
        connection.execute("ALTER TABLE jobs RENAME TO jobs_new")
        connection.execute(
            "CREATE TABLE jobs AS SELECT id, original_filename, source_filename, status, "
            "language, model, media_type, size_bytes, sha256, created_at, updated_at, "
            "started_at, completed_at, error_code, error_message, process_exit_code FROM jobs_new"
        )
        connection.execute("DROP TABLE jobs_new")

    repository.initialize()

    with repository._connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
    assert "speaker_detection" in columns
    migrated = repository.get("00000000-0000-0000-0000-000000000001")
    assert migrated is not None
    assert migrated.speaker_detection == "none"


def test_initialize_adds_processing_mode_to_existing_attribution_runs(tmp_path):
    database_path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'test')"
        )
        connection.execute("CREATE TABLE attribution_runs (id TEXT PRIMARY KEY)")

    repository = JobRepository(database_path)
    repository.initialize()

    with repository._connect() as connection:
        columns = {
            row["name"]: row
            for row in connection.execute("PRAGMA table_info(attribution_runs)")
        }
        versions = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations")
        }
    assert columns["processing_mode"]["dflt_value"] == "'precise'"
    assert versions == {1, 2}
