"""API общего словаря терминов без доступа к содержимому чужих расшифровок."""

from __future__ import annotations

import sqlite3
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Body, Query, Request
from pydantic import BaseModel, Field

from ..auth import ApiError, CsrfSessionDependency, SessionDependency
from ..vocabulary import (
    SharedVocabularyTerm,
    VocabularyError,
    VocabularyService,
    VocabularyTermNotFoundError,
    VocabularyTermStateError,
)

router = APIRouter(prefix="/api/v1/vocabulary", tags=["vocabulary"])


class CreateTermRequest(BaseModel):
    """Новая общая карточка специфичного термина."""

    canonical: str = Field(min_length=1, max_length=250)
    variants: list[str] = Field(default_factory=list, max_length=30)
    context_hint: str | None = Field(default=None, max_length=1_000)


class RenameTermRequest(BaseModel):
    """Исправление канонического написания общего термина."""

    canonical: str = Field(min_length=1, max_length=250)


class ContextHintRequest(BaseModel):
    """Добавление либо очистка безопасной общей подсказки."""

    context_hint: str | None = Field(default=None, max_length=1_000)


class AddVariantRequest(BaseModel):
    """Добавление альтернативного написания или произношения."""

    variant: str = Field(min_length=1, max_length=250)


CreatePayload = Annotated[CreateTermRequest, Body()]
RenamePayload = Annotated[RenameTermRequest, Body()]
ContextPayload = Annotated[ContextHintRequest, Body()]
VariantPayload = Annotated[AddVariantRequest, Body()]


def _service(request: Request) -> VocabularyService:
    return request.app.state.vocabulary_service


def _term_projection(term: SharedVocabularyTerm) -> dict[str, object]:
    """Выдаёт только безопасную общую проекцию без автора и истории."""

    return {
        "id": term.id,
        "canonical": term.canonical,
        "variants": list(term.variants),
        "context_hint": term.context_hint,
        "state": term.state,
        "created_at": term.created_at,
        "updated_at": term.updated_at,
        "archived_at": term.archived_at,
    }


def _translate_error(error: Exception) -> ApiError:
    if isinstance(error, VocabularyTermNotFoundError):
        return ApiError(404, "vocabulary_term_not_found", "Термин словаря не найден")
    if isinstance(error, sqlite3.IntegrityError):
        return ApiError(
            409,
            "vocabulary_term_conflict",
            "Термин или вариант уже используется в общем словаре",
        )
    if isinstance(error, VocabularyTermStateError):
        return ApiError(409, "vocabulary_term_state_conflict", str(error))
    if isinstance(error, (VocabularyError, ValueError)):
        return ApiError(422, "invalid_vocabulary_term", str(error))
    return ApiError(503, "vocabulary_unavailable", "Общий словарь временно недоступен")


def _require_admin(session: CsrfSessionDependency) -> None:
    if session.role != "admin":
        raise ApiError(403, "admin_required", "Требуется роль администратора")


def _audit(request: Request, *, actor_id: str, action: str, term_id: str) -> None:
    request.app.state.database.add_audit_event(
        actor_id=actor_id,
        action=action,
        target_type="vocabulary_term",
        target_id=term_id,
    )


@router.get("")
def list_terms(
    request: Request,
    session: SessionDependency,
    query: str = Query(default="", max_length=250),
) -> dict[str, object]:
    """Показывает всем вошедшим активную безопасную часть общего словаря."""

    del session
    try:
        terms = _service(request).list_terms(query=query)
    except Exception as error:
        raise _translate_error(error) from error
    return {"items": [_term_projection(term) for term in terms]}


@router.post("", status_code=201)
def create_term(
    request: Request,
    payload: CreatePayload,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Создаёт термин для будущих запусков без неявной правки транскрипций."""

    try:
        term = _service(request).create_term(
            term_id=str(uuid4()),
            canonical=payload.canonical,
            variants=payload.variants,
            context_hint=payload.context_hint,
            actor_id=session.user_id,
        )
    except Exception as error:
        raise _translate_error(error) from error
    _audit(request, actor_id=session.user_id, action="vocabulary_term_created", term_id=term.id)
    return {"term": _term_projection(term)}


@router.patch("/{term_id}")
def rename_term(
    request: Request,
    term_id: str,
    payload: RenamePayload,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Позволяет участнику уточнить общее каноническое написание."""

    try:
        term = _service(request).rename_term(
            term_id=term_id,
            canonical=payload.canonical,
            actor_id=session.user_id,
        )
    except Exception as error:
        raise _translate_error(error) from error
    _audit(request, actor_id=session.user_id, action="vocabulary_term_renamed", term_id=term.id)
    return {"term": _term_projection(term)}


@router.patch("/{term_id}/context")
def update_context_hint(
    request: Request,
    term_id: str,
    payload: ContextPayload,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Изменяет подсказку, которая может использоваться лишь в новых запусках."""

    try:
        term = _service(request).update_context_hint(
            term_id=term_id,
            context_hint=payload.context_hint,
            actor_id=session.user_id,
        )
    except Exception as error:
        raise _translate_error(error) from error
    _audit(request, actor_id=session.user_id, action="vocabulary_context_updated", term_id=term.id)
    return {"term": _term_projection(term)}


@router.post("/{term_id}/variants", status_code=201)
def add_variant(
    request: Request,
    term_id: str,
    payload: VariantPayload,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Добавляет общий вариант термина без доступа к чужим заданиям."""

    try:
        term = _service(request).add_variant(
            term_id=term_id,
            variant=payload.variant,
            actor_id=session.user_id,
        )
    except Exception as error:
        raise _translate_error(error) from error
    _audit(request, actor_id=session.user_id, action="vocabulary_variant_added", term_id=term.id)
    return {"term": _term_projection(term)}


@router.post("/{term_id}/archive")
def archive_term(
    request: Request,
    term_id: str,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Скрывает термин из будущих подсказок; действие обратимо и административно."""

    _require_admin(session)
    try:
        term = _service(request).archive_term(term_id=term_id, actor_id=session.user_id)
    except Exception as error:
        raise _translate_error(error) from error
    _audit(request, actor_id=session.user_id, action="vocabulary_term_archived", term_id=term.id)
    return {"term": _term_projection(term)}


@router.post("/{term_id}/restore")
def restore_term(
    request: Request,
    term_id: str,
    session: CsrfSessionDependency,
) -> dict[str, object]:
    """Возвращает архивный термин в подсказки будущих запусков."""

    _require_admin(session)
    try:
        term = _service(request).restore_term(term_id=term_id, actor_id=session.user_id)
    except Exception as error:
        raise _translate_error(error) from error
    _audit(request, actor_id=session.user_id, action="vocabulary_term_restored", term_id=term.id)
    return {"term": _term_projection(term)}
