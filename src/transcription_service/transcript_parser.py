from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

ANCHOR_PATTERN = re.compile(r"^ts_(\d+)_(\d+)_(S\d+)$")


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    id: str
    ordinal: int
    source_anchor: str
    source_label: str
    start_ms: int
    end_ms: int
    text: str


class TranscriptParseError(ValueError):
    pass


class _NoScribeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current: tuple[str, int, int, str] | None = None
        self.fragments: list[str] = []
        self.parsed: list[tuple[str, int, int, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a" or self.current is not None:
            return
        attributes = dict(attrs)
        anchor = attributes.get("name") or attributes.get("id") or ""
        match = ANCHOR_PATTERN.fullmatch(anchor)
        if match:
            start, end, label = match.groups()
            self.current = (anchor, int(start), int(end), label)
            self.fragments = []

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.fragments.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self.current is None:
            return
        anchor, start, end, label = self.current
        text = " ".join("".join(self.fragments).split())
        self.parsed.append((anchor, start, end, label, text))
        self.current = None
        self.fragments = []


def parse_noscribe_html(path: Path) -> list[TranscriptSegment]:
    parser = _NoScribeParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    if parser.current is not None:
        raise TranscriptParseError("Незакрытая временная ссылка в HTML noScribe")
    if not parser.parsed:
        raise TranscriptParseError("В HTML noScribe не найдены реплики с таймкодами")
    anchors: set[str] = set()
    result: list[TranscriptSegment] = []
    for ordinal, (anchor, start, end, label, text) in enumerate(parser.parsed):
        if anchor in anchors:
            raise TranscriptParseError(f"Повторяющийся таймкод {anchor}")
        if end <= start:
            raise TranscriptParseError(f"Некорректный интервал {anchor}")
        anchors.add(anchor)
        result.append(
            TranscriptSegment(
                id=str(uuid.uuid4()),
                ordinal=ordinal,
                source_anchor=anchor,
                source_label=label,
                start_ms=start,
                end_ms=end,
                text=_strip_visible_prefix(text, label),
            )
        )
    return result


def _strip_visible_prefix(text: str, label: str) -> str:
    return re.sub(
        rf"^{re.escape(label)}:\s*(?:\[[0-9:.,]+\]\s*)?",
        "",
        text,
        count=1,
    ).strip()
