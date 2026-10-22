"""Защищённое локальное выделение WAV-образцов для voice-directory.

Образец создаётся только после явного подтверждения в UI. Этот модуль ничего
не знает о профилях, SQLite или сети: он берёт интервал из уже нормализованного
private job WAV и создаёт отдельный неизменяемый файл в private samples root.

Абсолютный путь артефакта предназначен исключительно для внутреннего worker-а.
В базу следует сохранять только protected_path, контрольную сумму и метаданные
качества.
"""

from __future__ import annotations

import os
import tempfile
import wave
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from math import sqrt
from pathlib import Path, PurePath
from types import MappingProxyType
from typing import Final

_EXPECTED_SAMPLE_RATE_HZ: Final = 16_000
_EXPECTED_SAMPLE_WIDTH_BYTES: Final = 2
_SILENCE_THRESHOLD: Final = 1


class VoiceSampleArtifactError(RuntimeError):
    """Образец голоса нельзя безопасно создать из private WAV."""


class VoiceSampleArtifactSecurityError(VoiceSampleArtifactError):
    """Путь образца, root или имя артефакта не соответствует private-контракту."""


class VoiceSampleArtifactFormatError(VoiceSampleArtifactError):
    """Входной WAV не является ожидаемым нормализованным mono PCM."""


@dataclass(frozen=True, slots=True)
class VoiceSampleQuality:
    """Локальные измерения качества, пригодные для audit и quality gate.

    rms и peak нормализованы в диапазоне от 0 до 1. non_silent_ratio отражает
    долю PCM-sample с ненулевой амплитудой; это технический сигнал, а не
    биометрический вывод о человеке.
    """

    sample_rate_hz: int
    sample_width_bytes: int
    frame_count: int
    rms: float
    peak: float
    non_silent_ratio: float

    @property
    def duration_ms(self) -> int:
        """Длительность сохранённого фрагмента, округлённая до миллисекунды."""

        return round(self.frame_count * 1000 / self.sample_rate_hz)

    def as_metadata(self) -> Mapping[str, object]:
        """Возвращает JSON-совместимые метаданные без абсолютных путей."""

        return MappingProxyType(
            {
                "duration_ms": self.duration_ms,
                "sample_rate_hz": self.sample_rate_hz,
                "sample_width_bytes": self.sample_width_bytes,
                "frame_count": self.frame_count,
                "rms": self.rms,
                "peak": self.peak,
                "non_silent_ratio": self.non_silent_ratio,
            }
        )


@dataclass(frozen=True, slots=True)
class ProtectedVoiceSampleArtifact:
    """Результат атомарного создания отдельного защищённого WAV-образца."""

    artifact_id: str
    artifact_path: Path
    protected_path: str
    content_sha256: str
    duration_ms: int
    quality: VoiceSampleQuality

    @property
    def quality_metadata(self) -> Mapping[str, object]:
        """Метаданные, которые можно передать в voice repository."""

        return self.quality.as_metadata()


class VoiceSampleArtifactWriter:
    """Выделяет WAV-образец из private jobs root в отдельный private samples root.

    Поддерживается ровно результат FfmpegNormalizer: WAV, PCM, mono, 16 kHz и
    signed 16-bit little-endian samples. Жёсткий контракт исключает скрытый
    ресемплинг, обработку сетевого пути и различия между embedding worker-ами.
    """

    def __init__(
        self,
        *,
        jobs_root: Path,
        samples_root: Path,
        expected_sample_rate_hz: int = _EXPECTED_SAMPLE_RATE_HZ,
    ) -> None:
        if expected_sample_rate_hz <= 0:
            raise ValueError("Ожидаемая частота дискретизации должна быть положительной.")
        self._jobs_root = _require_private_root(jobs_root, label="Корневой каталог job")
        self._samples_root = _require_private_root(samples_root, label="Корневой каталог образцов")
        self._expected_sample_rate_hz = expected_sample_rate_hz

    def create_from_interval(
        self,
        source_wav: Path,
        *,
        start_ms: int,
        end_ms: int,
        artifact_id: str,
    ) -> ProtectedVoiceSampleArtifact:
        """Копирует точный положительный временной интервал в новый private WAV.

        Границы не обрезаются молча. Если diarization вернул границу вне WAV,
        вызывающий слой обязан явно обработать несогласованность run-а, вместо
        создания образца с неясным составом аудио.
        """

        _validate_interval(start_ms, end_ms)
        normalized_artifact_id = _validate_artifact_id(artifact_id)
        source = _require_private_file(
            source_wav,
            root=self._jobs_root,
            label="Исходный WAV",
        )
        if source.suffix.lower() != ".wav":
            raise VoiceSampleArtifactFormatError("Исходный файл должен иметь расширение .wav.")

        destination = self._samples_root / f"{normalized_artifact_id}.wav"
        _ensure_new_destination(destination, root=self._samples_root)

        try:
            with wave.open(str(source), "rb") as reader:
                _validate_normalized_wav(
                    reader,
                    expected_sample_rate_hz=self._expected_sample_rate_hz,
                )
                total_frames = reader.getnframes()
                start_frame, end_frame = _interval_frames(
                    start_ms=start_ms,
                    end_ms=end_ms,
                    sample_rate_hz=reader.getframerate(),
                    total_frames=total_frames,
                )
                reader.setpos(start_frame)
                pcm = reader.readframes(end_frame - start_frame)
        except VoiceSampleArtifactError:
            raise
        except (OSError, wave.Error) as error:
            raise VoiceSampleArtifactFormatError(
                "Не удалось прочитать исходный PCM WAV."
            ) from error

        expected_bytes = (end_frame - start_frame) * _EXPECTED_SAMPLE_WIDTH_BYTES
        if len(pcm) != expected_bytes:
            raise VoiceSampleArtifactFormatError("Исходный PCM WAV содержит неполный фрагмент.")

        quality = _measure_pcm_s16le(
            pcm,
            sample_rate_hz=self._expected_sample_rate_hz,
        )
        _write_new_wav_atomically(
            destination,
            root=self._samples_root,
            sample_rate_hz=self._expected_sample_rate_hz,
            pcm=pcm,
        )
        return ProtectedVoiceSampleArtifact(
            artifact_id=normalized_artifact_id,
            artifact_path=destination,
            protected_path=destination.name,
            content_sha256=_file_sha256(destination),
            duration_ms=quality.duration_ms,
            quality=quality,
        )


