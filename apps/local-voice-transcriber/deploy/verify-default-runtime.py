"""Проверяет, что доставленные по контрольным суммам модели реально загружаются локально."""

from __future__ import annotations

import argparse
import faulthandler
import subprocess
from pathlib import Path

from local_voice_transcriber.asr import OfflineAsrAdapter
from local_voice_transcriber.diarization import SherpaDiarizationAdapter
from local_voice_transcriber.runtime import load_asr_model_packs
from local_voice_transcriber.voice.embedding import SherpaVoiceEmbeddingAdapter


def main() -> None:
    faulthandler.enable()
    parser = argparse.ArgumentParser()
    parser.add_argument("--models-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    arguments = parser.parse_args()

    print("Loading and verifying the local model manifest...", flush=True)
    packs = load_asr_model_packs(arguments.manifest, models_root=arguments.models_root)
    print("Loading local ONNX Runtime ASR and VAD...", flush=True)
    OfflineAsrAdapter(
        packs.asr,
        models_root=arguments.models_root,
        language=packs.language,
        vad_pack=packs.vad,
    ).preflight()
    if packs.diarization is None:
        raise RuntimeError("Default manifest must contain a voice pack.")
    print("Loading local Sherpa diarization model...", flush=True)
    SherpaDiarizationAdapter(
        packs.diarization,
        models_root=arguments.models_root,
        audio_root=arguments.models_root,
    ).preflight()
    print("Loading local Sherpa voice embedding model...", flush=True)
    dimension = SherpaVoiceEmbeddingAdapter(
        packs.diarization,
        models_root=arguments.models_root,
        audio_root=arguments.models_root,
    ).embedding_dimension()
    if dimension != packs.diarization.embedding_dimension:
        raise RuntimeError("Voice embedding dimension does not match the manifest.")
    print("Starting local FFmpeg...", flush=True)
    completed = subprocess.run(
        [str(arguments.ffmpeg), "-version"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise RuntimeError("FFmpeg did not start.")
    print("Local ASR, VAD, diarization, voice embedding, and FFmpeg are ready.")


if __name__ == "__main__":
    main()
