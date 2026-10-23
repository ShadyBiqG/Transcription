"""Состояния и неизменяемые проекции durable job queue."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class JobStatus(StrEnum):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALLED = "stalled"


class JobStage(StrEnum):
    EXTRACTING_AUDIO = "extracting_audio"
    ASR = "asr"
    DIARIZING = "diarizing"
    MATCHING = "matching"
    PUBLISHING = "publishing"


TERMINAL_STATUSES = frozenset({JobStatus.READY, JobStatus.FAILED, JobStatus.CANCELLED})


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    owner_id: str
    source_name: str
    source_path: str
    status: JobStatus
    current_run_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class JobRun:
    id: str
    job_id: str
    request_id: str
    asr_pack_id: str
    voice_pack_id: str | None
    status: JobStatus
    stage: JobStage | None
    attempt: int
    max_attempts: int
    lease_owner: str | None
    lease_expires_at: str | None
    heartbeat_at: str | None
    cancel_requested: bool
    error_code: str | None
    error_message: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None
    diagnostics_data: dict[str, object] | None = None
