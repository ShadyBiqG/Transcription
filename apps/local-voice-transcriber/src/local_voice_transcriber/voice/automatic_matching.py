"""Безопасное автоматическое сопоставление speaker-групп с общим индексом.

Модуль связывает локальные интервалы диаризации, extractor эмбеддингов и
неизменяемое активное поколение ZeusDB. Решение о кандидате или
автоназначении остаётся в :mod:`local_voice_transcriber.voice.matching`;
здесь нет собственных правил ранжирования голосов. Результаты намеренно не
содержат путь к WAV, идентификатор vector record или байты эмбеддинга.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Protocol

from ..diarization.models import DiarizationInterval
from .index import VectorSearchHit, VoiceVectorIndex
from .matching import match_speaker_group
from .models import (
    DecisionProvenance,
    MatchingThresholds,
    SearchHit,
    SpeakerInterval,
    VoiceMatchDecision,
    VoiceMatchStatus,
)


class ActiveIndexGeneration(Protocol):
    """Безопасная часть метаданных active поколения, нужная matcher-у."""

    id: str


class SharedVoiceProfile(Protocol):
    """Безопасная проекция общего профиля для фильтрации stale hit-ов."""

    id: str


class VoiceMatchingRepository(Protocol):
    """Минимальный read-only контракт общего голосового справочника."""

    def active_generation(self, voice_pack_id: str) -> ActiveIndexGeneration | None:
        """Возвращает текущий проверенный snapshot либо `None`."""

    def list_shared_profiles(self) -> Sequence[SharedVoiceProfile]:
        """Возвращает только активные безопасные профили."""


VoiceEmbeddingExtractor = Callable[[Path, int, int], Sequence[float]]


@dataclass(frozen=True, slots=True)
class AutomaticVoiceMatchingConfig:
    """Неизменяемая конфигурация одного запуска голосового сопоставления.

    Пустой `auto_assign_profile_ids` безопасен: поиск всё ещё может показать
    кандидата, но автоматическое назначение не пройдёт профильный gate из
    `matching.py`. Набор заполняется только явной политикой экземпляра.
    """

    voice_pack_id: str
    thresholds: MatchingThresholds
    search_limit: int = 12
    interval_quality_score: float = 1.0
    auto_assign_profile_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.voice_pack_id:
            raise ValueError("Идентификатор voice pack обязателен.")
        if self.search_limit < 1:
            raise ValueError("Лимит поиска голосового индекса должен быть положительным.")
        if not isfinite(self.interval_quality_score) or not 0 <= self.interval_quality_score <= 1:
            raise ValueError("Оценка качества интервала должна быть числом от 0 до 1.")
        if any(not profile_id for profile_id in self.auto_assign_profile_ids):
            raise ValueError("Идентификаторы профилей для автоназначения не могут быть пустыми.")

    @property
    def is_calibrated_for_matching(self) -> bool:
        """Разрешает поиск только с полным набором калиброванных порогов.

        Выключение auto-assignment не мешает показать кандидата: финальное
        решение останется `candidate` благодаря правилам `matching.py`. Пока
        пороги не утверждены, поиск не запускается и группа остаётся UNKNOWN.
        """

        thresholds = self.thresholds
        return (
            thresholds.calibrated
            and thresholds.candidate_max_distance is not None
            and thresholds.auto_max_distance is not None
            and thresholds.min_margin is not None
        )


@dataclass(frozen=True, slots=True)
class VoiceMatchConfidence:
    """Безопасные агрегированные признаки уверенности без vector bytes."""

    best_distance: float
    margin: float | None
    evidence_count: int
    coverage: float

    def __post_init__(self) -> None:
        if not isfinite(self.best_distance) or self.best_distance < 0:
            raise ValueError("Лучшая distance должна быть конечным неотрицательным числом.")
        if self.margin is not None and (not isfinite(self.margin) or self.margin < 0):
            raise ValueError("Margin должна быть конечным неотрицательным числом.")
        if self.evidence_count < 0:
            raise ValueError("Количество подтверждений не может быть отрицательным.")
        if not isfinite(self.coverage) or not 0 <= self.coverage <= 1:
            raise ValueError("Покрытие должно быть числом от 0 до 1.")


@dataclass(frozen=True, slots=True)
class AutomaticSpeakerGroupMatch:
    """Готовый для publisher-а результат одной локальной speaker-группы.

    `decision` сформирован доменным модулем и хранит не более трёх
    безопасных кандидатов. `profile_id` дублируется намеренно: это упрощает
    проекцию в editor, не заставляя потребителя интерпретировать статус.
    """

    speaker_group_id: str
    decision: VoiceMatchDecision
    profile_id: str | None
    confidence: VoiceMatchConfidence | None
    provenance: DecisionProvenance

    def __post_init__(self) -> None:
        if not self.speaker_group_id:
            raise ValueError("Идентификатор speaker-группы обязателен.")
        if self.profile_id != self.decision.selected_profile_id:
            raise ValueError("Проекция профиля должна совпадать с решением сопоставления.")
        if self.provenance is not self.decision.provenance:
            raise ValueError("Происхождение проекции должно совпадать с решением сопоставления.")


class AutomaticVoiceMatchingService:
    """Выполняет один fail-closed поиск по speaker-группам диаризации."""

    def __init__(
        self,
        *,
        repository: VoiceMatchingRepository,
        vector_index: VoiceVectorIndex,
        extractor: VoiceEmbeddingExtractor,
        config: AutomaticVoiceMatchingConfig,
    ) -> None:
        self._repository = repository
        self._vector_index = vector_index
        self._extractor = extractor
        self._config = config

    def match_groups(
        self,
        *,
        normalized_wav_path: Path,
        intervals: Sequence[DiarizationInterval],
    ) -> tuple[AutomaticSpeakerGroupMatch, ...]:
        """Сопоставляет группы текущей диаризации с active поколением ZeusDB.

        Ошибка подготовки модели, индекса либо одного интервала не прерывает
        транскрипцию и не создаёт частичного назначения: соответствующая группа
        получает безопасный статус `UNKNOWN`. После любой ошибки повторное
        использование исходного WAV остаётся под контролем вызывающего worker-а.
        """

        grouped = _group_intervals(intervals)
        if not grouped:
            return ()
        if not self._config.is_calibrated_for_matching:
            return tuple(
                _unknown_match(
                    group_id,
                    has_overlap=_group_has_overlap(group, intervals),
                    reason="Автоматическое сопоставление недоступно до калибровки порогов.",
                )
                for group_id, group in grouped
            )

        try:
            generation = self._repository.active_generation(self._config.voice_pack_id)
            active_profile_ids = {
                profile.id for profile in self._repository.list_shared_profiles() if profile.id
            }
        except Exception:
            return tuple(
                _unknown_match(
                    group_id,
                    has_overlap=_group_has_overlap(group, intervals),
                    reason="Общий голосовой справочник временно недоступен.",
                )
                for group_id, group in grouped
            )
        if generation is None or not active_profile_ids:
            return tuple(
                _unknown_match(
                    group_id,
                    has_overlap=_group_has_overlap(group, intervals),
                    reason="В общем голосовом индексе нет активного проверенного поколения.",
                )
                for group_id, group in grouped
            )

        return tuple(
            self._match_group(
                group_id=group_id,
                group=group,
                all_intervals=intervals,
                normalized_wav_path=normalized_wav_path,
                generation_id=generation.id,
                active_profile_ids=active_profile_ids,
            )
            for group_id, group in grouped
        )

    def _match_group(
        self,
        *,
        group_id: str,
        group: Sequence[DiarizationInterval],
        all_intervals: Sequence[DiarizationInterval],
        normalized_wav_path: Path,
        generation_id: str,
        active_profile_ids: set[str],
    ) -> AutomaticSpeakerGroupMatch:
        has_overlap = _group_has_overlap(group, all_intervals)
        try:
            speaker_intervals = tuple(
                SpeakerInterval(
                    interval_id=interval.interval_id,
                    duration_ms=interval.end_ms - interval.start_ms,
                    quality_score=self._config.interval_quality_score,
                    has_overlap=has_overlap,
                )
                for interval in group
            )
            hits = tuple(
                hit
                for interval in group
                for hit in self._search_interval(
                    interval=interval,
                    normalized_wav_path=normalized_wav_path,
                    generation_id=generation_id,
                    active_profile_ids=active_profile_ids,
                )
            )
            decision = match_speaker_group(speaker_intervals, hits, self._config.thresholds)
        except Exception:
            return _unknown_match(
                group_id,
                has_overlap=has_overlap,
                reason="Голосовое сопоставление этой группы не выполнилось безопасно.",
            )
        return _project_match(group_id, decision)

    def _search_interval(
        self,
        *,
        interval: DiarizationInterval,
        normalized_wav_path: Path,
        generation_id: str,
        active_profile_ids: set[str],
    ) -> tuple[SearchHit, ...]:
        vector = self._extractor(normalized_wav_path, interval.start_ms, interval.end_ms)
        raw_hits = self._vector_index.search(
            generation_id=generation_id,
            vector=vector,
            limit=self._config.search_limit,
            active_profile_ids=active_profile_ids,
        )
        return tuple(
            _to_search_hit(
                interval_id=interval.interval_id,
                hit=hit,
                profile_auto_assign_enabled=(
                    hit.profile_id in self._config.auto_assign_profile_ids
                ),
            )
            for hit in raw_hits
            if hit.profile_id in active_profile_ids
        )


def _group_intervals(
    intervals: Sequence[DiarizationInterval],
) -> tuple[tuple[str, tuple[DiarizationInterval, ...]], ...]:
    by_group: dict[str, list[DiarizationInterval]] = {}
    for interval in intervals:
        by_group.setdefault(interval.speaker_group_id, []).append(interval)
    return tuple(
        (
            group_id,
            tuple(sorted(group, key=lambda item: (item.start_ms, item.end_ms, item.interval_id))),
        )
        for group_id, group in sorted(by_group.items())
    )


def _group_has_overlap(
    group: Sequence[DiarizationInterval],
    all_intervals: Sequence[DiarizationInterval],
) -> bool:
    """Находит одновременную речь другой группы в абсолютной временной шкале."""

    return any(
        own.speaker_group_id != other.speaker_group_id
        and own.start_ms < other.end_ms
        and other.start_ms < own.end_ms
        for own in group
        for other in all_intervals
    )


def _to_search_hit(
    *,
    interval_id: str,
    hit: VectorSearchHit,
    profile_auto_assign_enabled: bool,
) -> SearchHit:
    return SearchHit(
        interval_id=interval_id,
        profile_id=hit.profile_id,
        record_id=hit.record_id,
        distance=hit.distance,
        profile_auto_assign_enabled=profile_auto_assign_enabled,
    )


def _project_match(
    speaker_group_id: str,
    decision: VoiceMatchDecision,
) -> AutomaticSpeakerGroupMatch:
    confidence = (
        VoiceMatchConfidence(
            best_distance=decision.best_distance,
            margin=decision.margin,
            evidence_count=decision.evidence_count,
            coverage=decision.coverage,
        )
        if decision.best_distance is not None
        else None
    )
    return AutomaticSpeakerGroupMatch(
        speaker_group_id=speaker_group_id,
        decision=decision,
        profile_id=decision.selected_profile_id,
        confidence=confidence,
        provenance=decision.provenance,
    )


def _unknown_match(
    speaker_group_id: str,
    *,
    has_overlap: bool,
    reason: str,
) -> AutomaticSpeakerGroupMatch:
    """Возвращает fail-closed результат без раскрытия внутренней ошибки."""

    return _project_match(
        speaker_group_id,
        VoiceMatchDecision(
            status=VoiceMatchStatus.UNKNOWN,
            provenance=DecisionProvenance.AUTOMATIC,
            selected_profile_id=None,
            candidates=(),
            best_distance=None,
            second_distance=None,
            margin=None,
            evidence_count=0,
            coverage=0.0,
            has_overlap=has_overlap,
            reason=reason,
        ),
    )

