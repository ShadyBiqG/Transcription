from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from local_voice_transcriber.diarization.models import DiarizationInterval
from local_voice_transcriber.voice.automatic_matching import (
    AutomaticVoiceMatchingConfig,
    AutomaticVoiceMatchingService,
)
from local_voice_transcriber.voice.index import VectorSearchHit
from local_voice_transcriber.voice.models import MatchingThresholds, QualityPolicy, VoiceMatchStatus


@dataclass(frozen=True)
class _Generation:
    id: str = "generation-1"


@dataclass(frozen=True)
class _Profile:
    id: str


_DEFAULT_GENERATION = object()


class _Repository:
    def __init__(
        self,
        *,
        generation: _Generation | None | object = _DEFAULT_GENERATION,
        profiles: Sequence[_Profile] = (_Profile("alex"), _Profile("boris")),
        fail: bool = False,
    ) -> None:
        self._generation = _Generation() if generation is _DEFAULT_GENERATION else generation
        self._profiles = profiles
        self._fail = fail

    def active_generation(self, voice_pack_id: str) -> _Generation | None:
        assert voice_pack_id == "sherpa-voice-v1"
        if self._fail:
            raise RuntimeError("внутренняя ошибка")
        return self._generation

    def list_shared_profiles(self) -> Sequence[_Profile]:
        if self._fail:
            raise RuntimeError("внутренняя ошибка")
        return self._profiles


class _VectorIndex:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[float, ...], int, set[str] | None]] = []

    def search(
        self,
        *,
        generation_id: str,
        vector: Sequence[float],
        limit: int,
        active_profile_ids: set[str] | None = None,
    ) -> tuple[VectorSearchHit, ...]:
        query = tuple(vector)
        self.calls.append((generation_id, query, limit, active_profile_ids))
        return (
            VectorSearchHit(record_id="alex-reference", profile_id="alex", distance=0.10),
            VectorSearchHit(record_id="boris-reference", profile_id="boris", distance=0.35),
        )


class _Extractor:
    def __init__(self, *, fail_for_start_ms: int | None = None) -> None:
        self.calls: list[tuple[Path, int, int]] = []
        self._fail_for_start_ms = fail_for_start_ms

    def __call__(self, wav_path: Path, start_ms: int, end_ms: int) -> tuple[float, ...]:
        self.calls.append((wav_path, start_ms, end_ms))
        if start_ms == self._fail_for_start_ms:
            raise RuntimeError("сбой extractor")
        return (float(start_ms + 1), 1.0)


def _config(**changes: object) -> AutomaticVoiceMatchingConfig:
    defaults: dict[str, object] = {
        "voice_pack_id": "sherpa-voice-v1",
        "thresholds": MatchingThresholds(
            candidate_max_distance=0.50,
            auto_max_distance=0.25,
            min_margin=0.10,
            min_intervals=2,
            min_coverage=0.75,
            quality_policy=QualityPolicy(min_duration_ms=500, min_quality_score=0.20),
            calibrated=True,
            auto_assign_enabled=True,
        ),
        "auto_assign_profile_ids": frozenset({"alex"}),
    }
    defaults.update(changes)
    return AutomaticVoiceMatchingConfig(**defaults)  # type: ignore[arg-type]


def _interval(
    interval_id: str,
    *,
    start_ms: int,
    end_ms: int,
    group_id: str = "local-1",
) -> DiarizationInterval:
    return DiarizationInterval(
        interval_id=interval_id,
        start_ms=start_ms,
        end_ms=end_ms,
        raw_speaker_label=f"speaker-{group_id}",
        speaker_group_id=group_id,
    )


def test_aggregates_group_intervals_through_existing_matching_logic() -> None:
    index = _VectorIndex()
    extractor = _Extractor()
    service = AutomaticVoiceMatchingService(
        repository=_Repository(),
        vector_index=index,
        extractor=extractor,
        config=_config(),
    )

    result = service.match_groups(
        normalized_wav_path=Path("C:/private/jobs/job-1/audio.wav"),
        intervals=(
            _interval("turn-2", start_ms=1_000, end_ms=2_000),
            _interval("turn-1", start_ms=0, end_ms=1_000),
        ),
    )

    assert len(result) == 1
    group = result[0]
    assert group.speaker_group_id == "local-1"
    assert group.decision.status is VoiceMatchStatus.AUTO_ASSIGNED
    assert group.profile_id == "alex"
    assert group.confidence is not None
    assert group.confidence.best_distance == 0.10
    assert group.confidence.evidence_count == 2
    assert [call[1] for call in index.calls] == [(1.0, 1.0), (1001.0, 1.0)]
    assert len(extractor.calls) == 2
    assert "audio.wav" not in repr(group)