def _validate_interval(start_ms: int, end_ms: int) -> None:
    if isinstance(start_ms, bool) or isinstance(end_ms, bool):
        raise ValueError("Границы интервала должны быть целыми миллисекундами.")
    if not isinstance(start_ms, int) or not isinstance(end_ms, int):
        raise ValueError("Границы интервала должны быть целыми миллисекундами.")
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("Нужен положительный интервал с началом не меньше нуля.")


def _validate_artifact_id(artifact_id: str) -> str:
    if not isinstance(artifact_id, str):
        raise VoiceSampleArtifactSecurityError("Идентификатор образца должен быть строкой.")
    value = artifact_id.strip()
    if not value or len(value) > 128:
        raise VoiceSampleArtifactSecurityError("Идентификатор образца пустой или слишком длинный.")
    if not value[0].isalnum() or any(not (char.isalnum() or char in "-_") for char in value):
        raise VoiceSampleArtifactSecurityError(
            "Идентификатор образца может содержать только буквы, цифры, '-' и '_'."
        )
    return value


def _require_private_root(root: Path, *, label: str) -> Path:
    path = _absolute_path(root)
    _reject_unc_path(path, label)
    if not path.exists() or not path.is_dir():
        raise VoiceSampleArtifactSecurityError(
            f"{label} должен существовать как локальный каталог."
        )
    if _has_symlink_in_path(path):
        raise VoiceSampleArtifactSecurityError(f"{label} не может содержать symbolic link.")
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise VoiceSampleArtifactSecurityError(f"{label} недоступен.") from error


def _require_private_file(path: Path, *, root: Path, label: str) -> Path:
    candidate = _absolute_path(path)
    _reject_unc_path(candidate, label)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise VoiceSampleArtifactSecurityError(
            f"{label} находится вне разрешённого private jobs root."
        ) from error
    if _has_symlink_in_path(candidate, stop_at=root):
        raise VoiceSampleArtifactSecurityError(f"{label} не может быть symbolic link.")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, OSError, ValueError) as error:
        raise VoiceSampleArtifactSecurityError(
            f"{label} находится вне разрешённого private jobs root."
        ) from error
    if not resolved.is_file():
        raise VoiceSampleArtifactSecurityError(f"{label} должен быть обычным файлом.")
    return resolved


def _ensure_new_destination(destination: Path, *, root: Path) -> None:
    candidate = _absolute_path(destination)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise VoiceSampleArtifactSecurityError(
            "Путь нового образца находится вне private samples root."
        ) from error
    if candidate.parent != root or candidate.suffix.lower() != ".wav":
        raise VoiceSampleArtifactSecurityError(
            "Образец должен быть WAV-файлом непосредственно в samples root."
        )
    if _has_symlink_in_path(candidate.parent, stop_at=root):
        raise VoiceSampleArtifactSecurityError("Каталог образцов не может содержать symbolic link.")
    if candidate.exists() or candidate.is_symlink():
        raise VoiceSampleArtifactSecurityError(
            "Защищённый образец с таким идентификатором уже существует."
        )


def _validate_normalized_wav(
    reader: wave.Wave_read,
    *,
    expected_sample_rate_hz: int,
) -> None:
    if reader.getnchannels() != 1:
        raise VoiceSampleArtifactFormatError("Исходный WAV должен быть моно.")
    if reader.getcomptype() != "NONE":
        raise VoiceSampleArtifactFormatError("Поддерживается только несжатый PCM WAV.")
    if reader.getsampwidth() != _EXPECTED_SAMPLE_WIDTH_BYTES:
        raise VoiceSampleArtifactFormatError("Исходный WAV должен быть 16-bit PCM.")
    if reader.getframerate() != expected_sample_rate_hz:
        raise VoiceSampleArtifactFormatError(
            f"Исходный WAV должен иметь частоту {expected_sample_rate_hz} Гц."
        )
    if reader.getnframes() <= 0:
        raise VoiceSampleArtifactFormatError("Исходный WAV не содержит PCM-кадров.")


