"""Безопасные health и readiness проверки без путей и секретов."""

from __future__ import annotations

from typing import Any

from .config import Settings
from .database import Database


def health_payload(settings: Settings, database: Database) -> tuple[dict[str, Any], bool]:
    """Формирует публичный технический статус кандидата."""

    database_ok = database.ping()
    checks = {
        "database": "ok" if database_ok else "error",
        "worker": "not_configured",
        "model_packs": "not_configured",
        "voice_index": "not_configured",
    }
    return (
        {
            "status": "ok" if database_ok else "error",
            "service": "transcription-next",
            "instance_id": settings.instance_id,
            "checks": checks,
        },
        database_ok,
    )
