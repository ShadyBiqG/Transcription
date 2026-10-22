from __future__ import annotations

import sys
import wave
from hashlib import sha256
from pathlib import Path
from types import ModuleType

import pytest

from local_voice_transcriber.diarization import LocalDiarizationPack
from local_voice_transcriber.voice.embedding import (
    SherpaVoiceEmbeddingAdapter,
    VoiceEmbeddingPackVerificationError,
    VoiceEmbeddingRuntimeError,
    _load_sherpa_embedding_extractor,
)


class FakeStream:
    def __init__(self) -> None:
        self.accepted: list[tuple[float, tuple[float, ...]]] = []
        self.finished = False

    def accept_waveform(self, sample_rate: float, waveform: tuple[float, ...]) -> None:
        self.accepted.append((sample_rate, waveform))

    def input_finished(self) -> None:
        self.finished = True


class FakeExtractor:
    def __init__(
        self,
        *,
        vector: tuple[float, ...] = (0.25, -0.75),
        ready: bool = True,
        dimension: int = 2,
    ) -> None:
        self._vector = vector
        self._ready = ready
        self._dimension = dimension
        self.stream = FakeStream()
        self.compute_calls = 0

    @property
    def dim(self) -> int:
        return self._dimension

    def create_stream(self) -> FakeStream:
        return self.stream

    def is_ready(self, stream: FakeStream) -> bool:
        assert stream is self.stream
        return self._ready

    def compute(self, stream: FakeStream) -> tuple[float, ...]:
        assert stream is self.stream
        self.compute_calls += 1
        return self._vector


def test_extracts_immutable_finite_embedding_for_exact_wav_interval(tmp_path: Path) -> None:
    pack, models_root, audio_root = make_pack(tmp_path)
    wav_path = write_pcm_wav(audio_root / "run.wav")
    extractor = FakeExtractor()
    calls: list[LocalDiarizationPack] = []
    adapter = SherpaVoiceEmbeddingAdapter(
        pack,
        models_root=models_root,
        audio_root=audio_root,
        loader=lambda received_pack: _capture_loader(calls, received_pack, extractor),
        wav_reader=lambda _path: ((0.0, 0.25, -0.25, 0.5, 0.75), 1_000),
    )

    vector = adapter.extract(wav_path, start_ms=1, end_ms=4)

    assert vector == (0.25, -0.75)
    assert isinstance(vector, tuple)
    assert calls == [pack]
    assert extractor.stream.accepted == [(1_000.0, (0.25, -0.25, 0.5))]
    assert extractor.stream.finished is True
    assert extractor.compute_calls == 1


def test_returns_verified_dimension_without_exposing_embedding_values(tmp_path: Path) -> None:
    pack, models_root, audio_root = make_pack(tmp_path)
    extractor = FakeExtractor(dimension=192)
    adapter = SherpaVoiceEmbeddingAdapter(
        pack,
        models_root=models_root,
        audio_root=audio_root,
        loader=lambda _pack: extractor,
    )

    assert adapter.embedding_dimension() == 192
    assert extractor.compute_calls == 0


def test_rejects_modified_pack_and_wav_outside_private_root(tmp_path: Path) -> None:
    pack, models_root, audio_root = make_pack(tmp_path)
    outside_wav = write_pcm_wav(tmp_path / "outside.wav")
    adapter = SherpaVoiceEmbeddingAdapter(
        pack,
        models_root=models_root,
        audio_root=audio_root,
        loader=lambda _pack: FakeExtractor(),
    )

    with pytest.raises(VoiceEmbeddingRuntimeError, match="Входной WAV"):
        adapter.extract(outside_wav, start_ms=0, end_ms=1)

    (pack.directory / "embedding.onnx").write_bytes(b"replaced")
    with pytest.raises(VoiceEmbeddingPackVerificationError, match="сумма"):
        adapter.verify_pack()


