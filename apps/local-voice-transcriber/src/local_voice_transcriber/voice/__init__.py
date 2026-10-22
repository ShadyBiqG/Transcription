"""Чистая доменная логика голосового справочника Transcription Next."""

from .matching import confirm_voice_match, match_speaker_group, reject_voice_match
from .models import (
    DecisionProvenance,
    IntervalQualityAssessment,
    MatchingThresholds,
    QualityPolicy,
    SearchHit,
    SpeakerInterval,
    VoiceMatchCandidate,
    VoiceMatchDecision,
    VoiceMatchStatus,
)
from .quality import assess_interval_quality

__all__ = [
    "DecisionProvenance",
    "IntervalQualityAssessment",
    "MatchingThresholds",
    "QualityPolicy",
    "SearchHit",
    "SpeakerInterval",
    "VoiceMatchCandidate",
    "VoiceMatchDecision",
    "VoiceMatchStatus",
    "assess_interval_quality",
    "confirm_voice_match",
    "match_speaker_group",
    "reject_voice_match",
]
