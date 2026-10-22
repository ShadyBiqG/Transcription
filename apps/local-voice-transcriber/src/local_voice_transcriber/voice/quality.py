"""Проверка качества diarization-интервалов до поиска профилей."""

from __future__ import annotations

from .models import IntervalQualityAssessment, QualityPolicy, SpeakerInterval


def assess_interval_quality(
    interval: SpeakerInterval,
    policy: QualityPolicy,
) -> IntervalQualityAssessment:
    """Возвращает все причины отклонения, не скрывая первичную диагностику.

    Overlap не является дефектом качества: такой интервал может помочь оператору
    увидеть кандидата, но дальнейшее автоназначение группы заблокирует matching.
    """

    reasons: list[str] = []
    if interval.duration_ms < policy.min_duration_ms:
        reasons.append("Длительность интервала меньше допустимой.")
    if interval.quality_score < policy.min_quality_score:
        reasons.append("Оценка качества эмбеддинга ниже допустимой.")
    if interval.speech_ratio < policy.min_speech_ratio:
        reasons.append("Доля речи в интервале ниже допустимой.")
    if policy.require_embedding and not interval.embedding_valid:
        reasons.append("Эмбеддинг интервала невалиден.")

    return IntervalQualityAssessment(
        interval_id=interval.interval_id,
        accepted=not reasons,
        reasons=tuple(reasons),
    )
