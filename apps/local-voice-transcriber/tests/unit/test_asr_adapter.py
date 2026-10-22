from __future__ import annotations

import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import ModuleType

import pytest

from local_voice_transcriber.asr import (
    AsrRuntimeError,
    LocalModelPack,
    OfflineAsrAdapter,
    PackVerificationError,
)
from local_voice_transcriber.asr.adapter import _load_onnx_asr_locally


@dataclass
class RawSegment:
    start: float
    end: float
    text: str
    tokens: list[str] | None = None
    timestamps: list[float] | None = None
    logprobs: list[float] | None = None


class FakeRecognizer:
    def __init__(self, result: list[RawSegment]) -> None:
        self._result = result
        self.calls: list[str] = []

    def recognize(self, wav_path: str) -> list[RawSegment]:
        self.calls.append(wav_path)
        return self._result


def test_local_adapter_makes_token_timestamps_absolute_and_projects_words(tmp_path: Path) -> None:
    pack, root = make_pack(tmp_path)
    wav_path = tmp_path / "audio.wav"
    wav_path.write_bytes(b"RIFF")
    recognizer = FakeRecognizer(
        [
            RawSegment(
                start=1.5,
                end=2.5,
                text="Привет мир",
                tokens=["▁Привет", "▁мир"],
                timestamps=[0.0, 0.3],
                logprobs=[-0.2, -0.4],
            )
        ]
    )
    adapter = OfflineAsrAdapter(
        pack,
        models_root=root,
        language="ru",
        loader=lambda _pack, _vad: recognizer,
    )

    recognition = adapter.transcribe(wav_path)

    segment = recognition.segments[0]
    assert segment.word_timing_status == "available"
    token_times = [(token.start_ms, token.end_ms) for token in segment.tokens]
    assert token_times == [(1500, 1800), (1800, 2500)]
    assert [(word.text, word.start_ms, word.end_ms) for word in segment.words] == [
        ("Привет", 1500, 1800),
        ("мир", 1800, 2500),
    ]
    assert segment.confidence == pytest.approx(-0.3)
    assert recognizer.calls == [str(wav_path)]


def test_missing_or_invalid_timestamps_are_segment_only_fallback(tmp_path: Path) -> None:
    pack, root = make_pack(tmp_path)
    wav_path = tmp_path / "audio.wav"
    wav_path.write_bytes(b"RIFF")
    adapter = OfflineAsrAdapter(
        pack,
        models_root=root,
        language="ru",
        loader=lambda _pack, _vad: FakeRecognizer(
            [
                RawSegment(
                    start=1.0,
                    end=2.0,
                    text="без точных слов",
                    tokens=["▁без"],
                    timestamps=None,
                )
            ]
        ),
    )

    segment = adapter.transcribe(wav_path).segments[0]

    assert segment.word_timing_status == "unavailable"
    assert segment.tokens == ()
    assert segment.words == ()


def test_model_pack_must_be_hash_verified_and_inside_models_root(tmp_path: Path) -> None:
    pack, root = make_pack(tmp_path)
    wav_path = tmp_path / "audio.wav"
    wav_path.write_bytes(b"RIFF")
    adapter = OfflineAsrAdapter(
        pack,
        models_root=root,
        language="ru",
        loader=lambda _pack, _vad: FakeRecognizer([]),
    )
    (pack.directory / "config.json").write_text("изменено", encoding="utf-8")

    with pytest.raises(PackVerificationError):
        adapter.transcribe(wav_path)

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_pack = LocalModelPack("asr", "gigaam-v3-ctc", outside, {"x": "0" * 64})
    with pytest.raises(PackVerificationError):
        OfflineAsrAdapter(
            outside_pack,
            models_root=root,
            language="ru",
            loader=lambda _pack, _vad: FakeRecognizer([]),
        ).verify_packs()


def test_preflight_loads_the_local_runtime_without_recognizing_audio(tmp_path: Path) -> None:
    pack, root = make_pack(tmp_path)
    calls: list[tuple[LocalModelPack, LocalModelPack | None]] = []

    def loader(received_pack: LocalModelPack, vad_pack: LocalModelPack | None) -> FakeRecognizer:
        calls.append((received_pack, vad_pack))
        return FakeRecognizer([])

    adapter = OfflineAsrAdapter(
        pack,
        models_root=root,
        language="ru",
        loader=loader,
    )

    adapter.preflight()

    assert calls == [(pack, None)]


def test_preflight_normalizes_runtime_loading_failure(tmp_path: Path) -> None:
    pack, root = make_pack(tmp_path)
    adapter = OfflineAsrAdapter(
        pack,
        models_root=root,
        language="ru",
        loader=lambda _pack, _vad: (_ for _ in ()).throw(ValueError("broken runtime")),
    )

    with pytest.raises(AsrRuntimeError, match="не удалось загрузить"):
        adapter.preflight()


def test_default_loader_forces_cpu_offline_and_local_model_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asr_pack, _root = make_pack(tmp_path)
    vad_directory = tmp_path / "models" / "vad"
    vad_directory.mkdir()
    (vad_directory / "vad.onnx").write_bytes(b"vad")
    vad_pack = LocalModelPack(
        "vad", "silero", vad_directory, {"vad.onnx": sha256(b"vad").hexdigest()}
    )
    calls: list[tuple[str, object]] = []

    class FakeRecognizerWithModes(FakeRecognizer):
        def with_vad(self, vad: object) -> FakeRecognizerWithModes:
            calls.append(("with_vad", vad))
            return self

        def with_timestamps(self) -> FakeRecognizerWithModes:
            calls.append(("with_timestamps", None))
            return self

    recognizer = FakeRecognizerWithModes([])

    class FakeManager:
        def __init__(self, *, providers: list[str]) -> None:
            calls.append(("providers", providers))

        def create_asr(self, name: str, **kwargs: object) -> FakeRecognizerWithModes:
            calls.append(("asr", (name, kwargs)))
            return recognizer

        def create_vad(self, name: str, **kwargs: object) -> object:
            calls.append(("vad", (name, kwargs)))
            return "fake-vad"

    package = ModuleType("onnx_asr")
    loader = ModuleType("onnx_asr.loader")
    loader.Manager = FakeManager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "onnx_asr", package)
    monkeypatch.setitem(sys.modules, "onnx_asr.loader", loader)

    assert _load_onnx_asr_locally(asr_pack, vad_pack) is recognizer

    assert calls == [
        ("providers", ["CPUExecutionProvider"]),
        ("asr", ("gigaam-v3-ctc", {"local_dir": asr_pack.directory, "offline": True})),
        ("vad", ("silero", {"local_dir": vad_pack.directory, "offline": True})),
        ("with_vad", "fake-vad"),
        ("with_timestamps", None),
    ]


def make_pack(tmp_path: Path) -> tuple[LocalModelPack, Path]:
    root = tmp_path / "models"
    directory = root / "gigaam"
    directory.mkdir(parents=True)
    content = "локальный-model-pack".encode()
    (directory / "config.json").write_bytes(content)
    return (
        LocalModelPack(
            pack_id="gigaam-v3",
            model_name="gigaam-v3-ctc",
            directory=directory,
            files={"config.json": sha256(content).hexdigest()},
        ),
        root,
    )
