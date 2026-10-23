"""Неизменяемые проекции редактора транскрипции.

Редактор намеренно работает со строковыми идентификаторами source-отрезков и
групп. Он не меняет ASR или диаризацию: каждая правка создаёт новый снимок.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class RevisionOrigin(StrEnum):
    """Причина появления неизменяемой ревизии."""

    BASELINE = "baseline"
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    RESTORE = "restore"


class CleanDraftStatus(StrEnum):
    """Результат локальной подготовки чистого черновика."""

    APPLIED = "applied"
    UNCHANGED = "unchanged"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SourceSegmentSnapshot:
    """Проекция source-отрезка, достаточная для начальной ревизии.

    ``speaker_group_ids`` содержит все группы из links диаризации. Они могут
    пересекаться по времени; это не повод отбрасывать связь из редактора.
    """

    source_segment_id: str
    ordinal: int
    text: str
    primary_group_id: str | None = None
    speaker_group_ids: tuple[str, ...] = ()
    requires_review: bool = False

    def __post_init__(self) -> None:
        _required(self.source_segment_id, "Идентификатор исходного отрезка")
        if self.ordinal < 0:
            raise ValueError("Порядковый номер отрезка не может быть отрицательным")
        _ensure_unique(self.speaker_group_ids, "Идентификаторы групп отрезка")
        if self.primary_group_id is not None:
            _required(self.primary_group_id, "Основная группа")
            if self.primary_group_id not in self.speaker_group_ids:
                raise ValueError("Основная группа должна присутствовать в связях отрезка")


@dataclass(frozen=True, slots=True)
class GroupSnapshot:
    """Снимок назначения и подписи локальной группы говорящего."""

    speaker_group_id: str
    label_snapshot: str
    profile_id: str | None = None
    assignment_status: str = "unknown"
    provenance: str = "source"
    merged_into_group_id: str | None = None

    def __post_init__(self) -> None:
        _required(self.speaker_group_id, "Идентификатор группы")
        _required(self.label_snapshot, "Подпись группы")
        _required(self.assignment_status, "Статус назначения")
        _required(self.provenance, "Источник назначения")
        if self.profile_id is not None:
            _required(self.profile_id, "Идентификатор голосового профиля")
        if self.merged_into_group_id is not None:
            _required(self.merged_into_group_id, "Целевая группа объединения")
            if self.merged_into_group_id == self.speaker_group_id:
                raise ValueError("Группа не может быть объединена сама с собой")


@dataclass(frozen=True, slots=True)
class GroupUpdate:
    """Полная новая редакторская проекция одной группы.

    Полная форма исключает неявное сохранение устаревшего profile_id при
    ручной замене метки. Для очистки профиля передаётся ``profile_id=None``.
    """

    label_snapshot: str
    profile_id: str | None
    assignment_status: str
    provenance: str = "manual"

    def __post_init__(self) -> None:
        _required(self.label_snapshot, "Подпись группы")
        _required(self.assignment_status, "Статус назначения")
        _required(self.provenance, "Источник назначения")
        if self.profile_id is not None:
            _required(self.profile_id, "Идентификатор голосового профиля")


@dataclass(frozen=True, slots=True)
class RevisionSegment:
    """Текстовый снимок строки редактора, ссылающийся на source-отрезок."""

    source_segment_id: str
    ordinal: int
    text: str
    primary_group_id: str | None
    speaker_group_ids: tuple[str, ...]
    requires_review: bool

    def __post_init__(self) -> None:
        _required(self.source_segment_id, "Идентификатор исходного отрезка")
        if self.ordinal < 0:
            raise ValueError("Порядковый номер отрезка не может быть отрицательным")
        _ensure_unique(self.speaker_group_ids, "Идентификаторы групп отрезка")
        if (
            self.primary_group_id is not None
            and self.primary_group_id not in self.speaker_group_ids
        ):
            raise ValueError("Основная группа должна присутствовать в снимке отрезка")


@dataclass(frozen=True, slots=True)
class TranscriptRevisionInfo:
    """Краткая запись для истории ревизий без содержимого транскрипции."""

    id: str
    job_id: str
    job_run_id: str
    number: int
    parent_id: str | None
    origin: RevisionOrigin
    created_by: str | None
    expected_parent_number: int | None
    comment: str | None
    restored_from_id: str | None
    is_current: bool
    created_at: str


@dataclass(frozen=True, slots=True)
class TranscriptRevision:
    """Полный неизменяемый снимок, отдаваемый редактору."""

    info: TranscriptRevisionInfo
    groups: tuple[GroupSnapshot, ...]
    segments: tuple[RevisionSegment, ...]

    def effective_group_id(self, speaker_group_id: str) -> str:
        """Возвращает видимую группу, учитывая merge данного снимка."""

        groups = {group.speaker_group_id: group for group in self.groups}
        current_id = speaker_group_id
        seen: set[str] = set()
        while True:
            if current_id in seen:
                raise ValueError("В снимке ревизии обнаружен цикл объединений")
            seen.add(current_id)
            group = groups.get(current_id)
            if group is None or group.merged_into_group_id is None:
                return current_id
            current_id = group.merged_into_group_id

    def effective_primary_group_id(self, segment: RevisionSegment) -> str | None:
        """Возвращает группу, которую нужно показать рядом со строкой."""

        if segment.primary_group_id is None:
            return None
        return self.effective_group_id(segment.primary_group_id)


@dataclass(frozen=True, slots=True)
class CleanDraftInfo:
    """Статус чистого представления без раскрытия диагностик очистителя."""

    job_id: str
    owner_id: str
    source_revision_id: str
    clean_revision_id: str | None
    status: CleanDraftStatus
    reason: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class CleanDraftResult:
    """Результат автоматической очистки вместе с актуальной ревизией."""

    info: CleanDraftInfo
    revision: TranscriptRevision


@dataclass(frozen=True, slots=True)
class GroupMergeOperation:
    """Append-only запись объединения либо его обратной операции."""

    id: str
    job_id: str
    source_group_id: str
    target_group_id: str
    operation: str
    revision_id: str
    reverses_operation_id: str | None
    reversed_by: str | None
    created_by: str
    created_at: str


def _required(value: str, field_name: str) -> None:
    if not value:
        raise ValueError(f"{field_name} обязателен")


def _ensure_unique(values: tuple[str, ...], field_name: str) -> None:
    if any(not value for value in values):
        raise ValueError(f"{field_name} не могут быть пустыми")
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} должны быть уникальны")


def display_speaker_label(label: str) -> str:
    """Показывает старые generic-метки в принятой форме «Спикер N»."""

    match = re.fullmatch(r"\s*говорящий\s+(\d+)\s*", label, flags=re.IGNORECASE)
    return f"Спикер {match.group(1)}" if match else label
