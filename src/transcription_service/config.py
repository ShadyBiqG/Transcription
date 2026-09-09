from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_NOSCRIBE_PATH = Path(r"C:\Program Files (x86)\noScribe\noScribe.exe")


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

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "tmp"

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
        )
