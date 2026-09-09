from __future__ import annotations

import asyncio

from .attribution_repository import AttributionRepository
from .attribution_service import AttributionService


class AttributionWorker:
    def __init__(
        self, service: AttributionService, repository: AttributionRepository
    ) -> None:
        self.service = service
        self.repository = repository
        self._event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.running = False

    def start(self) -> None:
        if self._task is None:
            self.running = True
            self._task = asyncio.create_task(self._run())

    def notify(self) -> None:
        self._event.set()

    async def stop(self) -> None:
        self.running = False
        self._event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while self.running:
            run = await asyncio.to_thread(self.repository.claim_next)
            if run is None:
                self._event.clear()
                try:
                    await asyncio.wait_for(self._event.wait(), timeout=2)
                except TimeoutError:
                    pass
                continue
            await self.service.process_run(run)
