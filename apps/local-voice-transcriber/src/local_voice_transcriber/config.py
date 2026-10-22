"""Конфигурация изолированного приложения-кандидата."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .voice.models import MatchingThresholds, QualityPolicy


class ConfigurationError(ValueError):
    """Некорректная или небезопасная конфигурация приложения."""


def _as_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} должен иметь логическое значение")


def _as_positive_int(value: str, *, name: str, minimum: int = 1) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} должен быть целым числом") from error
    if result < minimum:
        raise ConfigurationError(f"{name} должен быть не меньше {minimum}")
    return result


def _as_non_negative_float(value: str, *, name: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} должен быть числом") from error
    if result < 0:
        raise ConfigurationError(f"{name} не может быть отрицательным")
    return result


def _optional_non_negative_float(value: str | None, *, name: str) -> float | None:
    if value is None or not value.strip():
        return None
    return _as_non_negative_float(value, name=name)


def _default_voice_matching_thresholds() -> MatchingThresholds:
    """Без калибровки сопоставление всегда остаётся в fail-closed состоянии."""

    return MatchingThresholds(
        candidate_max_distance=None,
        auto_max_distance=None,
        min_margin=None,
        min_intervals=1,
        min_coverage=0.0,
    )


def _default_app_root() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Settings:
    """Параметры, не пересекающиеся с legacy-сервисом."""

    data_dir: Path
    database_path: Path
    host: str
    port: int
    instance_id: str
    cookie_secure: bool
    session_ttl_days: int
    auth_rate_limit: int
    zeusdb_dir: Path | None = None
    models_dir: Path | None = None
    asr_manifest_path: Path | None = None
    ffmpeg_path: Path | None = None
    voice_matching_thresholds: MatchingThresholds = field(
        default_factory=_default_voice_matching_thresholds
    )
    cookie_name: str = "local_voice_transcriber_session"
    csrf_cookie_name: str = "local_voice_transcriber_csrf"
    csrf_header_name: str = "X-CSRF-Token"

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        app_root: Path | None = None,
    ) -> Settings:
        values = os.environ if environ is None else environ
        root = (app_root or _default_app_root()).resolve()
        data_dir = Path(values.get("NEXT_DATA_DIR", root / "data")).expanduser()
        if not data_dir.is_absolute():
            data_dir = root / data_dir
        data_dir = data_dir.resolve()

        database_value = values.get("NEXT_DATABASE_PATH")
        database_path = data_dir / "transcription-next.sqlite3"
        if database_value:
            database_path = Path(database_value).expanduser()
            if not database_path.is_absolute():
                database_path = root / database_path
            database_path = database_path.resolve()

        try:
            database_path.relative_to(data_dir)
        except ValueError as error:
            raise ConfigurationError(
                "NEXT_DATABASE_PATH должен находиться внутри NEXT_DATA_DIR"
            ) from error

        zeusdb_dir = data_dir / "zeusdb"
        zeusdb_value = values.get("NEXT_ZEUSDB_DIR")
        if zeusdb_value:
            zeusdb_dir = Path(zeusdb_value).expanduser()
            if not zeusdb_dir.is_absolute():
                zeusdb_dir = root / zeusdb_dir
            zeusdb_dir = zeusdb_dir.resolve()
        try:
            zeusdb_dir.relative_to(data_dir)
        except ValueError as error:
            raise ConfigurationError(
                "NEXT_ZEUSDB_DIR должен находиться внутри NEXT_DATA_DIR"
            ) from error

        models_dir = data_dir / "models"
        models_value = values.get("NEXT_MODELS_DIR")
        if models_value:
            models_dir = Path(models_value).expanduser()
            if not models_dir.is_absolute():
                models_dir = root / models_dir
            models_dir = models_dir.resolve()
        try:
            models_dir.relative_to(data_dir)
        except ValueError as error:
            raise ConfigurationError(
                "NEXT_MODELS_DIR должен находиться внутри NEXT_DATA_DIR"
            ) from error

        asr_manifest_path: Path | None = None
        manifest_value = values.get("NEXT_ASR_MANIFEST", "").strip()
        if manifest_value:
            asr_manifest_path = Path(manifest_value).expanduser()
            if not asr_manifest_path.is_absolute():
                asr_manifest_path = models_dir / asr_manifest_path
            asr_manifest_path = asr_manifest_path.resolve()
            try:
                asr_manifest_path.relative_to(models_dir)
            except ValueError as error:
                raise ConfigurationError(
                    "NEXT_ASR_MANIFEST должен находиться внутри NEXT_MODELS_DIR"
                ) from error

        ffmpeg_path: Path | None = None
        ffmpeg_value = values.get("NEXT_FFMPEG_PATH", "").strip()
        if ffmpeg_value:
            ffmpeg_path = Path(ffmpeg_value).expanduser()
            if not ffmpeg_path.is_absolute():
                ffmpeg_path = root / ffmpeg_path
            ffmpeg_path = ffmpeg_path.resolve()

        host = values.get("NEXT_HOST", "127.0.0.1").strip()
        if not host:
            raise ConfigurationError("NEXT_HOST не может быть пустым")
        instance_id = values.get("NEXT_INSTANCE_ID", "transcription-next-candidate").strip()
        if not instance_id:
            raise ConfigurationError("NEXT_INSTANCE_ID не может быть пустым")
        try:
            voice_matching_thresholds = MatchingThresholds(
                candidate_max_distance=_optional_non_negative_float(
                    values.get("NEXT_VOICE_CANDIDATE_MAX_DISTANCE"),
                    name="NEXT_VOICE_CANDIDATE_MAX_DISTANCE",
                ),
                auto_max_distance=_optional_non_negative_float(
                    values.get("NEXT_VOICE_AUTO_MAX_DISTANCE"),
                    name="NEXT_VOICE_AUTO_MAX_DISTANCE",
                ),
                min_margin=_optional_non_negative_float(
                    values.get("NEXT_VOICE_MIN_MARGIN"),
                    name="NEXT_VOICE_MIN_MARGIN",
                ),
                min_intervals=_as_positive_int(
                    values.get("NEXT_VOICE_MIN_INTERVALS", "1"),
                    name="NEXT_VOICE_MIN_INTERVALS",
                ),
                min_coverage=_as_non_negative_float(
                    values.get("NEXT_VOICE_MIN_COVERAGE", "0"),
                    name="NEXT_VOICE_MIN_COVERAGE",
                ),
                quality_policy=QualityPolicy(
                    min_duration_ms=_as_positive_int(
                        values.get("NEXT_VOICE_MIN_DURATION_MS", "1000"),
                        name="NEXT_VOICE_MIN_DURATION_MS",
                        minimum=0,
                    ),
                    min_quality_score=_as_non_negative_float(
                        values.get("NEXT_VOICE_MIN_QUALITY", "0.01"),
                        name="NEXT_VOICE_MIN_QUALITY",
                    ),
                    min_speech_ratio=_as_non_negative_float(
                        values.get("NEXT_VOICE_MIN_SPEECH_RATIO", "0"),
                        name="NEXT_VOICE_MIN_SPEECH_RATIO",
                    ),
                ),
                calibrated=_as_bool(
                    values.get("NEXT_VOICE_CALIBRATED", "false"),
                    name="NEXT_VOICE_CALIBRATED",
                ),
                auto_assign_enabled=_as_bool(
                    values.get("NEXT_VOICE_AUTO_ASSIGN_ENABLED", "false"),
                    name="NEXT_VOICE_AUTO_ASSIGN_ENABLED",
                ),
            )
        except ValueError as error:
            raise ConfigurationError(f"Некорректны параметры voice matching: {error}") from error
        return cls(
            data_dir=data_dir,
            database_path=database_path,
            host=host,
            port=_as_positive_int(values.get("NEXT_PORT", "8010"), name="NEXT_PORT"),
            instance_id=instance_id,
            cookie_secure=_as_bool(
                values.get("NEXT_COOKIE_SECURE", "false"), name="NEXT_COOKIE_SECURE"
            ),
            session_ttl_days=_as_positive_int(
                values.get("NEXT_SESSION_TTL_DAYS", "14"), name="NEXT_SESSION_TTL_DAYS"
            ),
            auth_rate_limit=_as_positive_int(
                values.get("NEXT_AUTH_RATE_LIMIT", "10"), name="NEXT_AUTH_RATE_LIMIT"
            ),
            zeusdb_dir=zeusdb_dir,
            models_dir=models_dir,
            asr_manifest_path=asr_manifest_path,
            ffmpeg_path=ffmpeg_path,
            voice_matching_thresholds=voice_matching_thresholds,
        )
