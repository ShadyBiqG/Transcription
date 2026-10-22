"""Строгая загрузка офлайн-манифестов ASR/VAD-пакетов."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from local_voice_transcriber.asr import LocalModelPack
from local_voice_transcriber.diarization import LocalDiarizationPack

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ModelPackManifestError(RuntimeError):
    """Манифест локальных моделей нельзя безопасно применить."""


@dataclass(frozen=True, slots=True)
class AsrModelPacks:
    """Проверенная конфигурация локальных ASR, VAD и voice-packs."""

    language: str
    asr: LocalModelPack
    vad: LocalModelPack | None
    diarization: LocalDiarizationPack | None


def load_asr_model_packs(manifest_path: Path, *, models_root: Path) -> AsrModelPacks:
    """Загружает только существующие локальные пакеты без сетевых операций."""

    manifest = _read_manifest(manifest_path)
    root = _resolve_models_root(models_root)

    _require_schema_version(manifest)
    language = _required_text(manifest, "language", context="манифест")
    asr = _parse_pack(manifest, field="asr", models_root=root)

    vad_raw = manifest.get("vad")
    vad = None if vad_raw is None else _parse_pack(manifest, field="vad", models_root=root)
    diarization_raw = manifest.get("diarization")
    diarization = (
        None
        if diarization_raw is None
        else _parse_diarization_pack(manifest, models_root=root)
    )
    return AsrModelPacks(language=language, asr=asr, vad=vad, diarization=diarization)


def _read_manifest(path: Path) -> Mapping[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        message = "Не удалось прочитать локальный манифест model pack"
        raise ModelPackManifestError(message) from error
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ModelPackManifestError("Манифест model pack содержит некорректный JSON") from error
    if not isinstance(parsed, dict):
        raise ModelPackManifestError("Корень манифеста model pack должен быть объектом")
    return parsed


def _resolve_models_root(models_root: Path) -> Path:
    try:
        root = models_root.resolve(strict=True)
    except OSError as error:
        raise ModelPackManifestError("Каталог локальных моделей недоступен") from error
    if not root.is_dir():
        raise ModelPackManifestError("Каталог локальных моделей не является каталогом")
    return root


def _require_schema_version(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise ModelPackManifestError("Поддерживается только schema_version 1")


def _parse_pack(
    manifest: Mapping[str, Any],
    *,
    field: str,
    models_root: Path,
) -> LocalModelPack:
    raw_pack = manifest.get(field)
    if not isinstance(raw_pack, dict):
        raise ModelPackManifestError(f"Раздел {field} манифеста должен быть объектом")

    pack_id = _required_text(raw_pack, "pack_id", context=f"раздел {field}")
    model_name = _required_text(raw_pack, "model_name", context=f"раздел {field}")
    directory_value = _required_text(raw_pack, "directory", context=f"раздел {field}")
    directory = _resolve_relative_path(
        directory_value,
        base=models_root,
        allowed_root=models_root,
        field_name=f"directory раздела {field}",
        expected="directory",
    )
    files = _parse_files(raw_pack, pack_field=field, directory=directory, models_root=models_root)
    return LocalModelPack(
        pack_id=pack_id,
        model_name=model_name,
        directory=directory,
        files=MappingProxyType(files),
    )


def _parse_diarization_pack(
    manifest: Mapping[str, Any],
    *,
    models_root: Path,
) -> LocalDiarizationPack:
    field = "diarization"
    raw_pack = manifest.get(field)
    if not isinstance(raw_pack, dict):
        raise ModelPackManifestError(f"Раздел {field} манифеста должен быть объектом")
    pack_id = _required_text(raw_pack, "pack_id", context=f"раздел {field}")
    directory_value = _required_text(raw_pack, "directory", context=f"раздел {field}")
    directory = _resolve_relative_path(
        directory_value,
        base=models_root,
        allowed_root=models_root,
        field_name=f"directory раздела {field}",
        expected="directory",
    )
    segmentation_model = _required_text(
        raw_pack,
        "segmentation_model",
        context=f"раздел {field}",
    )
    embedding_model = _required_text(
        raw_pack,
        "embedding_model",
        context=f"раздел {field}",
    )
    files = _parse_files(raw_pack, pack_field=field, directory=directory, models_root=models_root)
    return LocalDiarizationPack(
        pack_id=pack_id,
        directory=directory,
        segmentation_model=segmentation_model,
        embedding_model=embedding_model,
        files=MappingProxyType(files),
    )


def _parse_files(
    raw_pack: Mapping[str, Any],
    *,
    pack_field: str,
    directory: Path,
    models_root: Path,
) -> dict[str, str]:
    raw_files = raw_pack.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        message = f"Раздел files pack {pack_field} должен быть непустым объектом"
        raise ModelPackManifestError(message)

    files: dict[str, str] = {}
    for relative_name, expected_hash in raw_files.items():
        if not isinstance(relative_name, str) or not relative_name.strip():
            message = f"Имя файла pack {pack_field} должно быть непустой строкой"
            raise ModelPackManifestError(message)
        if not isinstance(expected_hash, str) or not _SHA256_RE.fullmatch(expected_hash):
            raise ModelPackManifestError(f"Хеш файла pack {pack_field} должен быть SHA-256")
        _resolve_relative_path(
            relative_name,
            base=directory,
            allowed_root=directory,
            field_name=f"файл {relative_name!r} pack {pack_field}",
            expected="file",
        )
        _ensure_within_models_root(
            (directory / relative_name).resolve(strict=True),
            models_root,
            f"файл {relative_name!r} pack {pack_field}",
        )
        files[relative_name] = expected_hash.lower()
    return files


def _required_text(source: Mapping[str, Any], field: str, *, context: str) -> str:
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ModelPackManifestError(f"Поле {field} в {context} должно быть непустой строкой")
    return value.strip()


def _resolve_relative_path(
    value: str,
    *,
    base: Path,
    allowed_root: Path,
    field_name: str,
    expected: str,
) -> Path:
    raw_path = Path(value)
    if raw_path.is_absolute():
        raise ModelPackManifestError(f"{field_name} должен быть относительным путём")
    try:
        resolved = (base / raw_path).resolve(strict=True)
    except OSError as error:
        raise ModelPackManifestError(f"{field_name} не существует локально") from error
    _ensure_within_models_root(resolved, allowed_root, field_name)
    if expected == "directory" and not resolved.is_dir():
        raise ModelPackManifestError(f"{field_name} должен указывать на каталог")
    if expected == "file" and not resolved.is_file():
        raise ModelPackManifestError(f"{field_name} должен указывать на файл")
    return resolved


def _ensure_within_models_root(path: Path, root: Path, field_name: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as error:
        message = f"{field_name} выходит за разрешенный каталог моделей"
        raise ModelPackManifestError(message) from error
