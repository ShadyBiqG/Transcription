"""Явное обучение общего голосового профиля из приватной локальной группы."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field

from ..auth import ApiError, CsrfSessionDependency
from ..editor import TranscriptRevision
from ..jobs import Job, JobRepository
from ..voice.index_writer import VoiceIndexWriterError
from ..voice.repository import (
    VoiceDirectoryError,
    VoiceProfileNotFoundError,
    VoiceProfileStateError,
)
from ..voice.sample_artifact import VoiceSampleArtifactError

router = APIRouter(
    prefix="/api/v1/jobs/{job_id}/transcript/groups",
    tags=["voice-training"],
)
_MIN_SAMPLE_DURATION_MS = 1_000


class EnrollGroupSampleRequest(BaseModel):
    """Явное действие пользователя: отдать группу как образец выбранному профилю."""

    profile_id: str = Field(min_length=1, max_length=128)


def _jobs(request: Request) -> JobRepository:
    return request.app.state.jobs_repository


def _require_owned_job(request: Request, *, job_id: str, owner_id: str) -> Job:
    try:
        return _jobs(request).get_job(job_id, owner_id)
    except KeyError as error:
        raise ApiError(404, "not_found", "Задание не найдено") from error


def _current_revision(request: Request, *, job_id: str, owner_id: str) -> TranscriptRevision:
    try:
        return request.app.state.editor_repository.get_current_revision(
            owner_id=owner_id,
            job_id=job_id,
        )
    except KeyError as error:
        raise ApiError(409, "transcript_unavailable", "Транскрипция ещё не опубликована") from error


def _select_unambiguous_segment(
    request: Request,
    *,
    job: Job,
    revision: TranscriptRevision,
    speaker_group_id: str,
) -> tuple[str, int, int]:
    """Берёт самый длинный однозначный ASR-отрезок группы, а не всю запись."""

    if job.current_run_id is None:
        raise ApiError(409, "transcript_unavailable", "У задания нет готового запуска")
    source_rows = {
        str(row["id"]): row
        for row in _jobs(request).source_segments(job.current_run_id)
    }
    candidates: list[tuple[int, int, str, int, int]] = []
    for segment in revision.segments:
        if segment.primary_group_id != speaker_group_id or segment.requires_review:
            continue
        row = source_rows.get(segment.source_segment_id)
        if row is None:
            continue
        start_ms = int(row["start_ms"])
        end_ms = int(row["end_ms"])
        duration_ms = end_ms - start_ms
        if duration_ms >= _MIN_SAMPLE_DURATION_MS:
            candidates.append(
                (duration_ms, -segment.ordinal, segment.source_segment_id, start_ms, end_ms)
            )
    if not candidates:
        raise ApiError(
            422,
            "voice_sample_not_available",
            "Для образца нужна однозначная реплика группы длительностью не менее одной секунды",
        )
    _, _, source_segment_id, start_ms, end_ms = max(candidates)
    return source_segment_id, start_ms, end_ms


def _normalized_wav_path(request: Request, *, job: Job) -> Path:
    if job.current_run_id is None:
        raise ApiError(409, "transcript_unavailable", "У задания нет готового запуска")
    return (
        request.app.state.settings.data_dir
        / "jobs"
        / job.id
        / "runs"
        / job.current_run_id
        / "normalized.wav"
    )


def _translate_error(error: Exception) -> ApiError:
    if isinstance(error, VoiceProfileNotFoundError):
        return ApiError(404, "voice_profile_not_found", "Голосовой профиль не найден")
    if isinstance(error, VoiceProfileStateError):
        return ApiError(409, "voice_profile_state_conflict", str(error))
    if isinstance(error, (VoiceSampleArtifactError, VoiceDirectoryError, ValueError)):
        return ApiError(422, "voice_sample_invalid", str(error))
    return ApiError(503, "voice_sample_unavailable", "Не удалось подготовить голосовой образец")


@router.post(
    "/{speaker_group_id}/samples",
    status_code=status.HTTP_202_ACCEPTED,
)
def enroll_group_sample(
    request: Request,
    job_id: str,
    speaker_group_id: str,
    payload: EnrollGroupSampleRequest,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Создаёт образец только по явному действию владельца транскрипции.

    Исправление группы и добавление образца разделены: первый сценарий меняет
    editor revision, второй создаёт из однозначной реплики protected WAV и
    ставит durable outbox-операцию для общего ZeusDB индекса.
    """

    if (
        not request.app.state.voice_diarization_ready
        or request.app.state.voice_pack_id is None
        or request.app.state.voice_sample_artifact_writer is None
        or request.app.state.voice_index_writer is None
    ):
        raise ApiError(
            503,
            "voice_pack_unavailable",
            "Локальный voice pack или общий индекс ещё не прошёл проверку готовности",
        )
    job = _require_owned_job(request, job_id=job_id, owner_id=session.user_id)
    revision = _current_revision(request, job_id=job.id, owner_id=session.user_id)
    group = next(
        (item for item in revision.groups if item.speaker_group_id == speaker_group_id),
        None,
    )
    if group is None:
        raise ApiError(404, "speaker_group_not_found", "Группа говорящего не найдена")
    if group.merged_into_group_id is not None:
        raise ApiError(
            409,
            "speaker_group_merged",
            "Для образца выберите итоговую, а не объединённую группу",
        )
    request.app.state.voice_directory_service.require_active_profile(payload.profile_id)
    source_segment_id, start_ms, end_ms = _select_unambiguous_segment(
        request,
        job=job,
        revision=revision,
        speaker_group_id=speaker_group_id,
    )
    sample_id = str(uuid4())
    artifact = None
    try:
        artifact = request.app.state.voice_sample_artifact_writer.create_from_interval(
            _normalized_wav_path(request, job=job),
            start_ms=start_ms,
            end_ms=end_ms,
            artifact_id=sample_id,
        )
        enrollment = request.app.state.voice_directory_service.add_explicit_sample(
            explicit_confirm=True,
            sample_id=sample_id,
            operation_id=str(uuid4()),
            profile_id=payload.profile_id,
            voice_pack_id=request.app.state.voice_pack_id,
            protected_artifact_id=sample_id,
            artifact_content_sha256=artifact.content_sha256,
            protected_path=artifact.protected_path,
            quality=artifact.quality_metadata,
            actor_id=session.user_id,
            source_group_id=speaker_group_id,
            source_interval_id=source_segment_id,
        )
    except Exception as error:
        if artifact is not None:
            artifact.artifact_path.unlink(missing_ok=True)
        raise _translate_error(error) from error
    request.app.state.database.add_audit_event(
        actor_id=session.user_id,
        action="voice_sample_explicitly_enrolled",
        target_type="voice_profile",
        target_id=payload.profile_id,
    )
    indexing_state = "active"
    try:
        request.app.state.voice_index_writer.reconcile()
        request.app.state.voice_index_reason = None
    except VoiceIndexWriterError:
        indexing_state = "pending"
        request.app.state.voice_index_reason = (
            "Общий голосовой индекс временно недоступен; образец будет повторно обработан."
        )
        request.app.state.database.add_audit_event(
            actor_id=session.user_id,
            action="voice_index_reconciliation_deferred",
            target_type="voice_profile",
            target_id=payload.profile_id,
        )
    return {
        "sample": {
            "id": enrollment.id,
            "profile_id": enrollment.profile_id,
            "voice_pack_id": enrollment.voice_pack_id,
            "state": enrollment.state,
            "created_at": enrollment.created_at,
        },
        "indexing_state": indexing_state,
    }
