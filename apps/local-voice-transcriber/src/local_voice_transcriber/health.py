"""Безопасные health и readiness проверки без путей и секретов."""

from __future__ import annotations

from typing import Any

from .config import Settings
from .database import Database
from .jobs.dispatcher import WorkerState


def health_payload(
    settings: Settings,
    database: Database,
    *,
    worker_state: WorkerState = "stopped",
    require_worker: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Формирует публичный технический статус кандидата."""

    database_ok = database.ping()
    checks = {
        "database": "ok" if database_ok else "error",
        "worker": worker_state,
        "model_packs": "not_configured",
        "voice_index": "not_configured",
    }
    ready = database_ok and (not require_worker or worker_state == "running")
    return (
        {
            "status": "ok" if ready else "error",
            "service": "transcription-next",
            "instance_id": settings.instance_id,
            "checks": checks,
        },
        ready,
    )
