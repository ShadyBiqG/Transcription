"""Owner-only HTTP-маршруты для загрузки и просмотра приватных заданий."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Header, Query, Request, UploadFile, status
from fastapi.responses import FileResponse

from ..auth import ApiError, CsrfSessionDependency, SessionDependency
from ..jobs import Job, JobRepository, JobRun, JobStatus
from ..jobs.repository import InvalidRunState
from ..vocabulary.suggestions import suggest_for_revision

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])
_MAX_UPLOAD_BYTES = 512 * 1024 * 1024
_SIGNATURE_WINDOW_BYTES = 64 * 1024
_MAX_ID3_TAG_BYTES = 16 * 1024 * 1024
_UPLOAD_FILE = File(...)


@dataclass(frozen=True)
class _MediaFormat:
    """Разрешённый исходный контейнер и его безопасный HTTP-тип."""

    extension: str
    media_type: str
    signature_matches: Callable[[bytes, int], bool]


def _ebml_vint_width(first_byte: int) -> int | None:
    for width in range(1, 9):
        if first_byte & (0x80 >> (width - 1)):
            return width
    return None


def _read_ebml_size(signature: bytes, offset: int) -> tuple[int | None, int] | None:
    if offset >= len(signature):
        return None
    width = _ebml_vint_width(signature[offset])
    if width is None or offset + width > len(signature):
        return None
    value = signature[offset] & (0xFF >> width)
    for index in range(1, width):
        value = value << 8 | signature[offset + index]
    # Все единицы в VINT означают неизвестный размер. Он допустим для Segment,
    # но не для заголовка EBML и его вложенных полей.
    unknown_size = (1 << (7 * width)) - 1
    return (None if value == unknown_size else value), width


def _read_ebml_element_id(signature: bytes, offset: int) -> tuple[bytes, int] | None:
    if offset >= len(signature):
        return None
    width = _ebml_vint_width(signature[offset])
    if width is None or offset + width > len(signature):
        return None
    return signature[offset : offset + width], width


def _webm_ebml_header_end(signature: bytes) -> int | None:
    if not signature.startswith(b"\x1a\x45\xdf\xa3"):
        return None
    header_size = _read_ebml_size(signature, 4)
    if header_size is None or header_size[0] is None:
        return None
    length, width = header_size
    position = 4 + width
    header_end = position + length
    if header_end > len(signature):
        return None
    has_webm_doc_type = False
    while position < header_end:
        element_id = _read_ebml_element_id(signature, position)
        if element_id is None:
            return None
        identifier, identifier_width = element_id
        size_info = _read_ebml_size(signature, position + identifier_width)
        if size_info is None or size_info[0] is None:
            return None
        size, size_width = size_info
        payload_start = position + identifier_width + size_width
        payload_end = payload_start + size
        if payload_end > header_end:
            return None
        if identifier == b"\x42\x82" and signature[payload_start:payload_end] == b"webm":
            has_webm_doc_type = True
        position = payload_end
    return header_end if has_webm_doc_type else None


def _is_webm(signature: bytes, _total_size: int) -> bool:
    # WebM использует полноценный EBML-заголовок с DocType и Segment, а не
    # одиночный magic number, который легко добавить к произвольному файлу.
    header_end = _webm_ebml_header_end(signature)
    if header_end is None:
        return False
    position = header_end
    while position < len(signature):
        element_id = _read_ebml_element_id(signature, position)
        if element_id is None:
            return False
        identifier, identifier_width = element_id
        size_info = _read_ebml_size(signature, position + identifier_width)
        if size_info is None:
            return False
        size, size_width = size_info
        if identifier == b"\x18\x53\x80\x67":
            return True
        if size is None:
            return False
        position += identifier_width + size_width + size
    return False


def _is_mp3_frame_header(signature: bytes, offset: int = 0) -> bool:
    if offset + 4 > len(signature) or signature[offset] != 0xFF:
        return False
    second, third = signature[offset + 1], signature[offset + 2]
    version = (second >> 3) & 0b11
    layer = (second >> 1) & 0b11
    bitrate_index = third >> 4
    sample_rate_index = (third >> 2) & 0b11
    return (
        second & 0xE0 == 0xE0
        and version != 0b01
        and layer != 0
        and bitrate_index not in {0, 0b1111}
        and sample_rate_index != 0b11
    )


def _is_mp3(signature: bytes, total_size: int) -> bool:
    if not signature.startswith(b"ID3"):
        return _is_mp3_frame_header(signature)
    if len(signature) < 10 or signature[3] not in {2, 3, 4}:
        return False
    major_version, flags = signature[3], signature[5]
    if (major_version == 2 and flags != 0) or (major_version == 3 and flags & 0x1F):
        return False
    if major_version == 4 and flags & 0x0F:
        return False
    encoded_size = signature[6:10]
    if any(value & 0x80 for value in encoded_size):
        return False
    tag_size = sum(value << (7 * (3 - index)) for index, value in enumerate(encoded_size))
    if tag_size > _MAX_ID3_TAG_BYTES:
        return False
    frame_offset = 10 + tag_size
    if major_version == 4 and flags & 0x10:
        frame_offset += 10
    if frame_offset + 4 > len(signature):
        # Не загружаем мегабайтные ID3-обложки целиком только ради проверки. Если
        # frame header за пределами окна, полная запись всё же должна содержать
        # как минимум четыре байта после корректно объявленного tag.
        return total_size >= frame_offset + 4
    return _is_mp3_frame_header(signature, frame_offset)


def _is_wav(signature: bytes, _total_size: int) -> bool:
    return len(signature) >= 12 and signature[:4] == b"RIFF" and signature[8:12] == b"WAVE"


def _is_aac(signature: bytes, _total_size: int) -> bool:
    return signature.startswith(b"ADIF") or (
        len(signature) >= 2 and signature[0] == 0xFF and signature[1] & 0xF6 == 0xF0
    )


def _ogg_crc32(page: bytes) -> int:
    """Вычисляет определённый Ogg побитовый CRC страницы с нулевым checksum полем."""

    value = 0
    for byte in page:
        value ^= byte << 24
        for _ in range(8):
            value = (value << 1) ^ (0x04C11DB7 if value & 0x80000000 else 0)
            value &= 0xFFFFFFFF
    return value


def _read_ogg_first_page(signature: bytes) -> tuple[int, int, bytes, tuple[int, ...]] | None:
    if len(signature) < 27 or signature[:4] != b"OggS" or signature[4] != 0:
        return None
    header_type = signature[5]
    if header_type & ~0x07:
        return None
    sequence = int.from_bytes(signature[18:22], byteorder="little")
    segment_count = signature[26]
    table_start = 27
    body_start = table_start + segment_count
    if segment_count == 0 or body_start > len(signature):
        return None
    lacing_values = tuple(signature[table_start:body_start])
    body_end = body_start + sum(lacing_values)
    if body_end > len(signature):
        return None
    checksum = int.from_bytes(signature[22:26], byteorder="little")
    page = bytearray(signature[:body_end])
    page[22:26] = b"\x00" * 4
    if checksum == 0 or checksum != _ogg_crc32(page):
        return None
    return header_type, sequence, signature[body_start:body_end], lacing_values


def _first_ogg_packet(body: bytes, lacing_values: tuple[int, ...]) -> bytes | None:
    packet_end = 0
    for lacing_value in lacing_values:
        packet_end += lacing_value
        if lacing_value < 255:
            return body[:packet_end]
    return None


def _is_ogg(signature: bytes, _total_size: int) -> bool:
    return _read_ogg_first_page(signature) is not None


def _is_opus_head(packet: bytes) -> bool:
    if len(packet) < 19 or packet[:8] != b"OpusHead" or packet[8] != 1 or packet[9] == 0:
        return False
    channel_count = packet[9]
    mapping_family = packet[18]
    if mapping_family == 0:
        return len(packet) == 19
    if len(packet) != 21 + channel_count:
        return False
    stream_count, coupled_count = packet[19], packet[20]
    if stream_count == 0 or coupled_count > stream_count:
        return False
    return all(
        channel_map == 0xFF or channel_map < stream_count + coupled_count
        for channel_map in packet[21:]
    )


def _is_opus(signature: bytes, _total_size: int) -> bool:
    page = _read_ogg_first_page(signature)
    if page is None:
        return False
    header_type, sequence, body, lacing_values = page
    if header_type & 0b11 != 0b10 or sequence != 0:
        return False
    packet = _first_ogg_packet(body, lacing_values)
    return packet is not None and _is_opus_head(packet)


def _iso_base_media_brand(signature: bytes) -> bytes | None:
    if len(signature) < 12 or signature[4:8] != b"ftyp":
        return None
    # ftyp всегда занимает как минимум 8 байт: длину и тип box.
    if int.from_bytes(signature[:4], byteorder="big") < 8:
        return None
    return signature[8:12]


_GENERIC_ISO_BASE_MEDIA_BRANDS = {
    b"isom",
    b"iso2",
    b"mp41",
    b"mp42",
    b"avc1",
    b"dash",
    b"MSNV",
    b"3gp4",
    b"3gp5",
    b"3g2a",
    b"3g2b",
    b"F4V ",
    b"f4v ",
    b"mp71",
}


def _is_m4a(signature: bytes, _total_size: int) -> bool:
    # Некоторые рекордеры записывают m4a с общим ISO brand (например, isom/mp42),
    # а не M4A. В обоих случаях FFmpeg безопасно проверит и извлечёт аудиодорожку.
    return _iso_base_media_brand(signature) in {
        b"M4A ",
        b"M4B ",
        b"M4P ",
        b"f4a ",
        b"F4A ",
        *_GENERIC_ISO_BASE_MEDIA_BRANDS,
    }


def _is_mp4(signature: bytes, _total_size: int) -> bool:
    return _iso_base_media_brand(signature) in _GENERIC_ISO_BASE_MEDIA_BRANDS


def _is_mov(signature: bytes, _total_size: int) -> bool:
    return _iso_base_media_brand(signature) == b"qt  "


_MEDIA_FORMATS: dict[str, _MediaFormat] = {
    ".webm": _MediaFormat(".webm", "video/webm", _is_webm),
    ".mp3": _MediaFormat(".mp3", "audio/mpeg", _is_mp3),
    ".wav": _MediaFormat(".wav", "audio/wav", _is_wav),
    ".m4a": _MediaFormat(".m4a", "audio/mp4", _is_m4a),
    ".aac": _MediaFormat(".aac", "audio/aac", _is_aac),
    ".ogg": _MediaFormat(".ogg", "audio/ogg", _is_ogg),
    ".opus": _MediaFormat(".opus", "audio/ogg", _is_opus),
    ".mp4": _MediaFormat(".mp4", "video/mp4", _is_mp4),
    ".mov": _MediaFormat(".mov", "video/quicktime", _is_mov),
}


def _repository(request: Request) -> JobRepository:
    return request.app.state.jobs_repository


def _job_projection(job: Job, runs: list[JobRun] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": job.id,
        "source_name": job.source_name,
        "status": job.status,
        "current_run_id": job.current_run_id,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
    if runs is not None:
        payload["runs"] = [_run_projection(run) for run in runs]
    return payload


def _run_projection(run: JobRun) -> dict[str, object]:
    return {
        "id": run.id,
        "status": run.status,
        "stage": run.stage,
        "attempt": run.attempt,
        "max_attempts": run.max_attempts,
        "cancel_requested": run.cancel_requested,
        "error_code": run.error_code,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "diagnostics": _run_diagnostics(run),
    }


def _duration_seconds(start: str | None, end: str | None) -> float | None:
    if start is None or end is None:
        return None
    try:
        elapsed = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except ValueError:
        return None
    return round(elapsed, 3) if elapsed >= 0 else None


def _safe_pack_name(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", value):
        return value
    return None


_REASON_TEXT = {
    "worker_processing_error": "Локальная обработка завершилась ошибкой",
    "worker_heartbeat_expired": "Связь с обработчиком была прервана",
    "retry_exhausted": "Попытки обработки исчерпаны",
}


def _run_diagnostics(run: JobRun) -> dict[str, object]:
    """Проецирует только разрешённые данные без путей, manifest и ошибок worker."""

    data = run.diagnostics_data or {}
    raw_packs = data.get("packs")
    packs = raw_packs if isinstance(raw_packs, dict) else {}
    stages = data.get("stages")
    stages = stages if isinstance(stages, dict) else {}
    group_count = None
    for stage_name in ("publishing", "asr", "matching", "diarizing"):
        final_stage = stages.get(stage_name)
        if isinstance(final_stage, dict):
            value = final_stage.get("local_speaker_group_count")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                group_count = value
                break
    events: list[dict[str, object]] = []
    raw_events = data.get("stage_events")
    if isinstance(raw_events, list):
        for event in raw_events:
            if not isinstance(event, dict) or event.get("stage") not in {
                "extracting_audio", "asr", "diarizing", "matching", "publishing"
            }:
                continue
            started_at = event.get("started_at")
            completed_at = event.get("completed_at")
            if not isinstance(started_at, str):
                continue
            if completed_at is not None and not isinstance(completed_at, str):
                continue
            attempt = event.get("attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                continue
            events.append({
                "attempt": attempt,
                "stage": event["stage"],
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_seconds": _duration_seconds(started_at, completed_at),
            })
    audio_duration_ms = data.get("audio_duration_ms")
    audio_duration = (
        round(audio_duration_ms / 1000, 3)
        if isinstance(audio_duration_ms, int)
        and not isinstance(audio_duration_ms, bool)
        and audio_duration_ms >= 0
        else None
    )
    return {
        "status": run.status,
        "current_stage": run.stage if run.status is JobStatus.PROCESSING else None,
        "reason_code": run.error_code if run.error_code in _REASON_TEXT else None,
        "reason": _REASON_TEXT.get(run.error_code),
        "attempt": run.attempt,
        "max_attempts": run.max_attempts,
        "retry_count": max(0, run.attempt - 1),
        "audio_duration_seconds": audio_duration,
        "queue_duration_seconds": _duration_seconds(run.created_at, run.started_at),
        "total_duration_seconds": _duration_seconds(run.started_at, run.completed_at),
        "stages": events,
        "asr_model": _safe_pack_name(packs.get("asr_model")),
        "asr_pack_id": _safe_pack_name(packs.get("asr_pack_id") or run.asr_pack_id),
        "vad_pack_id": _safe_pack_name(packs.get("vad_pack_id")),
        "voice_pack_id": _safe_pack_name(packs.get("voice_pack_id") or run.voice_pack_id),
        "local_speaker_group_count": group_count,
    }


def _encode_cursor(job: Job) -> str:
    payload = json.dumps([job.created_at, job.id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[str, str]:
    try:
        if len(value) > 512 or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
            raise ValueError("Недопустимый формат курсора")
        payload = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        decoded = json.loads(payload)
        if (
            not isinstance(decoded, list)
            or len(decoded) != 2
            or any(not isinstance(item, str) or not item for item in decoded)
        ):
            raise ValueError("Недопустимое содержимое курсора")
        created_at, job_id = decoded
        if datetime.fromisoformat(created_at).tzinfo is None:
            raise ValueError("В курсоре отсутствует часовой пояс")
        return created_at, job_id
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
    ) as error:
        raise ApiError(422, "invalid_cursor", "Некорректный курсор списка заданий") from error


def _find_owned_job(repository: JobRepository, job_id: str, owner_id: str) -> Job:
    try:
        return repository.get_job(job_id, owner_id)
    except KeyError as error:
        raise ApiError(404, "not_found", "Задание не найдено") from error


def _safe_path_component(value: str) -> str | None:
    """Допускает только один компонент пути для идентификаторов из хранилища."""

    component = Path(value)
    if (
        not value
        or value in {".", ".."}
        or component.name != value
        or component.drive
        or component.root
    ):
        return None
    return value


def _safe_descendant(root: Path, candidate: Path) -> Path | None:
    """Разрешает существующий либо будущий путь только внутри заданного root."""

    try:
        resolved_root = root.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
        resolved_candidate.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved_candidate


def _job_directory(request: Request, job: Job) -> Path | None:
    job_id = _safe_path_component(job.id)
    if job_id is None:
        return None
    jobs_root = request.app.state.settings.data_dir / "jobs"
    return _safe_descendant(jobs_root, jobs_root / job_id)


def _normalized_audio_path(request: Request, *, job: Job, owner_id: str) -> Path | None:
    """Находит готовый WAV только в каталоге запусков того же приватного задания."""

    job_directory = _job_directory(request, job)
    if job_directory is None:
        return None
    for run in _repository(request).list_runs(job_id=job.id, owner_id=owner_id):
        if run.status is not JobStatus.READY:
            continue
        run_id = _safe_path_component(run.id)
        if run_id is None:
            continue
        normalized = _safe_descendant(
            job_directory,
            job_directory / "runs" / run_id / "normalized.wav",
        )
        if normalized is not None and normalized.is_file():
            return normalized
    return None


def _source_audio_path(request: Request, *, job: Job) -> tuple[Path, _MediaFormat] | None:
    """Возвращает исходный контейнер, если его путь остался в private data-root задания."""

    job_directory = _job_directory(request, job)
    if job_directory is None:
        return None
    source_path = _safe_descendant(job_directory, Path(job.source_path))
    if source_path is None or not source_path.is_file():
        return None
    media_format = _MEDIA_FORMATS.get(source_path.suffix.lower())
    if media_format is None:
        return None
    return source_path, media_format


def _source_metadata(filename: str | None) -> tuple[str, _MediaFormat]:
    """Возвращает безопасное отображаемое имя и формат, заданный расширением."""

    if not filename:
        raise ApiError(422, "unsupported_media", "Укажите имя поддерживаемого медиафайла")
    # UploadFile может содержать путь из клиента. Убираем оба варианта разделителя,
    # в том числе когда клиент и сервер работают на разных ОС.
    candidate = Path(filename.replace("\\", "/")).name
    candidate = "".join(
        character for character in candidate if character >= " " and character != "\x7f"
    )
    candidate = candidate.strip()
    suffix = Path(candidate).suffix.lower()
    media_format = _MEDIA_FORMATS.get(suffix)
    if media_format is None:
        extensions = ", ".join(_MEDIA_FORMATS)
        raise ApiError(422, "unsupported_media", f"Поддерживаются файлы: {extensions}")
    stem = Path(candidate).stem.strip(" .")
    if not stem:
        raise ApiError(422, "invalid_media", "Имя медиафайла некорректно")
    # Имя никогда не становится путём на диске, но ограничение защищает БД и HTTP-заголовок.
    return f"{stem[:240]}{media_format.extension}", media_format


def _discard_partial_upload(destination: Path) -> None:
    destination.unlink(missing_ok=True)
    try:
        destination.parent.rmdir()
    except OSError:
        # Каталог создаётся исключительно для этой загрузки. Если диск или ОС не
        # дали его удалить, повторная загрузка всё равно получит новый UUID job.
        pass


async def _save_upload(upload: UploadFile, destination: Path, media_format: _MediaFormat) -> None:
    """Потоково сохраняет upload, проверяя размер и сигнатуру выбранного контейнера."""

    written = 0
    signature = b""
    try:
        destination.parent.mkdir(parents=True, exist_ok=False)
        with destination.open("xb") as output:
            while chunk := await upload.read(1024 * 1024):
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    raise ApiError(
                        413,
                        "upload_too_large",
                        "Размер файла превышает допустимый лимит",
                    )
                if len(signature) < _SIGNATURE_WINDOW_BYTES:
                    signature += chunk[: _SIGNATURE_WINDOW_BYTES - len(signature)]
                output.write(chunk)
    except Exception:
        _discard_partial_upload(destination)
        raise
    finally:
        await upload.close()
    if written == 0 or not media_format.signature_matches(signature, written):
        _discard_partial_upload(destination)
        raise ApiError(
            422,
            "invalid_media",
            f"Содержимое файла не соответствует формату {media_format.extension}",
        )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_job(
    request: Request,
    session: CsrfSessionDependency,
    media: UploadFile = _UPLOAD_FILE,
) -> dict[str, object]:
    """Загружает исходную запись только в private data-root владельца."""

    repository = _repository(request)
    settings = request.app.state.settings
    job_id = str(uuid4())
    try:
        source_name, media_format = _source_metadata(media.filename)
    except ApiError:
        await media.close()
        raise
    source_path = settings.data_dir / "jobs" / job_id / f"source{media_format.extension}"
    await _save_upload(media, source_path, media_format)
    try:
        job = repository.create_job(
            job_id=job_id,
            owner_id=session.user_id,
            source_name=source_name,
            source_path=str(source_path),
        )
    except Exception:
        _discard_partial_upload(source_path)
        raise
    return {"job": _job_projection(job)}


@router.get("")
def list_jobs(
    request: Request,
    session: SessionDependency,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> dict[str, object]:
    repository = _repository(request)
    page, has_more = repository.list_jobs_page(
        session.user_id, limit=limit, cursor=_decode_cursor(cursor) if cursor is not None else None
    )
    items = []
    for job, run in page:
        item = _job_projection(job)
        item["current_run"] = _run_projection(run) if run is not None else None
        items.append(item)
    return {"items": items, "next_cursor": _encode_cursor(page[-1][0]) if has_more else None}


@router.get("/{job_id}")
def get_job(request: Request, job_id: str, session: SessionDependency) -> dict[str, object]:
    repository = _repository(request)
    job = _find_owned_job(repository, job_id, session.user_id)
    runs = repository.list_runs(job_id=job.id, owner_id=session.user_id)
    return {"job": _job_projection(job, runs)}


@router.get("/{job_id}/vocabulary/suggestions")
def get_vocabulary_suggestions(
    request: Request,
    job_id: str,
    session: SessionDependency,
    expected_revision_number: int = Query(ge=1),
) -> dict[str, object]:
    """Предлагает словарные исправления текущей ревизии без её изменения."""

    _find_owned_job(_repository(request), job_id, session.user_id)
    try:
        revision = request.app.state.editor_repository.get_current_revision(
            owner_id=session.user_id, job_id=job_id
        )
    except KeyError as error:
        raise ApiError(409, "transcript_unavailable", "Транскрипция ещё не опубликована") from error
    if revision.info.number != expected_revision_number:
        raise ApiError(
            409,
            "revision_conflict",
            "Транскрипция была изменена; обновите редактор",
            {
                "expected_revision_number": expected_revision_number,
                "current_revision_number": revision.info.number,
            },
        )
    terms = request.app.state.vocabulary_service.list_terms()
    return {
        "revision_number": revision.info.number,
        "items": suggest_for_revision(revision, terms),
    }


@router.post("/{job_id}/runs", status_code=status.HTTP_202_ACCEPTED)
def create_run(
    request: Request,
    job_id: str,
    session: CsrfSessionDependency,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    """Ставит новый immutable run в очередь, когда локальный pack готов."""

    repository = _repository(request)
    job = _find_owned_job(repository, job_id, session.user_id)
    if not request.app.state.asr_ready:
        raise ApiError(
            503,
            "asr_pack_unavailable",
            "Локальный ASR model pack ещё не прошёл проверку готовности",
        )
    if request.app.state.dispatcher_status() != "running":
        raise ApiError(503, "worker_unavailable", "Локальный обработчик сейчас недоступен")
    if not idempotency_key or len(idempotency_key) > 128:
        raise ApiError(422, "idempotency_key_required", "Передайте заголовок Idempotency-Key")
    try:
        run = repository.create_run(
            run_id=str(uuid4()),
            job_id=job.id,
            request_id=idempotency_key,
            asr_pack_id=request.app.state.asr_pack_id,
            voice_pack_id=request.app.state.voice_pack_id,
            diagnostics_packs=request.app.state.diagnostic_packs,
        )
    except InvalidRunState as error:
        raise ApiError(
            409, "active_run_exists", "Запись уже ожидает обработки или обрабатывается"
        ) from error
    return {"run": _run_projection(run)}


@router.post("/{job_id}/cancel")
def cancel_job(request: Request, job_id: str, session: CsrfSessionDependency) -> dict[str, object]:
    repository = _repository(request)
    job = _find_owned_job(repository, job_id, session.user_id)
    if job.current_run_id is None:
        raise ApiError(409, "no_active_run", "У задания нет запуска для отмены")
    try:
        run = repository.get_run_for_job(
            run_id=job.current_run_id, job_id=job.id, owner_id=session.user_id
        )
        cancelled = repository.request_cancel(run.id)
    except KeyError as error:
        raise ApiError(404, "not_found", "Запуск задания не найден") from error
    return {"run": _run_projection(cancelled)}


@router.get("/{job_id}/audio")
def get_audio(request: Request, job_id: str, session: SessionDependency) -> FileResponse:
    repository = _repository(request)
    job = _find_owned_job(repository, job_id, session.user_id)
    normalized = _normalized_audio_path(request, job=job, owner_id=session.user_id)
    if normalized is not None:
        return FileResponse(
            normalized,
            media_type="audio/wav",
            filename=f"{Path(job.source_name).stem}.wav",
            content_disposition_type="inline",
        )
    source = _source_audio_path(request, job=job)
    if source is None:
        raise ApiError(404, "not_found", "Аудиофайл не найден")
    source_path, media_format = source
    return FileResponse(
        source_path,
        media_type=media_format.media_type,
        filename=job.source_name,
        content_disposition_type="inline",
    )


@router.get("/{job_id}/source")
def get_source(request: Request, job_id: str, session: SessionDependency) -> dict[str, object]:
    repository = _repository(request)
    job = _find_owned_job(repository, job_id, session.user_id)
    if job.current_run_id is None:
        return {"segments": [], "word_timing_status": "unavailable"}
    segments = repository.source_segments(job.current_run_id)
    return {
        "segments": [
            {
                "id": str(row["id"]),
                "ordinal": row["ordinal"],
                "start_ms": row["start_ms"],
                "end_ms": row["end_ms"],
                "text": row["raw_text"],
                "word_timing_status": row["word_timing_status"],
            }
            for row in segments
        ]
    }


@router.get("/{job_id}/speaker-groups")
def get_speaker_groups(
    request: Request,
    job_id: str,
    session: SessionDependency,
) -> dict[str, object]:
    _find_owned_job(_repository(request), job_id, session.user_id)
    try:
        revision = request.app.state.editor_repository.get_current_revision(
            owner_id=session.user_id,
            job_id=job_id,
        )
    except KeyError:
        return {"items": []}
    return {
        "items": [
            {
                "id": group.speaker_group_id,
                "label": group.label_snapshot,
                "assignment_status": group.assignment_status,
                "requires_review": any(
                    group.speaker_group_id in segment.speaker_group_ids and segment.requires_review
                    for segment in revision.segments
                ),
            }
            for group in revision.groups
        ]
    }
