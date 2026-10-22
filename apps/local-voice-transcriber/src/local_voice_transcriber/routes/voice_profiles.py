"""API общего каталога голосов без раскрытия источников private jobs."""

from __future__ import annotations

import sqlite3
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Body, Query, Request
from pydantic import BaseModel, Field

from ..auth import ApiError, CsrfSessionDependency, SessionDependency
from ..voice.repository import (
    SharedVoiceProfile,
    VoiceDirectoryError,
    VoiceDirectoryRepository,
    VoiceProfileNotFoundError,
    VoiceProfileStateError,
)
from ..voice.service import VoiceDirectoryService

router = APIRouter(prefix="/api/v1/voice-profiles", tags=["voice-directory"])


class CreateProfileRequest(BaseModel):
    """Общая карточка говорящего без образца и без доступа к чужому аудио."""

    display_name: str = Field(min_length=1, max_length=240)
    aliases: list[str] = Field(default_factory=list, max_length=20)


class RenameProfileRequest(BaseModel):
    """Запрос на исправление видимого имени общего профиля."""

    display_name: str = Field(min_length=1, max_length=240)


class AddAliasRequest(BaseModel):
    """Запрос на добавление общего варианта имени."""

    alias: str = Field(min_length=1, max_length=240)


class ProfileTransitionRequest(BaseModel):
    """Причина обратимой архивации либо восстановления профиля."""

    reason: str = Field(min_length=1, max_length=500)


CreatePayload = Annotated[CreateProfileRequest, Body()]
RenamePayload = Annotated[RenameProfileRequest, Body()]
AliasPayload = Annotated[AddAliasRequest, Body()]
TransitionPayload = Annotated[ProfileTransitionRequest, Body()]


def _repository(request: Request) -> VoiceDirectoryRepository:
    return request.app.state.voice_directory_repository


def _service(request: Request) -> VoiceDirectoryService:
    return request.app.state.voice_directory_service


def _profile_projection(profile: SharedVoiceProfile) -> dict[str, object]:
    """Строгое DTO: provenance, путь и record/vector IDs сюда не попадают."""

    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "aliases": list(profile.aliases),
        "state": profile.state,
        "active_sample_count": profile.active_sample_count,
        "created_at": profile.created_at,
        "archived_at": profile.archived_at,
    }


def _translate_error(error: Exception) -> ApiError:
    if isinstance(error, VoiceProfileNotFoundError):
        return ApiError(404, "voice_profile_not_found", "Голосовой профиль не найден")
    if isinstance(error, sqlite3.IntegrityError):
        return ApiError(
            409,
            "voice_profile_conflict",
            "Имя или псевдоним уже используется в общем справочнике",
        )
    if isinstance(error, VoiceProfileStateError):
        return ApiError(409, "voice_profile_state_conflict", str(error))
    if isinstance(error, (VoiceDirectoryError, ValueError)):
        return ApiError(422, "invalid_voice_profile", str(error))
    return ApiError(503, "voice_directory_unavailable", "Общий голосовой справочник недоступен")


def _require_admin(session: CsrfSessionDependency) -> None:
    if session.role != "admin":
        raise ApiError(403, "admin_required", "Требуется роль администратора")


@router.get("")
def list_profiles(
    request: Request,
    session: SessionDependency,
    query: str = Query(default="", max_length=240),
) -> dict[str, object]:
    """Показывает всем вошедшим только безопасные общие карточки."""

    del session
    try:
        profiles = _service(request).list_profiles(query=query)
    except Exception as error:
        raise _translate_error(error) from error
    return {"items": [_profile_projection(profile) for profile in profiles]}


@router.post("", status_code=201)
def create_profile(
    request: Request,
    payload: CreatePayload,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Создаёт общую карточку, но не добавляет голосовой образец неявно."""

    profile_id = str(uuid4())
    try:
        profile = _service(request).create_profile(
            profile_id=profile_id,
            display_name=payload.display_name,
            aliases=payload.aliases,
            actor_id=session.user_id,
        )
    except Exception as error:
        raise _translate_error(error) from error
    request.app.state.database.add_audit_event(
        actor_id=session.user_id,
        action="voice_profile_created",
        target_type="voice_profile",
        target_id=profile.id,
    )
    return {"profile": _profile_projection(profile)}


@router.patch("/{profile_id}")
def rename_profile(
    request: Request,
    profile_id: str,
    payload: RenamePayload,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Позволяет участнику исправить имя общего профиля с аудитом."""

    try:
        profile = _service(request).rename_profile(
            profile_id=profile_id,
            display_name=payload.display_name,
            actor_id=session.user_id,
        )
    except Exception as error:
        raise _translate_error(error) from error
    request.app.state.database.add_audit_event(
        actor_id=session.user_id,
        action="voice_profile_renamed",
        target_type="voice_profile",
        target_id=profile.id,
    )
    return {"profile": _profile_projection(profile)}


@router.post("/{profile_id}/aliases", status_code=201)
def add_profile_alias(
    request: Request,
    profile_id: str,
    payload: AliasPayload,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Добавляет вариант имени без раскрытия личных источников образца."""

    try:
        profile = _service(request).add_alias(
            profile_id=profile_id,
            alias=payload.alias,
            actor_id=session.user_id,
        )
    except Exception as error:
        raise _translate_error(error) from error
    request.app.state.database.add_audit_event(
        actor_id=session.user_id,
        action="voice_profile_alias_added",
        target_type="voice_profile",
        target_id=profile.id,
    )
    return {"profile": _profile_projection(profile)}


@router.post("/{profile_id}/archive")
def archive_profile(
    request: Request,
    profile_id: str,
    payload: TransitionPayload,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Архивация меняет shared index и потому оставлена администратору."""

    _require_admin(session)
    try:
        result = _service(request).archive_profile(
            profile_id=profile_id,
            actor_id=session.user_id,
            reason=payload.reason,
        )
    except Exception as error:
        raise _translate_error(error) from error
    request.app.state.database.add_audit_event(
        actor_id=session.user_id,
        action="voice_profile_archived",
        target_type="voice_profile",
        target_id=result.profile.id,
    )
    return {
        "profile": _profile_projection(result.profile),
        "pending_operation_ids": list(result.pending_operation_ids),
    }


@router.post("/{profile_id}/restore")
def restore_profile(
    request: Request,
    profile_id: str,
    payload: TransitionPayload,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Восстанавливает профиль через новое поколение общего индекса."""

    _require_admin(session)
    try:
        result = _service(request).restore_profile(
            profile_id=profile_id,
            actor_id=session.user_id,
            reason=payload.reason,
        )
    except Exception as error:
        raise _translate_error(error) from error
    request.app.state.database.add_audit_event(
        actor_id=session.user_id,
        action="voice_profile_restored",
        target_type="voice_profile",
        target_id=result.profile.id,
    )
    return {
        "profile": _profile_projection(result.profile),
        "pending_operation_ids": list(result.pending_operation_ids),
    }
