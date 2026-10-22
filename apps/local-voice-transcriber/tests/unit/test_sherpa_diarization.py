from __future__ import annotations

import sys
import wave
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import ModuleType

import pytest

from local_voice_transcriber.diarization import (
    DiarizationPackVerificationError,
    DiarizationParameters,
    DiarizationRuntimeError,
    LocalDiarizationPack,
    SherpaDiarizationAdapter,
)
from local_voice_transcriber.diarization.sherpa import _load_sherpa_diarizer


@dataclass
class RawTurn:
    start: float
    end: float
    speaker: int | str


class FakeDiarizer:
    def __init__(self, turns: list[RawTurn], *, sample_rate: int = 16_000) -> None:
        self._turns = turns
        self._sample_rate = sample_rate
        self.calls: list[tuple[float, ...]] = []

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def process(self, samples: tuple[float, ...]) -> list[RawTurn]:
        self.calls.append(samples)
        return self._turns


def test_converts_local_sherpa_turns_to_ordered_domain_intervals(tmp_path: Path) -> None:
    pack, models_root, audio_root = make_pack(tmp_path)
    wav_path = write_pcm_wav(audio_root / "run.wav")
    diarizer = FakeDiarizer(
        [
            RawTurn(start=2.02, end=2.5, speaker=1),
            RawTurn(start=0.5, end=1.25, speaker=0),
        ]
    )
    calls: list[tuple[LocalDiarizationPack, DiarizationParameters]] = []
    adapter = SherpaDiarizationAdapter(
        pack,
        models_root=models_root,
        audio_root=audio_root,
        loader=lambda received_pack, parameters: _capture_loader(
            calls,
            received_pack,
            parameters,
            diarizer,
        ),
    )

    intervals = adapter.diarize(wav_path)

    assert [
        (
            item.interval_id,
            item.start_ms,
            item.end_ms,
            item.raw_speaker_label,
            item.speaker_group_id,
        )
        for item in intervals
    ] == [
        ("diarization-000001", 500, 1250, "0", "local-speaker:0"),
        ("diarization-000002", 2020, 2500, "1", "local-speaker:1"),
    ]
    assert calls == [(pack, DiarizationParameters())]
    assert diarizer.calls == [(0.0, 0.5, -0.5)]


def test_rejects_unverified_model_path_and_wav_outside_private_root(tmp_path: Path) -> None:
    pack, models_root, audio_root = make_pack(tmp_path)
    outside_wav = write_pcm_wav(tmp_path / "outside.wav")
    adapter = SherpaDiarizationAdapter(
        pack,
        models_root=models_root,
        audio_root=audio_root,
        loader=lambda _pack, _parameters: FakeDiarizer([]),
    )

    with pytest.raises(DiarizationPackVerificationError, match="Входной WAV"):
        adapter.diarize(outside_wav)

    traversal_pack = LocalDiarizationPack(
        pack_id="voice-pack",
        directory=pack.directory,
        segmentation_model="../outside.onnx",
        embedding_model=pack.embedding_model,
        files=pack.files,
    )
    with pytest.raises(DiarizationPackVerificationError, match="выходит"):
        SherpaDiarizationAdapter(
            traversal_pack,
            models_root=models_root,
            audio_root=audio_root,
            loader=lambda _pack, _parameters: FakeDiarizer([]),
        ).verify_pack()


def test_rejects_modified_model_pack_and_stereo_wav(tmp_path: Path) -> None:
    pack, models_root, audio_root = make_pack(tmp_path)
    segmentation_path = pack.directory / pack.segmentation_model
    segmentation_path.write_bytes("изменено".encode())
    adapter = SherpaDiarizationAdapter(
        pack,
        models_root=models_root,
        audio_root=audio_root,
        loader=lambda _pack, _parameters: FakeDiarizer([]),
    )

    with pytest.raises(DiarizationPackVerificationError, match="сумма"):
        adapter.verify_pack()

    pack, models_root, audio_root = make_pack(tmp_path / "fresh")
    stereo_wav = write_pcm_wav(audio_root / "stereo.wav", channels=2)
    adapter = SherpaDiarizationAdapter(
        pack,
        models_root=models_root,
        audio_root=audio_root,
        loader=lambda _pack, _parameters: FakeDiarizer([]),
    )
    with pytest.raises(DiarizationRuntimeError, match="моно"):
        adapter.diarize(stereo_wav)


