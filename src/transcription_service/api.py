from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .database import JobRepository
from .models import HealthResponse, JobResponse, JobStatus, TranscriptionJob
from .noscribe import NoScribeRunner
from .service import JobService, ServiceError
from .worker import JobWorker


def create_app(
    settings: Settings | None = None,
    runner: NoScribeRunner | None = None,
    *,
    start_worker: bool = True,
) -> FastAPI:
    settings = settings or Settings.from_env()
    runner = runner or NoScribeRunner(settings)
    repository = JobRepository(settings.database_path)
    service = JobService(settings, repository, runner)
    worker = JobWorker(service)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        service.initialize()
        await asyncio.to_thread(service.refresh_readiness)
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            await worker.stop()

    app = FastAPI(
        title="Local Transcription Service",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = service
    app.state.worker = worker

    static_dir = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    async def require_token(authorization: str | None = Header(default=None)) -> None:
        expected = settings.api_token
        if expected is None:
            return
        scheme, _, provided = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(provided, expected):
            raise HTTPException(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
                detail="Требуется корректный Bearer-токен",
            )

    @app.exception_handler(ServiceError)
    async def service_error_handler(_, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        readiness = service.readiness
        return HealthResponse(
            noscribe="ready" if readiness.ready else "unavailable",
            models=list(readiness.models),
            worker="running" if worker.running else "stopped",
        )

    protected = [Depends(require_token)]

    @app.post(
        "/api/v1/jobs",
        response_model=JobResponse,
        status_code=202,
        dependencies=protected,
    )
    async def create_job(
        file: Annotated[UploadFile, File()],
        language: Annotated[str, Form()] = settings.default_language,
        model: Annotated[str, Form()] = settings.default_model,
    ) -> JobResponse:
        job = await service.create_job(file, language, model)
        worker.notify()
        return _job_response(job)

    @app.get("/api/v1/jobs", response_model=list[JobResponse], dependencies=protected)
    async def list_jobs(limit: int = Query(default=50, ge=1, le=100)) -> list[JobResponse]:
        return [_job_response(job) for job in service.list_jobs(limit)]

    @app.get("/api/v1/jobs/{job_id}", response_model=JobResponse, dependencies=protected)
    async def get_job(job_id: str) -> JobResponse:
        return _job_response(service.get_job(job_id))

    @app.get("/api/v1/jobs/{job_id}/transcript", dependencies=protected)
    async def download_transcript(job_id: str) -> FileResponse:
        job = service.get_job(job_id)
        path = service.transcript_path(job_id)
        safe_stem = Path(job.original_filename).stem[:100] or "transcript"
        return FileResponse(path, media_type="text/vtt", filename=f"{safe_stem}.vtt")

    @app.get("/api/v1/jobs/{job_id}/manifest", dependencies=protected)
    async def download_manifest(job_id: str) -> FileResponse:
        return FileResponse(
            service.manifest_path(job_id),
            media_type="application/json",
            filename=f"{job_id}-manifest.json",
        )

    return app


def _job_response(job: TranscriptionJob) -> JobResponse:
    base = f"/api/v1/jobs/{job.id}"
    return JobResponse(
        id=job.id,
        original_filename=job.original_filename,
        status=job.status,
        language=job.language,
        model=job.model,
        size_bytes=job.size_bytes,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error_code=job.error_code,
        error_message=job.error_message,
        transcript_url=f"{base}/transcript" if job.status is JobStatus.COMPLETED else None,
        manifest_url=f"{base}/manifest",
    )


app = create_app()
