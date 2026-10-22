"""Проекция token timings onnx-asr в абсолютную шкалу и слова редактора."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import isfinite

from .models import AsrToken, AsrWord


class TimingProjectionError(ValueError):
    """Модель не вернула достаточных данных для безопасной word-проекции."""


@dataclass(frozen=True, slots=True)
class RelativeToken:
    """Токен onnx-asr: отметка дана относительно VAD-сегмента в секундах."""

    text: str
    timestamp_seconds: float
    logprob: float | None = None


class TokenWordProjector:
    """Конвертирует результаты `TimestampedSegmentResult` без угадывания времени."""

    def absolute_tokens(
        self,
        tokens: Iterable[RelativeToken],
        *,
        segment_start_ms: int,
        segment_end_ms: int,
    ) -> tuple[AsrToken, ...]:
        """Возвращает абсолютные токены; конец токена — начало следующего."""

        raw_tokens = tuple(tokens)
        if not raw_tokens:
            raise TimingProjectionError("Модель не вернула токены")
        starts = [
            segment_start_ms + self._seconds_to_ms(token.timestamp_seconds) for token in raw_tokens
        ]
        if starts[0] < segment_start_ms or starts[-1] > segment_end_ms:
            raise TimingProjectionError("Временные метки токенов выходят за VAD-сегмент")
        if any(right < left for left, right in zip(starts, starts[1:], strict=False)):
            raise TimingProjectionError("Временные метки токенов не отсортированы")

        absolute: list[AsrToken] = []
        for index, token in enumerate(raw_tokens):
            start_ms = starts[index]
            end_ms = starts[index + 1] if index + 1 < len(starts) else segment_end_ms
            if end_ms < start_ms or not token.text:
                raise TimingProjectionError("Некорректный токен модели")
            absolute.append(AsrToken(token.text, start_ms, end_ms, token.logprob))
        return tuple(absolute)

    def words(self, tokens: Iterable[AsrToken]) -> tuple[AsrWord, ...]:
        """Склеивает sentencepiece и пробельные токены, не выдумывая границы слов."""

        result: list[AsrWord] = []
        current_text = ""
        current_start: int | None = None
        current_end: int | None = None

        def flush() -> None:
            nonlocal current_text, current_start, current_end
            text = current_text.strip()
            if text and current_start is not None and current_end is not None:
                result.append(AsrWord(text, current_start, current_end))
            current_text = ""
            current_start = None
            current_end = None

        for token in tokens:
            normalized = token.text.replace("▁", " ")
            if not normalized:
                continue
            pieces = normalized.split()
            starts_word = normalized[0].isspace()
            if starts_word:
                flush()
            if len(pieces) > 1:
                # Один токен с несколькими словами не даёт честных отдельных timestamps.
                raise TimingProjectionError("Токен содержит несколько слов")
            if not pieces:
                flush()
                continue
            if current_start is None:
                current_start = token.start_ms
            current_text += pieces[0]
            current_end = token.end_ms
        flush()
        if not result:
            raise TimingProjectionError("Из токенов нельзя получить слова")
        return tuple(result)

    @staticmethod
    def _seconds_to_ms(value: float) -> int:
        if not isfinite(value) or value < 0:
            raise TimingProjectionError("Некорректная секундна́я метка")
        return round(value * 1000)
