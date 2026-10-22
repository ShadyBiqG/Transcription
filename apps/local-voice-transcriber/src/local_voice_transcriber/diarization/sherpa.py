"""Локальный адаптер офлайн-диаризации Sherpa-ONNX.

Модуль не загружает модели, не делает HTTP-запросов и не выполняет ресемплинг.
Передавать в него можно только подготовленный FFmpeg моно PCM WAV из private
jobs root. Веса должны быть заранее доставлены в models root и проверены по
контрольным суммам.
"""

from __future__ import annotations

import math
import wave
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Protocol

from .models import DiarizationInterval


class DiarizationPackVerificationError(RuntimeError):
    """Локальный voice pack отсутствует, повреждён или выходит за models root."""


class DiarizationRuntimeError(RuntimeError):
    """Sherpa-ONNX или входной WAV не соответствуют контракту адаптера."""


@dataclass(frozen=True, slots=True)
class LocalDiarizationPack:
    """Описание заранее доставленного локального пакета диаризации.

    Пути к ONNX-файлам задаются относительно directory только POSIX-именами из
    manifest. Оба файла обязаны быть включены в files с SHA-256.
    """

    pack_id: str
    directory: Path
    segmentation_model: str
    embedding_model: str
    files: Mapping[str, str]
    embedding_dimension: int | None = None


@dataclass(frozen=True, slots=True)
class DiarizationParameters:
    """Параметры кластеризации, передаваемые в публичный API Sherpa-ONNX."""

    num_speakers: int = -1
    clustering_threshold: float = 0.5
    window_shift_ratio: float = 0.1
    min_duration_on: float = 0.3
    min_duration_off: float = 0.5

    def __post_init__(self) -> None:
        if self.num_speakers != -1 and self.num_speakers < 1:
            raise ValueError("Количество говорящих должно быть -1 или положительным числом.")
        for name, value, minimum in (
            ("Порог кластеризации", self.clustering_threshold, 0.0),
            ("Доля сдвига окна", self.window_shift_ratio, 0.0),
            ("Минимальная длительность речи", self.min_duration_on, 0.0),
            ("Минимальная длительность паузы", self.min_duration_off, 0.0),
        ):
            if not math.isfinite(value) or value <= minimum:
                raise ValueError(f"{name} должна быть конечным числом больше {minimum}.")


class OfflineSpeakerDiarizer(Protocol):
    """Минимальная поверхность публичного diarizer для изолированных тестов."""

    @property
    def sample_rate(self) -> int: ...

    def process(self, samples: Sequence[float]) -> object: ...


DiarizerLoader = Callable[[LocalDiarizationPack, DiarizationParameters], OfflineSpeakerDiarizer]
WavReader = Callable[[Path], tuple[Sequence[float], int]]


class SherpaDiarizationAdapter:
    """Преобразует результат локального Sherpa в неизменяемые интервалы домена."""

    def __init__(
        self,
        pack: LocalDiarizationPack,
        *,
        models_root: Path,
        audio_root: Path,
        parameters: DiarizationParameters | None = None,
        loader: DiarizerLoader | None = None,
        wav_reader: WavReader | None = None,
    ) -> None:
        self._pack = pack
        self._models_root = models_root
        self._audio_root = audio_root
        self._parameters = parameters or DiarizationParameters()
        self._loader = loader or _load_sherpa_diarizer
        self._wav_reader = wav_reader or _read_pcm_mono_wav

    def verify_pack(self) -> None:
        """Проверяет, что все указанные веса локальны, неизменены и допустимы."""

        _ensure_local_path(self._models_root, "models root")
        if not self._pack.pack_id.strip():
            raise DiarizationPackVerificationError("Идентификатор voice pack обязателен.")
        directory = _resolve_inside(
            self._pack.directory,
            self._models_root,
            label="Каталог voice pack",
            must_exist=True,
        )
        if not directory.is_dir():
            raise DiarizationPackVerificationError("Каталог voice pack не существует.")
        if not self._pack.files:
            raise DiarizationPackVerificationError("Манифест voice pack не содержит файлов.")

        listed_paths = {
            relative_name: _verify_model_file(
                directory,
                relative_name,
                expected_hash,
            )
            for relative_name, expected_hash in self._pack.files.items()
        }
        for model_name, label in (
            (self._pack.segmentation_model, "segmentation model"),
            (self._pack.embedding_model, "embedding model"),
        ):
            normalized = _relative_model_path(model_name)
            if normalized not in listed_paths:
                raise DiarizationPackVerificationError(
                    f"В manifest отсутствует {label}."
                )

    def preflight(self) -> None:
        """Проверяет, что Sherpa-ONNX создаёт diarizer из локального voice pack."""

        self.verify_pack()
        try:
            self._loader(self._pack, self._parameters)
        except DiarizationPackVerificationError:
            raise
        except Exception as error:
            raise DiarizationRuntimeError("Локальный voice pack не удалось загрузить.") from error

    def diarize(self, wav_path: Path) -> tuple[DiarizationInterval, ...]:
        """Обрабатывает только mono PCM WAV из candidate jobs root."""

        self.verify_pack()
        normalized_wav_path = _resolve_inside(
            wav_path,
            self._audio_root,
            label="Входной WAV",
            must_exist=True,
        )
        if normalized_wav_path.suffix.lower() != ".wav":
            raise DiarizationRuntimeError("Для диаризации требуется WAV.")
        try:
            samples, sample_rate = self._wav_reader(normalized_wav_path)
            diarizer = self._loader(self._pack, self._parameters)
            expected_sample_rate = int(diarizer.sample_rate)
        except (DiarizationPackVerificationError, DiarizationRuntimeError):
            raise
        except Exception as error:
            raise DiarizationRuntimeError("Локальная диаризация не выполнена.") from error
        if sample_rate != expected_sample_rate:
            raise DiarizationRuntimeError(
                "Частота WAV не совпадает с требованиями voice pack; "
                "нужна предварительная нормализация FFmpeg."
            )
        try:
            result = diarizer.process(tuple(float(value) for value in samples))
            turns = _sorted_turns(result)
            return tuple(
                _to_interval(turn, index)
                for index, turn in enumerate(turns, start=1)
            )
        except DiarizationRuntimeError:
            raise
        except Exception as error:
            raise DiarizationRuntimeError("Sherpa-ONNX вернул некорректную диаризацию.") from error


