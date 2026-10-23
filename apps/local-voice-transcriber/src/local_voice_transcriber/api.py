"""HTTP-композиция изолированного фундамента Transcription Next."""

from __future__ import annotations

import sqlite3
import time
import wave
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from os import getpid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .asr import AsrRuntimeError, OfflineAsrAdapter, PackVerificationError
from .auth import (
    AdminDependency,
    ApiError,
    CsrfSessionDependency,
    SessionDependency,
    hash_password,
    issue_session,
    normalize_login,
    session_cookie_max_age,
    validate_password,
    verify_password,
)
from .cleaning import (
    FallbackTranscriptCleaner,
    LocalLlamaCppDeletionCleaner,
    RuleBasedDeletionCleaner,
    TranscriptCleaner,
)
from .config import Settings
from .database import Database, LastActiveAdminError
from .diarization import (
    DiarizationInterval,
    DiarizationPackVerificationError,
    DiarizationRuntimeError,
    LocalDiarizationPack,
    SherpaDiarizationAdapter,
)
from .editor import AsrBaselinePublisher, EditorRepository
from .health import health_payload
from .jobs import JobRepository
from .jobs.dispatcher import OneWorkerDispatcher
from .jobs.media import FfmpegNormalizer
from .jobs.models import JobStatus
from .jobs.pipeline import AsrJobProcessor
from .routes.editor import router as editor_router
from .routes.jobs import router as jobs_router
from .routes.vocabulary import router as vocabulary_router
from .routes.voice_profiles import router as voice_profiles_router
from .routes.voice_samples import router as voice_samples_router
from .runtime import ModelPackManifestError, load_asr_model_packs
from .vector_store import ZeusDBVoiceVectorStore
from .vocabulary import VocabularyRepository, VocabularyService
from .voice.automatic_matching import (
    AutomaticSpeakerGroupMatch,
    AutomaticVoiceMatchingConfig,
    AutomaticVoiceMatchingService,
)
from .voice.embedding import (
    SherpaVoiceEmbeddingAdapter,
    VoiceEmbeddingPackVerificationError,
    VoiceEmbeddingRuntimeError,
)
from .voice.index_writer import VoiceIndexWriter, VoiceIndexWriterError
from .voice.models import MatchingThresholds
from .voice.repository import VoiceDirectoryRepository
from .voice.sample_artifact import VoiceSampleArtifactWriter
from .voice.service import VoiceDirectoryService


class CredentialsRequest(BaseModel):
    login: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class AdminCreateRequest(CredentialsRequest):
    role: Literal["user", "admin"] = "user"


class AdminUpdateRequest(BaseModel):
    role: Literal["user", "admin"] | None = None
    is_active: bool | None = None


@dataclass(slots=True)
class RateLimiter:
    """Небольшой process-local limiter для публичных auth запросов."""

    limit: int
    window_seconds: int = 60
    _attempts: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        attempts = self._attempts[key]
        while attempts and attempts[0] <= now - self.window_seconds:
            attempts.popleft()
        if len(attempts) >= self.limit:
            return False
        attempts.append(now)
        return True


def _public_user(user: dict[str, object]) -> dict[str, object]:
    return {
        "id": user["id"],
        "login": user["login"],
        "role": user["role"],
        "is_active": bool(user["is_active"]),
        "created_at": user["created_at"],
    }


def _rate_limit(request: Request) -> None:
    client_host = request.client.host if request.client else "unknown"
    key = f"{request.url.path}:{client_host}"
    if not request.app.state.auth_rate_limiter.allow(key):
        raise ApiError(429, "rate_limited", "Слишком много попыток, повторите позже")


RateLimitDependency = Annotated[None, Depends(_rate_limit)]


