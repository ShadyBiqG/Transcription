"""Неизменяемые исходные данные и результаты связывания диаризации.

Этот слой не выполняет кластеризацию и не меняет выход Sherpa ONNX. Он только
фиксирует пересечения в общей временной шкале, чтобы редактор мог отобразить
несколько говорящих без потери исходных интервалов.
"""

from __future__ import annotations

from dataclasses import dataclass


class InvalidDiarizationTimingError(ValueError):
    """Временной интервал диаризации или ASR задан некорректно."""


def _validate_non_empty(value: str, field_name: str) -> None:
    if not value:
        raise ValueError(f"{field_name} обязателен.")


def _validate_interval(start_ms: int, end_ms: int) -> None:
    if start_ms < 0 or end_ms <= start_ms:
        raise InvalidDiarizationTimingError("Интервал должен иметь положительную длительность.")


@dataclass(frozen=True, slots=True)
class AsrSegmentReference:
    """Ссылка на неизменяемый ASR-отрезок в абсолютной временной шкале.

    Текст и токены остаются в ASR-слое. Здесь намеренно хранится только ключ и
    границы, нужные для построения связи 0..N.
    """

    segment_id: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        _validate_non_empty(self.segment_id, "Идентификатор ASR-отрезка")
        _validate_interval(self.start_ms, self.end_ms)


@dataclass(frozen=True, slots=True)
class DiarizationInterval:
    """Неизменяемый интервал, возвращённый диаризацией.

    ``raw_speaker_label`` всегда сохраняется как пришёл от модели. Группа —
    локальная техническая проекция кластеризации данного job run и не заменяет
    сырую метку. Пересечения между интервалами разрешены: это не ошибка данных.
    """

    interval_id: str
    start_ms: int
    end_ms: int
    raw_speaker_label: str
    speaker_group_id: str

    def __post_init__(self) -> None:
        _validate_non_empty(self.interval_id, "Идентификатор диаризационного интервала")
        _validate_non_empty(self.raw_speaker_label, "Сырая метка говорящего")
        _validate_non_empty(self.speaker_group_id, "Идентификатор группы говорящего")
        _validate_interval(self.start_ms, self.end_ms)


@dataclass(frozen=True, slots=True)
class MappingPolicy:
    """Правило, когда пересечение достаточно для основной speaker-метки.

    Доля рассчитывается от длительности ASR-отрезка. Более короткие пересечения
    всё равно попадают в links и не теряются, но не могут единолично назначить
    основную группу.
    """

    min_significant_overlap_ratio: float = 0.20

    def __post_init__(self) -> None:
        if not 0 < self.min_significant_overlap_ratio <= 1:
            raise ValueError("Порог существенного пересечения должен быть в диапазоне (0; 1].")


@dataclass(frozen=True, slots=True)
class DiarizationLink:
    """Одна неизменяемая связь ASR-отрезка с диаризационным интервалом."""

    asr_segment_id: str
    diarization_interval_id: str
    speaker_group_id: str
    overlap_ms: int
    overlap_ratio: float
    is_primary: bool
    requires_review: bool

    def __post_init__(self) -> None:
        _validate_non_empty(self.asr_segment_id, "Идентификатор ASR-отрезка")
        _validate_non_empty(self.diarization_interval_id, "Идентификатор диаризационного интервала")
        _validate_non_empty(self.speaker_group_id, "Идентификатор группы говорящего")
        if self.overlap_ms <= 0:
            raise ValueError("Пересечение должно иметь положительную длительность.")
        if not 0 < self.overlap_ratio <= 1:
            raise ValueError("Доля пересечения должна быть в диапазоне (0; 1].")


@dataclass(frozen=True, slots=True)
class SegmentDiarizationMapping:
    """Все связи одного ASR-отрезка и безопасная primary-проекция для UI."""

    asr_segment_id: str
    links: tuple[DiarizationLink, ...]
    primary_speaker_group_id: str | None
    requires_review: bool

    def __post_init__(self) -> None:
        _validate_non_empty(self.asr_segment_id, "Идентификатор ASR-отрезка")
        if any(link.asr_segment_id != self.asr_segment_id for link in self.links):
            raise ValueError("Связь относится к другому ASR-отрезку.")
        primary_links = tuple(link for link in self.links if link.is_primary)
        if self.primary_speaker_group_id is None:
            if primary_links:
                raise ValueError("Primary-связь требует основную группу.")
        elif len(primary_links) != 1:
            raise ValueError("Для основной группы нужна ровно одна primary-связь.")
        elif primary_links[0].speaker_group_id != self.primary_speaker_group_id:
            raise ValueError("Primary-связь и основная группа не совпадают.")
        if any(link.requires_review != self.requires_review for link in self.links):
            raise ValueError("Статус ручной проверки должен быть одинаков для всех связей отрезка.")
