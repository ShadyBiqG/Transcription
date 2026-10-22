from __future__ import annotations

import pytest

from local_voice_transcriber.voice import (
    DecisionProvenance,
    MatchingThresholds,
    QualityPolicy,
    SearchHit,
    SpeakerInterval,
    VoiceMatchStatus,
    assess_interval_quality,
    confirm_voice_match,
    match_speaker_group,
    reject_voice_match,
)


def _thresholds(**changes: object) -> MatchingThresholds:
    defaults: dict[str, object] = {
        "candidate_max_distance": 0.50,
        "auto_max_distance": 0.30,
        "min_margin": 0.05,
        "min_intervals": 2,
        "min_coverage": 0.70,
        "quality_policy": QualityPolicy(min_duration_ms=500, min_quality_score=0.20),
        "calibrated": True,
        "auto_assign_enabled": True,
    }
    defaults.update(changes)
    return MatchingThresholds(**defaults)  # type: ignore[arg-type]


def _interval(
    interval_id: str,
    *,
    duration_ms: int = 1_000,
    quality: float = 1.0,
    overlap: bool = False,
) -> SpeakerInterval:
    return SpeakerInterval(
        interval_id=interval_id,
        duration_ms=duration_ms,
        quality_score=quality,
        has_overlap=overlap,
    )


def _hit(
    interval_id: str,
    profile_id: str,
    distance: float,
    **changes: object,
) -> SearchHit:
    defaults: dict[str, object] = {
        "interval_id": interval_id,
        "profile_id": profile_id,
        "record_id": f"{profile_id}-{interval_id}-{distance}",
        "distance": distance,
        "profile_auto_assign_enabled": True,
    }
    defaults.update(changes)
    return SearchHit(**defaults)  # type: ignore[arg-type]


def test_aggregates_best_hit_per_interval_and_profile_with_weighted_median() -> None:
    decision = match_speaker_group(
        [_interval("i1", quality=0.25), _interval("i2", quality=0.75)],
        [
            _hit("i1", "alex", 0.40),
            _hit("i1", "alex", 0.10, record_id="duplicate-better"),
            _hit("i2", "alex", 0.20),
            _hit("i1", "boris", 0.35),
            _hit("i2", "boris", 0.36),
        ],
        _thresholds(auto_max_distance=0.25),
    )

    assert decision.status is VoiceMatchStatus.AUTO_ASSIGNED
    assert decision.selected_profile_id == "alex"
    assert decision.best_distance == 0.20
    assert decision.second_distance == 0.36
    assert decision.margin == pytest.approx(0.16)
    assert decision.evidence_count == 2
    assert decision.coverage == 1.0
    assert all(candidate.coverage == 1.0 for candidate in decision.candidates)
    assert decision.auto_sample_created is False


def test_returns_at_most_three_distinct_active_profiles_and_filters_archived_samples() -> None:
    decision = match_speaker_group(
        [_interval("i1"), _interval("i2")],
        [
            _hit("i1", "p1", 0.12),
            _hit("i2", "p1", 0.13),
            _hit("i1", "p2", 0.20),
            _hit("i2", "p2", 0.21),
            _hit("i1", "p3", 0.30),
            _hit("i2", "p3", 0.31),
            _hit("i1", "p4", 0.40),
            _hit("i2", "p4", 0.41),
            _hit("i1", "archived", 0.01, sample_is_active=False),
            _hit("i2", "archived", 0.01, sample_is_active=False),
            _hit("i1", "rejected", 0.02, sample_is_rejected=True),
        ],
        _thresholds(),
    )

    assert [candidate.profile_id for candidate in decision.candidates] == ["p1", "p2", "p3"]
    assert decision.selected_profile_id == "p1"


def test_uses_lower_distance_as_better_match() -> None:
    decision = match_speaker_group(
        [_interval("i1"), _interval("i2")],
        [
            _hit("i1", "near", 0.20),
            _hit("i2", "near", 0.20),
            _hit("i1", "far", 0.40),
            _hit("i2", "far", 0.40),
        ],
        _thresholds(auto_max_distance=0.25),
    )

    assert decision.status is VoiceMatchStatus.AUTO_ASSIGNED
    assert decision.selected_profile_id == "near"


def test_returns_unknown_when_no_usable_interval_or_candidate_is_too_far() -> None:
    no_quality = match_speaker_group(
        [_interval("i1", quality=0.10)],
        [_hit("i1", "alex", 0.01)],
        _thresholds(),
    )
    too_far = match_speaker_group(
        [_interval("i1"), _interval("i2")],
        [_hit("i1", "alex", 0.90), _hit("i2", "alex", 0.90)],
        _thresholds(),
    )

    assert no_quality.status is VoiceMatchStatus.UNKNOWN
    assert too_far.status is VoiceMatchStatus.UNKNOWN
    assert too_far.selected_profile_id is None