def _set_session_cookies(
    response: Response,
    *,
    session_token: str,
    csrf_token: str,
    expires_at: str,
    settings: Settings,
) -> None:
    max_age = session_cookie_max_age(expires_at)
    response.set_cookie(
        key=settings.cookie_name,
        value=session_token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(settings.cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Создаёт приложение без запуска legacy-кода и без побочных эффектов импорта."""

    application_settings = settings or Settings.from_env()
    database = Database(application_settings.database_path)
    jobs_repository = JobRepository(application_settings.database_path)
    editor_repository = EditorRepository(application_settings.database_path)
    voice_directory_repository = VoiceDirectoryRepository(application_settings.database_path)
    voice_directory_service = VoiceDirectoryService(voice_directory_repository)
    vocabulary_repository = VocabularyRepository(application_settings.database_path)
    vocabulary_service = VocabularyService(vocabulary_repository)
    vector_store = ZeusDBVoiceVectorStore(application_settings.zeusdb_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.database.initialize()
        app.state.jobs_repository.migrate()
        app.state.editor_repository.migrate()
        app.state.voice_directory_repository.migrate()
        app.state.vocabulary_repository.migrate()
        worker_configuration = _configure_asr_worker(
            application_settings,
            app.state.jobs_repository,
            app.state.editor_repository,
            app.state.voice_directory_repository,
            app.state.voice_directory_service,
            app.state.vector_store,
        )
        (
            dispatcher,
            asr_pack_id,
            asr_reason,
            voice_pack_id,
            voice_reason,
            embedding_adapter,
            diarization_pack,
        ) = worker_configuration[:7]
        diagnostic_packs = worker_configuration[7] if len(worker_configuration) > 7 else {}
        app.state.asr_ready = dispatcher is not None
        app.state.asr_pack_id = asr_pack_id
        app.state.diagnostic_packs = diagnostic_packs
        app.state.asr_unavailable_reason = asr_reason
        app.state.asr_dispatcher = dispatcher
        app.state.voice_pack_id = voice_pack_id
        app.state.voice_diarization_ready = voice_pack_id is not None
        app.state.voice_diarization_reason = voice_reason
        app.state.voice_embedding_adapter = embedding_adapter
        jobs_root = application_settings.data_dir / "jobs"
        samples_root = application_settings.data_dir / "protected" / "voice-samples"
        jobs_root.mkdir(parents=True, exist_ok=True)
        samples_root.mkdir(parents=True, exist_ok=True)
        app.state.voice_sample_artifact_writer = VoiceSampleArtifactWriter(
            jobs_root=jobs_root,
            samples_root=samples_root,
        )
        app.state.voice_index_writer = None
        app.state.voice_index_reason = "Локальный voice pack не настроен"
        if (
            voice_pack_id is not None
            and embedding_adapter is not None
            and diarization_pack is not None
        ):
            if diarization_pack.embedding_dimension is None:
                app.state.voice_index_reason = (
                    "В манифесте voice pack не указана размерность embedding_dimension"
                )
            else:
                app.state.voice_index_writer = VoiceIndexWriter(
                    app.state.voice_directory_repository,
                    app.state.vector_store,
                    voice_pack_id=voice_pack_id,
                    voice_pack_sha256=_voice_pack_digest(diarization_pack),
                    embedding_dimension=diarization_pack.embedding_dimension,
                    protected_root=samples_root,
                    extractor=lambda path: _extract_full_sample_embedding(embedding_adapter, path),
                )
                try:
                    app.state.voice_index_writer.reconcile()
                    app.state.voice_index_reason = None
                except VoiceIndexWriterError:
                    app.state.voice_index_reason = (
                        "Общий голосовой индекс пока недоступен; повтор будет выполнен "
                        "при следующем явном добавлении образца или перезапуске."
                    )
        if dispatcher is not None:
            dispatcher.start()
        try:
            yield
        finally:
            if dispatcher is not None:
                await dispatcher.stop()

    app = FastAPI(title="Transcription Next", version="0.1.0", lifespan=lifespan)
    app.state.settings = application_settings
    app.state.database = database
    app.state.jobs_repository = jobs_repository
    app.state.editor_repository = editor_repository
    app.state.voice_directory_repository = voice_directory_repository
    app.state.voice_directory_service = voice_directory_service
    app.state.vocabulary_repository = vocabulary_repository
    app.state.vocabulary_service = vocabulary_service
    app.state.vector_store = vector_store
    app.state.auth_rate_limiter = RateLimiter(application_settings.auth_rate_limit)
    app.state.asr_ready = False
    app.state.asr_pack_id = ""
    app.state.diagnostic_packs = {}
    app.state.asr_unavailable_reason = "Локальный ASR model pack не настроен"
    app.state.asr_dispatcher = None
    app.state.dispatcher_status = lambda: (
        app.state.asr_dispatcher.state if app.state.asr_dispatcher is not None else "stopped"
    )
    app.state.voice_pack_id = None
    app.state.voice_diarization_ready = False
    app.state.voice_diarization_reason = "Локальный voice pack не настроен"
    app.state.voice_embedding_adapter = None
    app.state.voice_sample_artifact_writer = None
    app.state.voice_index_writer = None
    app.state.voice_index_reason = "Локальный voice pack не настроен"

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"code": error.code, "message": error.message, "details": error.details},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "Некорректные данные запроса",
                "details": {"errors": error.errors()},
            },
        )

    @app.middleware("http")
    async def prevent_stale_interface_cache(request: Request, call_next):
        """Не даёт браузеру использовать устаревшие HTML, CSS и JavaScript после обновления."""

        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    def public_health(*, require_worker: bool = False) -> JSONResponse:
        payload, is_ready = health_payload(
            application_settings,
            database,
            worker_state=app.state.dispatcher_status(),
            require_worker=require_worker,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content=payload,
        )

    @app.get("/health", tags=["service"])
    @app.get("/api/v1/health", tags=["service"])
    def get_health() -> JSONResponse:
        return public_health()

    @app.get("/ready", tags=["service"])
    @app.get("/api/v1/ready", tags=["service"])
    def get_ready() -> JSONResponse:
        return public_health(require_worker=True)

    static_root = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_root), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_root / "index.html")

    @app.post("/api/v1/auth/register", status_code=status.HTTP_201_CREATED, tags=["auth"])
    def register(credentials: CredentialsRequest, _: RateLimitDependency) -> dict[str, object]:
        login = normalize_login(credentials.login)
        validate_password(credentials.password)
        try:
            user = database.create_user(
                login=login, password_hash=hash_password(credentials.password)
            )
        except sqlite3.IntegrityError as error:
            raise ApiError(409, "login_taken", "Этот логин уже занят") from error
        database.add_audit_event(
            actor_id=user["id"],
            action="user_registered",
            target_type="user",
            target_id=user["id"],
        )
        return {"user": _public_user(user)}

    @app.post("/api/v1/auth/login", tags=["auth"])
    def login(
        credentials: CredentialsRequest,
        response: Response,
        _: RateLimitDependency,
    ) -> dict[str, object]:
        login_value = normalize_login(credentials.login)
        user = database.get_user_by_login(login_value)
        if (
            user is None
            or not bool(user["is_active"])
            or not verify_password(credentials.password, user["password_hash"])
        ):
            raise ApiError(401, "invalid_credentials", "Неверный логин или пароль")
        session_token, csrf_token, expires_at = issue_session(
            database, user_id=user["id"], settings=application_settings
        )
        _set_session_cookies(
            response,
            session_token=session_token,
            csrf_token=csrf_token,
            expires_at=expires_at,
            settings=application_settings,
        )
        database.add_audit_event(
            actor_id=user["id"],
            action="session_created",
            target_type="session",
            target_id="created",
        )
        return {
            "user": _public_user(user),
            "csrf_header_name": application_settings.csrf_header_name,
        }

    @app.get("/api/v1/auth/me", tags=["auth"])
    def me(session: SessionDependency) -> dict[str, object]:
        user = database.get_user(session.user_id)
        if user is None:
            raise ApiError(401, "authentication_required", "Сессия отсутствует или истекла")
        return {
            "user": _public_user(user),
            "csrf_header_name": application_settings.csrf_header_name,
        }

    @app.post("/api/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
    def logout(response: Response, session: CsrfSessionDependency) -> Response:
        database.revoke_session(session.session_id)
        database.add_audit_event(
            actor_id=session.user_id,
            action="session_revoked",
            target_type="session",
            target_id=session.session_id,
        )
        _clear_session_cookies(response, application_settings)
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @app.get("/api/v1/admin/users", tags=["admin"])
    def list_users(_: AdminDependency) -> dict[str, object]:
        statistics = jobs_repository.recording_statistics_by_owner()
        return {
            "items": [
                {
                    **_public_user(user),
                    "statistics": statistics.get(
                        user["id"],
                        {"recordings_total": 0, "by_status": dict.fromkeys(JobStatus, 0)},
                    ),
                }
                for user in database.list_users()
            ]
        }

    @app.post("/api/v1/admin/users", status_code=status.HTTP_201_CREATED, tags=["admin"])
    def create_user_by_admin(
        payload: AdminCreateRequest,
        admin: CsrfSessionDependency,
    ) -> dict[str, object]:
        if admin.role != "admin":
            raise ApiError(403, "admin_required", "Требуется роль администратора")
        login_value = normalize_login(payload.login)
        validate_password(payload.password)
        try:
            user = database.create_user(
                login=login_value, password_hash=hash_password(payload.password), role=payload.role
            )
        except sqlite3.IntegrityError as error:
            raise ApiError(409, "login_taken", "Этот логин уже занят") from error
        database.add_audit_event(
            actor_id=admin.user_id,
            action="admin_created_user",
            target_type="user",
            target_id=user["id"],
        )
        return {"user": _public_user(user)}

    @app.patch("/api/v1/admin/users/{user_id}", tags=["admin"])
    def update_user_by_admin(
        user_id: str,
        payload: AdminUpdateRequest,
        admin: CsrfSessionDependency,
    ) -> dict[str, object]:
        if admin.role != "admin":
            raise ApiError(403, "admin_required", "Требуется роль администратора")
        user = database.get_user(user_id)
        if user is None:
            raise ApiError(404, "not_found", "Пользователь не найден")
        if payload.role is None and payload.is_active is None:
            raise ApiError(422, "no_changes", "Не переданы изменения")
        try:
            updated_user = database.update_user(
                user_id=user_id,
                role=payload.role,
                is_active=payload.is_active,
                protect_last_admin=True,
            )
        except LastActiveAdminError as error:
            raise ApiError(
                409, "last_admin", "Нельзя отключить или понизить последнего администратора"
            ) from error
        database.add_audit_event(
            actor_id=admin.user_id,
            action="admin_updated_user",
            target_type="user",
            target_id=user_id,
        )
        return {"user": _public_user(updated_user or user)}

    @app.get("/api/v1/admin/overview", tags=["admin"])
    def admin_overview(admin: AdminDependency) -> dict[str, object]:
        """Возвращает только агрегированные технические данные без content jobs."""

        worker_state = app.state.dispatcher_status()
        health, _ = health_payload(
            application_settings, database, worker_state=worker_state
        )
        vector_store_status = vector_store.status()
        users = database.list_users()
        return {
            "service": health["service"],
            "instance_id": health["instance_id"],
            "checks": health["checks"],
            "users_count": len(users),
            "users_total": len(users),
            "users_active": sum(bool(user["is_active"]) for user in users),
            "users_admins": sum(user["role"] == "admin" for user in users),
            **jobs_repository.recording_statistics(),
            "active_voice_profiles": len(voice_directory_service.list_profiles()),
            "voice_vector_store": {
                "availability": vector_store_status.availability,
                "reason": vector_store_status.reason,
            },
            "queue": {"configured": True, "worker": worker_state},
            "asr": {
                "ready": app.state.asr_ready and worker_state == "running",
                "pack_id": app.state.asr_pack_id or None,
                "reason": app.state.asr_unavailable_reason
                or ("Локальный worker недоступен" if worker_state != "running" else None),
            },
            "diarization": {
                "ready": app.state.voice_diarization_ready,
                "pack_id": app.state.voice_pack_id,
                "reason": app.state.voice_diarization_reason,
            },
            "voice_index": {
                "ready": app.state.voice_index_writer is not None
                and app.state.voice_index_reason is None,
                "reason": app.state.voice_index_reason,
            },
            "actor_id": admin.user_id,
        }

    @app.post("/api/v1/admin/voice-index/reconcile", tags=["admin"])
    def reconcile_voice_index(admin: CsrfSessionDependency) -> dict[str, object]:
        """Повторяет durable outbox без доступа к приватным транскрипциям."""

        if admin.role != "admin":
            raise ApiError(403, "admin_required", "Требуется роль администратора")
        writer = app.state.voice_index_writer
        if writer is None:
            raise ApiError(
                503,
                "voice_index_unavailable",
                "Общий голосовой индекс не готов: проверьте voice pack и ZeusDB",
            )
        try:
            result = writer.reconcile()
        except VoiceIndexWriterError as error:
            app.state.voice_index_reason = (
                "Общий голосовой индекс не выполнил повторную обработку outbox."
            )
            raise ApiError(
                503,
                "voice_index_unavailable",
                "Общий голосовой индекс временно недоступен",
            ) from error
        app.state.voice_index_reason = None
        database.add_audit_event(
            actor_id=admin.user_id,
            action="voice_index_reconciliation_requested",
            target_type="voice_index",
            target_id=app.state.voice_pack_id or "not_configured",
        )
        return {
            "generation_id": result.generation.id if result.generation is not None else None,
            "processed_operation_count": len(result.operation_ids),
        }

    app.include_router(jobs_router)
    app.include_router(editor_router)
    app.include_router(voice_profiles_router)
    app.include_router(voice_samples_router)
    app.include_router(vocabulary_router)

    return app


def _configure_asr_worker(
    settings: Settings,
    repository: JobRepository,
    editor_repository: EditorRepository,
    voice_directory_repository: VoiceDirectoryRepository,
    voice_directory_service: VoiceDirectoryService,
    vector_store: ZeusDBVoiceVectorStore,
) -> tuple[
    OneWorkerDispatcher | None,
    str,
    str | None,
    str | None,
    str | None,
    SherpaVoiceEmbeddingAdapter | None,
    LocalDiarizationPack | None,
    dict[str, str | None],
]:
    """Собирает worker только из проверенных локальных ресурсов."""

    if settings.models_dir is None or settings.asr_manifest_path is None:
        return (
            None, "", "Не задан манифест локального ASR/VAD model pack",
            None, None, None, None, {},
        )
    if settings.ffmpeg_path is None:
        return None, "", "Не задан путь к локальному FFmpeg", None, None, None, None, {}
    if not settings.ffmpeg_path.is_file():
        return None, "", "Локальный FFmpeg недоступен", None, None, None, None, {}
    try:
        packs = load_asr_model_packs(settings.asr_manifest_path, models_root=settings.models_dir)
        asr_adapter = OfflineAsrAdapter(
            packs.asr,
            models_root=settings.models_dir,
            language=packs.language,
            vad_pack=packs.vad,
        )
        asr_adapter.preflight()
    except (AsrRuntimeError, ModelPackManifestError, PackVerificationError):
        return (
            None, "", "Локальный ASR/VAD model pack не прошёл проверку",
            None, None, None, None, {},
        )
    diarizer = None
    embedding_adapter = None
    voice_pack_id: str | None = None
    voice_reason: str | None = "Локальный voice pack не настроен"
    diarization_pack: LocalDiarizationPack | None = None
    voice_matcher: _RuntimeVoiceMatcher | None = None
    if packs.diarization is not None:
        try:
            diarizer = SherpaDiarizationAdapter(
                packs.diarization,
                models_root=settings.models_dir,
                audio_root=settings.data_dir / "jobs",
            )
            diarizer.preflight()
            embedding_adapter = SherpaVoiceEmbeddingAdapter(
                packs.diarization,
                models_root=settings.models_dir,
                audio_root=settings.data_dir,
            )
            actual_embedding_dimension = embedding_adapter.embedding_dimension()
            if (
                packs.diarization.embedding_dimension is not None
                and packs.diarization.embedding_dimension != actual_embedding_dimension
            ):
                raise VoiceEmbeddingRuntimeError(
                    "Размерность embedding в manifest не совпадает с локальным voice pack."
                )
            voice_pack_id = packs.diarization.pack_id
            diarization_pack = packs.diarization
            if packs.diarization.embedding_dimension is not None:
                voice_matcher = _RuntimeVoiceMatcher(
                    repository=voice_directory_repository,
                    vector_index=vector_store,
                    embedding_adapter=embedding_adapter,
                    voice_pack_id=voice_pack_id,
                    thresholds=settings.voice_matching_thresholds,
                )
            voice_reason = None
        except (
            DiarizationPackVerificationError,
            DiarizationRuntimeError,
            VoiceEmbeddingPackVerificationError,
            VoiceEmbeddingRuntimeError,
        ):
            diarizer = None
            embedding_adapter = None
            diarization_pack = None
            voice_matcher = None
            voice_reason = "Локальный voice pack не прошёл проверку"
    normalizer = FfmpegNormalizer(settings.ffmpeg_path, jobs_root=settings.data_dir / "jobs")
    processor = AsrJobProcessor(
        repository,
        normalizer,
        asr_adapter,
        jobs_root=settings.data_dir / "jobs",
        publisher=AsrBaselinePublisher(
            repository,
            editor_repository,
            active_profile_label=lambda profile_id: voice_directory_service.require_active_profile(
                profile_id
            ).display_name,
            cleaner=_configured_transcript_cleaner(settings),
        ),
        diarizer=diarizer,
        voice_matcher=voice_matcher,
    )
    dispatcher = OneWorkerDispatcher(
        repository,
        processor,
        worker_id=f"{settings.instance_id}-{getpid()}",
    )
    return (
        dispatcher,
        packs.asr.pack_id,
        None,
        voice_pack_id,
        voice_reason,
        embedding_adapter,
        diarization_pack,
        {
            "asr_model": packs.asr.model_name,
            "asr_pack_id": packs.asr.pack_id,
            "vad_pack_id": packs.vad.pack_id if packs.vad is not None else None,
            "voice_pack_id": voice_pack_id,
        },
    )


def _configured_transcript_cleaner(settings: Settings) -> TranscriptCleaner:
    """Собирает только локальный optional cleaner с безусловным offline fallback.

    Штатный установщик намеренно не запускает отдельный процесс llama.cpp, поэтому
    даже включённая, но неполная конфигурация никогда не делает ASR job ошибочным.
    """

    fallback = RuleBasedDeletionCleaner()
    if (
        not settings.cleaner_enabled
        or settings.cleaner_endpoint is None
        or settings.cleaner_model_path is None
        or settings.cleaner_llama_server_path is None
        or not settings.cleaner_model_path.is_file()
        or not settings.cleaner_llama_server_path.is_file()
    ):
        return FallbackTranscriptCleaner(fallback)
    return FallbackTranscriptCleaner(
        fallback,
        primary=LocalLlamaCppDeletionCleaner(settings.cleaner_endpoint),
    )


def _voice_pack_digest(pack: LocalDiarizationPack) -> str:
    """Строит стабильный fingerprint только из уже проверенного model pack."""

    digest = sha256()
    digest.update(pack.pack_id.encode("utf-8"))
    digest.update(b"\n")
    digest.update(pack.segmentation_model.encode("utf-8"))
    digest.update(b"\n")
    digest.update(pack.embedding_model.encode("utf-8"))
    digest.update(b"\n")
    digest.update(str(pack.embedding_dimension or "").encode("ascii"))
    digest.update(b"\n")
    for relative_path, expected_sha256 in sorted(pack.files.items()):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"=")
        digest.update(expected_sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass(slots=True)
class _RuntimeVoiceMatcher:
    """Создаёт matching-снимок с текущими active профилями на каждый job run."""

    repository: VoiceDirectoryRepository
    vector_index: ZeusDBVoiceVectorStore
    embedding_adapter: SherpaVoiceEmbeddingAdapter
    voice_pack_id: str
    thresholds: MatchingThresholds

    def match_groups(
        self,
        *,
        normalized_wav_path: Path,
        intervals: tuple[DiarizationInterval, ...],
    ) -> tuple[AutomaticSpeakerGroupMatch, ...]:
        """Не даёт сбою справочника прервать публикацию личного транскрипта."""

        auto_assign_profile_ids = frozenset()
        try:
            if self.thresholds.calibrated and self.thresholds.auto_assign_enabled:
                auto_assign_profile_ids = frozenset(
                    profile.id for profile in self.repository.list_shared_profiles()
                )
        except Exception:
            # Основной сервис превратит недоступность directory в UNKNOWN.
            pass
        service = AutomaticVoiceMatchingService(
            repository=self.repository,
            vector_index=self.vector_index,
            extractor=lambda path, start_ms, end_ms: self.embedding_adapter.extract(
                path,
                start_ms=start_ms,
                end_ms=end_ms,
            ),
            config=AutomaticVoiceMatchingConfig(
                voice_pack_id=self.voice_pack_id,
                thresholds=self.thresholds,
                auto_assign_profile_ids=auto_assign_profile_ids,
            ),
        )
        return service.match_groups(
            normalized_wav_path=normalized_wav_path,
            intervals=intervals,
        )


def _extract_full_sample_embedding(
    adapter: SherpaVoiceEmbeddingAdapter,
    sample_path: Path,
) -> tuple[float, ...]:
    """Извлекает embedding целого protected WAV внутри допустимой границы."""

    try:
        with wave.open(str(sample_path), "rb") as reader:
            frame_count = reader.getnframes()
            sample_rate = reader.getframerate()
    except (OSError, wave.Error) as error:
        raise VoiceEmbeddingRuntimeError("Не удалось прочитать protected WAV образца.") from error
    if frame_count < 1 or sample_rate < 1:
        raise VoiceEmbeddingRuntimeError("Protected WAV образца не содержит аудиофреймов.")
    end_ms = max(1, frame_count * 1000 // sample_rate)
    return adapter.extract(sample_path, start_ms=0, end_ms=end_ms)


app = create_app()
