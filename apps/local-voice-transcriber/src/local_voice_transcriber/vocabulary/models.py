"""Модели общего словаря специфичных слов.

Словарь не хранит чужие расшифровки и не изменяет их автоматически. Он лишь
предоставляет общий набор терминов для явного использования в следующих
запусках распознавания.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class VocabularyError(RuntimeError):
    """Базовая ошибка домена общего словаря."""


class VocabularyTermNotFoundError(VocabularyError):
    """Термин не существует."""


class VocabularyTermStateError(VocabularyError):
    """Операция недопустима для текущего состояния термина."""


class VocabularyTermState(StrEnum):
    """Жизненный цикл общего термина."""

    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class SharedVocabularyTerm:
    """Безопасная общая проекция словарной записи.

    В проекции нет идентификатора автора, истории действий и данных о том, в
    чьих заданиях термин использовался. Она пригодна для выдачи всем
    авторизованным пользователям.
    """

    id: str
    canonical: str
    variants: tuple[str, ...]
    context_hint: str | None
    state: VocabularyTermState
    created_at: str
    updated_at: str
    archived_at: str | None
