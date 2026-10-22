"""Контракт запуска очереди только с проверенными local-only ASR ресурсами."""

from __future__ import annotations

from hashlib import sha256

from fastapi.testclient import TestClient

from local_voice_transcriber.api import create_app
from local_voice_transcriber.config import Settings


def test_verified_local_manifest_enables_asr_worker_without_loading_model_from_network(
    settings: Settings,
) -> None:
    models_root = settings.data_dir / "models"
    asr_directory = models_root / "gigaam"
    asr_directory.mkdir(parents=True)
    model = asr_directory / "model.onnx"
    model.write_bytes(b"verified-local-model")
    manifest = models_root / "asr-pack.json"
    manifest.write_text(
        (
            "{"
            '"schema_version":1,'
            '"language":"ru",'
            '"asr":{'
            '"pack_id":"gigaam-v3-local",'
            '"model_name":"gigaam-v3-ctc",'
            '"directory":"gigaam",'
            f'"files":{{"model.onnx":"{sha256(model.read_bytes()).hexdigest()}"}}'
            "}"
            "}"
        ),
        encoding="utf-8",
    )
    ffmpeg = settings.data_dir / "ffmpeg.exe"
    ffmpeg.write_bytes(b"not-run-in-readiness-test")
    configured = Settings(
        data_dir=settings.data_dir,
        database_path=settings.database_path,
        host=settings.host,
        port=settings.port,
        instance_id=settings.instance_id,
        cookie_secure=settings.cookie_secure,
        session_ttl_days=settings.session_ttl_days,
        auth_rate_limit=settings.auth_rate_limit,
        zeusdb_dir=settings.zeusdb_dir,
        models_dir=models_root,
        asr_manifest_path=manifest,
        ffmpeg_path=ffmpeg,
    )

    with TestClient(create_app(configured)) as client:
        assert client.app.state.asr_ready is True
        assert client.app.state.asr_pack_id == "gigaam-v3-local"
        assert client.app.state.asr_unavailable_reason is None
