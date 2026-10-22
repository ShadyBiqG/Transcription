"""Один persistent worker с lease, heartbeat, cancel и recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from .models import Job, JobRun
from .repository import JobRepository


class JobProcessor(Protocol):
    """Обработчик одного run; он обязан проверять отмену между стадиями."""

    async def process(
        self, job: Job, run: JobRun, cancellation_requested: Callable[[], bool]
    ) -> None: ...


class OneWorkerDispatcher:
    """Последовательно обрабатывает runs и не оставляет потерянный processing run."""

    def __init__(
        self,
        repository: JobRepository,
        processor: JobProcessor,
        *,
        worker_id: str,
        lease_seconds: int = 30,
        poll_seconds: float = 0.2,
    ) -> None:
        self._repository = repository
        self._processor = processor
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name=f"next-worker-{self._worker_id}")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def run_once(self) -> bool:
        claimed = self._repository.claim_next_run(
            worker_id=self._worker_id, lease_seconds=self._lease_seconds
        )
        if claimed is None:
            return False
        job, run = claimed
        heartbeat = asyncio.create_task(self._heartbeat(run.id), name=f"next-heartbeat-{run.id}")
        try:
            await self._processor.process(
                job,
                run,
                lambda: self._stop_event.is_set() or self._repository.cancel_requested(run.id),
            )
            if self._repository.cancel_requested(run.id) or self._stop_event.is_set():
                self._repository.mark_cancelled(run_id=run.id, worker_id=self._worker_id)
            else:
                self._repository.mark_ready(run_id=run.id, worker_id=self._worker_id)
        except Exception:
            if self._repository.cancel_requested(run.id) or self._stop_event.is_set():
                self._repository.mark_cancelled(run_id=run.id, worker_id=self._worker_id)
            else:
                self._repository.mark_failed(
                    run_id=run.id, worker_id=self._worker_id, error_code="worker_processing_error"
                )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        return True

    async def recover(self) -> None:
        self._repository.mark_expired_as_stalled()
        self._repository.requeue_stalled()

    async def _run(self) -> None:
        await self.recover()
        while not self._stop_event.is_set():
            handled = await self.run_once()
            if not handled:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_seconds)
                except TimeoutError:
                    continue

    async def _heartbeat(self, run_id: str) -> None:
        period = max(self._lease_seconds / 3, 0.1)
        while True:
            await asyncio.sleep(period)
            if not self._repository.heartbeat(
                run_id=run_id, worker_id=self._worker_id, lease_seconds=self._lease_seconds
            ):
                return
