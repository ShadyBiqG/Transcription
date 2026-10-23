from __future__ import annotations

import pytest
from conftest import login, register
from fastapi.testclient import TestClient


def _headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("local_voice_transcriber_csrf") or ""}


def _webm() -> bytes:
    ebml_header = (
        b"\x42\x86\x81\x01"  # Версия EBML.
        b"\x42\xf7\x81\x01"  # Минимальная версия чтения EBML.
        b"\x42\xf2\x81\x04"  # Максимальная длина идентификатора.
        b"\x42\xf3\x81\x08"  # Максимальная длина размера.
        b"\x42\x82\x84webm"  # Тип документа.
        b"\x42\x87\x81\x04"  # Версия типа документа.
        b"\x42\x85\x81\x02"  # Минимальная версия чтения типа документа.
    )
    return b"\x1a\x45\xdf\xa3\x9f" + ebml_header + b"\x18\x53\x80\x67\xff"


def _mp3() -> bytes:
    return b"ID3\x04\x00\x00\x00\x00\x00\x00" + _mp3_frame()


def _mp3_frame() -> bytes:
    return b"\xff\xfb\x90\x64"


def _synchsafe(value: int) -> bytes:
    assert 0 <= value < 1 << 28
    return bytes(value >> shift & 0x7F for shift in (21, 14, 7, 0))


def _mp3_with_id3_tag(tag_size: int, trailing: bytes = b"") -> bytes:
    return b"ID3\x04\x00\x00" + _synchsafe(tag_size) + b"\x00" * tag_size + trailing


def _wav() -> bytes:
    return b"RIFF\x24\x00\x00\x00WAVEfmt " + b"test-wav"


def _m4a() -> bytes:
    return b"\x00\x00\x00\x18ftypM4A " + b"test-m4a"


def _aac() -> bytes:
    return b"\xff\xf1\x50\x80" + b"test-aac"


def _ogg() -> bytes:
    return _ogg_page(b"\x01vorbis")


def _opus() -> bytes:
    opus_head = b"OpusHead\x01\x02\x00\x00\x80\xbb\x00\x00\x00\x00\x00"
    return _ogg_page(opus_head)


def _multichannel_opus() -> bytes:
    opus_head = b"OpusHead\x01\x02\x00\x00\x80\xbb\x00\x00\x00\x00\x01\x01\x01\x00\x01"
    return _ogg_page(opus_head)


def _ogg_crc32(page: bytes | bytearray) -> int:
    value = 0
    for byte in page:
        value ^= byte << 24
        for _ in range(8):
            value = (value << 1) ^ (0x04C11DB7 if value & 0x80000000 else 0)
            value &= 0xFFFFFFFF
    return value


def _ogg_page(packet: bytes) -> bytes:
    page = bytearray(
        b"OggS\x00\x02"  # Сигнатура, версия и начало логического потока.
        + b"\x00" * 8  # Позиция гранулы.
        + b"\x01\x00\x00\x00"  # Серийный номер потока.
        + b"\x00\x00\x00\x00"  # Номер страницы.
        + b"\x00" * 4  # Место для контрольной суммы.
        + b"\x01"  # Одно значение таблицы сегментов.
        + bytes([len(packet)])
        + packet
    )
    page[22:26] = _ogg_crc32(page).to_bytes(4, byteorder="little")
    return bytes(page)


def _mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypisom" + b"test-mp4"


def _mov() -> bytes:
    return b"\x00\x00\x00\x18ftypqt  " + b"test-mov"


def _create_job(client: TestClient) -> str:
    response = client.post(
        "/api/v1/jobs",
        headers=_headers(client),
        files={"media": ("meeting.webm", _webm(), "video/webm")},
    )
    assert response.status_code == 201, response.text
    return response.json()["job"]["id"]


def test_upload_and_list_are_private(client: TestClient) -> None:
    register(client, "owner")
    login(client, "owner")
    job_id = _create_job(client)

    listed = client.get("/api/v1/jobs")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [job_id]
    assert listed.json()["next_cursor"] is None
    assert listed.json()["items"][0]["current_run"] is None

    other = TestClient(client.app)
    with other:
        register(other, "other")
        login(other, "other")
        assert other.get(f"/api/v1/jobs/{job_id}").status_code == 404
        assert other.get(f"/api/v1/jobs/{job_id}/audio").status_code == 404
        assert other.get(f"/api/v1/jobs/{job_id}/source").status_code == 404


