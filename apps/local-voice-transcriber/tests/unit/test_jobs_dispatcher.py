from __future__ import annotations

import asyncio
from pathlib import Path

from local_voice_transcriber.jobs import JobRepository, JobStatus, OneWorkerDispatcher


class SuccessfulProcessor:
    async def process(self, _job, _run, _cancellation_requested) -> None:
        return None


class CancellingProcessor:
    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    async def process(self, _job, run, _cancellation_requested) -> None:
        self._repository.request_cancel(run.id)


class FailingProcessor:
    async def process(self, _job, _run, _cancellation_requested) -> None:
        raise RuntimeError("не раскрывать эту ошибку пользователю")


def test_dispatcher_finishes_successful_job(tmp_path: Path) -> None:
    repository = queued_repository(tmp_path)
    dispatcher = OneWorkerDispatcher(
        repository, SuccessfulProcessor(), worker_id="worker", lease_seconds=3
    )

    handled = asyncio.run(dispatcher.run_once())

    assert handled
    assert repository.get_run("run-1").status is JobStatus.READY
    assert repository.get_job("job-1").status is JobStatus.READY


def test_dispatcher_honours_cancel_on_stage_boundary(tmp_path: Path) -> None:
    repository = queued_repository(tmp_path)
    dispatcher = OneWorkerDispatcher(
        repository, CancellingProcessor(repository), worker_id="worker", lease_seconds=3
    )

    asyncio.run(dispatcher.run_once())

    assert repository.get_run("run-1").status is JobStatus.CANCELLED


def test_dispatcher_records_only_safe_failure_code(tmp_path: Path) -> None:
    repository = queued_repository(tmp_path)
    dispatcher = OneWorkerDispatcher(
        repository, FailingProcessor(), worker_id="worker", lease_seconds=3
    )

    asyncio.run(dispatcher.run_once())

    run = repository.get_run("run-1")
    assert run.status is JobStatus.FAILED
    assert run.error_code == "worker_processing_error"
    assert run.error_message == "Локальная обработка завершилась ошибкой"


def queued_repository(tmp_path: Path) -> JobRepository:
    repository = JobRepository(tmp_path / "next.sqlite3")
    repository.migrate()
    repository.create_job(
        job_id="job-1",
        owner_id="owner-1",
        source_name="meeting.webm",
        source_path=str(tmp_path / "jobs" / "job-1" / "source.webm"),
    )
    repository.create_run(run_id="run-1", job_id="job-1", request_id="r1", asr_pack_id="asr-1")
    return repository
