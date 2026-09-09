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
    user_id: str | None = None
    original_filename: str
    source_filename: str = "source.webm"
    status: JobStatus
    language: str
    model: str
    speaker_detection: str = Field(default="auto", pattern=r"^(?:none|auto|[1-9]|10)$")
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
    speaker_detection: str
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


class AuthCredentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class UserResponse(BaseModel):
    id: str
    email: str
    created_at: datetime


class User(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    email: str
    password_hash: str
    created_at: datetime
    is_active: bool = True
