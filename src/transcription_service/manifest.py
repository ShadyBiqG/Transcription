from __future__ import annotations

import json
import os
from pathlib import Path

from .models import JobStatus, TranscriptionJob

MANIFEST_SCHEMA_VERSION = 1


def build_manifest(job: TranscriptionJob) -> dict[str, object]:
    artifacts = {"source": job.source_filename}
    if job.status is JobStatus.COMPLETED:
        artifacts["transcript_vtt"] = "transcript.vtt"

    error: dict[str, str] | None = None
    if job.error_code and job.error_message:
        error = {"code": job.error_code, "message": job.error_message}

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "job_id": job.id,
        "status": job.status.value,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "source": {
            "original_filename": job.original_filename,
            "artifact": job.source_filename,
            "media_type": job.media_type,
            "size_bytes": job.size_bytes,
            "sha256": job.sha256,
        },
        "transcription": {
            "engine": "noscribe",
            "language": job.language,
            "model": job.model,
            "timestamps": True,
            "exit_code": job.process_exit_code,
        },
        "artifacts": artifacts,
        "error": error,
    }


def write_manifest(path: Path, job: TranscriptionJob) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(build_manifest(job), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
