"""Локальный ASR без сетевых вызовов и без зависимости от legacy-приложения."""

from .adapter import LocalModelPack, OfflineAsrAdapter, PackVerificationError
from .models import AsrRecognition, AsrSegment, AsrToken, AsrWord
from .timing import TokenWordProjector

__all__ = [
    "AsrRecognition",
    "AsrSegment",
    "AsrToken",
    "AsrWord",
    "LocalModelPack",
    "OfflineAsrAdapter",
    "PackVerificationError",
    "TokenWordProjector",
]
