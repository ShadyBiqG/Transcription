from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_NOSCRIBE_PATH = Path(r"C:\Program Files (x86)\noScribe\noScribe.exe")
DEFAULT_NOSCRIBE_FFMPEG_PATH = Path(
    r"C:\Program Files (x86)\noScribe\_internal\noScribeEdit\_internal\ffmpeg_win\ffmpeg.exe"
)


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    noscribe_path: Path
    default_language: str
    default_model: str
    allowed_models: tuple[str, ...]
    max_upload_bytes: int
    noscribe_timeout_seconds: int
    session_ttl_days: int = 30
    cookie_secure: bool = False
    ffmpeg_path: Path | None = None
    routerai_base_url: str = "https://routerai.ru/api/v1"
    routerai_timeout_seconds: int = 60
    attribution_max_frames: int = 3
    model_catalog_refresh_hours: int = 24

    def __post_init__(self) -> None:
        if self.max_upload_bytes <= 0:
            raise ValueError("Лимит загрузки должен быть больше нуля")
        if self.noscribe_timeout_seconds <= 0:
            raise ValueError("Таймаут noScribe должен быть больше нуля")
        if self.session_ttl_days <= 0:
            raise ValueError("Срок действия сессии должен быть больше нуля")
        if self.default_model not in self.allowed_models:
            raise ValueError("Модель по умолчанию отсутствует в списке разрешенных")
        if not self.default_language:
            raise ValueError("Язык по умолчанию не может быть пустым")
        if self.routerai_timeout_seconds <= 0:
            raise ValueError("Таймаут RouterAI должен быть больше нуля")
        if not 1 <= self.attribution_max_frames <= 5:
            raise ValueError("Количество кадров должно быть от 1 до 5")
        if self.model_catalog_refresh_hours <= 0:
            raise ValueError("Период обновления каталога должен быть больше нуля")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def resolved_ffmpeg_path(self) -> Path | None:
        if self.ffmpeg_path is not None:
            return self.ffmpeg_path
        command = shutil.which("ffmpeg")
        if command:
            return Path(command)
        if DEFAULT_NOSCRIBE_FFMPEG_PATH.is_file():
            return DEFAULT_NOSCRIBE_FFMPEG_PATH
        return None

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        models = tuple(
            value.strip()
            for value in os.getenv("TRANSCRIPTION_ALLOWED_MODELS", "fast,precise").split(",")
            if value.strip()
        )
        return cls(
            data_dir=Path(os.getenv("TRANSCRIPTION_DATA_DIR", "./data")).resolve(),
            noscribe_path=Path(
                os.getenv("TRANSCRIPTION_NOSCRIBE_PATH", str(DEFAULT_NOSCRIBE_PATH))
            ),
            default_language=os.getenv("TRANSCRIPTION_DEFAULT_LANGUAGE", "ru").strip(),
            default_model=os.getenv("TRANSCRIPTION_DEFAULT_MODEL", "precise").strip(),
            allowed_models=models,
            max_upload_bytes=int(
                os.getenv("TRANSCRIPTION_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024 * 1024))
            ),
            noscribe_timeout_seconds=int(
                os.getenv("TRANSCRIPTION_NOSCRIBE_TIMEOUT_SECONDS", str(6 * 60 * 60))
            ),
            session_ttl_days=int(os.getenv("TRANSCRIPTION_SESSION_TTL_DAYS", "30")),
            cookie_secure=os.getenv("TRANSCRIPTION_COOKIE_SECURE", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            ffmpeg_path=(
                Path(value).resolve()
                if (value := os.getenv("TRANSCRIPTION_FFMPEG_PATH", "").strip())
                else None
            ),
            routerai_base_url=os.getenv(
                "TRANSCRIPTION_ROUTERAI_BASE_URL", "https://routerai.ru/api/v1"
            ).rstrip("/"),
            routerai_timeout_seconds=int(
                os.getenv("TRANSCRIPTION_ROUTERAI_TIMEOUT_SECONDS", "60")
            ),
            attribution_max_frames=int(os.getenv("TRANSCRIPTION_ATTRIBUTION_MAX_FRAMES", "3")),
            model_catalog_refresh_hours=int(
                os.getenv("TRANSCRIPTION_MODEL_CATALOG_REFRESH_HOURS", "24")
            ),
        )
