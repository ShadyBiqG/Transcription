"""Чистая агрегация результатов голосового поиска на уровне speaker-группы."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import replace

from .models import (
    DecisionProvenance,
    MatchingThresholds,
    SearchHit,
    SpeakerInterval,
    VoiceMatchCandidate,
    VoiceMatchDecision,
    VoiceMatchStatus,
)
from .quality import assess_interval_quality


def match_speaker_group(
    intervals: Sequence[SpeakerInterval],
    search_hits: Iterable[SearchHit],
    thresholds: MatchingThresholds,
) -> VoiceMatchDecision:
    """Строит до трёх разных профилей и принимает автоматическое решение.

    Для каждого ``(interval, profile)`` остаётся ровно один hit с минимальной
    distance. Далее distance профиля — quality-weighted median этих значений.
    Такой порядок не позволяет множеству sample одного профиля получить перевес.
    """

    accepted_intervals = _accepted_intervals(intervals, thresholds)
    if not accepted_intervals:
        return _decision(
            status=VoiceMatchStatus.UNKNOWN,
            selected_profile_id=None,
            candidates=(),
            reason="Нет пригодных интервалов для голосового сопоставления.",
            has_overlap=any(interval.has_overlap for interval in intervals),
        )

    # Сохраняем лучший hit на интервал и профиль, сразу скрывая архивы и rejected.
    best_by_interval_profile: dict[tuple[str, str], SearchHit] = {}
    for hit in search_hits:
        if hit.interval_id not in accepted_intervals or not _is_searchable(hit):
            continue
        key = (hit.interval_id, hit.profile_id)
        current = best_by_interval_profile.get(key)
        if current is None or (hit.distance, hit.record_id) < (current.distance, current.record_id):
            best_by_interval_profile[key] = hit

    group_duration_ms = sum(interval.duration_ms for interval in intervals)
    candidates = [
        replace(
            candidate,
            coverage=_coverage(candidate.interval_ids, accepted_intervals, group_duration_ms),
        )
        for candidate in _aggregate_candidates(accepted_intervals, best_by_interval_profile)
    ]
    has_overlap = any(interval.has_overlap for interval in intervals)
    if not candidates:
        return _decision(
            status=VoiceMatchStatus.UNKNOWN,
            selected_profile_id=None,
            candidates=(),
            reason="В активном голосовом индексе нет подходящих профилей.",
            has_overlap=has_overlap,
        )

    top_candidates = tuple(candidates[:3])
    best = top_candidates[0]
    second = top_candidates[1] if len(top_candidates) > 1 else None
    margin = second.distance - best.distance if second is not None else None
    if thresholds.candidate_max_distance is None:
        return _decision(
            status=VoiceMatchStatus.UNKNOWN,
            selected_profile_id=None,
            candidates=top_candidates,
            best_distance=best.distance,
            second_distance=second.distance if second else None,
            margin=margin,
            evidence_count=best.evidence_count,
            coverage=best.coverage,
            has_overlap=has_overlap,
            reason="Порог кандидата ещё не настроен.",
        )
    if best.distance > thresholds.candidate_max_distance:
        return _decision(
            status=VoiceMatchStatus.UNKNOWN,
            selected_profile_id=None,
            candidates=top_candidates,
            best_distance=best.distance,
            second_distance=second.distance if second else None,
            margin=margin,
            evidence_count=best.evidence_count,
            coverage=best.coverage,
            has_overlap=has_overlap,
            reason="Лучшая distance превышает порог кандидата.",
        )

    ambiguity_reason = _candidate_gate_failure(best, margin, thresholds)
    if ambiguity_reason is not None:
        return _decision(
            status=VoiceMatchStatus.AMBIGUOUS,
            selected_profile_id=best.profile_id,
            candidates=top_candidates,
            best_distance=best.distance,
            second_distance=second.distance if second else None,
            margin=margin,
            evidence_count=best.evidence_count,
            coverage=best.coverage,
            has_overlap=has_overlap,
            reason=ambiguity_reason,
        )

    auto_reason = _auto_assignment_blocker(best, has_overlap, thresholds)
    if auto_reason is not None:
        return _decision(
            status=VoiceMatchStatus.CANDIDATE,
            selected_profile_id=best.profile_id,
            candidates=top_candidates,
            best_distance=best.distance,
            second_distance=second.distance if second else None,
            margin=margin,
            evidence_count=best.evidence_count,
            coverage=best.coverage,
            has_overlap=has_overlap,
            reason=auto_reason,
        )

    return _decision(
        status=VoiceMatchStatus.AUTO_ASSIGNED,
        selected_profile_id=best.profile_id,
        candidates=top_candidates,
        best_distance=best.distance,
        second_distance=second.distance if second else None,
        margin=margin,
        evidence_count=best.evidence_count,
        coverage=best.coverage,
        has_overlap=has_overlap,
        reason="Пройдены калиброванные условия автоматического назначения.",
    )


def confirm_voice_match(decision: VoiceMatchDecision, profile_id: str) -> VoiceMatchDecision:
    """Фиксирует явный выбор владельца для всей группы, не создавая sample."""

    if not profile_id:
        raise ValueError("Идентификатор выбранного профиля обязателен.")
    selected_candidate = next(
        (candidate for candidate in decision.candidates if candidate.profile_id == profile_id),
        None,
    )
    return replace(
        decision,
        status=VoiceMatchStatus.CONFIRMED,
        provenance=DecisionProvenance.MANUAL,
        selected_profile_id=profile_id,
        best_distance=selected_candidate.distance if selected_candidate else None,
        second_distance=None,
        margin=None,
        evidence_count=selected_candidate.evidence_count if selected_candidate else 0,
        coverage=selected_candidate.coverage if selected_candidate else 0.0,
        reason="Профиль подтверждён владельцем группы.",
        auto_sample_created=False,
    )


def reject_voice_match(decision: VoiceMatchDecision) -> VoiceMatchDecision:
    """Фиксирует явное отклонение предложения владельцем без удаления истории."""

    return replace(
        decision,
        status=VoiceMatchStatus.REJECTED,
        provenance=DecisionProvenance.MANUAL,
        selected_profile_id=None,
        reason="Предложение голосового профиля отклонено владельцем группы.",
        auto_sample_created=False,
    )


def _is_searchable(hit: SearchHit) -> bool:
    return hit.profile_is_active and hit.sample_is_active and not hit.sample_is_rejected


def _accepted_intervals(
    intervals: Sequence[SpeakerInterval],
    thresholds: MatchingThresholds,
) -> dict[str, SpeakerInterval]:
    accepted: dict[str, SpeakerInterval] = {}
    seen_ids: set[str] = set()
    for interval in intervals:
        if interval.interval_id in seen_ids:
            raise ValueError("Идентификаторы интервалов в группе должны быть уникальны.")
        seen_ids.add(interval.interval_id)
        if not assess_interval_quality(interval, thresholds.quality_policy).accepted:
            continue
        # Нулевой вес не определяет quality-weighted median, даже если оператор
        # временно сконфигурировал разрешённый минимум качества в ноль.
        if interval.quality_score > 0:
            accepted[interval.interval_id] = interval
    return accepted


def _aggregate_candidates(
    intervals: dict[str, SpeakerInterval],
    best_by_interval_profile: dict[tuple[str, str], SearchHit],
) -> list[VoiceMatchCandidate]:
    by_profile: dict[str, list[SearchHit]] = defaultdict(list)
    for (_, profile_id), hit in best_by_interval_profile.items():
        by_profile[profile_id].append(hit)

    candidates: list[VoiceMatchCandidate] = []
    for profile_id, hits in by_profile.items():
        hits.sort(key=lambda hit: hit.interval_id)
        weighted_values = [
            (hit.distance, intervals[hit.interval_id].quality_score)
            for hit in hits
        ]
        interval_ids = tuple(hit.interval_id for hit in hits)
        candidates.append(
            VoiceMatchCandidate(
                profile_id=profile_id,
                distance=_weighted_median(weighted_values),
                evidence_count=len(hits),
                coverage=0.0,
                interval_ids=interval_ids,
                profile_auto_assign_enabled=all(
                    hit.profile_auto_assign_enabled for hit in hits
                ),
            )
        )
    return sorted(candidates, key=lambda candidate: (candidate.distance, candidate.profile_id))


def _weighted_median(values: Sequence[tuple[float, float]]) -> float:
    """Возвращает нижнюю взвешенную медиану; веса должны быть положительными."""

    if not values:
        raise ValueError("Нельзя вычислить медиану пустого набора.")
    ordered = sorted(values)
    total_weight = sum(weight for _, weight in ordered)
    if total_weight <= 0:
        raise ValueError("Для взвешенной медианы нужен положительный вес.")
    midpoint = total_weight / 2
    accumulated = 0.0
    for distance, weight in ordered:
        accumulated += weight
        if accumulated >= midpoint:
            return distance
    return ordered[-1][0]


def _coverage(
    interval_ids: Sequence[str],
    intervals: dict[str, SpeakerInterval],
    group_duration_ms: int,
) -> float:
    if group_duration_ms <= 0:
        return 0.0
    evidence_duration = sum(intervals[interval_id].duration_ms for interval_id in interval_ids)
    return min(1.0, evidence_duration / group_duration_ms)


def _candidate_gate_failure(
    best: VoiceMatchCandidate,
    margin: float | None,
    thresholds: MatchingThresholds,
) -> str | None:
    if best.evidence_count < thresholds.min_intervals:
        return "Недостаточно интервалов, подтверждающих кандидата."
    if best.coverage < thresholds.min_coverage:
        return "Покрытие речи кандидатом ниже допустимого."
    if thresholds.min_margin is None:
        return "Минимальный margin ещё не настроен."
    if margin is None or margin < thresholds.min_margin:
        return "Разница distance до следующего профиля недостаточна."
    return None


def _auto_assignment_blocker(
    best: VoiceMatchCandidate,
    has_overlap: bool,
    thresholds: MatchingThresholds,
) -> str | None:
    if has_overlap:
        return "В группе есть одновременная речь: требуется ручная проверка."
    if not thresholds.calibrated:
        return "Автоматическое назначение недоступно до калибровки."
    if not thresholds.auto_assign_enabled:
        return "Автоматическое назначение отключено в наборе порогов."
    if not best.profile_auto_assign_enabled:
        return "Для профиля отключено автоматическое назначение."
    if thresholds.auto_max_distance is None:
        return "Порог автоматического назначения ещё не настроен."
    if best.distance > thresholds.auto_max_distance:
        return "Лучшая distance превышает порог автоматического назначения."
    return None


def _decision(
    *,
    status: VoiceMatchStatus,
    selected_profile_id: str | None,
    candidates: Sequence[VoiceMatchCandidate],
    reason: str,
    best_distance: float | None = None,
    second_distance: float | None = None,
    margin: float | None = None,
    evidence_count: int = 0,
    coverage: float = 0.0,
    has_overlap: bool = False,
) -> VoiceMatchDecision:
    return VoiceMatchDecision(
        status=status,
        provenance=DecisionProvenance.AUTOMATIC,
        selected_profile_id=selected_profile_id,
        candidates=tuple(candidates),
        best_distance=best_distance,
        second_distance=second_distance,
        margin=margin,
        evidence_count=evidence_count,
        coverage=coverage,
        has_overlap=has_overlap,
        reason=reason,
    )
