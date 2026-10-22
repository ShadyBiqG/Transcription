"""Нормализованные неизменяемые результаты автоматического распознавания."""

from __future__ import annotations

from dataclasses import dataclass


class InvalidTimingError(ValueError):
    """Временная метка модели не образует допустимый интервал."""


@dataclass(frozen=True, slots=True)
class AsrToken:
    """Токен в абсолютной временной шкале исходной записи."""

    text: str
    start_ms: int
    end_ms: int
    logprob: float | None = None
    word_id: int | None = None

    def __post_init__(self) -> None:
        _validate_interval(self.start_ms, self.end_ms)
        if not self.text:
            raise ValueError("Токен не может быть пустым")


@dataclass(frozen=True, slots=True)
class AsrWord:
    """Производное слово; его нет, если модель не дала пригодные token timings."""

    text: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        _validate_interval(self.start_ms, self.end_ms)
        if not self.text.strip():
            raise ValueError("Слово не может быть пустым")


@dataclass(frozen=True, slots=True)
class AsrSegment:
    """Неизменяемый ASR-отрезок, независимый от диаризации."""

    start_ms: int
    end_ms: int
    text: str
    confidence: float | None
    tokens: tuple[AsrToken, ...]
    words: tuple[AsrWord, ...]
    word_timing_status: str

    def __post_init__(self) -> None:
        _validate_interval(self.start_ms, self.end_ms)
        if self.word_timing_status not in {"available", "unavailable"}:
            raise ValueError("Неизвестный статус временных меток слов")
        if self.word_timing_status == "available" and not self.words:
            raise ValueError("Статус available требует хотя бы одно слово")
        if self.word_timing_status == "unavailable" and self.words:
            raise ValueError("При unavailable слова не сохраняются")
        for token in self.tokens:
            if token.start_ms < self.start_ms or token.end_ms > self.end_ms:
                raise InvalidTimingError("Токен выходит за границы ASR-отрезка")
        for word in self.words:
            if word.start_ms < self.start_ms or word.end_ms > self.end_ms:
                raise InvalidTimingError("Слово выходит за границы ASR-отрезка")


@dataclass(frozen=True, slots=True)
class AsrRecognition:
    """Полный результат ASR для одного нормализованного WAV."""

    language: str
    segments: tuple[AsrSegment, ...]


def _validate_interval(start_ms: int, end_ms: int) -> None:
    if start_ms < 0 or end_ms < start_ms:
        raise InvalidTimingError("Некорректный временной интервал")
