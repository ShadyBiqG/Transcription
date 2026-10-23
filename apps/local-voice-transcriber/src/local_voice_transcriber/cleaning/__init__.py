"""Локальная безопасная подготовка чистого черновика.

Модуль не выполняет сетевых запросов. Внешний локальный раннер при необходимости
может реализовать тот же протокол, однако его ответ всё равно обязан пройти
проверку удаления без добавления или перестановки исходных символов.
"""

from .deletion import (
    CleanableSegment,
    CleanDraftCandidate,
    DeletionOnlyCandidateValidator,
    DeletionOnlyValidationError,
    RuleBasedDeletionCleaner,
    TranscriptCleaner,
    allowed_clean_texts,
)
from .local_llm import FallbackTranscriptCleaner, LocalLlamaCppDeletionCleaner

__all__ = [
    "CleanDraftCandidate",
    "CleanableSegment",
    "DeletionOnlyCandidateValidator",
    "DeletionOnlyValidationError",
    "FallbackTranscriptCleaner",
    "LocalLlamaCppDeletionCleaner",
    "RuleBasedDeletionCleaner",
    "TranscriptCleaner",
    "allowed_clean_texts",
]
