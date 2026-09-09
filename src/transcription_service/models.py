from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TranscriptionJob(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    original_filename: str
    source_filename: str = "source.webm"
    status: JobStatus
    language: str
    model: str
    media_type: str
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    process_exit_code: int | None = None


class JobResponse(BaseModel):
    id: str
    original_filename: str
    status: JobStatus
    language: str
    model: str
    size_bytes: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    error_message: str | None
    transcript_url: str | None
    manifest_url: str


class HealthResponse(BaseModel):
    service: str = "ok"
    noscribe: str
    models: list[str]
    worker: str
