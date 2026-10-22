"""Прикладные сценарии общего словаря без привязки к HTTP."""

from __future__ import annotations

from collections.abc import Sequence

from .models import SharedVocabularyTerm, VocabularyTermState
from .repository import VocabularyRepository


class VocabularyService:
    """Явные операции редактирования общего словаря.

    Сервис специально не принимает текст сегмента или идентификатор
    транскрипции: добавление и редактирование терминов не может неявно менять
    чью-либо готовую расшифровку.
    """

    def __init__(self, repository: VocabularyRepository) -> None:
        self._repository = repository

    def create_term(
        self,
        *,
        term_id: str,
        canonical: str,
        actor_id: str,
        variants: Sequence[str] = (),
        context_hint: str | None = None,
    ) -> SharedVocabularyTerm:
        """Создаёт видимый всем общий термин."""

        return self._repository.create_term(
            term_id=term_id,
            canonical=canonical,
            actor_id=actor_id,
            variants=variants,
            context_hint=context_hint,
        )

    def list_terms(self, *, query: str = "") -> list[SharedVocabularyTerm]:
        """Ищет только активные термины для будущих задач."""

        return self._repository.search_active_terms(query)

    def list_all_terms(
        self,
        *,
        state: VocabularyTermState | None = None,
    ) -> list[SharedVocabularyTerm]:
        """Возвращает общие проекции, в том числе для административного обзора."""

        return self._repository.list_shared_terms(state=state)

    def rename_term(
        self,
        *,
        term_id: str,
        canonical: str,
        actor_id: str,
    ) -> SharedVocabularyTerm:
        """Явно меняет каноническую форму общего термина."""

        return self._repository.rename_term(
            term_id=term_id,
            canonical=canonical,
            actor_id=actor_id,
        )

    def update_context_hint(
        self,
        *,
        term_id: str,
        context_hint: str | None,
        actor_id: str,
    ) -> SharedVocabularyTerm:
        """Явно меняет общую контекстную подсказку."""

        return self._repository.update_context_hint(
            term_id=term_id,
            context_hint=context_hint,
            actor_id=actor_id,
        )

    def add_variant(
        self,
        *,
        term_id: str,
        variant: str,
        actor_id: str,
    ) -> SharedVocabularyTerm:
        """Добавляет общий вариант написания или произношения."""

        return self._repository.add_variant(
            term_id=term_id,
            variant=variant,
            actor_id=actor_id,
        )

    def archive_term(self, *, term_id: str, actor_id: str) -> SharedVocabularyTerm:
        """Скрывает термин из дальнейших активных подсказок."""

        return self._repository.archive_term(term_id=term_id, actor_id=actor_id)

    def restore_term(self, *, term_id: str, actor_id: str) -> SharedVocabularyTerm:
        """Возвращает архивный термин в общий активный словарь."""

        return self._repository.restore_term(term_id=term_id, actor_id=actor_id)
