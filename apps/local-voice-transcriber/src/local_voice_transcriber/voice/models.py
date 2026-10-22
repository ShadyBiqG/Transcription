"""Неизменяемые типы доменной логики голосового сопоставления.

Модуль намеренно не зависит от SQLite, ZeusDB или ONNX Runtime. Адаптер индекса
переводит результат поиска в :class:`SearchHit`, а этот слой принимает решение
только по уже полученным техническим признакам.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite


class VoiceMatchStatus(StrEnum):
    """Состояние соответствия локальной группы общему голосовому профилю."""

    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"
    CANDIDATE = "candidate"
    AUTO_ASSIGNED = "auto_assigned"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class DecisionProvenance(StrEnum):
    """Источник решения, который должен быть сохранён вместе с ревизией."""

    AUTOMATIC = "automatic"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class SpeakerInterval:
    """Пригодный для поиска интервал локальной diarization-группы.

    ``quality_score`` — нормализованная оценка качества эмбеддинга от 0 до 1.
    Пересечение с другой речью не делает интервал невалидным, но запрещает
    автоматическое назначение всей группы.
    """

    interval_id: str
    duration_ms: int
    quality_score: float
    speech_ratio: float = 1.0
    embedding_valid: bool = True
    has_overlap: bool = False

    def __post_init__(self) -> None:
        if not self.interval_id:
            raise ValueError("Идентификатор интервала обязателен.")
        if self.duration_ms < 0:
            raise ValueError("Длительность интервала не может быть отрицательной.")
        _validate_ratio(self.quality_score, "Оценка качества")
        _validate_ratio(self.speech_ratio, "Доля речи")


@dataclass(frozen=True, slots=True)
class SearchHit:
    """Один технический результат поиска в текущем поколении голосового индекса.

    ``distance`` имеет семантику ZeusDB: меньшее значение означает более близкий
    голос. Идентификатор записи остаётся только техническим и не выдаётся UI.
    """

    interval_id: str
    profile_id: str
    record_id: str
    distance: float
    profile_is_active: bool = True
    sample_is_active: bool = True
    sample_is_rejected: bool = False
    profile_auto_assign_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.interval_id:
            raise ValueError("Идентификатор интервала обязателен.")
        if not self.profile_id:
            raise ValueError("Идентификатор профиля обязателен.")
        if not self.record_id:
            raise ValueError("Идентификатор записи индекса обязателен.")
        if not isfinite(self.distance) or self.distance < 0:
            raise ValueError("Distance должна быть конечным неотрицательным числом.")


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    """Минимальные требования к интервалу до его участия в сопоставлении."""

    min_duration_ms: int = 0
    min_quality_score: float = 0.01
    min_speech_ratio: float = 0.0
    require_embedding: bool = True

    def __post_init__(self) -> None:
        if self.min_duration_ms < 0:
            raise ValueError("Минимальная длительность не может быть отрицательной.")
        _validate_ratio(self.min_quality_score, "Минимальная оценка качества")
        _validate_ratio(self.min_speech_ratio, "Минимальная доля речи")


@dataclass(frozen=True, slots=True)
class IntervalQualityAssessment:
    """Результат quality gate для одного diarization-интервала."""

    interval_id: str
    accepted: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatchingThresholds:
    """Снимок порогов, с которым запускается один voice run.

    Порог distance сравнивается оператором ``<=``: он не является процентом
    сходства. ``calibrated`` относится только к автоматическому назначению —
    показывать кандидатов можно и до калибровки.
    """

    candidate_max_distance: float | None
    auto_max_distance: float | None
    min_margin: float | None
    min_intervals: int
    min_coverage: float
    quality_policy: QualityPolicy = field(default_factory=QualityPolicy)
    calibrated: bool = False
    auto_assign_enabled: bool = False

    def __post_init__(self) -> None:
        _validate_optional_non_negative(self.candidate_max_distance, "Порог кандидата")
        _validate_optional_non_negative(self.auto_max_distance, "Порог автоназначения")
        _validate_optional_non_negative(self.min_margin, "Минимальный margin")
        if self.min_intervals < 1:
            raise ValueError("Минимум интервалов должен быть не меньше одного.")
        _validate_ratio(self.min_coverage, "Минимальное покрытие")
        if (
            self.candidate_max_distance is not None
            and self.auto_max_distance is not None
            and self.auto_max_distance > self.candidate_max_distance
        ):
            raise ValueError("Порог автоназначения не может быть слабее порога кандидата.")


@dataclass(frozen=True, slots=True)
class VoiceMatchCandidate:
    """Агрегированный кандидат, доступный для показа в панели группы."""

    profile_id: str
    distance: float
    evidence_count: int
    coverage: float
    interval_ids: tuple[str, ...]
    profile_auto_assign_enabled: bool

    def __post_init__(self) -> None:
        if not self.profile_id:
            raise ValueError("Идентификатор профиля обязателен.")
        if not isfinite(self.distance) or self.distance < 0:
            raise ValueError("Distance кандидата должна быть неотрицательной.")
        if self.evidence_count < 1:
            raise ValueError("У кандидата должен быть хотя бы один интервал.")
        _validate_ratio(self.coverage, "Покрытие кандидата")


@dataclass(frozen=True, slots=True)
class VoiceMatchDecision:
    """Решение для целой speaker-группы, готовое к фиксации в ревизии.

    Поле ``auto_sample_created`` всегда ложно. Добавление reference sample —
    отдельное явное действие владельца и никогда не является побочным эффектом
    поиска или auto-assignment.
    """

    status: VoiceMatchStatus
    provenance: DecisionProvenance
    selected_profile_id: str | None
    candidates: tuple[VoiceMatchCandidate, ...]
    best_distance: float | None
    second_distance: float | None
    margin: float | None
    evidence_count: int
    coverage: float
    has_overlap: bool
    reason: str
    auto_sample_created: bool = False

    def __post_init__(self) -> None:
        if self.auto_sample_created:
            raise ValueError("Автоматическое создание голосового образца запрещено.")
        _validate_optional_non_negative(self.best_distance, "Лучшая distance")
        _validate_optional_non_negative(self.second_distance, "Вторая distance")
        _validate_optional_non_negative(self.margin, "Margin")
        if self.evidence_count < 0:
            raise ValueError("Количество интервалов не может быть отрицательным.")
        _validate_ratio(self.coverage, "Покрытие")
        if not self.reason:
            raise ValueError("Причина решения обязательна.")
        if self.status in {VoiceMatchStatus.AUTO_ASSIGNED, VoiceMatchStatus.CONFIRMED}:
            if not self.selected_profile_id:
                raise ValueError("Для назначения требуется выбранный профиль.")
        if self.status in {VoiceMatchStatus.UNKNOWN, VoiceMatchStatus.REJECTED}:
            if self.selected_profile_id is not None:
                raise ValueError("У неизвестного или отклонённого решения нет профиля.")


def _validate_ratio(value: float, label: str) -> None:
    if not isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{label} должна быть конечным числом от 0 до 1.")


def _validate_optional_non_negative(value: float | None, label: str) -> None:
    if value is not None and (not isfinite(value) or value < 0):
        raise ValueError(f"{label} должна быть конечным неотрицательным числом.")
