import asyncio
import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .admin_repository import AdminRepository
from .admin_service import AdminService
from .attribution_repository import AttributionRepository
from .attribution_service import AttributionService
from .attribution_worker import AttributionWorker
from .auth import hash_password, new_session_token, verify_password
from .config import Settings
from .database import JobRepository
from .frames import FFmpegFrameExtractor
from .models import (
    AuthCredentials,
    HealthResponse,
    JobResponse,
    JobStatus,
    TranscriptionJob,
    User,
    UserResponse,
)
from .noscribe import NoScribeRunner
from .routerai import RouterAIClient
from .routes.admin_settings import create_router as create_admin_settings_router
from .routes.admin_statistics import create_router as create_admin_statistics_router
from .routes.attribution import create_router as create_attribution_router
from .service import JobService, ServiceError
from .worker import JobWorker

SESSION_COOKIE = "transcription_session"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def create_app(
    settings: Settings | None = None,
    runner: NoScribeRunner | None = None,
    *,
    start_worker: bool = True,
    routerai: RouterAIClient | None = None,
    frame_extractor: FFmpegFrameExtractor | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    runner = runner or NoScribeRunner(settings)
    repository = JobRepository(settings.database_path)
    service = JobService(settings, repository, runner)
    worker = JobWorker(service)
    routerai = routerai or RouterAIClient(
        settings.routerai_base_url, settings.routerai_timeout_seconds
    )
    frame_extractor = frame_extractor or FFmpegFrameExtractor(settings.resolved_ffmpeg_path)
    attribution_repository = AttributionRepository(settings.database_path)
    admin_repository = AdminRepository(settings.database_path)
    admin_service = AdminService(admin_repository, routerai, settings.jobs_dir)
    attribution_service = AttributionService(
        repository,
        attribution_repository,
        admin_repository,
        admin_service,
        routerai,
        frame_extractor,
        settings.jobs_dir,
        settings.attribution_max_frames,
    )
    attribution_worker = AttributionWorker(attribution_service, attribution_repository)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        service.initialize()
        attribution_repository.recover_running()
        await asyncio.to_thread(service.refresh_readiness)
        if start_worker:
            worker.start()
            attribution_worker.start()
        try:
            yield
        finally:
            await worker.stop()
            await attribution_worker.stop()

    app = FastAPI(
        title="Local Transcription Service",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = service
    app.state.worker = worker
    app.state.attribution_worker = attribution_worker
    app.state.attribution_service = attribution_service
    app.state.admin_service = admin_service
    dummy_password_hash = hash_password("invalid-authentication-password")

    static_dir = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    async def current_user(
        session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> User:
        user = repository.get_user_by_session(session_token) if session_token else None
        if user is None:
            raise HTTPException(status_code=401, detail="Требуется вход в систему")
        return user

    async def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Требуются права администратора")
        return user

    def normalize_email(email: str) -> str:
        normalized = email.strip().lower()
        if not EMAIL_PATTERN.fullmatch(normalized):
            raise HTTPException(
                status_code=422, detail="Укажите корректный адрес электронной почты"
            )
        return normalized

    def set_session_cookie(response: Response, user: User) -> None:
        token = new_session_token()
        repository.create_session(user.id, token, settings.session_ttl_days)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=settings.session_ttl_days * 24 * 60 * 60,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
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

    @app.post("/api/v1/auth/register", response_model=UserResponse, status_code=201)
    async def register(credentials: AuthCredentials, response: Response) -> UserResponse:
        email = normalize_email(credentials.email)
        if repository.get_user_by_email(email) is not None:
            raise HTTPException(
                status_code=409, detail="Пользователь с такой почтой уже существует"
            )
        try:
            user = repository.create_user(email, hash_password(credentials.password))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=409, detail="Пользователь с такой почтой уже существует"
            ) from exc
        set_session_cookie(response, user)
        return _user_response(user)

    @app.post("/api/v1/auth/login", response_model=UserResponse)
    async def login(credentials: AuthCredentials, response: Response) -> UserResponse:
        email = normalize_email(credentials.email)
        user = repository.get_user_by_email(email)
        password_hash = user.password_hash if user else dummy_password_hash
        password_valid = verify_password(credentials.password, password_hash)
        if user is None or not user.is_active or not password_valid:
            raise HTTPException(status_code=401, detail="Неверная почта или пароль")
        set_session_cookie(response, user)
        return _user_response(user)

    @app.post("/api/v1/auth/logout", status_code=204)
    async def logout(
        session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> Response:
        if session_token:
            repository.delete_session(session_token)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/v1/auth/me", response_model=UserResponse)
    async def me(user: Annotated[User, Depends(current_user)]) -> UserResponse:
        return _user_response(user)

    app.include_router(
        create_attribution_router(
            current_user,
            attribution_service,
            attribution_repository,
            attribution_worker,
            settings.jobs_dir,
        )
    )
    app.include_router(create_admin_statistics_router(require_admin, admin_service))
    app.include_router(create_admin_settings_router(require_admin, admin_service))

    @app.post(
        "/api/v1/jobs",
        response_model=JobResponse,
        status_code=202,
    )
    async def create_job(
        file: Annotated[UploadFile, File()],
        user: Annotated[User, Depends(current_user)],
        language: Annotated[str, Form()] = settings.default_language,
        model: Annotated[str, Form()] = settings.default_model,
        speaker_detection: Annotated[
            str, Form(pattern=r"^(?:none|auto|[1-9]|10)$")
        ] = "auto",
    ) -> JobResponse:
        job = await service.create_job(file, language, model, speaker_detection, user.id)
        worker.notify()
        return _job_response(job)

    @app.get("/api/v1/jobs", response_model=list[JobResponse])
    async def list_jobs(
        user: Annotated[User, Depends(current_user)],
        limit: int = Query(default=50, ge=1, le=100),
    ) -> list[JobResponse]:
        return [_job_response(job) for job in service.list_jobs(limit, user.id)]

    @app.get("/api/v1/jobs/{job_id}", response_model=JobResponse)
    async def get_job(
        job_id: str, user: Annotated[User, Depends(current_user)]
    ) -> JobResponse:
        return _job_response(service.get_job(job_id, user.id))

    @app.get("/api/v1/jobs/{job_id}/transcript")
    async def download_transcript(
        job_id: str, user: Annotated[User, Depends(current_user)]
    ) -> FileResponse:
        job = service.get_job(job_id, user.id)
        path = service.transcript_path(job_id, user.id)
        safe_stem = Path(job.original_filename).stem[:100] or "transcript"
        media_type = "text/html" if path.suffix.lower() == ".html" else "text/vtt"
        return FileResponse(
            path,
            media_type=media_type,
            filename=f"{safe_stem}{path.suffix.lower()}",
            content_disposition_type="inline",
            headers={
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"
            },
        )

    @app.get("/api/v1/jobs/{job_id}/manifest")
    async def download_manifest(
        job_id: str, user: Annotated[User, Depends(current_user)]
    ) -> FileResponse:
        return FileResponse(
            service.manifest_path(job_id, user.id),
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
        speaker_detection=job.speaker_detection,
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


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        created_at=user.created_at,
        role=user.role,
        is_admin=user.is_admin,
    )


app = create_app()