def _interval_frames(
    *,
    start_ms: int,
    end_ms: int,
    sample_rate_hz: int,
    total_frames: int,
) -> tuple[int, int]:
    if end_ms * sample_rate_hz > total_frames * 1000:
        raise VoiceSampleArtifactFormatError(
            "Интервал образца выходит за длительность исходного WAV."
        )
    start_frame = (start_ms * sample_rate_hz + 999) // 1000
    end_frame = (end_ms * sample_rate_hz) // 1000
    if start_frame >= end_frame:
        raise VoiceSampleArtifactFormatError(
            "Интервал образца не содержит ни одного PCM-кадра после привязки к WAV."
        )
    return start_frame, end_frame


def _measure_pcm_s16le(pcm: bytes, *, sample_rate_hz: int) -> VoiceSampleQuality:
    if len(pcm) % _EXPECTED_SAMPLE_WIDTH_BYTES != 0:
        raise VoiceSampleArtifactFormatError("PCM-фрагмент содержит неполный sample.")
    frame_count = len(pcm) // _EXPECTED_SAMPLE_WIDTH_BYTES
    if frame_count <= 0:
        raise VoiceSampleArtifactFormatError("PCM-фрагмент пустой.")
    sum_squares = 0
    peak = 0
    non_silent_count = 0
    for offset in range(0, len(pcm), _EXPECTED_SAMPLE_WIDTH_BYTES):
        sample = int.from_bytes(pcm[offset : offset + 2], "little", signed=True)
        magnitude = abs(sample)
        sum_squares += sample * sample
        peak = max(peak, magnitude)
        if magnitude > _SILENCE_THRESHOLD:
            non_silent_count += 1
    return VoiceSampleQuality(
        sample_rate_hz=sample_rate_hz,
        sample_width_bytes=_EXPECTED_SAMPLE_WIDTH_BYTES,
        frame_count=frame_count,
        rms=sqrt(sum_squares / frame_count) / 32768,
        peak=min(peak / 32768, 1.0),
        non_silent_ratio=non_silent_count / frame_count,
    )


def _write_new_wav_atomically(
    destination: Path,
    *,
    root: Path,
    sample_rate_hz: int,
    pcm: bytes,
) -> None:
    _ensure_new_destination(destination, root=root)
    file_descriptor: int | None = None
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.stem}.",
            suffix=".tmp",
            dir=root,
        )
        temporary_path = Path(temporary_name)
        os.chmod(temporary_path, 0o600)
        with os.fdopen(file_descriptor, "wb") as raw_file:
            file_descriptor = None
            with wave.open(raw_file, "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(_EXPECTED_SAMPLE_WIDTH_BYTES)
                writer.setframerate(sample_rate_hz)
                writer.setcomptype("NONE", "not compressed")
                writer.writeframes(pcm)
            raw_file.flush()
            os.fsync(raw_file.fileno())
        _ensure_new_destination(destination, root=root)
        try:
            os.link(temporary_path, destination)
        except FileExistsError as error:
            raise VoiceSampleArtifactSecurityError(
                "Защищённый образец с таким идентификатором уже существует."
            ) from error
        except OSError as error:
            raise VoiceSampleArtifactError(
                "Не удалось атомарно опубликовать защищённый WAV-образец."
            ) from error
    except VoiceSampleArtifactError:
        raise
    except (OSError, wave.Error) as error:
        raise VoiceSampleArtifactError("Не удалось записать защищённый WAV-образец.") from error
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _file_sha256(path: Path) -> str:
    digest = sha256()
    try:
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise VoiceSampleArtifactError("Не удалось вычислить SHA-256 образца.") from error
    return digest.hexdigest()


def _absolute_path(path: Path) -> Path:
    try:
        return Path(os.path.abspath(path))
    except (TypeError, OSError) as error:
        raise VoiceSampleArtifactSecurityError("Путь образца некорректен.") from error


def _has_symlink_in_path(path: Path, *, stop_at: Path | None = None) -> bool:
    """Проверяет каждый существующий элемент пути, не следуя его ссылкам."""

    normalized_path = _absolute_path(path)
    normalized_stop = _absolute_path(stop_at) if stop_at is not None else None
    current = normalized_path
    while True:
        if current.is_symlink():
            return True
        if normalized_stop is not None and current == normalized_stop:
            return False
        if current.parent == current:
            return False
        current = current.parent


def _reject_unc_path(path: Path, label: str) -> None:
    text = str(PurePath(path))
    if path.drive.startswith("\\\\") or text.startswith("\\\\"):
        raise VoiceSampleArtifactSecurityError(f"{label} не может быть сетевым UNC-путём.")
