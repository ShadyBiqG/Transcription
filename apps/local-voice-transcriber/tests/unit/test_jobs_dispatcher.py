from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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


def test_dispatcher_records_only_safe_failure_code(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repository = queued_repository(tmp_path)
    dispatcher = OneWorkerDispatcher(
        repository, FailingProcessor(), worker_id="worker", lease_seconds=3
    )

    with caplog.at_level(logging.ERROR):
        asyncio.run(dispatcher.run_once())

    run = repository.get_run("run-1")
    assert run.status is JobStatus.FAILED
    assert run.error_code == "worker_processing_error"
    assert run.error_message == "Локальная обработка завершилась ошибкой"
    assert "не раскрывать эту ошибку" not in caplog.text
    assert "FailingProcessor.process" not in caplog.text
    assert "стек:" in caplog.text


def test_restart_recovers_lease_after_expiry_without_stealing_live_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = queued_repository(tmp_path)
    moment = [datetime(2026, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr("local_voice_transcriber.jobs.repository._now", lambda: moment[0])
    claimed = repository.claim_next_run(worker_id="previous-worker", lease_seconds=1)
    assert claimed is not None

    async def scenario() -> None:
        dispatcher = OneWorkerDispatcher(
            repository,
            SuccessfulProcessor(),
            worker_id="new-worker",
            lease_seconds=1,
            poll_seconds=0.01,
        )
        dispatcher.start()
        try:
            await asyncio.sleep(0.05)
            assert repository.get_run("run-1").status is JobStatus.PROCESSING
            assert repository.get_run("run-1").lease_owner == "previous-worker"
            moment[0] += timedelta(seconds=2)
            async with asyncio.timeout(2):
                while repository.get_run("run-1").status is not JobStatus.READY:
                    await asyncio.sleep(0.01)
            run = repository.get_run("run-1")
            assert run.attempt == 2
            assert run.status is JobStatus.READY
        finally:
            await dispatcher.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("failing_method", ["mark_expired_as_stalled", "claim_next_run"])
def test_transient_sqlite_error_degrades_then_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing_method: str,
) -> None:
    repository = queued_repository(tmp_path)
    original = getattr(repository, failing_method)
    failures = 0
    states: list[str] = []

    def fail_once(*args, **kwargs):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise sqlite3.OperationalError("private-content /private/path")
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, failing_method, fail_once)

    async def scenario() -> None:
        dispatcher = OneWorkerDispatcher(
            repository,
            SuccessfulProcessor(),
            worker_id="worker",
            lease_seconds=1,
            poll_seconds=0.01,
            on_state_change=states.append,
        )
        dispatcher.start()
        try:
            async with asyncio.timeout(2):
                while repository.get_run("run-1").status is not JobStatus.READY:
                    await asyncio.sleep(0.01)
            assert dispatcher.state == "running"
            assert "degraded" in states
            assert states.index("degraded") < len(states) - 1
        finally:
            await dispatcher.stop()

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert failures >= 2
    assert "private-content" not in caplog.text
    assert "/private/path" not in caplog.text


def test_dispatcher_recovers_before_publishing_ready_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = queued_repository(tmp_path)
    dispatcher = OneWorkerDispatcher(
        repository,
        SuccessfulProcessor(),
        worker_id="worker",
        lease_seconds=1,
        poll_seconds=0.01,
    )
    original_claim = repository.claim_next_run
    original_mark_ready = repository.mark_ready
    failures = 0

    def fail_first_claim(*args, **kwargs):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise sqlite3.OperationalError("temporary database error")
        return original_claim(*args, **kwargs)

    def mark_ready_and_check_state(*args, **kwargs):
        original_mark_ready(*args, **kwargs)
        assert dispatcher.state == "running"

    monkeypatch.setattr(repository, "claim_next_run", fail_first_claim)
    monkeypatch.setattr(repository, "mark_ready", mark_ready_and_check_state)

    async def scenario() -> None:
        dispatcher.start()
        try:
            async with asyncio.timeout(2):
                while repository.get_run("run-1").status is not JobStatus.READY:
                    await asyncio.sleep(0.01)
        finally:
            await dispatcher.stop()

    asyncio.run(scenario())
    assert failures >= 2


def test_unexpected_dispatch_task_failure_exposes_stopped_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    repository = queued_repository(tmp_path)
    states: list[str] = []

    def fail_claim(*_args, **_kwargs):
        raise RuntimeError("private-content /private/path")

    monkeypatch.setattr(repository, "claim_next_run", fail_claim)

    async def scenario() -> None:
        dispatcher = OneWorkerDispatcher(
            repository, SuccessfulProcessor(), worker_id="worker", on_state_change=states.append
        )
        dispatcher.start()
        assert dispatcher._task is not None
        await asyncio.wait({dispatcher._task})
        await asyncio.sleep(0)
        assert dispatcher.state == "stopped"
        assert states[-1] == "stopped"
        await dispatcher.stop()

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert "private-content" not in caplog.text
    assert "/private/path" not in caplog.text


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
