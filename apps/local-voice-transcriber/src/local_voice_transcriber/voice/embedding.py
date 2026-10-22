"""Извлечение локальных голосовых эмбеддингов через Sherpa-ONNX.

Адаптер принимает только подготовленный mono PCM WAV из приватного jobs root.
Модель не загружается из сети: до запуска проверяется весь
LocalDiarizationPack, включая SHA-256 каждого файла. Вектор остаётся
в памяти worker-а и возвращается как неизменяемый tuple; этот модуль не
формирует HTTP-проекции и не сохраняет байты эмбеддингов в SQLite.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from math import ceil, isfinite
from pathlib import Path
from typing import Protocol

from ..diarization.sherpa import (
    DiarizationPackVerificationError,
    DiarizationRuntimeError,
    LocalDiarizationPack,
    SherpaDiarizationAdapter,
    _read_pcm_mono_wav,
    _relative_model_path,
    _resolve_inside,
)


class VoiceEmbeddingPackVerificationError(RuntimeError):
    """Локальный voice pack не прошёл проверку перед извлечением эмбеддинга."""


class VoiceEmbeddingRuntimeError(RuntimeError):
    """Входной WAV, интервал или результат Sherpa не соответствует контракту."""


class SpeakerEmbeddingStream(Protocol):
    """Минимальная поверхность локального stream в публичном API Sherpa-ONNX."""

    def accept_waveform(self, sample_rate: float, waveform: Sequence[float]) -> None: ...

    def input_finished(self) -> None: ...


class SpeakerEmbeddingExtractor(Protocol):
    """Минимальная поверхность SpeakerEmbeddingExtractor для тестовой подмены."""

    @property
    def dim(self) -> int: ...

    def create_stream(self) -> SpeakerEmbeddingStream: ...

    def is_ready(self, stream: SpeakerEmbeddingStream) -> bool: ...

    def compute(self, stream: SpeakerEmbeddingStream) -> Sequence[float]: ...


EmbeddingExtractorLoader = Callable[[LocalDiarizationPack], SpeakerEmbeddingExtractor]
WavReader = Callable[[Path], tuple[Sequence[float], int]]


class SherpaVoiceEmbeddingAdapter:
    """Извлекает один embedding из заданной части private mono PCM WAV.

    Sherpa публично поддерживает ресемплинг в accept_waveform, поэтому WAV
    не принудительно привязывается к sample rate модели. Это не отменяет
    обязательной предварительной нормализации медиа до mono PCM WAV.
    """

    def __init__(
        self,
        pack: LocalDiarizationPack,
        *,
        models_root: Path,
        audio_root: Path,
        loader: EmbeddingExtractorLoader | None = None,
        wav_reader: WavReader | None = None,
    ) -> None:
        self._pack = pack
        self._models_root = models_root
        self._audio_root = audio_root
        self._loader = loader or _load_sherpa_embedding_extractor
        self._wav_reader = wav_reader or _read_pcm_mono_wav

    def verify_pack(self) -> None:
        """Проверяет local-only models root, pack paths и SHA-256 весов.

        В pack диаризации embedding относится к тому же набору версий, что и
        segmentation model. Поэтому перед созданием embedding extractor
        проверяется весь pack, а не только один ONNX-файл.
        """

        try:
            SherpaDiarizationAdapter(
                self._pack,
                models_root=self._models_root,
                audio_root=self._audio_root,
            ).verify_pack()
        except DiarizationPackVerificationError as error:
            raise VoiceEmbeddingPackVerificationError(str(error)) from error

    def extract(
        self,
        wav_path: Path,
        *,
        start_ms: int,
        end_ms: int,
    ) -> tuple[float, ...]:
        """Возвращает неизменяемый конечный embedding для полуинтервала [start, end).

        Границы измеряются от начала WAV. Вызов не принимает сырые байты и не
        делает записи на диск, чтобы protected audio не покидало worker.
        """

        _validate_interval(start_ms, end_ms)
        self.verify_pack()
        normalized_wav_path = _resolve_private_wav_path(wav_path, self._audio_root)
        try:
            raw_samples, sample_rate = self._wav_reader(normalized_wav_path)
            samples = _validated_samples(raw_samples)
            start_index, end_index = _interval_sample_bounds(
                start_ms=start_ms,
                end_ms=end_ms,
                sample_rate=sample_rate,
                frame_count=len(samples),
            )
        except VoiceEmbeddingRuntimeError:
            raise
        except DiarizationRuntimeError as error:
            raise VoiceEmbeddingRuntimeError(str(error)) from error
        except Exception as error:
            raise VoiceEmbeddingRuntimeError(
                "Не удалось прочитать mono PCM WAV для голосового эмбеддинга."
            ) from error

        interval_samples = samples[start_index:end_index]
        try:
            extractor = self._loader(self._pack)
            stream = extractor.create_stream()
            stream.accept_waveform(float(sample_rate), interval_samples)
            stream.input_finished()
            if not extractor.is_ready(stream):
                raise VoiceEmbeddingRuntimeError(
                    "Интервал речи слишком короткий для извлечения голосового эмбеддинга."
                )
            vector = extractor.compute(stream)
            return _validated_vector(vector, expected_dimension=extractor.dim)
        except VoiceEmbeddingRuntimeError:
            raise
        except Exception as error:
            raise VoiceEmbeddingRuntimeError(
                "Sherpa-ONNX не смог извлечь голосовой эмбеддинг локально."
            ) from error

    def embedding_dimension(self) -> int:
        """Возвращает проверенную размерность локального voice pack без вектора."""

        self.verify_pack()
        try:
            dimension = int(self._loader(self._pack).dim)
        except VoiceEmbeddingPackVerificationError:
            raise
        except Exception as error:
            raise VoiceEmbeddingRuntimeError(
                "Sherpa-ONNX не смог определить размерность локального voice pack."
            ) from error
        if dimension < 1:
            raise VoiceEmbeddingRuntimeError(
                "Sherpa-ONNX вернул недопустимую размерность voice pack."
            )
        return dimension


def _load_sherpa_embedding_extractor(
    pack: LocalDiarizationPack,
) -> SpeakerEmbeddingExtractor:
    """Создаёт public Sherpa extractor только с абсолютным локальным путём."""

    try:
        import sherpa_onnx
    except ImportError as error:
        raise VoiceEmbeddingPackVerificationError("Пакет sherpa-onnx не установлен.") from error

    try:
        directory = pack.directory.resolve(strict=True)
        embedding_model = directory / _relative_model_path(pack.embedding_model)
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(embedding_model),
            provider="cpu",
        )
        if not config.validate():
            raise VoiceEmbeddingPackVerificationError(
                "Sherpa-ONNX отклонил конфигурацию локального voice pack."
            )
        return sherpa_onnx.SpeakerEmbeddingExtractor(config)
    except VoiceEmbeddingPackVerificationError:
        raise
    except Exception as error:
        raise VoiceEmbeddingPackVerificationError(
            "Не удалось создать extractor для локального voice pack."
        ) from error


def _resolve_private_wav_path(wav_path: Path, audio_root: Path) -> Path:
    try:
        resolved = _resolve_inside(
            wav_path,
            audio_root,
            label="Входной WAV",
            must_exist=True,
        )
    except DiarizationPackVerificationError as error:
        raise VoiceEmbeddingRuntimeError(str(error)) from error
    if resolved.suffix.lower() != ".wav":
        raise VoiceEmbeddingRuntimeError("Для голосового эмбеддинга требуется WAV.")
    return resolved


def _validate_interval(start_ms: int, end_ms: int) -> None:
    if (
        isinstance(start_ms, bool)
        or isinstance(end_ms, bool)
        or not isinstance(start_ms, int)
        or not isinstance(end_ms, int)
    ):
        raise VoiceEmbeddingRuntimeError("Границы голосового интервала должны быть целыми ms.")
    if start_ms < 0:
        raise VoiceEmbeddingRuntimeError("Начало голосового интервала не может быть отрицательным.")
    if end_ms <= start_ms:
        raise VoiceEmbeddingRuntimeError("Конец голосового интервала должен быть позже начала.")


def _validated_samples(values: Sequence[float]) -> tuple[float, ...]:
    samples = tuple(float(value) for value in values)
    if not samples:
        raise VoiceEmbeddingRuntimeError("WAV не содержит аудиофреймов.")
    if any(not isfinite(value) or value < -1.0 or value > 1.0 for value in samples):
        raise VoiceEmbeddingRuntimeError(
            "PCM WAV содержит sample вне допустимого диапазона от -1 до 1."
        )
    return samples


def _interval_sample_bounds(
    *,
    start_ms: int,
    end_ms: int,
    sample_rate: int,
    frame_count: int,
) -> tuple[int, int]:
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0:
        raise VoiceEmbeddingRuntimeError("WAV содержит недопустимую частоту дискретизации.")
    start_index = ceil(start_ms * sample_rate / 1000)
    end_index = ceil(end_ms * sample_rate / 1000)
    if start_index >= frame_count or end_index > frame_count:
        raise VoiceEmbeddingRuntimeError(
            "Границы голосового интервала выходят за длительность WAV."
        )
    if end_index <= start_index:
        raise VoiceEmbeddingRuntimeError(
            "Голосовой интервал не содержит ни одного PCM sample."
        )
    return start_index, end_index


def _validated_vector(values: Sequence[float], *, expected_dimension: int) -> tuple[float, ...]:
    if (
        isinstance(expected_dimension, bool)
        or not isinstance(expected_dimension, int)
        or expected_dimension < 1
    ):
        raise VoiceEmbeddingRuntimeError("Sherpa-ONNX вернул недопустимую размерность эмбеддинга.")
    vector = tuple(float(value) for value in values)
    if len(vector) != expected_dimension:
        raise VoiceEmbeddingRuntimeError(
            "Размерность голосового эмбеддинга не совпадает с voice pack."
        )
    if any(not isfinite(value) for value in vector):
        raise VoiceEmbeddingRuntimeError(
            "Sherpa-ONNX вернул голосовой эмбеддинг с неконечными значениями."
        )
    if not any(value != 0 for value in vector):
        raise VoiceEmbeddingRuntimeError("Sherpa-ONNX вернул нулевой голосовой эмбеддинг.")
    return vector
