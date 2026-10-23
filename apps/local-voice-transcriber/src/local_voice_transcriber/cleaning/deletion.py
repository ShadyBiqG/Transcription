"""Проверяемая локальная очистка только удалением исходных символов."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


class DeletionOnlyValidationError(ValueError):
    """Кандидат очистки добавил, заменил или затронул неизвестный отрезок."""


@dataclass(frozen=True, slots=True)
class CleanableSegment:
    """Минимальная проекция исходной реплики для локального очистителя."""

    source_segment_id: str
    text: str
    protected_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CleanDraftCandidate:
    """Предложенные тексты только для реально изменяемых реплик."""

    segment_texts: Mapping[str, str]


class TranscriptCleaner(Protocol):
    """Локальный очиститель, который не получает сетевых клиентов или секретов."""

    def clean(self, segments: Sequence[CleanableSegment]) -> CleanDraftCandidate: ...


class DeletionOnlyCandidateValidator:
    """Разрешает лишь заранее вычисленные безопасные варианты исходной реплики.

    Простая проверка подпоследовательности недостаточна: она позволяла бы
    удалить произвольную букву из ФИО, термина или даты. Здесь внешний локальный
    LLM может выбрать только вариант, полученный из whitelist междометий либо
    реплики из одной пунктуации. Любое сомнительное предложение отклоняется.
    """

    def validate(
        self,
        segments: Sequence[CleanableSegment],
        candidate: CleanDraftCandidate,
    ) -> dict[str, str]:
        source_by_id = {segment.source_segment_id: segment.text for segment in segments}
        unknown_ids = set(candidate.segment_texts).difference(source_by_id)
        if unknown_ids:
            raise DeletionOnlyValidationError("Очиститель изменил неизвестный отрезок")
        cleaned: dict[str, str] = {}
        for source_segment_id, proposed_text in candidate.segment_texts.items():
            if not isinstance(proposed_text, str):
                raise DeletionOnlyValidationError("Очиститель вернул текст неверного типа")
            source_text = source_by_id[source_segment_id]
            segment = next(item for item in segments if item.source_segment_id == source_segment_id)
            if proposed_text not in allowed_clean_texts(segment):
                raise DeletionOnlyValidationError(
                    "Очиститель выбрал недопустимое изменение исходной реплики"
                )
            if not _is_subsequence(proposed_text, source_text):
                raise DeletionOnlyValidationError("Очиститель добавил символы в исходную реплику")
            if proposed_text != source_text:
                cleaned[source_segment_id] = proposed_text
        return cleaned


class RuleBasedDeletionCleaner:
    """Малый офлайн-очиститель заведомо безопасных междометий и мусора ASR.

    Он удаляет только самостоятельные бессодержательные реплики и начальные
    звуки-паразиты. Смысловые слова вроде «да», «нет», «ну» и «вот» намеренно
    не считаются мусором: для них нужна осмысленная ручная правка, а не догадка.
    """

    _punctuation_only = re.compile(r"^[\s\W_]+$", re.UNICODE)
    _standalone_filler = re.compile(
        r"^\s*(?:э+|м+|эм+|мм+|м-м+|гм+|угу+|ага+)(?:[\s,.!?:;…—–-]*)$",
        re.IGNORECASE,
    )
    _leading_filler = re.compile(
        r"^\s*(?:(?:э+|м+|эм+|мм+|м-м+|гм+)(?:\s*[,;:…—–-]\s*|\s+))+",
        re.IGNORECASE,
    )

    def clean(self, segments: Sequence[CleanableSegment]) -> CleanDraftCandidate:
        changes: dict[str, str] = {}
        for segment in segments:
            cleaned = _preferred_clean_text(segment)
            if cleaned != segment.text:
                changes[segment.source_segment_id] = cleaned
        return CleanDraftCandidate(segment_texts=changes)


def allowed_clean_texts(segment: CleanableSegment) -> frozenset[str]:
    """Строит малый whitelist вариантов без удаления ФИО, чисел или терминов."""

    text = segment.text
    options = {text}
    if not text.strip() or RuleBasedDeletionCleaner._punctuation_only.fullmatch(text):
        options.add("")
    elif RuleBasedDeletionCleaner._standalone_filler.fullmatch(text):
        options.add("")
    else:
        without_leading_filler = RuleBasedDeletionCleaner._leading_filler.sub("", text)
        if without_leading_filler != text:
            options.add(without_leading_filler)
    protected_tokens = (*_number_and_date_tokens(text), *segment.protected_tokens)
    return frozenset(
        option
        for option in options
        if _preserves_protected_tokens(text, option, protected_tokens)
    )


def _preferred_clean_text(segment: CleanableSegment) -> str:
    """Выбирает единственное безопасное изменение, если оно существует."""

    alternatives = sorted(allowed_clean_texts(segment).difference({segment.text}), key=len)
    return alternatives[0] if alternatives else segment.text


def _number_and_date_tokens(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"(?<!\w)\d[\d./:-]*(?!\w)", text, flags=re.UNICODE))


def _preserves_protected_tokens(source: str, candidate: str, tokens: Sequence[str]) -> bool:
    source_normalized = source.casefold()
    candidate_normalized = candidate.casefold()
    return all(
        not token.strip()
        or candidate_normalized.count(token.casefold()) >= source_normalized.count(token.casefold())
        for token in tokens
    )


def _is_subsequence(candidate: str, source: str) -> bool:
    """Проверяет, что candidate получен исключительно удалением из source."""

    source_iterator = iter(source)
    for character in candidate:
        for source_character in source_iterator:
            if source_character == character:
                break
        else:
            return False
    return True
