"""Безопасный адаптер общего persistent vector store на базе ZeusDB."""

from .zeus import (
    NativeZeusDBTransport,
    VectorStoreAvailability,
    VectorStoreStatus,
    VectorStoreUnavailableError,
    ZeusDBVoiceVectorStore,
)

__all__ = [
    "NativeZeusDBTransport",
    "VectorStoreAvailability",
    "VectorStoreStatus",
    "VectorStoreUnavailableError",
    "ZeusDBVoiceVectorStore",
]
