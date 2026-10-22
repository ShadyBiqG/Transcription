"""Неизменяемое связывание ASR-отрезков с результатами диаризации."""

from .mapping import DiarizationMapper
from .models import (
    AsrSegmentReference,
    DiarizationInterval,
    DiarizationLink,
    MappingPolicy,
    SegmentDiarizationMapping,
)
from .sherpa import (
    DiarizationPackVerificationError,
    DiarizationParameters,
    DiarizationRuntimeError,
    LocalDiarizationPack,
    SherpaDiarizationAdapter,
)

__all__ = [
    "AsrSegmentReference",
    "DiarizationInterval",
    "DiarizationLink",
    "DiarizationMapper",
    "DiarizationPackVerificationError",
    "DiarizationParameters",
    "DiarizationRuntimeError",
    "LocalDiarizationPack",
    "MappingPolicy",
    "SherpaDiarizationAdapter",
    "SegmentDiarizationMapping",
]