def test_rejects_stereo_wav_and_invalid_or_outside_interval(tmp_path: Path) -> None:
    pack, models_root, audio_root = make_pack(tmp_path)
    stereo_wav = write_pcm_wav(audio_root / "stereo.wav", channels=2)
    adapter = SherpaVoiceEmbeddingAdapter(
        pack,
        models_root=models_root,
        audio_root=audio_root,
        loader=lambda _pack: FakeExtractor(),
    )

    with pytest.raises(VoiceEmbeddingRuntimeError, match="моно"):
        adapter.extract(stereo_wav, start_ms=0, end_ms=1)

    wav_path = write_pcm_wav(audio_root / "run.wav")
    with pytest.raises(VoiceEmbeddingRuntimeError, match="целыми"):
        adapter.extract(wav_path, start_ms=0.0, end_ms=1)  # type: ignore[arg-type]
    with pytest.raises(VoiceEmbeddingRuntimeError, match="позже"):
        adapter.extract(wav_path, start_ms=2, end_ms=2)
    with pytest.raises(VoiceEmbeddingRuntimeError, match="выходят"):
        adapter.extract(wav_path, start_ms=0, end_ms=100)


def test_rejects_unready_or_nonfinite_embedding_without_returning_partial_vector(
    tmp_path: Path,
) -> None:
    pack, models_root, audio_root = make_pack(tmp_path)
    wav_path = write_pcm_wav(audio_root / "run.wav")
    too_short = FakeExtractor(ready=False)
    adapter = SherpaVoiceEmbeddingAdapter(
        pack,
        models_root=models_root,
        audio_root=audio_root,
        loader=lambda _pack: too_short,
        wav_reader=lambda _path: ((0.0, 0.25, -0.25), 1_000),
    )

    with pytest.raises(VoiceEmbeddingRuntimeError, match="слишком короткий"):
        adapter.extract(wav_path, start_ms=0, end_ms=2)
    assert too_short.compute_calls == 0

    invalid = FakeExtractor(vector=(float("nan"), 0.5))
    adapter = SherpaVoiceEmbeddingAdapter(
        pack,
        models_root=models_root,
        audio_root=audio_root,
        loader=lambda _pack: invalid,
        wav_reader=lambda _path: ((0.0, 0.25, -0.25), 1_000),
    )
    with pytest.raises(VoiceEmbeddingRuntimeError, match="неконечными"):
        adapter.extract(wav_path, start_ms=0, end_ms=2)


def test_default_loader_uses_public_sherpa_speaker_embedding_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack, _models_root, _audio_root = make_pack(tmp_path)
    calls: list[tuple[str, object]] = []

    class Config:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("config", kwargs))

        def validate(self) -> bool:
            return True

    class Extractor:
        def __init__(self, config: object) -> None:
            calls.append(("extractor", config))

    package = ModuleType("sherpa_onnx")
    package.SpeakerEmbeddingExtractorConfig = Config  # type: ignore[attr-defined]
    package.SpeakerEmbeddingExtractor = Extractor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sherpa_onnx", package)

    extractor = _load_sherpa_embedding_extractor(pack)

    assert isinstance(extractor, Extractor)
    assert calls[0] == (
        "config",
        {
            "model": str((pack.directory / "embedding.onnx").resolve()),
            "provider": "cpu",
        },
    )
    assert calls[1][0] == "extractor"


def _capture_loader(
    calls: list[LocalDiarizationPack],
    pack: LocalDiarizationPack,
    extractor: FakeExtractor,
) -> FakeExtractor:
    calls.append(pack)
    return extractor


def make_pack(tmp_path: Path) -> tuple[LocalDiarizationPack, Path, Path]:
    models_root = tmp_path / "models"
    directory = models_root / "voice-pack"
    directory.mkdir(parents=True)
    segmentation = b"segmentation-model"
    embedding = b"embedding-model"
    (directory / "segmentation.onnx").write_bytes(segmentation)
    (directory / "embedding.onnx").write_bytes(embedding)
    audio_root = tmp_path / "jobs"
    audio_root.mkdir()
    return (
        LocalDiarizationPack(
            pack_id="voice-pack-v1",
            directory=directory,
            segmentation_model="segmentation.onnx",
            embedding_model="embedding.onnx",
            files={
                "segmentation.onnx": sha256(segmentation).hexdigest(),
                "embedding.onnx": sha256(embedding).hexdigest(),
            },
        ),
        models_root,
        audio_root,
    )


def write_pcm_wav(path: Path, *, channels: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(b"\x00\x00\x00\x40\x00\xc0" * channels)
    return path
