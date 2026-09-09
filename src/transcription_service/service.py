from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from pathlib import Path

from fastapi import UploadFile

from .config import Settings
from .database import JobRepository, utc_now
from .manifest import write_manifest
from .models import JobStatus, TranscriptionJob
from .noscribe import NoScribeReadiness, NoScribeRunner


class ServiceError(RuntimeError):
    status_code = 400


class InvalidUpload(ServiceError):
    pass


class UploadTooLarge(ServiceError):
    status_code = 413


class NoScribeUnavailable(ServiceError):
    status_code = 503


class JobNotFound(ServiceError):
    status_code = 404


class ArtifactNotReady(ServiceError):
    status_code = 409


class JobService:
    chunk_size = 1024 * 1024

    def __init__(
        self, settings: Settings, repository: JobRepository, runner: NoScribeRunner
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.runner = runner
        self.readiness = NoScribeReadiness(False, (), "Проверка noScribe еще не выполнена")

    def initialize(self) -> None:
        self.settings.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.repository.initialize()
        for job in self.repository.recover_running():
            self.write_job_manifest(job)

    def refresh_readiness(self) -> NoScribeReadiness:
        self.readiness = self.runner.check_readiness()
        return self.readiness

    async def create_job(self, upload: UploadFile, language: str, model: str) -> TranscriptionJob:
        if not self.readiness.ready:
            raise NoScribeUnavailable(self.readiness.message or "noScribe недоступен")
        if model not in self.readiness.models:
            raise InvalidUpload(f"Модель {model!r} недоступна")
        language = language.strip().lower()
        self.runner.build_arguments(Path("source.webm"), Path("transcript.vtt"), language, model)

        original_filename = Path(upload.filename or "").name
        if not original_filename or Path(original_filename).suffix.lower() != ".webm":
            raise InvalidUpload("Поддерживаются только файлы .webm")

        job_id = str(uuid.uuid4())
        temp_job_dir = self.settings.temp_dir / job_id
        final_job_dir = self.settings.jobs_dir / job_id
        temp_job_dir.mkdir(parents=True, exist_ok=False)
        temporary_source = temp_job_dir / "source.webm"
        size = 0
        digest = hashlib.sha256()
        try:
            with temporary_source.open("wb") as target:
                while chunk := await upload.read(self.chunk_size):
                    size += len(chunk)
                    if size > self.settings.max_upload_bytes:
                        raise UploadTooLarge(
                            f"Файл превышает лимит {self.settings.max_upload_bytes} байт"
                        )
                    digest.update(chunk)
                    target.write(chunk)
            if size == 0:
                raise InvalidUpload("Нельзя загрузить пустой файл")

            final_job_dir.mkdir(parents=True, exist_ok=False)
            temporary_source.replace(final_job_dir / "source.webm")
            now = utc_now()
            job = TranscriptionJob(
                id=job_id,
                original_filename=original_filename,
                status=JobStatus.QUEUED,
                language=language,
                model=model,
                media_type=upload.content_type or "video/webm",
                size_bytes=size,
                sha256=digest.hexdigest(),
                created_at=now,
                updated_at=now,
            )
            self.repository.create(job)
            self.write_job_manifest(job)
            return job
        except Exception:
            if final_job_dir.exists() and self.repository.get(job_id) is None:
                shutil.rmtree(final_job_dir, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(temp_job_dir, ignore_errors=True)
            await upload.close()

    def get_job(self, job_id: str) -> TranscriptionJob:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", job_id):
            raise JobNotFound("Задание не найдено")
        job = self.repository.get(job_id)
        if job is None:
            raise JobNotFound("Задание не найдено")
        return job

    def list_jobs(self, limit: int) -> list[TranscriptionJob]:
        return self.repository.list(limit)

    def job_dir(self, job_id: str) -> Path:
        job = self.get_job(job_id)
        resolved = (self.settings.jobs_dir / job.id).resolve()
        if self.settings.jobs_dir.resolve() not in resolved.parents:
            raise JobNotFound("Задание не найдено")
        return resolved

    def transcript_path(self, job_id: str) -> Path:
        job = self.get_job(job_id)
        if job.status is not JobStatus.COMPLETED:
            raise ArtifactNotReady("Транскрипция еще не готова")
        path = self.job_dir(job_id) / "transcript.vtt"
        if not path.is_file() or path.stat().st_size == 0:
            raise JobNotFound("Файл транскрипции не найден")
        return path

    def manifest_path(self, job_id: str) -> Path:
        path = self.job_dir(job_id) / "manifest.json"
        if not path.is_file():
            raise JobNotFound("Manifest не найден")
        return path

    def write_job_manifest(self, job: TranscriptionJob) -> None:
        write_manifest(self.settings.jobs_dir / job.id / "manifest.json", job)
