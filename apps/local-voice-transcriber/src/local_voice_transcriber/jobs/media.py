"""Безопасная локальная нормализация аудио для job pipeline."""

from __future__ import annotations

import subprocess
from pathlib import Path


class MediaNormalizationError(RuntimeError):
    """FFmpeg не подготовил безопасный PCM WAV."""


class FfmpegNormalizer:
    """Вызывает FFmpeg только со статическим argv и путями из candidate data root."""

    _SOURCE_SUFFIXES = {
        ".webm",
        ".mp3",
        ".wav",
        ".m4a",
        ".aac",
        ".ogg",
        ".opus",
        ".mp4",
        ".mov",
    }

    def __init__(self, executable: Path, *, jobs_root: Path, timeout_seconds: int = 600) -> None:
        self._executable = executable
        self._jobs_root = jobs_root
        self._timeout_seconds = timeout_seconds

    def normalize(self, source: Path, destination: Path) -> None:
        self._validate_path(source, suffixes=self._SOURCE_SUFFIXES, must_exist=True)
        self._validate_path(destination, suffixes={".wav"}, must_exist=False)
        if not self._executable.is_file():
            raise MediaNormalizationError("FFmpeg не установлен")
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = [
            str(self._executable),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise MediaNormalizationError("Локальная нормализация аудио не выполнена") from error
        if not destination.is_file() or destination.stat().st_size == 0:
            raise MediaNormalizationError("FFmpeg не создал нормализованный WAV")

    def _validate_path(self, path: Path, *, suffixes: set[str], must_exist: bool) -> None:
        try:
            root = self._jobs_root.resolve(strict=True)
            resolved = path.resolve(strict=must_exist)
            resolved.relative_to(root)
        except (FileNotFoundError, ValueError) as error:
            raise MediaNormalizationError("Путь медиа находится вне candidate jobs root") from error
        if resolved.suffix.lower() not in suffixes:
            raise MediaNormalizationError("Недопустимый формат локального медиа")
