from __future__ import annotations

import os
import wave
from hashlib import sha256
from pathlib import Path

import pytest

from local_voice_transcriber.voice.sample_artifact import (
    VoiceSampleArtifactFormatError,
    VoiceSampleArtifactSecurityError,
    VoiceSampleArtifactWriter,
)


def test_creates_private_wav_for_exact_interval_with_hash_and_quality(tmp_path: Path) -> None:
    jobs_root, samples_root = make_roots(tmp_path)
    source = write_pcm_wav(
        jobs_root / "job-1" / "runs" / "run-1" / "normalized.wav",
        samples=(-32768, -16384, 0, 16384, 32767, 0) + (0,) * 26,
        sample_rate_hz=16_000,
    )
    writer = VoiceSampleArtifactWriter(jobs_root=jobs_root, samples_root=samples_root)

    artifact = writer.create_from_interval(
        source,
        start_ms=0,
        end_ms=1,
        artifact_id="sample_alex-01",
    )

    assert artifact.artifact_path == samples_root / "sample_alex-01.wav"
    assert artifact.protected_path == "sample_alex-01.wav"
    assert artifact.content_sha256 == sha256(artifact.artifact_path.read_bytes()).hexdigest()
    assert read_samples(artifact.artifact_path) == (
        -32768,
        -16384,
        0,
        16384,
        32767,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    assert artifact.duration_ms == 1
    assert artifact.quality.frame_count == 16
    assert artifact.quality.peak == 1.0
    assert artifact.quality.non_silent_ratio == 0.25
    assert artifact.quality_metadata == {
        "duration_ms": 1,
        "sample_rate_hz": 16000,
        "sample_width_bytes": 2,
        "frame_count": 16,
        "rms": artifact.quality.rms,
        "peak": 1.0,
        "non_silent_ratio": 0.25,
    }


def test_rejects_non_normalized_wav_and_invalid_or_out_of_bounds_interval(tmp_path: Path) -> None:
    jobs_root, samples_root = make_roots(tmp_path)
    writer = VoiceSampleArtifactWriter(jobs_root=jobs_root, samples_root=samples_root)
    stereo = write_pcm_wav(jobs_root / "stereo.wav", samples=(1, 2), channels=2)
    wrong_rate = write_pcm_wav(
        jobs_root / "wrong-rate.wav",
        samples=(1,) * 16,
        sample_rate_hz=8_000,
    )
    source = write_pcm_wav(jobs_root / "valid.wav", samples=(1,) * 16)

    with pytest.raises(VoiceSampleArtifactFormatError, match="моно"):
        writer.create_from_interval(stereo, start_ms=0, end_ms=1, artifact_id="stereo")
    with pytest.raises(VoiceSampleArtifactFormatError, match="16000"):
        writer.create_from_interval(wrong_rate, start_ms=0, end_ms=1, artifact_id="rate")
    with pytest.raises(ValueError, match="положительный"):
        writer.create_from_interval(source, start_ms=1, end_ms=1, artifact_id="empty")
    with pytest.raises(VoiceSampleArtifactFormatError, match="выходит"):
        writer.create_from_interval(source, start_ms=0, end_ms=2, artifact_id="outside")


def test_rejects_outside_paths_invalid_artifact_id_and_existing_output(tmp_path: Path) -> None:
    jobs_root, samples_root = make_roots(tmp_path)
    writer = VoiceSampleArtifactWriter(jobs_root=jobs_root, samples_root=samples_root)
    source = write_pcm_wav(jobs_root / "valid.wav", samples=(1,) * 32)
    outside = write_pcm_wav(tmp_path / "outside.wav", samples=(1,) * 32)

    with pytest.raises(VoiceSampleArtifactSecurityError, match="вне"):
        writer.create_from_interval(outside, start_ms=0, end_ms=1, artifact_id="outside")
    with pytest.raises(VoiceSampleArtifactSecurityError, match="только"):
        writer.create_from_interval(source, start_ms=0, end_ms=1, artifact_id="../escape")

    existing = samples_root / "taken.wav"
    existing.write_bytes(b"do-not-overwrite")
    with pytest.raises(VoiceSampleArtifactSecurityError, match="уже существует"):
        writer.create_from_interval(source, start_ms=0, end_ms=1, artifact_id="taken")
    assert existing.read_bytes() == b"do-not-overwrite"


def test_rejects_symlinked_wav_even_when_the_target_is_inside_jobs_root(tmp_path: Path) -> None:
    jobs_root, samples_root = make_roots(tmp_path)
    source = write_pcm_wav(jobs_root / "actual.wav", samples=(1,) * 32)
    linked = jobs_root / "linked.wav"
    try:
        os.symlink(source, linked)
    except (NotImplementedError, OSError):
        pytest.skip("Создание symbolic link недоступно в текущей среде.")
    writer = VoiceSampleArtifactWriter(jobs_root=jobs_root, samples_root=samples_root)

    with pytest.raises(VoiceSampleArtifactSecurityError, match="symbolic link"):
        writer.create_from_interval(linked, start_ms=0, end_ms=1, artifact_id="linked")


def make_roots(tmp_path: Path) -> tuple[Path, Path]:
    jobs_root = tmp_path / "jobs"
    samples_root = tmp_path / "samples"
    jobs_root.mkdir()
    samples_root.mkdir()
    return jobs_root, samples_root


def write_pcm_wav(
    path: Path,
    *,
    samples: tuple[int, ...],
    sample_rate_hz: int = 16_000,
    channels: int = 1,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate_hz)
        writer.writeframes(frames * channels)
    return path


def read_samples(path: Path) -> tuple[int, ...]:
    with wave.open(str(path), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == 16_000
        payload = reader.readframes(reader.getnframes())
    return tuple(
        int.from_bytes(payload[offset : offset + 2], "little", signed=True)
        for offset in range(0, len(payload), 2)
    )
