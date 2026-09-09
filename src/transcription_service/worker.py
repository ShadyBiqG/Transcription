from __future__ import annotations

import asyncio
import logging

from .service import JobService

logger = logging.getLogger(__name__)


class JobWorker:
    def __init__(self, service: JobService, poll_seconds: float = 1.0) -> None:
        self.service = service
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name="transcription-job-worker")

    def notify(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            self._task = None

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            job = self.service.repository.claim_next()
            if job is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass
                continue

            self.service.write_job_manifest(job)
            directory = self.service.job_dir(job.id)
            try:
                result = await self.service.runner.run(
                    directory / job.source_filename,
                    directory / "transcript.vtt",
                    directory / "noscribe.log",
                    job.language,
                    job.model,
                )
                if result.success:
                    completed = self.service.repository.mark_completed(
                        job.id, result.exit_code or 0
                    )
                else:
                    completed = self.service.repository.mark_failed(
                        job.id,
                        result.error_code or "noscribe_failed",
                        result.error_message or "Ошибка обработки noScribe",
                        result.exit_code,
                    )
                self.service.write_job_manifest(completed)
                logger.info("Задание %s завершено: %s", job.id, completed.status.value)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Необработанная ошибка задания %s", job.id)
                failed = self.service.repository.mark_failed(
                    job.id,
                    "worker_error",
                    "Внутренняя ошибка обработчика",
                )
                self.service.write_job_manifest(failed)