def _load_sherpa_diarizer(
    pack: LocalDiarizationPack,
    parameters: DiarizationParameters,
) -> OfflineSpeakerDiarizer:
    """Создаёт diarizer только из локально проверенных путей публичного API."""

    try:
        import sherpa_onnx
    except ImportError as error:
        raise DiarizationPackVerificationError("Пакет sherpa-onnx не установлен.") from error

    directory = pack.directory.resolve(strict=True)
    segmentation_model = directory / _relative_model_path(pack.segmentation_model)
    embedding_model = directory / _relative_model_path(pack.embedding_model)
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(segmentation_model),
                window_shift_ratio=parameters.window_shift_ratio,
            ),
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(embedding_model)),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=parameters.num_speakers,
            threshold=parameters.clustering_threshold,
        ),
        min_duration_on=parameters.min_duration_on,
        min_duration_off=parameters.min_duration_off,
    )
    if not config.validate():
        raise DiarizationPackVerificationError(
            "Sherpa-ONNX отклонил конфигурацию локального voice pack."
        )
    return sherpa_onnx.OfflineSpeakerDiarization(config)


def _read_pcm_mono_wav(wav_path: Path) -> tuple[tuple[float, ...], int]:
    """Считывает PCM WAV без дополнительных пакетов и запрещает stereo."""

    try:
        with wave.open(str(wav_path), "rb") as reader:
            if reader.getnchannels() != 1:
                raise DiarizationRuntimeError("WAV для диаризации должен быть моно.")
            if reader.getcomptype() != "NONE":
                raise DiarizationRuntimeError("Поддерживается только несжатый PCM WAV.")
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            if sample_width not in {1, 2, 3, 4} or sample_rate <= 0:
                raise DiarizationRuntimeError("Параметры PCM WAV не поддерживаются.")
            raw_samples = reader.readframes(reader.getnframes())
    except DiarizationRuntimeError:
        raise
    except (OSError, wave.Error) as error:
        raise DiarizationRuntimeError("Не удалось прочитать PCM WAV.") from error
    return _decode_pcm_samples(raw_samples, sample_width), sample_rate


def _decode_pcm_samples(raw_samples: bytes, sample_width: int) -> tuple[float, ...]:
    if len(raw_samples) % sample_width != 0:
        raise DiarizationRuntimeError("PCM WAV содержит неполный sample.")
    if sample_width == 1:
        return tuple((value - 128) / 128 for value in raw_samples)
    if sample_width == 2:
        return tuple(
            int.from_bytes(raw_samples[index : index + 2], "little", signed=True) / 32768
            for index in range(0, len(raw_samples), 2)
        )
    if sample_width == 3:
        samples: list[float] = []
        for index in range(0, len(raw_samples), 3):
            value = int.from_bytes(raw_samples[index : index + 3] + b"\x00", "little")
            if value >= 1 << 23:
                value -= 1 << 24
            samples.append(value / (1 << 23))
        return tuple(samples)
    return tuple(
        int.from_bytes(raw_samples[index : index + 4], "little", signed=True) / (1 << 31)
        for index in range(0, len(raw_samples), 4)
    )


