from pathlib import Path

from transcription_service.attribution_service import (
    _group_pending_segments,
    _representative_segments,
    aggregate_frame_results,
)
from transcription_service.frames import choose_frame_times
from transcription_service.transcript_parser import parse_noscribe_html


def test_parser_extracts_noscribe_segments(tmp_path: Path) -> None:
    source = tmp_path / "transcript.html"
    source.write_text(
        '<a name="ts_1000_4500_S00">S00: Первая фраза</a>'
        '<a name="ts_4500_9000_S01">S01: Вторая фраза</a>',
        encoding="utf-8",
    )

    segments = parse_noscribe_html(source)

    assert [(item.start_ms, item.end_ms, item.text) for item in segments] == [
        (1000, 4500, "Первая фраза"),
        (4500, 9000, "Вторая фраза"),
    ]


def test_frame_selection_avoids_segment_edges() -> None:
    assert choose_frame_times(0, 9000, 2) == [3600, 5400]
    assert choose_frame_times(0, 9000, 3) == [3150, 4500, 5850]
    assert choose_frame_times(1000, 1100, 3) == [1050]


def test_aggregate_requires_agreement_and_preserves_visible_label() -> None:
    result = aggregate_frame_results(
        [
            {"status": "detected", "speaker_label": "Гость", "confidence": 0.91},
            {"status": "detected", "speaker_label": " гость ", "confidence": 0.85},
            {"status": "unknown", "speaker_label": None, "confidence": 0.1},
        ]
    )

    assert result["status"] == "detected"
    assert result["speaker_label"] == "Гость"


def test_aggregate_marks_conflicting_frames_unknown() -> None:
    result = aggregate_frame_results(
        [
            {"status": "detected", "speaker_label": "Гость", "confidence": 0.9},
            {"status": "detected", "speaker_label": "Иван", "confidence": 0.9},
        ]
    )
    assert result["status"] == "unknown"


def test_aggregate_accepts_one_confident_detection_without_conflicts() -> None:
    result = aggregate_frame_results(
        [
            {"status": "no_highlight", "speaker_label": None, "confidence": 0.1},
            {"status": "detected", "speaker_label": "Гость", "confidence": 0.88},
            {"status": "label_unreadable", "speaker_label": None, "confidence": 0.2},
        ]
    )

    assert result["status"] == "detected"
    assert result["speaker_label"] == "Гость"


def test_aggregate_rejects_one_uncertain_detection() -> None:
    result = aggregate_frame_results(
        [
            {"status": "no_highlight", "speaker_label": None, "confidence": 0.1},
            {"status": "detected", "speaker_label": "Гость", "confidence": 0.6},
            {"status": "label_unreadable", "speaker_label": None, "confidence": 0.2},
        ]
    )

    assert result["status"] == "unknown"


def test_representative_segments_prefer_longest_utterances() -> None:
    segments = [
        {"id": "short", "ordinal": 0, "start_ms": 0, "end_ms": 1000},
        {"id": "long-late", "ordinal": 2, "start_ms": 2000, "end_ms": 9000},
        {"id": "long-early", "ordinal": 1, "start_ms": 1000, "end_ms": 8000},
        {"id": "medium", "ordinal": 3, "start_ms": 9000, "end_ms": 13000},
    ]

    selected = _representative_segments(segments, limit=3)

    assert [item["id"] for item in selected] == ["long-early", "long-late", "medium"]


def test_representative_segments_deprioritize_pauses() -> None:
    segments = [
        {
            "id": "pause",
            "ordinal": 0,
            "start_ms": 0,
            "end_ms": 20000,
            "text": "(20 секунд паузы)",
        },
        {
            "id": "speech",
            "ordinal": 1,
            "start_ms": 20000,
            "end_ms": 26000,
            "text": "Длинная реплика",
        },
    ]

    assert _representative_segments(segments, limit=1)[0]["id"] == "speech"


def test_processing_modes_group_by_voice_or_segment() -> None:
    segments = [
        {"id": "one", "source_label": "S00", "status": "pending"},
        {"id": "two", "source_label": "S00", "status": "pending"},
        {"id": "done", "source_label": "S01", "status": "detected"},
    ]

    fast = _group_pending_segments(segments, precise=False)
    precise = _group_pending_segments(segments, precise=True)

    assert list(fast) == ["S00"]
    assert [item["id"] for item in fast["S00"]] == ["one", "two"]
    assert list(precise) == ["one", "two"]
