"""Owner-only API для просмотра и правки снимков транскрипции."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, Field

from ..auth import ApiError, CsrfSessionDependency, SessionDependency
from ..editor import (
    EditorRepository,
    GroupSnapshot,
    GroupUpdate,
    InvalidGroupOperationError,
    RevisionConflictError,
    TranscriptRevision,
    TranscriptRevisionInfo,
)
from ..jobs import JobRepository
from ..voice.repository import VoiceProfileNotFoundError, VoiceProfileStateError

router = APIRouter(prefix="/api/v1/jobs/{job_id}/transcript", tags=["editor"])


class GroupUpdateRequest(BaseModel):
    """Новая проекция одной локальной группы для ручной ревизии."""

    label_snapshot: str = Field(min_length=1, max_length=240)
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    assignment_status: str = Field(min_length=1, max_length=64)
    provenance: str = Field(default="manual", min_length=1, max_length=64)

    def to_domain(self) -> GroupUpdate:
        return GroupUpdate(
            label_snapshot=self.label_snapshot,
            profile_id=self.profile_id,
            assignment_status=self.assignment_status,
            provenance=self.provenance,
        )


class ManualRevisionRequest(BaseModel):
    """Изменения, которые будут сохранены отдельной immutable-ревизией."""

    expected_revision_number: int = Field(ge=1)
    segment_texts: dict[str, str] = Field(default_factory=dict)
    group_updates: dict[str, GroupUpdateRequest] = Field(default_factory=dict)
    comment: str | None = Field(default=None, max_length=500)


class RestoreRevisionRequest(BaseModel):
    """Запрос на создание новой ревизии из исторического снимка."""

    revision_id: str = Field(min_length=1, max_length=128)
    expected_revision_number: int = Field(ge=1)
    comment: str | None = Field(default=None, max_length=500)


class MergeGroupsRequest(BaseModel):
    """Запрос на объединение двух локальных групп в новом снимке."""

    source_group_id: str = Field(min_length=1, max_length=128)
    target_group_id: str = Field(min_length=1, max_length=128)
    expected_revision_number: int = Field(ge=1)
    comment: str | None = Field(default=None, max_length=500)


class UnmergeGroupRequest(BaseModel):
    """Запрос на отмену ранее видимого объединения."""

    source_group_id: str = Field(min_length=1, max_length=128)
    expected_revision_number: int = Field(ge=1)
    comment: str | None = Field(default=None, max_length=500)


ManualPayload = Annotated[ManualRevisionRequest, Body()]
RestorePayload = Annotated[RestoreRevisionRequest, Body()]
MergePayload = Annotated[MergeGroupsRequest, Body()]
UnmergePayload = Annotated[UnmergeGroupRequest, Body()]


def _editor(request: Request) -> EditorRepository:
    return request.app.state.editor_repository


def _jobs(request: Request) -> JobRepository:
    return request.app.state.jobs_repository


def _require_owned_job(request: Request, *, job_id: str, owner_id: str) -> None:
    try:
        _jobs(request).get_job(job_id, owner_id)
    except KeyError as error:
        raise ApiError(404, "not_found", "Задание не найдено") from error


def _revision_info_projection(info: TranscriptRevisionInfo) -> dict[str, object]:
    return {
        "id": info.id,
        "job_id": info.job_id,
        "job_run_id": info.job_run_id,
        "number": info.number,
        "parent_id": info.parent_id,
        "origin": info.origin,
        "created_by": info.created_by,
        "expected_parent_number": info.expected_parent_number,
        "comment": info.comment,
        "restored_from_id": info.restored_from_id,
        "is_current": info.is_current,
        "created_at": info.created_at,
    }


def _group_projection(group: GroupSnapshot) -> dict[str, object]:
    return {
        "speaker_group_id": group.speaker_group_id,
        "label": group.label_snapshot,
        "profile_id": group.profile_id,
        "assignment_status": group.assignment_status,
        "provenance": group.provenance,
        "merged_into_group_id": group.merged_into_group_id,
    }


def _revision_projection(revision: TranscriptRevision) -> dict[str, object]:
    return {
        "revision": _revision_info_projection(revision.info),
        "groups": [_group_projection(group) for group in revision.groups],
        "segments": [
            {
                "source_segment_id": segment.source_segment_id,
                "ordinal": segment.ordinal,
                "text": segment.text,
                "primary_group_id": segment.primary_group_id,
                "speaker_group_ids": list(segment.speaker_group_ids),
                "requires_review": segment.requires_review,
            }
            for segment in revision.segments
        ],
    }


def _translate_editor_error(error: Exception) -> ApiError:
    if isinstance(error, RevisionConflictError):
        return ApiError(
            409,
            "revision_conflict",
            "Транскрипция была изменена другим действием; обновите редактор",
            {
                "expected_revision_number": error.expected_number,
                "current_revision_number": error.current_number,
            },
        )
    if isinstance(error, (InvalidGroupOperationError, ValueError)):
        return ApiError(422, "invalid_editor_operation", str(error))
    if isinstance(error, VoiceProfileNotFoundError):
        return ApiError(404, "voice_profile_not_found", "Голосовой профиль не найден")
    if isinstance(error, VoiceProfileStateError):
        return ApiError(409, "voice_profile_state_conflict", str(error))
    if isinstance(error, KeyError):
        return ApiError(404, "not_found", "Ревизия или группа не найдена")
    return ApiError(409, "transcript_unavailable", "Транскрипция ещё не опубликована")


@router.get("")
def get_current_transcript(
    request: Request,
    job_id: str,
    session: SessionDependency,
) -> dict[str, object]:
    """Возвращает текущий immutable-снимок только владельцу задания."""

    _require_owned_job(request, job_id=job_id, owner_id=session.user_id)
    try:
        revision = _editor(request).get_current_revision(owner_id=session.user_id, job_id=job_id)
    except Exception as error:
        raise _translate_editor_error(error) from error
    return _revision_projection(revision)


@router.get("/revisions")
def list_transcript_revisions(
    request: Request,
    job_id: str,
    session: SessionDependency,
) -> dict[str, object]:
    """Возвращает историю без раскрытия чужих задач и контента."""

    _require_owned_job(request, job_id=job_id, owner_id=session.user_id)
    try:
        revisions = _editor(request).list_revisions(owner_id=session.user_id, job_id=job_id)
    except Exception as error:
        raise _translate_editor_error(error) from error
    return {"items": [_revision_info_projection(revision) for revision in revisions]}


@router.get("/revisions/{revision_id}")
def get_transcript_revision(
    request: Request,
    job_id: str,
    revision_id: str,
    session: SessionDependency,
) -> dict[str, object]:
    """Открывает конкретный снимок истории только в owner-scoped job."""

    _require_owned_job(request, job_id=job_id, owner_id=session.user_id)
    try:
        revision = _editor(request).get_revision(
            owner_id=session.user_id,
            job_id=job_id,
            revision_id=revision_id,
        )
    except Exception as error:
        raise _translate_editor_error(error) from error
    return _revision_projection(revision)


@router.patch("")
def create_manual_revision(
    request: Request,
    job_id: str,
    payload: ManualPayload,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Сохраняет текстовые и групповые правки новой ревизией."""

    _require_owned_job(request, job_id=job_id, owner_id=session.user_id)
    try:
        group_updates = {
            group_id: update.to_domain() for group_id, update in payload.group_updates.items()
        }
        for update in group_updates.values():
            if update.profile_id is not None:
                request.app.state.voice_directory_service.require_active_profile(update.profile_id)
        revision = _editor(request).create_manual_revision(
            owner_id=session.user_id,
            job_id=job_id,
            expected_current_revision=payload.expected_revision_number,
            segment_texts=payload.segment_texts,
            group_updates=group_updates,
            created_by=session.user_id,
            comment=payload.comment,
        )
    except Exception as error:
        raise _translate_editor_error(error) from error
    return _revision_projection(revision)


