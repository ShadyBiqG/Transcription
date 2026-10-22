"""Один persistent worker с lease, heartbeat, cancel и recovery."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import traceback
from collections.abc import Callable
from typing import Literal, Protocol

from .models import Job, JobRun
from .repository import JobRepository

WorkerState = Literal["running", "degraded", "stopped"]
_LOGGER = logging.getLogger(__name__)


def _log_safe_exception(operation: str, error: BaseException) -> None:
    """Пишет стек без значений исключения, строк кода и локальных путей."""

    frames = traceback.extract_tb(error.__traceback__)
    stack = " <- ".join(f"{frame.name}:{frame.lineno}" for frame in frames)
    _LOGGER.error("Сбой worker при %s (%s); стек: %s", operation, type(error).__name__, stack)


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
        on_state_change: Callable[[WorkerState], None] | None = None,
    ) -> None:
        self._repository = repository
        self._processor = processor
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._on_state_change = on_state_change
        self._state: WorkerState = "stopped"
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def state(self) -> WorkerState:
        """Текущее состояние, включая неожиданное завершение dispatch task."""

        return self._state if self.running else "stopped"

    def _set_state(self, state: WorkerState) -> None:
        if self._state == state:
            return
        self._state = state
        if self._on_state_change is not None:
            self._on_state_change(state)

    def start(self) -> None:
        if self.running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name=f"next-worker-{self._worker_id}")
        self._task.add_done_callback(self._task_done)
        self._set_state("degraded")

    def _task_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                _log_safe_exception("работе диспетчера", error)
        if task is self._task:
            self._set_state("stopped")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await self._task
            except (Exception, asyncio.CancelledError):
                # Ошибка уже отражена в состоянии worker и записана callback задачи.
                pass
            self._task = None
        self._set_state("stopped")

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
        except Exception as error:
            _log_safe_exception("обработке задания", error)
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
        recovery_interval = max(self._poll_seconds, min(self._lease_seconds / 3, 5.0))
        next_recovery = 0.0
        failure_count = 0
        while not self._stop_event.is_set():
            try:
                if time.monotonic() >= next_recovery:
                    await self.recover()
                    next_recovery = time.monotonic() + recovery_interval
                handled = await self.run_once()
            except sqlite3.Error as error:
                _log_safe_exception("доступе к очереди", error)
                self._set_state("degraded")
                failure_count += 1
                delay = min(max(self._poll_seconds, 0.1) * 2 ** min(failure_count - 1, 5), 5.0)
                await self._wait_or_stop(delay)
                continue
            failure_count = 0
            self._set_state("running")
            if not handled:
                await self._wait_or_stop(self._poll_seconds)

    async def _wait_or_stop(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        except TimeoutError:
            pass

    async def _heartbeat(self, run_id: str) -> None:
        period = max(self._lease_seconds / 3, 0.1)
        while True:
            await asyncio.sleep(period)
            if not self._repository.heartbeat(
                run_id=run_id, worker_id=self._worker_id, lease_seconds=self._lease_seconds
            ):
                return
