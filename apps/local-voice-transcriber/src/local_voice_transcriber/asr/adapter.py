"""Адаптер onnx-asr, допускающий только заранее доставленные локальные пакеты."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from .models import AsrRecognition, AsrSegment
from .timing import RelativeToken, TimingProjectionError, TokenWordProjector


class PackVerificationError(RuntimeError):
    """Локальный model pack неполон, повреждён или лежит вне разрешенного root."""


class AsrRuntimeError(RuntimeError):
    """onnx-asr не удалось загрузить или он выдал неподдерживаемый результат."""


@dataclass(frozen=True, slots=True)
class LocalModelPack:
    """Манифест ASR/VAD-пака, уже доставленного оператором на сервер."""

    pack_id: str
    model_name: str
    directory: Path
    files: Mapping[str, str]


class Recognizer(Protocol):
    """Минимальная поверхность onnx-asr для изолированного тестирования."""

    def recognize(self, wav_path: str) -> Iterable[object]: ...


RecognizerLoader = Callable[[LocalModelPack, LocalModelPack | None], Recognizer]


class OfflineAsrAdapter:
    """Распознаёт WAV локальным onnx-asr и сохраняет только нормализованный результат."""

    def __init__(
        self,
        pack: LocalModelPack,
        *,
        models_root: Path,
        language: str,
        vad_pack: LocalModelPack | None = None,
        loader: RecognizerLoader | None = None,
        projector: TokenWordProjector | None = None,
    ) -> None:
        self._pack = pack
        self._vad_pack = vad_pack
        self._models_root = models_root
        self._language = language
        self._loader = loader or _load_onnx_asr_locally
        self._projector = projector or TokenWordProjector()

    def verify_packs(self) -> None:
        self._verify_pack(self._pack)
        if self._vad_pack is not None:
            self._verify_pack(self._vad_pack)

    def transcribe(self, wav_path: Path) -> AsrRecognition:
        self.verify_packs()
        if wav_path.suffix.lower() != ".wav" or not wav_path.is_file():
            raise AsrRuntimeError("Для ASR требуется существующий локальный WAV")
        try:
            results = tuple(self._loader(self._pack, self._vad_pack).recognize(str(wav_path)))
        except PackVerificationError:
            raise
        except Exception as error:  # Runtime exception не должен уносить worker.
            raise AsrRuntimeError("Локальное ASR-распознавание завершилось ошибкой") from error
        return AsrRecognition(
            language=self._language,
            segments=tuple(self._convert_segment(item) for item in results),
        )

    def _verify_pack(self, pack: LocalModelPack) -> None:
        try:
            root = self._models_root.resolve(strict=True)
            directory = pack.directory.resolve(strict=True)
            directory.relative_to(root)
        except (FileNotFoundError, ValueError) as error:
            message = "Пак модели находится вне разрешенного model root"
            raise PackVerificationError(message) from error
        if not pack.files:
            raise PackVerificationError("Манифест model pack не содержит файлов")
        for relative_name, expected_hash in pack.files.items():
            file_path = (directory / relative_name).resolve(strict=True)
            try:
                file_path.relative_to(directory)
            except ValueError as error:
                raise PackVerificationError("Манифест содержит выход за каталог модели") from error
            actual_hash = _file_sha256(file_path)
            if actual_hash.lower() != expected_hash.lower():
                raise PackVerificationError("Контрольная сумма model pack не совпала")

    def _convert_segment(self, raw: object) -> AsrSegment:
        start_ms = _seconds_to_ms(_required(raw, "start"))
        end_ms = _seconds_to_ms(_required(raw, "end"))
        text = str(_required(raw, "text")).strip()
        token_values = _optional_sequence(raw, "tokens")
        timestamps = _optional_sequence(raw, "timestamps")
        logprobs = _optional_sequence(raw, "logprobs")
        if len(token_values) != len(timestamps) or not token_values:
            return AsrSegment(
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                confidence=None,
                tokens=(),
                words=(),
                word_timing_status="unavailable",
            )
        if logprobs and len(logprobs) != len(token_values):
            return AsrSegment(
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                confidence=None,
                tokens=(),
                words=(),
                word_timing_status="unavailable",
            )
        relative = tuple(
            RelativeToken(
                text=str(token),
                timestamp_seconds=float(timestamps[index]),
                logprob=float(logprobs[index]) if logprobs else None,
            )
            for index, token in enumerate(token_values)
        )
        try:
            tokens = self._projector.absolute_tokens(
                relative, segment_start_ms=start_ms, segment_end_ms=end_ms
            )
            words = self._projector.words(tokens)
        except (TimingProjectionError, ValueError):
            return AsrSegment(
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                confidence=None,
                tokens=(),
                words=(),
                word_timing_status="unavailable",
            )
        return AsrSegment(
            start_ms=start_ms,
            end_ms=end_ms,
            text=text,
            confidence=_mean(logprobs),
            tokens=tokens,
            words=words,
            word_timing_status="available",
        )


def _load_onnx_asr_locally(pack: LocalModelPack, vad_pack: LocalModelPack | None) -> Recognizer:
    """Импортируется только при реальном запуске; нормальные тесты его не вызывают."""

    try:
        from onnx_asr.loader import Manager
    except ImportError as error:
        raise PackVerificationError("Пакет onnx-asr не установлен") from error
    manager = Manager(providers=["CPUExecutionProvider"])
    model = manager.create_asr(
        pack.model_name,
        local_dir=pack.directory,
        offline=True,
    )
    if vad_pack is not None:
        vad = manager.create_vad(
            vad_pack.model_name,
            local_dir=vad_pack.directory,
            offline=True,
        )
        model = model.with_vad(vad)
    return model.with_timestamps()


def _seconds_to_ms(value: object) -> int:
    try:
        return round(float(value) * 1000)
    except (TypeError, ValueError) as error:
        raise AsrRuntimeError("onnx-asr вернул некорректную временную метку") from error


def _required(item: object, field: str) -> object:
    if not hasattr(item, field):
        raise AsrRuntimeError("onnx-asr вернул результат неверного формата")
    return getattr(item, field)


def _optional_sequence(item: object, field: str) -> tuple[Any, ...]:
    value = getattr(item, field, None)
    if value is None:
        return ()
    try:
        return tuple(value)
    except TypeError as error:
        raise AsrRuntimeError("onnx-asr вернул коллекцию неверного формата") from error


def _mean(values: tuple[Any, ...]) -> float | None:
    if not values:
        return None
    numeric = tuple(float(value) for value in values)
    return sum(numeric) / len(numeric)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