@router.post("/restore")
def restore_transcript_revision(
    request: Request,
    job_id: str,
    payload: RestorePayload,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Клонирует историческую редакцию как новую current-ревизию."""

    _require_owned_job(request, job_id=job_id, owner_id=session.user_id)
    try:
        revision = _editor(request).restore_revision(
            owner_id=session.user_id,
            job_id=job_id,
            revision_id=payload.revision_id,
            expected_current_revision=payload.expected_revision_number,
            created_by=session.user_id,
            comment=payload.comment,
        )
    except Exception as error:
        raise _translate_editor_error(error) from error
    return _revision_projection(revision)


@router.post("/groups/merge")
def merge_speaker_groups(
    request: Request,
    job_id: str,
    payload: MergePayload,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Объединяет две группы в следующей редакторской ревизии."""

    _require_owned_job(request, job_id=job_id, owner_id=session.user_id)
    try:
        revision = _editor(request).merge_groups(
            owner_id=session.user_id,
            job_id=job_id,
            source_group_id=payload.source_group_id,
            target_group_id=payload.target_group_id,
            expected_current_revision=payload.expected_revision_number,
            created_by=session.user_id,
            comment=payload.comment,
        )
    except Exception as error:
        raise _translate_editor_error(error) from error
    return _revision_projection(revision)


@router.post("/groups/unmerge")
def unmerge_speaker_group(
    request: Request,
    job_id: str,
    payload: UnmergePayload,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Отменяет последнее активное объединение в следующей ревизии."""

    _require_owned_job(request, job_id=job_id, owner_id=session.user_id)
    try:
        revision = _editor(request).unmerge_group(
            owner_id=session.user_id,
            job_id=job_id,
            source_group_id=payload.source_group_id,
            expected_current_revision=payload.expected_revision_number,
            created_by=session.user_id,
            comment=payload.comment,
        )
    except Exception as error:
        raise _translate_editor_error(error) from error
    return _revision_projection(revision)
