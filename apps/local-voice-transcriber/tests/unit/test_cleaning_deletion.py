from __future__ import annotations

import pytest

from local_voice_transcriber.cleaning import (
    CleanableSegment,
    CleanDraftCandidate,
    DeletionOnlyCandidateValidator,
    DeletionOnlyValidationError,
    RuleBasedDeletionCleaner,
)


def test_rule_based_cleaner_removes_only_safe_isolated_noise() -> None:
    segments = (
        CleanableSegment("noise", "Эм..."),
        CleanableSegment("punctuation", "."),
        CleanableSegment("speech", "Эм, давайте начнём"),
        CleanableSegment("meaning", "Ну, это важно"),
    )

    candidate = RuleBasedDeletionCleaner().clean(segments)

    assert candidate.segment_texts == {
        "noise": "",
        "punctuation": "",
        "speech": "давайте начнём",
    }
    assert DeletionOnlyCandidateValidator().validate(segments, candidate) == candidate.segment_texts


def test_validator_rejects_added_or_reordered_content() -> None:
    segments = (CleanableSegment("segment-1", "Исходный текст"),)

    with pytest.raises(DeletionOnlyValidationError):
        DeletionOnlyCandidateValidator().validate(
            segments,
            CleanDraftCandidate({"segment-1": "Новый текст"}),
        )

    with pytest.raises(DeletionOnlyValidationError):
        DeletionOnlyCandidateValidator().validate(
            segments,
            CleanDraftCandidate({"unknown": ""}),
        )


def test_validator_rejects_arbitrary_deletion_inside_terms_numbers_and_dates() -> None:
    segments = (
        CleanableSegment(
            "segment-1",
            "Эм, отчёт Иванова от 09.07.2026 по КОД-42",
            protected_tokens=("Иванова", "КОД-42"),
        ),
    )

    with pytest.raises(DeletionOnlyValidationError):
        DeletionOnlyCandidateValidator().validate(
            segments,
            CleanDraftCandidate({"segment-1": "отчёт Иванова от 09.07.202 по КОД-42"}),
        )

    assert DeletionOnlyCandidateValidator().validate(
        segments,
        CleanDraftCandidate({"segment-1": "отчёт Иванова от 09.07.2026 по КОД-42"}),
    ) == {"segment-1": "отчёт Иванова от 09.07.2026 по КОД-42"}