def _sorted_turns(result: object) -> tuple[object, ...]:
    sort_by_start_time = getattr(result, "sort_by_start_time", None)
    turns = tuple(sort_by_start_time() if callable(sort_by_start_time) else result)
    try:
        return tuple(
            sorted(
                turns,
                key=lambda turn: (
                    _seconds_to_ms(_required_turn_field(turn, "start")),
                    _seconds_to_ms(_required_turn_field(turn, "end")),
                    str(_required_turn_field(turn, "speaker")),
                ),
            )
        )
    except TypeError as error:
        raise DiarizationRuntimeError("Sherpa-ONNX вернул неитерируемый результат.") from error


def _to_interval(turn: object, index: int) -> DiarizationInterval:
    start_ms = _seconds_to_ms(_required_turn_field(turn, "start"))
    end_ms = _seconds_to_ms(_required_turn_field(turn, "end"))
    speaker_id = str(_required_turn_field(turn, "speaker")).strip()
    if not speaker_id:
        raise DiarizationRuntimeError("Sherpa-ONNX вернул пустую метку говорящего.")
    try:
        return DiarizationInterval(
            interval_id=f"diarization-{index:06d}",
            start_ms=start_ms,
            end_ms=end_ms,
            raw_speaker_label=speaker_id,
            speaker_group_id=f"local-speaker:{speaker_id}",
        )
    except ValueError as error:
        raise DiarizationRuntimeError(
            "Sherpa-ONNX вернул некорректные границы интервала."
        ) from error


def _required_turn_field(turn: object, field: str) -> object:
    if not hasattr(turn, field):
        raise DiarizationRuntimeError("Sherpa-ONNX вернул интервал неверного формата.")
    return getattr(turn, field)


def _seconds_to_ms(value: object) -> int:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as error:
        raise DiarizationRuntimeError("Sherpa-ONNX вернул некорректную временную метку.") from error
    if not math.isfinite(seconds):
        raise DiarizationRuntimeError("Sherpa-ONNX вернул неконечную временную метку.")
    return round(seconds * 1000)


def _ensure_local_path(path: Path, label: str) -> None:
    if _is_unc_path(path):
        raise DiarizationPackVerificationError(f"{label} не может быть сетевым UNC-путём.")


def _resolve_inside(path: Path, root: Path, *, label: str, must_exist: bool) -> Path:
    _ensure_local_path(path, label)
    _ensure_local_path(root, "Корневой каталог")
    try:
        if path.is_symlink():
            raise ValueError("symbolic link")
        root_path = root.resolve(strict=True)
        resolved = path.resolve(strict=must_exist)
        resolved.relative_to(root_path)
    except (FileNotFoundError, OSError, ValueError) as error:
        raise DiarizationPackVerificationError(
            f"{label} находится вне разрешённого локального root."
        ) from error
    return resolved


def _verify_model_file(directory: Path, relative_name: str, expected_hash: str) -> Path:
    relative_path = _relative_model_path(relative_name)
    if not _is_sha256(expected_hash):
        raise DiarizationPackVerificationError("Контрольная сумма model pack должна быть SHA-256.")
    candidate = directory / relative_path
    try:
        if candidate.is_symlink():
            raise ValueError("symbolic link")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(directory)
    except (FileNotFoundError, OSError, ValueError) as error:
        raise DiarizationPackVerificationError(
            "Манифест содержит выход за каталог voice pack."
        ) from error
    if not resolved.is_file():
        raise DiarizationPackVerificationError("Файл voice pack отсутствует.")
    if _file_sha256(resolved).lower() != expected_hash.lower():
        raise DiarizationPackVerificationError("Контрольная сумма voice pack не совпала.")
    return resolved


def _relative_model_path(value: str) -> str:
    if not value or "\\" in value:
        raise DiarizationPackVerificationError("Путь модели должен быть непустым POSIX-путём.")
    relative_path = PurePosixPath(value)
    if relative_path.is_absolute() or ".." in relative_path.parts or "." in relative_path.parts:
        raise DiarizationPackVerificationError("Путь модели выходит за каталог voice pack.")
    return relative_path.as_posix()


def _is_sha256(value: str) -> bool:
    if len(value) != 64:
        return False
    return all(character in "0123456789abcdefABCDEF" for character in value)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_unc_path(path: Path) -> bool:
    return path.drive.startswith("\\\\") or str(path).startswith("\\\\")