@pytest.mark.parametrize(
    ("hits", "thresholds", "expected_reason"),
    [
        (
            [_hit("i1", "alex", 0.20), _hit("i1", "boris", 0.40)],
            _thresholds(),
            "Недостаточно интервалов",
        ),
        (
            [
                _hit("i1", "alex", 0.20),
                _hit("i2", "alex", 0.20),
                _hit("i1", "boris", 0.22),
                _hit("i2", "boris", 0.22),
            ],
            _thresholds(min_margin=0.05),
            "Разница distance",
        ),
    ],
)
def test_returns_ambiguous_when_candidate_evidence_or_margin_is_insufficient(
    hits: list[SearchHit],
    thresholds: MatchingThresholds,
    expected_reason: str,
) -> None:
    decision = match_speaker_group([_interval("i1"), _interval("i2")], hits, thresholds)

    assert decision.status is VoiceMatchStatus.AMBIGUOUS
    assert expected_reason in decision.reason


@pytest.mark.parametrize(
    "thresholds",
    [
        _thresholds(calibrated=False),
        _thresholds(auto_assign_enabled=False),
        _thresholds(auto_max_distance=None),
        _thresholds(auto_max_distance=0.15),
    ],
)
def test_returns_candidate_when_auto_assignment_is_not_available(
    thresholds: MatchingThresholds,
) -> None:
    decision = match_speaker_group(
        [_interval("i1"), _interval("i2")],
        [
            _hit("i1", "alex", 0.20),
            _hit("i2", "alex", 0.20),
            _hit("i1", "boris", 0.40),
            _hit("i2", "boris", 0.40),
        ],
        thresholds,
    )

    assert decision.status is VoiceMatchStatus.CANDIDATE
    assert decision.selected_profile_id == "alex"
    assert decision.auto_sample_created is False


def test_overlap_disables_auto_assignment_but_preserves_candidate() -> None:
    decision = match_speaker_group(
        [_interval("i1", overlap=True), _interval("i2")],
        [
            _hit("i1", "alex", 0.20),
            _hit("i2", "alex", 0.20),
            _hit("i1", "boris", 0.40),
            _hit("i2", "boris", 0.40),
        ],
        _thresholds(),
    )

    assert decision.status is VoiceMatchStatus.CANDIDATE
    assert decision.has_overlap is True
    assert "одновременная речь" in decision.reason


def test_low_group_coverage_keeps_result_ambiguous() -> None:
    decision = match_speaker_group(
        [
            _interval("i1", duration_ms=1_000),
            _interval("i2", duration_ms=1_000),
            _interval("i3", duration_ms=2_000),
        ],
        [
            _hit("i1", "alex", 0.20),
            _hit("i2", "alex", 0.20),
            _hit("i1", "boris", 0.40),
            _hit("i2", "boris", 0.40),
        ],
        _thresholds(min_coverage=0.75),
    )

    assert decision.status is VoiceMatchStatus.AMBIGUOUS
    assert decision.coverage == 0.50
    assert "Покрытие речи" in decision.reason


def test_profile_can_individually_disable_auto_assignment() -> None:
    decision = match_speaker_group(
        [_interval("i1"), _interval("i2")],
        [
            _hit("i1", "alex", 0.20, profile_auto_assign_enabled=False),
            _hit("i2", "alex", 0.20, profile_auto_assign_enabled=False),
            _hit("i1", "boris", 0.40),
            _hit("i2", "boris", 0.40),
        ],
        _thresholds(),
    )

    assert decision.status is VoiceMatchStatus.CANDIDATE
    assert "Для профиля отключено" in decision.reason


def test_manual_confirmation_and_rejection_are_explicit_and_never_add_sample() -> None:
    automatic = match_speaker_group(
        [_interval("i1"), _interval("i2")],
        [
            _hit("i1", "alex", 0.20),
            _hit("i2", "alex", 0.20),
            _hit("i1", "boris", 0.40),
            _hit("i2", "boris", 0.40),
        ],
        _thresholds(calibrated=False),
    )

    confirmed = confirm_voice_match(automatic, "boris")
    rejected = reject_voice_match(automatic)

    assert confirmed.status is VoiceMatchStatus.CONFIRMED
    assert confirmed.provenance is DecisionProvenance.MANUAL
    assert confirmed.selected_profile_id == "boris"
    assert confirmed.auto_sample_created is False
    assert rejected.status is VoiceMatchStatus.REJECTED
    assert rejected.provenance is DecisionProvenance.MANUAL
    assert rejected.selected_profile_id is None


def test_quality_policy_rejects_bad_embedding_without_treating_overlap_as_bad_quality() -> None:
    assessment = assess_interval_quality(
        SpeakerInterval(
            interval_id="i1",
            duration_ms=400,
            quality_score=0.40,
            embedding_valid=False,
            has_overlap=True,
        ),
        QualityPolicy(min_duration_ms=500, min_quality_score=0.30),
    )

    assert assessment.accepted is False
    assert len(assessment.reasons) == 2
    assert all("одновременная" not in reason for reason in assessment.reasons)


def test_threshold_validation_prevents_weaker_auto_gate() -> None:
    with pytest.raises(ValueError, match="Порог автоназначения"):
        _thresholds(candidate_max_distance=0.30, auto_max_distance=0.31)
