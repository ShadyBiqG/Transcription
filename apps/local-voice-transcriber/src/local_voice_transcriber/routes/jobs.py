"""Owner-only HTTP-маршруты для загрузки и просмотра приватных заданий."""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Header, Query, Request, UploadFile, status
from fastapi.responses import FileResponse

from ..auth import ApiError, CsrfSessionDependency, SessionDependency
from ..jobs import Job, JobRepository, JobRun
from ..jobs.repository import InvalidRunState
from ..vocabulary.suggestions import suggest_for_revision

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])
_MAX_UPLOAD_BYTES = 512 * 1024 * 1024
_WEBM_SIGNATURE = b"\x1a\x45\xdf\xa3"
_UPLOAD_FILE = File(...)


def _repository(request: Request) -> JobRepository:
    return request.app.state.jobs_repository


def _job_projection(job: Job, runs: list[JobRun] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": job.id,
        "source_name": job.source_name,
        "status": job.status,
        "current_run_id": job.current_run_id,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
    if runs is not None:
        payload["runs"] = [_run_projection(run) for run in runs]
    return payload


def _run_projection(run: JobRun) -> dict[str, object]:
    return {
        "id": run.id,
        "status": run.status,
        "stage": run.stage,
        "attempt": run.attempt,
        "max_attempts": run.max_attempts,
        "cancel_requested": run.cancel_requested,
        "error_code": run.error_code,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }


def _encode_cursor(job: Job) -> str:
    payload = json.dumps([job.created_at, job.id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[str, str]:
    try:
        if len(value) > 512 or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
            raise ValueError("Недопустимый формат курсора")
        payload = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        decoded = json.loads(payload)
        if (
            not isinstance(decoded, list)
            or len(decoded) != 2
            or any(not isinstance(item, str) or not item for item in decoded)
        ):
            raise ValueError("Недопустимое содержимое курсора")
        created_at, job_id = decoded
        if datetime.fromisoformat(created_at).tzinfo is None:
            raise ValueError("В курсоре отсутствует часовой пояс")
        return created_at, job_id
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
    ) as error:
        raise ApiError(422, "invalid_cursor", "Некорректный курсор списка заданий") from error


def _find_owned_job(repository: JobRepository, job_id: str, owner_id: str) -> Job:
    try:
        return repository.get_job(job_id, owner_id)
    except KeyError as error:
        raise ApiError(404, "not_found", "Задание не найдено") from error


async def _save_upload(upload: UploadFile, destination: Path) -> None:
    """Сохраняет upload потоково, проверяя EBML-сигнатуру и лимит размера."""

    if not upload.filename or Path(upload.filename).suffix.lower() != ".webm":
        raise ApiError(422, "unsupported_media", "Разрешены только файлы .webm")
    destination.parent.mkdir(parents=True, exist_ok=False)
    written = 0
    signature = b""
    try:
        with destination.open("xb") as output:
            while chunk := await upload.read(1024 * 1024):
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    raise ApiError(
                        413,
                        "upload_too_large",
                        "Размер файла превышает допустимый лимит",
                    )
                if len(signature) < len(_WEBM_SIGNATURE):
                    signature += chunk[: len(_WEBM_SIGNATURE) - len(signature)]
                output.write(chunk)
    except ApiError:
        destination.unlink(missing_ok=True)
        destination.parent.rmdir()
        raise
    finally:
        await upload.close()
    if written < len(_WEBM_SIGNATURE) or signature != _WEBM_SIGNATURE:
        destination.unlink(missing_ok=True)
        destination.parent.rmdir()
        raise ApiError(422, "invalid_media", "Файл не имеет ожидаемой сигнатуры WebM")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_job(
    request: Request,
    session: CsrfSessionDependency,
    media: UploadFile = _UPLOAD_FILE,
) -> dict[str, object]:
    """Загружает исходную запись только в private data-root владельца."""

    repository = _repository(request)
    settings = request.app.state.settings
    job_id = str(uuid4())
    source_path = settings.data_dir / "jobs" / job_id / "source.webm"
    await _save_upload(media, source_path)
    try:
        job = repository.create_job(
            job_id=job_id,
            owner_id=session.user_id,
            source_name=Path(media.filename or "recording.webm").name,
            source_path=str(source_path),
        )
    except Exception:
        source_path.unlink(missing_ok=True)
        source_path.parent.rmdir()
        raise
    return {"job": _job_projection(job)}


@router.get("")
def list_jobs(
    request: Request,
    session: SessionDependency,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> dict[str, object]:
    repository = _repository(request)
    page, has_more = repository.list_jobs_page(
        session.user_id, limit=limit, cursor=_decode_cursor(cursor) if cursor is not None else None
    )
    items = []
    for job, run in page:
        item = _job_projection(job)
        item["current_run"] = _run_projection(run) if run is not None else None
        items.append(item)
    return {"items": items, "next_cursor": _encode_cursor(page[-1][0]) if has_more else None}


@router.get("/{job_id}")
def get_job(request: Request, job_id: str, session: SessionDependency) -> dict[str, object]:
    repository = _repository(request)
    job = _find_owned_job(repository, job_id, session.user_id)
    runs = repository.list_runs(job_id=job.id, owner_id=session.user_id)
    return {"job": _job_projection(job, runs)}


@router.get("/{job_id}/vocabulary/suggestions")
def get_vocabulary_suggestions(
    request: Request,
    job_id: str,
    session: SessionDependency,
    expected_revision_number: int = Query(ge=1),
) -> dict[str, object]:
    """Предлагает словарные исправления текущей ревизии без её изменения."""

    _find_owned_job(_repository(request), job_id, session.user_id)
    try:
        revision = request.app.state.editor_repository.get_current_revision(
            owner_id=session.user_id, job_id=job_id
        )
    except KeyError as error:
        raise ApiError(409, "transcript_unavailable", "Транскрипция ещё не опубликована") from error
    if revision.info.number != expected_revision_number:
        raise ApiError(
            409,
            "revision_conflict",
            "Транскрипция была изменена; обновите редактор",
            {
                "expected_revision_number": expected_revision_number,
                "current_revision_number": revision.info.number,
            },
        )
    terms = request.app.state.vocabulary_service.list_terms()
    return {
        "revision_number": revision.info.number,
        "items": suggest_for_revision(revision, terms),
    }


@router.post("/{job_id}/runs", status_code=status.HTTP_202_ACCEPTED)
def create_run(
    request: Request,
    job_id: str,
    session: CsrfSessionDependency,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    """Ставит новый immutable run в очередь, когда локальный pack готов."""

    repository = _repository(request)
    job = _find_owned_job(repository, job_id, session.user_id)
    if not request.app.state.asr_ready:
        raise ApiError(
            503,
            "asr_pack_unavailable",
            "Локальный ASR model pack ещё не прошёл проверку готовности",
        )
    if request.app.state.dispatcher_status() != "running":
        raise ApiError(503, "worker_unavailable", "Локальный обработчик сейчас недоступен")
    if not idempotency_key or len(idempotency_key) > 128:
        raise ApiError(422, "idempotency_key_required", "Передайте заголовок Idempotency-Key")
    try:
        run = repository.create_run(
            run_id=str(uuid4()),
            job_id=job.id,
            request_id=idempotency_key,
            asr_pack_id=request.app.state.asr_pack_id,
            voice_pack_id=request.app.state.voice_pack_id,
        )
    except InvalidRunState as error:
        raise ApiError(
            409, "active_run_exists", "Запись уже ожидает обработки или обрабатывается"
        ) from error
    return {"run": _run_projection(run)}


@router.post("/{job_id}/cancel")
def cancel_job(request: Request, job_id: str, session: CsrfSessionDependency) -> dict[str, object]:
    repository = _repository(request)
    job = _find_owned_job(repository, job_id, session.user_id)
    if job.current_run_id is None:
        raise ApiError(409, "no_active_run", "У задания нет запуска для отмены")
    try:
        run = repository.get_run_for_job(
            run_id=job.current_run_id, job_id=job.id, owner_id=session.user_id
        )
        cancelled = repository.request_cancel(run.id)
    except KeyError as error:
        raise ApiError(404, "not_found", "Запуск задания не найден") from error
    return {"run": _run_projection(cancelled)}


@router.get("/{job_id}/audio")
def get_audio(request: Request, job_id: str, session: SessionDependency) -> FileResponse:
    repository = _repository(request)
    job = _find_owned_job(repository, job_id, session.user_id)
    source_path = Path(job.source_path)
    if not source_path.is_file():
        raise ApiError(404, "not_found", "Аудиофайл не найден")
    return FileResponse(source_path, media_type="video/webm", filename=job.source_name)


@router.get("/{job_id}/source")
def get_source(request: Request, job_id: str, session: SessionDependency) -> dict[str, object]:
    repository = _repository(request)
    job = _find_owned_job(repository, job_id, session.user_id)
    if job.current_run_id is None:
        return {"segments": [], "word_timing_status": "unavailable"}
    segments = repository.source_segments(job.current_run_id)
    return {
        "segments": [
            {
                "id": str(row["id"]),
                "ordinal": row["ordinal"],
                "start_ms": row["start_ms"],
                "end_ms": row["end_ms"],
                "text": row["raw_text"],
                "word_timing_status": row["word_timing_status"],
            }
            for row in segments
        ]
    }


@router.get("/{job_id}/speaker-groups")
def get_speaker_groups(
    request: Request,
    job_id: str,
    session: SessionDependency,
) -> dict[str, object]:
    _find_owned_job(_repository(request), job_id, session.user_id)
    try:
        revision = request.app.state.editor_repository.get_current_revision(
            owner_id=session.user_id,
            job_id=job_id,
        )
    except KeyError:
        return {"items": []}
    return {
        "items": [
            {
                "id": group.speaker_group_id,
                "label": group.label_snapshot,
                "profile_id": group.profile_id,
                "assignment_status": group.assignment_status,
                "requires_review": any(
                    group.speaker_group_id in segment.speaker_group_ids and segment.requires_review
                    for segment in revision.segments
                ),
            }
            for group in revision.groups
        ]
    }
