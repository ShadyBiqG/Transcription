from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class AttributionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED_BUDGET = "blocked_budget"


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
    role: UserRole = UserRole.USER
    is_admin: bool = False


class User(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    email: str
    password_hash: str
    created_at: datetime
    is_active: bool = True
    role: UserRole = UserRole.USER

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN


class StartAttributionRequest(BaseModel):
    external_processing_consent: bool
    profile_id: str = "balanced"
    budget_amount: str | None = Field(default=None, pattern=r"^\d+(?:\.\d+)?$")
    budget_currency: str = Field(default="RUB", min_length=3, max_length=3)


class SegmentAttributionResponse(BaseModel):
    id: str
    source_label: str
    start_ms: int
    end_ms: int
    status: str
    speaker_label: str | None = None
    confidence: float | None = None
    manual_label: str | None = None


class AttributionRunResponse(BaseModel):
    id: str
    job_id: str
    status: AttributionStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    attributed_transcript_url: str | None = None
    attribution_json_url: str | None = None
    segments: list[SegmentAttributionResponse] = Field(default_factory=list)


class ManualAttributionRequest(BaseModel):
    speaker_label: str = Field(min_length=1, max_length=200)


class AdminSettingsUpdate(BaseModel):
    api_key: str | None = Field(default=None, min_length=8, max_length=4096)
    provider_name: str | None = Field(default=None, min_length=1, max_length=100)
    provider_base_url: str | None = Field(
        default=None, min_length=8, max_length=500, pattern=r"^https?://.+"
    )
    provider_enabled: bool | None = None
    primary_model_id: str | None = Field(default=None, max_length=300)
    fallback_model_id: str | None = Field(default=None, max_length=300)
    allowed_model_ids: list[str] | None = None
    global_budget: str | None = Field(default=None, pattern=r"^\d+(?:\.\d+)?$")
    default_job_budget: str | None = Field(default=None, pattern=r"^\d+(?:\.\d+)?$")
    currency: str = Field(default="RUB", min_length=3, max_length=3)
