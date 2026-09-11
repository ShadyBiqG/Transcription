from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FrameExtractionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExtractedFrame:
    path: Path
    requested_ms: int
    actual_ms: int
    sha256: str


def choose_frame_times(start_ms: int, end_ms: int, max_frames: int = 3) -> list[int]:
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("Некорректный временной интервал")
    duration = end_ms - start_ms
    if duration < 1200 or max_frames == 1:
        return [start_ms + duration // 2]
    count = min(max_frames, 5)
    if count == 2:
        fractions = (0.4, 0.6)
    elif count == 3:
        fractions = (0.35, 0.5, 0.65)
    else:
        fractions = tuple((index + 1) / (count + 1) for index in range(count))
    margin = min(350, duration // 5)
    return [
        min(end_ms - margin, max(start_ms + margin, start_ms + int(duration * fraction)))
        for fraction in fractions
    ]


class FFmpegFrameExtractor:
    def __init__(self, executable: Path | None, timeout_seconds: int = 60) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    @property
    def ready(self) -> bool:
        return self.executable is not None and self.executable.is_file()

    def extract(self, source: Path, timestamp_ms: int, destination: Path) -> ExtractedFrame:
        if not self.ready or self.executable is None:
            raise FrameExtractionError("FFmpeg не настроен или недоступен")
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.executable),
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp_ms / 1000:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            "-y",
            str(destination),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FrameExtractionError("Не удалось запустить FFmpeg") from exc
        invalid_output = not destination.is_file() or destination.stat().st_size == 0
        if completed.returncode != 0 or invalid_output:
            message = (completed.stderr or "FFmpeg не создал кадр").strip()[:500]
            raise FrameExtractionError(message)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        return ExtractedFrame(destination, timestamp_ms, timestamp_ms, digest)