def test_requires_sample_rate_expected_by_diarizer(tmp_path: Path) -> None:
    pack, models_root, audio_root = make_pack(tmp_path)
    wav_path = write_pcm_wav(audio_root / "run.wav")
    adapter = SherpaDiarizationAdapter(
        pack,
        models_root=models_root,
        audio_root=audio_root,
        loader=lambda _pack, _parameters: FakeDiarizer([], sample_rate=8_000),
        wav_reader=lambda _path: ((0.0,), 16_000),
    )

    with pytest.raises(DiarizationRuntimeError, match="Частота WAV"):
        adapter.diarize(wav_path)


def test_preflight_constructs_diarizer_without_processing_audio(tmp_path: Path) -> None:
    pack, models_root, audio_root = make_pack(tmp_path)
    calls: list[tuple[LocalDiarizationPack, DiarizationParameters]] = []
    adapter = SherpaDiarizationAdapter(
        pack,
        models_root=models_root,
        audio_root=audio_root,
        loader=lambda received_pack, parameters: _capture_loader(
            calls,
            received_pack,
            parameters,
            FakeDiarizer([]),
        ),
    )

    adapter.preflight()

    assert calls == [(pack, DiarizationParameters())]


def test_default_loader_uses_public_sherpa_offline_diarization_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack, _models_root, _audio_root = make_pack(tmp_path)
    calls: list[tuple[str, object]] = []

    class PyannoteConfig:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("pyannote", kwargs))

    class SegmentationConfig:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("segmentation", kwargs))

    class EmbeddingConfig:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("embedding", kwargs))

    class ClusteringConfig:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("clustering", kwargs))

    class DiarizationConfig:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("diarization", kwargs))

        def validate(self) -> bool:
            return True

    class ConstructedDiarizer:
        @property
        def sample_rate(self) -> int:
            return 16_000

        def __init__(self, config: object) -> None:
            calls.append(("diarizer", config))

        def process(self, samples: tuple[float, ...]) -> tuple[object, ...]:
            return ()

    package = ModuleType("sherpa_onnx")
    package.OfflineSpeakerSegmentationPyannoteModelConfig = PyannoteConfig  # type: ignore[attr-defined]
    package.OfflineSpeakerSegmentationModelConfig = SegmentationConfig  # type: ignore[attr-defined]
    package.SpeakerEmbeddingExtractorConfig = EmbeddingConfig  # type: ignore[attr-defined]
    package.FastClusteringConfig = ClusteringConfig  # type: ignore[attr-defined]
    package.OfflineSpeakerDiarizationConfig = DiarizationConfig  # type: ignore[attr-defined]
    package.OfflineSpeakerDiarization = ConstructedDiarizer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sherpa_onnx", package)

    diarizer = _load_sherpa_diarizer(
        pack,
        DiarizationParameters(
            num_speakers=3,
            clustering_threshold=0.42,
            window_shift_ratio=0.2,
            min_duration_on=0.25,
            min_duration_off=0.75,
        ),
    )

    assert isinstance(diarizer, ConstructedDiarizer)
    assert calls[0] == (
        "pyannote",
        {
            "model": str((pack.directory / "segmentation.onnx").resolve()),
            "window_shift_ratio": 0.2,
        },
    )
    assert calls[2] == (
        "embedding",
        {"model": str((pack.directory / "embedding.onnx").resolve())},
    )
    assert calls[3] == (
        "clustering",
        {"num_clusters": 3, "threshold": 0.42},
    )
    assert calls[4][0] == "diarization"
    assert calls[-1][0] == "diarizer"


def _capture_loader(
    calls: list[tuple[LocalDiarizationPack, DiarizationParameters]],
    pack: LocalDiarizationPack,
    parameters: DiarizationParameters,
    diarizer: FakeDiarizer,
) -> FakeDiarizer:
    calls.append((pack, parameters))
    return diarizer


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
