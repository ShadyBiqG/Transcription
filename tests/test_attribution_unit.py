from pathlib import Path

from transcription_service.attribution_service import aggregate_frame_results
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
    assert choose_frame_times(0, 9000, 3) == [1800, 4500, 7200]
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