def test_uncalibrated_configuration_fails_closed_without_reading_audio_or_index() -> None:
    thresholds = MatchingThresholds(
        candidate_max_distance=0.50,
        auto_max_distance=0.25,
        min_margin=0.10,
        min_intervals=1,
        min_coverage=0.0,
        calibrated=False,
        auto_assign_enabled=True,
    )
    index = _VectorIndex()
    extractor = _Extractor()
    service = AutomaticVoiceMatchingService(
        repository=_Repository(),
        vector_index=index,
        extractor=extractor,
        config=_config(thresholds=thresholds),
    )

    result = service.match_groups(
        normalized_wav_path=Path("C:/private/jobs/job-1/audio.wav"),
        intervals=(_interval("turn-1", start_ms=0, end_ms=1_000),),
    )

    assert result[0].decision.status is VoiceMatchStatus.UNKNOWN
    assert result[0].profile_id is None
    assert result[0].confidence is None
    assert extractor.calls == []
    assert index.calls == []


def test_calibrated_but_auto_disabled_configuration_returns_candidate() -> None:
    thresholds = MatchingThresholds(
        candidate_max_distance=0.50,
        auto_max_distance=0.25,
        min_margin=0.10,
        min_intervals=1,
        min_coverage=0.0,
        calibrated=True,
        auto_assign_enabled=False,
    )
    index = _VectorIndex()
    service = AutomaticVoiceMatchingService(
        repository=_Repository(),
        vector_index=index,
        extractor=_Extractor(),
        config=_config(thresholds=thresholds),
    )

    result = service.match_groups(
        normalized_wav_path=Path("C:/private/jobs/job-1/audio.wav"),
        intervals=(_interval("turn-1", start_ms=0, end_ms=1_000),),
    )

    assert result[0].decision.status is VoiceMatchStatus.CANDIDATE
    assert result[0].profile_id == "alex"
    assert index.calls


def test_missing_active_generation_returns_safe_unknown_without_audio_access() -> None:
    index = _VectorIndex()
    extractor = _Extractor()
    service = AutomaticVoiceMatchingService(
        repository=_Repository(generation=None),
        vector_index=index,
        extractor=extractor,
        config=_config(),
    )

    result = service.match_groups(
        normalized_wav_path=Path("C:/private/jobs/job-1/audio.wav"),
        intervals=(_interval("turn-1", start_ms=0, end_ms=1_000),),
    )

    assert result[0].decision.status is VoiceMatchStatus.UNKNOWN
    assert extractor.calls == []
    assert index.calls == []


def test_group_error_is_isolated_and_does_not_partially_assign_it() -> None:
    index = _VectorIndex()
    extractor = _Extractor(fail_for_start_ms=1_000)
    service = AutomaticVoiceMatchingService(
        repository=_Repository(),
        vector_index=index,
        extractor=extractor,
        config=_config(),
    )

    result = service.match_groups(
        normalized_wav_path=Path("C:/private/jobs/job-1/audio.wav"),
        intervals=(
            _interval("turn-1", start_ms=0, end_ms=1_000),
            _interval("turn-2", start_ms=1_000, end_ms=2_000),
        ),
    )

    assert result[0].decision.status is VoiceMatchStatus.UNKNOWN
    assert result[0].profile_id is None
    assert result[0].confidence is None
    assert len(index.calls) == 1


def test_cross_group_overlap_blocks_auto_assignment_but_keeps_safe_candidate() -> None:
    service = AutomaticVoiceMatchingService(
        repository=_Repository(),
        vector_index=_VectorIndex(),
        extractor=_Extractor(),
        config=_config(
            thresholds=MatchingThresholds(
                candidate_max_distance=0.50,
                auto_max_distance=0.25,
                min_margin=0.10,
                min_intervals=1,
                min_coverage=0.0,
                calibrated=True,
                auto_assign_enabled=True,
            )
        ),
    )

    result = service.match_groups(
        normalized_wav_path=Path("C:/private/jobs/job-1/audio.wav"),
        intervals=(
            _interval("turn-1", start_ms=0, end_ms=1_000, group_id="local-1"),
            _interval("turn-2", start_ms=500, end_ms=1_500, group_id="local-2"),
        ),
    )

    assert [item.decision.status for item in result] == [
        VoiceMatchStatus.CANDIDATE,
        VoiceMatchStatus.CANDIDATE,
    ]
    assert all(item.profile_id == "alex" for item in result)

