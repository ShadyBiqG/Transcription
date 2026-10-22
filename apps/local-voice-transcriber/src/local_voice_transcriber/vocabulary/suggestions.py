"""Чистые словарные предложения для текущего снимка транскрипции."""

from __future__ import annotations

import re
from collections.abc import Sequence

from ..editor.models import TranscriptRevision
from .models import SharedVocabularyTerm


def suggest_for_revision(
    revision: TranscriptRevision, terms: Sequence[SharedVocabularyTerm]
) -> list[dict[str, object]]:
    """Находит настроенные варианты без записи и автоматической замены текста."""

    patterns = [
        (term, variant, re.compile(r"(?<!\w)" + re.escape(variant) + r"(?!\w)", re.IGNORECASE))
        for term in terms
        for variant in term.variants
        if variant.casefold() != term.canonical.casefold()
    ]
    proposals: list[dict[str, object]] = []
    for segment in revision.segments:
        candidates = [
            (match.start(), match.end(), term, variant)
            for term, variant, pattern in patterns
            for match in pattern.finditer(segment.text)
        ]
        candidates.sort(key=lambda candidate: (candidate[0], -candidate[1], candidate[2].id))
        last_end = -1
        for start, end, term, variant in candidates:
            if start < last_end:
                continue
            proposals.append(
                {
                    "source_segment_id": segment.source_segment_id,
                    "ordinal": segment.ordinal,
                    "term_id": term.id,
                    "matched_variant": variant,
                    "canonical": term.canonical,
                    "start_offset": start,
                    "end_offset": end,
                }
            )
            last_end = end
    return proposals
