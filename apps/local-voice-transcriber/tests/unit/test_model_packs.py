from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path

import pytest

from local_voice_transcriber.runtime import ModelPackManifestError, load_asr_model_packs


def test_loads_valid_local_asr_and_vad_packs_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models_root = tmp_path / "models"
    asr_dir = models_root / "asr" / "gigaam"
    vad_dir = models_root / "vad" / "silero"
    asr_dir.mkdir(parents=True)
    vad_dir.mkdir(parents=True)
    asr_content = b"asr-local"
    vad_content = b"vad-local"
    (asr_dir / "model.onnx").write_bytes(asr_content)
    (vad_dir / "model.onnx").write_bytes(vad_content)
    manifest_path = tmp_path / "pack.json"
    _write_manifest(
        manifest_path,
        asr_directory="asr/gigaam",
        asr_files={"model.onnx": _sha256(asr_content)},
        vad_directory="vad/silero",
        vad_files={"model.onnx": _sha256(vad_content)},
    )

    def network_forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Загрузчик пакетов не должен обращаться к сети")

    monkeypatch.setattr(urllib.request, "urlopen", network_forbidden)

    packs = load_asr_model_packs(manifest_path, models_root=models_root)

    assert packs.language == "ru"
    assert packs.asr.pack_id == "gigaam-v3"
    assert packs.asr.model_name == "gigaam-v3-ctc"
    assert packs.asr.directory == asr_dir.resolve()
    assert dict(packs.asr.files) == {"model.onnx": _sha256(asr_content)}
    assert packs.vad is not None
    assert packs.vad.pack_id == "silero-v5"
    assert packs.vad.directory == vad_dir.resolve()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "schema_version"),
        ({"schema_version": 2}, "schema_version"),
        ({"schema_version": 1, "language": "ru"}, "asr"),
        (
            {
                "schema_version": 1,
                "language": "ru",
                "asr": {
                    "pack_id": "a",
                    "model_name": "m",
                    "directory": "asr",
                    "files": {"model.onnx": "not-a-hash"},
                },
            },
            "SHA-256",
        ),
    ],
)
def test_rejects_malformed_manifest(
    tmp_path: Path, payload: dict[str, object], message: str
) -> None:
    models_root = tmp_path / "models"
    models_root.mkdir()
    (models_root / "asr").mkdir()
    manifest_path = tmp_path / "pack.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ModelPackManifestError, match=message):
        load_asr_model_packs(manifest_path, models_root=models_root)


@pytest.mark.parametrize(
    ("directory", "files"),
    [
        ("../outside", {"model.onnx": "a" * 64}),
        ("asr/gigaam", {"../../../outside/model.onnx": "a" * 64}),
        ("asr/gigaam", {"C:/outside/model.onnx": "a" * 64}),
    ],
)
def test_rejects_manifest_path_traversal(
    tmp_path: Path, directory: str, files: dict[str, str]
) -> None:
    models_root = tmp_path / "models"
    (models_root / "asr" / "gigaam").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "model.onnx").write_bytes(b"outside")
    manifest_path = tmp_path / "pack.json"
    _write_manifest(
        manifest_path,
        asr_directory=directory,
        asr_files=files,
    )

    with pytest.raises(ModelPackManifestError, match="разрешенный каталог|относительным"):
        load_asr_model_packs(manifest_path, models_root=models_root)


def test_rejects_symlink_escaping_models_root(tmp_path: Path) -> None:
    models_root = tmp_path / "models"
    models_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "model.onnx").write_bytes(b"outside")
    escaped_directory = models_root / "asr"
    try:
        escaped_directory.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Среда не разрешает создание символических ссылок: {error}")

    manifest_path = tmp_path / "pack.json"
    _write_manifest(
        manifest_path,
        asr_directory="asr",
        asr_files={"model.onnx": _sha256(b"outside")},
    )

    with pytest.raises(ModelPackManifestError, match="разрешенный каталог"):
        load_asr_model_packs(manifest_path, models_root=models_root)


def _write_manifest(
    path: Path,
    *,
    asr_directory: str,
    asr_files: dict[str, str],
    vad_directory: str | None = None,
    vad_files: dict[str, str] | None = None,
) -> None:
    payload: dict[str, object] = {
        "schema_version": 1,
        "language": "ru",
        "asr": {
            "pack_id": "gigaam-v3",
            "model_name": "gigaam-v3-ctc",
            "directory": asr_directory,
            "files": asr_files,
        },
    }
    if vad_directory is not None and vad_files is not None:
        payload["vad"] = {
            "pack_id": "silero-v5",
            "model_name": "silero",
            "directory": vad_directory,
            "files": vad_files,
        }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