def test_upload_requires_csrf_and_valid_media(client: TestClient) -> None:
    register(client)
    login(client)

    no_csrf = client.post("/api/v1/jobs", files={"media": ("meeting.webm", _webm())})
    assert no_csrf.status_code == 403

    invalid = client.post(
        "/api/v1/jobs",
        headers=_headers(client),
        files={"media": ("meeting.webm", b"not-webm")},
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_media"


@pytest.mark.parametrize(
    ("filename", "payload", "content_type"),
    [
        ("meeting.webm", _webm(), "video/webm"),
        ("meeting.mp3", _mp3(), "audio/mpeg"),
        ("recorder.mp3", _mp3_frame(), "audio/mpeg"),
        ("meeting.wav", _wav(), "audio/wav"),
        ("meeting.m4a", _m4a(), "audio/mp4"),
        ("recorder.m4a", _mp4(), "audio/mp4"),
        ("meeting.aac", _aac(), "audio/aac"),
        ("meeting.ogg", _ogg(), "audio/ogg"),
        ("meeting.opus", _opus(), "audio/ogg"),
        ("surround.opus", _multichannel_opus(), "audio/ogg"),
        ("meeting.mp4", _mp4(), "video/mp4"),
        ("meeting.mov", _mov(), "video/quicktime"),
    ],
)
def test_upload_accepts_supported_media_only_with_matching_signature(
    client: TestClient, filename: str, payload: bytes, content_type: str
) -> None:
    register(client)
    login(client)

    response = client.post(
        "/api/v1/jobs",
        headers=_headers(client),
        files={"media": (filename, payload, content_type)},
    )

    assert response.status_code == 201, response.text
    job = response.json()["job"]
    assert job["source_name"] == filename
    audio = client.get(f"/api/v1/jobs/{job['id']}/audio")
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith(content_type)


def test_upload_accepts_id3_tag_larger_than_signature_window_when_audio_follows(
    client: TestClient,
) -> None:
    register(client)
    login(client)
    payload = _mp3_with_id3_tag(64 * 1024, _mp3_frame())

    response = client.post(
        "/api/v1/jobs",
        headers=_headers(client),
        files={"media": ("meeting.mp3", payload, "audio/mpeg")},
    )

    assert response.status_code == 201, response.text


def test_upload_rejects_large_id3_tag_without_bytes_for_audio_frame(client: TestClient) -> None:
    register(client)
    login(client)
    payload = _mp3_with_id3_tag(64 * 1024)

    response = client.post(
        "/api/v1/jobs",
        headers=_headers(client),
        files={"media": ("meeting.mp3", payload, "audio/mpeg")},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_media"


@pytest.mark.parametrize(
    ("filename", "payload", "error_code"),
    [
        ("meeting.mp3", _webm(), "invalid_media"),
        ("meeting.mp4", _wav(), "invalid_media"),
        ("meeting.opus", _ogg(), "invalid_media"),
        ("meeting.webm", b"not-a-container", "invalid_media"),
        ("meeting.exe", _webm(), "unsupported_media"),
    ],
)
def test_upload_rejects_wrong_or_unsupported_media(
    client: TestClient, filename: str, payload: bytes, error_code: str
) -> None:
    register(client)
    login(client)

    response = client.post(
        "/api/v1/jobs",
        headers=_headers(client),
        files={"media": (filename, payload, "application/octet-stream")},
    )

    assert response.status_code == 422
    assert response.json()["code"] == error_code
    assert client.get("/api/v1/jobs").json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("meeting.webm", b"\x1a\x45\xdf\xa3\x42\x82\x84webm" + b"arbitrary"),
        ("meeting.mp3", b"ID3\x04\x00\x00" + b"arbitrary"),
        ("meeting.opus", _ogg_page(b"arbitrary-OpusHead")),
    ],
)
def test_upload_rejects_unstructured_magic_marker_payloads(
    client: TestClient, filename: str, payload: bytes
) -> None:
    register(client)
    login(client)

    response = client.post(
        "/api/v1/jobs",
        headers=_headers(client),
        files={"media": (filename, payload, "application/octet-stream")},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_media"
    assert client.get("/api/v1/jobs").json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize(
    ("filename", "payload"), [("meeting.ogg", _ogg()), ("meeting.opus", _opus())]
)
def test_upload_rejects_ogg_with_invalid_page_crc(
    client: TestClient, filename: str, payload: bytes
) -> None:
    register(client)
    login(client)
    corrupted = bytearray(payload)
    corrupted[22] ^= 0x01

    response = client.post(
        "/api/v1/jobs",
        headers=_headers(client),
        files={"media": (filename, bytes(corrupted), "audio/ogg")},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_media"


def test_upload_sanitizes_client_path_before_exposing_source_name(client: TestClient) -> None:
    register(client)
    login(client)

    response = client.post(
        "/api/v1/jobs",
        headers=_headers(client),
        files={"media": (r"C:\\private\\meeting.MP3", _mp3(), "audio/mpeg")},
    )

    assert response.status_code == 201, response.text
    assert response.json()["job"]["source_name"] == "meeting.mp3"


def test_run_is_safely_blocked_without_verified_local_pack(client: TestClient) -> None:
    register(client)
    login(client)
    job_id = _create_job(client)

    response = client.post(
        f"/api/v1/jobs/{job_id}/runs",
        headers={**_headers(client), "Idempotency-Key": "client-request-1"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "asr_pack_unavailable"


def test_job_list_uses_private_stable_cursor_and_safe_current_run(
    logged_in_client: TestClient,
) -> None:
    client = logged_in_client
    owner_id = client.get("/api/v1/auth/me").json()["user"]["id"]
    repository = client.app.state.jobs_repository
    for number in range(53):
        repository.create_job(
            job_id=f"job-{number:03}",
            owner_id=owner_id,
            source_name=f"Запись {number}.webm",
            source_path=f"/private/secret-{number}.webm",
        )
    run = repository.create_run(
        run_id="run-visible", job_id="job-052", request_id="first", asr_pack_id="fake-pack"
    )

    first = client.get("/api/v1/jobs?limit=20")
    assert first.status_code == 200, first.text
    first_payload = first.json()
    assert len(first_payload["items"]) == 20
    assert first_payload["next_cursor"]
    current_run = first_payload["items"][0]["current_run"]
    assert current_run == {
        "id": run.id,
        "status": "queued",
        "stage": None,
        "attempt": 0,
        "max_attempts": 3,
        "cancel_requested": False,
        "error_code": None,
        "created_at": run.created_at,
        "started_at": None,
        "completed_at": None,
    }
    assert "/private/" not in first.text
    assert "fake-pack" not in first.text

    repository.create_job(
        job_id="newest", owner_id=owner_id, source_name="Новая.webm", source_path="/private/new"
    )
    seen = [item["id"] for item in first_payload["items"]]
    cursor = first_payload["next_cursor"]
    while cursor is not None:
        response = client.get("/api/v1/jobs", params={"limit": 20, "cursor": cursor})
        assert response.status_code == 200, response.text
        payload = response.json()
        seen.extend(item["id"] for item in payload["items"])
        cursor = payload["next_cursor"]
    assert len(seen) == len(set(seen)) == 53
    assert "newest" not in seen

    other = TestClient(client.app)
    with other:
        register(other, "other")
        login(other, "other")
        private = other.get("/api/v1/jobs", params={"cursor": first_payload["next_cursor"]})
        assert private.status_code == 200
        assert private.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"cursor": "bad!"}])
def test_job_list_rejects_invalid_pagination(
    logged_in_client: TestClient, params: dict[str, object]
) -> None:
    response = logged_in_client.get("/api/v1/jobs", params=params)
    assert response.status_code == 422


def test_run_requires_running_worker_even_with_ready_pack(logged_in_client: TestClient) -> None:
    client = logged_in_client
    job_id = _create_job(client)
    client.app.state.asr_ready = True
    client.app.state.asr_pack_id = "local-fake-pack"
    client.app.state.dispatcher_status = lambda: "stopped"
    response = client.post(
        f"/api/v1/jobs/{job_id}/runs",
        headers={**_headers(client), "Idempotency-Key": "first-request"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "worker_unavailable"


@pytest.mark.parametrize("processing", [False, True])
def test_active_run_rejects_new_key_and_retries_same_key(
    logged_in_client: TestClient, processing: bool
) -> None:
    client = logged_in_client
    job_id = _create_job(client)
    assert client.get(f"/api/v1/jobs/{job_id}").json()["job"]["status"] == "uploaded"
    client.app.state.asr_ready = True
    client.app.state.asr_pack_id = "local-fake-pack"
    client.app.state.dispatcher_status = lambda: "running"
    endpoint = f"/api/v1/jobs/{job_id}/runs"
    first_headers = {**_headers(client), "Idempotency-Key": "first-request"}

    first = client.post(endpoint, headers=first_headers)
    assert first.status_code == 202
    run_id = first.json()["run"]["id"]
    repository = client.app.state.jobs_repository
    if processing:
        assert repository.claim_next_run(worker_id="fake-worker", lease_seconds=30) is not None
    retry = client.post(endpoint, headers=first_headers)
    assert retry.status_code == 202
    assert retry.json()["run"]["id"] == run_id
    duplicate = client.post(
        endpoint, headers={**_headers(client), "Idempotency-Key": "second-request"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "active_run_exists"
    assert "message" in duplicate.json()
    owner_id = client.get("/api/v1/auth/me").json()["user"]["id"]
    assert len(repository.list_runs(job_id=job_id, owner_id=owner_id)) == 1
    if processing:
        repository.mark_ready(run_id=run_id, worker_id="fake-worker")
    else:
        repository.request_cancel(run_id)
    next_run = client.post(
        endpoint, headers={**_headers(client), "Idempotency-Key": "second-request"}
    )
    assert next_run.status_code == 202
    assert next_run.json()["run"]["id"] != run_id
