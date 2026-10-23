"""Owner-only API для просмотра и правки снимков транскрипции."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Body, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..auth import ApiError, CsrfSessionDependency, SessionDependency
from ..editor import (
    CleanDraftInfo,
    CleanDraftStatus,
    EditorRepository,
    GroupSnapshot,
    GroupUpdate,
    InvalidGroupOperationError,
    RevisionConflictError,
    TranscriptRevision,
    TranscriptRevisionInfo,
    display_speaker_label,
)
from ..jobs import JobRepository
from ..voice.repository import SpeakerIdentityUnavailableError, VoiceDirectoryError

router = APIRouter(prefix="/api/v1/jobs/{job_id}/transcript", tags=["editor"])


class GroupUpdateRequest(BaseModel):
    """Новая проекция одной локальной группы для ручной ревизии."""

    model_config = ConfigDict(extra="forbid")

    label_snapshot: str = Field(min_length=1, max_length=240)
    assignment_status: str = Field(min_length=1, max_length=64)
    provenance: str = Field(default="manual", min_length=1, max_length=64)

    def to_domain(self, *, profile_id: str | None) -> GroupUpdate:
        return GroupUpdate(
            label_snapshot=self.label_snapshot,
            profile_id=profile_id,
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


class SpeakerIdentityRequest(BaseModel):
    """Подпись, которую пользователь назначает сразу всей группе говорящего."""

    display_name: str = Field(min_length=1, max_length=240)
    expected_revision_number: int = Field(ge=1)


class SegmentSpeakerRequest(BaseModel):
    """Новое локальное назначение спикера для одной реплики по таймкоду."""

    speaker_group_id: str = Field(min_length=1, max_length=128)
    expected_revision_number: int = Field(ge=1)
    comment: str | None = Field(default=None, max_length=500)


class TranscriptViewPreferenceRequest(BaseModel):
    """Личная настройка выбора чистого или исходного представления."""

    preference: Literal["clean", "raw", "last_selected"]
    selected_view: Literal["clean", "raw"] | None = None


ManualPayload = Annotated[ManualRevisionRequest, Body()]
RestorePayload = Annotated[RestoreRevisionRequest, Body()]
MergePayload = Annotated[MergeGroupsRequest, Body()]
UnmergePayload = Annotated[UnmergeGroupRequest, Body()]
IdentityPayload = Annotated[SpeakerIdentityRequest, Body()]
SegmentSpeakerPayload = Annotated[SegmentSpeakerRequest, Body()]
ViewPreferencePayload = Annotated[TranscriptViewPreferenceRequest, Body()]


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
        "label": display_speaker_label(group.label_snapshot),
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
    if isinstance(error, KeyError):
        return ApiError(404, "not_found", "Ревизия или группа не найдена")
    return ApiError(409, "transcript_unavailable", "Транскрипция ещё не опубликована")


def _translate_speaker_identity_error(error: Exception) -> ApiError:
    """Не раскрывает существование и состояние общей карточки говорящего."""

    if isinstance(error, RevisionConflictError):
        return _translate_editor_error(error)
    if isinstance(error, SpeakerIdentityUnavailableError):
        return ApiError(
            422,
            "speaker_identity_unavailable",
            "Указанная подпись спикера сейчас недоступна",
        )
    if isinstance(error, VoiceDirectoryError):
        return ApiError(
            422,
            "speaker_identity_unavailable",
            "Не удалось назначить подпись спикера",
        )
    if isinstance(error, (InvalidGroupOperationError, ValueError)):
        return ApiError(422, "invalid_speaker_identity", "Укажите корректную подпись спикера")
    if isinstance(error, KeyError):
        return ApiError(404, "not_found", "Группа спикера не найдена")
    return ApiError(409, "transcript_unavailable", "Транскрипция ещё не опубликована")


def _transcript_text_export(revision: TranscriptRevision) -> str:
    """Строит читабельный текст только из текущего owner-scoped снимка."""

    groups = {group.speaker_group_id: group for group in revision.groups}
    lines = ["Транскрипция", ""]
    for segment in revision.segments:
        label = "Спикер"
        effective_group_id = revision.effective_primary_group_id(segment)
        if effective_group_id is not None:
            effective_group = groups.get(effective_group_id)
            if effective_group is not None:
                label = display_speaker_label(effective_group.label_snapshot)
        lines.append(f"{label}: {segment.text}")
    return "\n".join(lines) + "\n"


def _cleaning_projection(info: CleanDraftInfo | None) -> dict[str, object]:
    if info is None:
        return {
            "status": "not_processed",
            "fallback_to_raw": False,
            "message": "Чистый черновик ещё не подготовлен",
        }
    if info.status is CleanDraftStatus.FAILED:
        return {
            "status": info.status.value,
            "fallback_to_raw": True,
            "message": "Чистый черновик недоступен: показан исходный текст",
        }
    return {
        "status": info.status.value,
        "fallback_to_raw": False,
        "message": (
            "Чистый черновик подготовлен"
            if info.status is CleanDraftStatus.APPLIED
            else "Очистка не нашла безопасных удалений"
        ),
    }


def _view_projection(
    *,
    preference: str,
    requested_view: str,
    resolved_view: str,
    cleaning: CleanDraftInfo | None,
) -> dict[str, object]:
    return {
        "preference": preference,
        "requested": requested_view,
        "resolved": resolved_view,
        "cleaning": _cleaning_projection(cleaning),
    }


def _resolve_requested_view(
    request: Request,
    *,
    owner_id: str,
    job_id: str,
    requested_view: Literal["clean", "raw"] | None,
) -> tuple[str, str]:
    preference = request.app.state.database.get_transcript_view_preference(owner_id)
    if requested_view is not None:
        return preference, requested_view
    if preference == "last_selected":
        return preference, _editor(request).get_selected_view(owner_id=owner_id, job_id=job_id)
    return preference, preference


@router.get("")
def get_current_transcript(
    request: Request,
    job_id: str,
    session: SessionDependency,
    view: Literal["clean", "raw"] | None = Query(default=None),
) -> dict[str, object]:
    """Возвращает чистый либо исходный immutable-снимок только владельцу."""

    _require_owned_job(request, job_id=job_id, owner_id=session.user_id)
    try:
        preference, requested_view = _resolve_requested_view(
            request,
            owner_id=session.user_id,
            job_id=job_id,
            requested_view=view,
        )
        cleaning = _editor(request).get_clean_draft_info(
            owner_id=session.user_id,
            job_id=job_id,
        )
        fallback_to_raw = (
            requested_view == "clean"
            and cleaning is not None
            and cleaning.status is CleanDraftStatus.FAILED
        )
        resolved_view = "raw" if requested_view == "raw" or fallback_to_raw else "clean"
        revision = (
            _editor(request).get_baseline_revision(owner_id=session.user_id, job_id=job_id)
            if resolved_view == "raw"
            else _editor(request).get_current_revision(owner_id=session.user_id, job_id=job_id)
        )
    except Exception as error:
        raise _translate_editor_error(error) from error
    return {
        **_revision_projection(revision),
        "view": _view_projection(
            preference=preference,
            requested_view=requested_view,
            resolved_view=resolved_view,
            cleaning=cleaning,
        ),
    }


@router.get("/view-preference")
def get_transcript_view_preference(
    request: Request,
    job_id: str,
    session: SessionDependency,
) -> dict[str, str]:
    """Возвращает только личную настройку текущего владельца записи."""

    _require_owned_job(request, job_id=job_id, owner_id=session.user_id)
    try:
        preference = request.app.state.database.get_transcript_view_preference(session.user_id)
        selected_view = _editor(request).get_selected_view(
            owner_id=session.user_id,
            job_id=job_id,
        )
    except Exception as error:
        raise _translate_editor_error(error) from error
    return {"preference": preference, "selected_view": selected_view}


@router.patch("/view-preference")
def update_transcript_view_preference(
    request: Request,
    job_id: str,
    payload: ViewPreferencePayload,
    session: CsrfSessionDependency,
) -> dict[str, str]:
    """Сохраняет личный default и, при выборе, последнюю открытую проекцию."""

    _require_owned_job(request, job_id=job_id, owner_id=session.user_id)
    selected_view = payload.selected_view
    if selected_view is None and payload.preference in {"clean", "raw"}:
        selected_view = payload.preference
    try:
        preference = request.app.state.database.set_transcript_view_preference(
            user_id=session.user_id,
            preference=payload.preference,
        )
        if selected_view is not None:
            _editor(request).set_selected_view(
                owner_id=session.user_id,
                job_id=job_id,
                selected_view=selected_view,
            )
        else:
            selected_view = _editor(request).get_selected_view(
                owner_id=session.user_id,
                job_id=job_id,
            )
    except Exception as error:
        raise _translate_editor_error(error) from error
    return {"preference": preference, "selected_view": selected_view}


@router.get("/export")
def export_current_transcript(
    request: Request,
    job_id: str,
    session: SessionDependency,
    format: Literal["txt"] = Query(default="txt"),
) -> Response:
    """Скачивает читаемый текст актуальной ревизии только владельцу задания."""

    del format
    _require_owned_job(request, job_id=job_id, owner_id=session.user_id)
    try:
        revision = _editor(request).get_current_revision(owner_id=session.user_id, job_id=job_id)
    except Exception as error:
        raise _translate_editor_error(error) from error
    return Response(
        content=_transcript_text_export(revision),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="transcription.txt"'},
    )


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
        current = _editor(request).get_current_revision(owner_id=session.user_id, job_id=job_id)
        profile_ids = {group.speaker_group_id: group.profile_id for group in current.groups}
        group_updates = {
            group_id: update.to_domain(profile_id=profile_ids.get(group_id))
            for group_id, update in payload.group_updates.items()
        }
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


@router.put("/segments/{source_segment_id}/speaker")
def assign_segment_speaker(
    request: Request,
    job_id: str,
    source_segment_id: str,
    payload: SegmentSpeakerPayload,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Изменяет спикера одной реплики только через новую локальную ревизию."""

    _require_owned_job(request, job_id=job_id, owner_id=session.user_id)
    try:
        revision = _editor(request).assign_segment_speaker(
            owner_id=session.user_id,
            job_id=job_id,
            source_segment_id=source_segment_id,
            speaker_group_id=payload.speaker_group_id,
            expected_current_revision=payload.expected_revision_number,
            created_by=session.user_id,
            comment=payload.comment,
        )
    except Exception as error:
        raise _translate_editor_error(error) from error
    return _revision_projection(revision)


@router.post("/groups/{speaker_group_id}/identity")
def assign_speaker_group_identity(
    request: Request,
    job_id: str,
    speaker_group_id: str,
    payload: IdentityPayload,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Назначает имя всей локальной группе новой immutable-ревизией.

    Пользователь передаёт только подпись. Технический идентификатор общей
    карточки остаётся в серверном снимке и не попадает в публичный DTO.
    """

    _require_owned_job(request, job_id=job_id, owner_id=session.user_id)
    try:
        speaker, revision = _editor(request).assign_speaker_group_identity(
            owner_id=session.user_id,
            job_id=job_id,
            speaker_group_id=speaker_group_id,
            display_name=payload.display_name,
            expected_current_revision=payload.expected_revision_number,
            created_by=session.user_id,
            voice_directory_repository=request.app.state.voice_directory_repository,
        )
    except Exception as error:
        raise _translate_speaker_identity_error(error) from error
    request.app.state.database.add_audit_event(
        actor_id=session.user_id,
        action="speaker_group_identity_assigned",
        target_type="speaker",
        target_id=speaker.id,
    )
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
