from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from tests.conftest import register_user
from transcription_service.api import create_app
from transcription_service.attribution_repository import AttributionRepository
from transcription_service.database import JobRepository
from transcription_service.frames import ExtractedFrame
from transcription_service.models import UserRole
from transcription_service.routerai import RouterAIError, RouterAIResult


def _wait(client: TestClient, path: str, final: set[str]) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        payload = client.get(path).json()
        if payload["status"] in final:
            return payload
        time.sleep(0.03)
    raise AssertionError(f"Не дождались завершения {path}")


class FakeExtractor:
    def extract(self, source: Path, timestamp_ms: int, destination: Path) -> ExtractedFrame:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"jpeg-frame")
        return ExtractedFrame(
            destination,
            timestamp_ms,
            timestamp_ms,
            hashlib.sha256(b"jpeg-frame").hexdigest(),
        )


class FakeRouterAI:
    def __init__(self) -> None:
        self.base_url = "https://provider.test/v1"
        self.calls: list[str] = []
        self.frame_counts: list[int] = []

    async def fetch_models(self) -> list[dict]:
        return [
            {
                "id": model,
                "name": model,
                "architecture": {
                    "input_modalities": ["text", "image"],
                    "output_modalities": ["text"],
                },
                "supported_parameters": ["response_format", "structured_outputs"],
            }
            for model in ("vendor/primary", "vendor/fallback")
        ]

    async def analyze_frames(self, api_key, model, frame_paths, session_id, max_attempts=1):
        self.calls.append(model)
        self.frame_counts.append(len(frame_paths))
        if model == "vendor/primary":
            raise RouterAIError("primary unavailable", status_code=503, retryable=True)
        return RouterAIResult(
            results=[
                {
                    "frame_index": index,
                    "status": "detected",
                    "speaker_label": "Гость",
                    "confidence": 0.9,
                    "highlight_bbox": [0.1, 0.1, 0.4, 0.4],
                    "label_bbox": [0.1, 0.3, 0.2, 0.35],
                    "reason": "зелёная рамка",
                }
                for index in range(len(frame_paths))
            ],
            model=model,
            generation_id=None,
            provider_request_id="fake-request",
            usage={"input_units": 100, "output_units": 20, "total_units": 120},
        )

    async def fetch_generation(self, api_key, generation_id):
        return None

    async def test_key(self, api_key):
        return True


class IncompatiblePrimaryRouterAI(FakeRouterAI):
    async def analyze_frames(self, api_key, model, frame_paths, session_id, max_attempts=1):
        self.calls.append(model)
        self.frame_counts.append(len(frame_paths))
        if model == "vendor/primary":
            raise RouterAIError(
                "response_format json_schema is not supported",
                status_code=400,
                retryable=False,
            )
        return RouterAIResult(
            results=[
                {
                    "frame_index": index,
                    "status": "detected",
                    "speaker_label": "Гость",
                    "confidence": 0.9,
                    "highlight_bbox": [0.1, 0.1, 0.4, 0.4],
                    "label_bbox": [0.1, 0.3, 0.2, 0.35],
                    "reason": "зелёная рамка",
                }
                for index in range(len(frame_paths))
            ],
            model=model,
            generation_id=None,
            provider_request_id="fake-request",
            usage={"input_units": 100, "output_units": 20, "total_units": 120},
        )


def test_attribution_without_external_consent_creates_unknown_artifact(
    settings, fake_runner
) -> None:
    app = create_app(settings, fake_runner)
    with TestClient(app) as client:
        register_user(client)
        created = client.post(
            "/api/v1/jobs",
            files={"file": ("meeting.webm", b"fake-webm", "video/webm")},
            data={"language": "ru", "model": "precise", "speaker_detection": "auto"},
        ).json()
        job = _wait(client, f"/api/v1/jobs/{created['id']}", {"completed", "failed"})
        assert job["status"] == "completed"

        response = client.post(
            f"/api/v1/jobs/{job['id']}/attribution-runs",
            json={"external_processing_consent": False},
        )
        assert response.status_code == 202
        run_id = response.json()["id"]
        run_path = f"/api/v1/jobs/{job['id']}/attribution-runs/{run_id}"
        run = _wait(client, run_path, {"completed", "failed"})

        assert run["status"] == "completed"
        assert run["segments"][0]["status"] == "unknown"
        assert client.get(run["attribution_json_url"]).status_code == 200

        segment = run["segments"][0]
        edited = client.patch(
            f"{run_path}/segments/{segment['id']}",
            json={"speaker_label": "Гость"},
        )
        assert edited.status_code == 200
        artifact = client.get(run["attribution_json_url"]).json()
        schema_path = (
            Path(__file__).parents[1] / "contract" / "schemas" / "attribution.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(artifact)
        assert artifact["status"] == "completed"
        assert artifact["segments"][0]["speaker_label"] == "Гость"


def test_external_flow_uses_allowed_fallback_and_records_every_call(
    settings, fake_runner
) -> None:
    routerai = FakeRouterAI()
    app = create_app(
        settings,
        fake_runner,
        routerai=routerai,
        frame_extractor=FakeExtractor(),
    )
    with TestClient(app) as client:
        user = register_user(client, "admin@example.com")
        JobRepository(settings.database_path).set_user_role(user["id"], UserRole.ADMIN)
        assert client.post("/api/v1/admin/models/refresh").status_code == 200
        configured = client.patch(
            "/api/v1/admin/settings",
            json={
                "api_key": "routerai-test-secret",
                "provider_enabled": True,
                "primary_model_id": "vendor/primary",
                "fallback_model_id": "vendor/fallback",
                "allowed_model_ids": ["vendor/primary", "vendor/fallback"],
            },
        )
        assert configured.status_code == 200
        created = client.post(
            "/api/v1/jobs",
            files={"file": ("meeting.webm", b"fake-webm", "video/webm")},
            data={"language": "ru", "model": "precise", "speaker_detection": "auto"},
        ).json()
        job = _wait(client, f"/api/v1/jobs/{created['id']}", {"completed", "failed"})
        started = client.post(
            f"/api/v1/jobs/{job['id']}/attribution-runs",
            json={"external_processing_consent": True},
        ).json()
        path = f"/api/v1/jobs/{job['id']}/attribution-runs/{started['id']}"
        run = _wait(client, path, {"completed", "failed"})
        persisted_job = client.get(f"/api/v1/jobs/{job['id']}").json()
        persisted_list_item = client.get("/api/v1/jobs").json()[0]
        usage = client.get("/api/v1/admin/external-usage").json()
        filtered = client.get(
            "/api/v1/admin/external-usage",
            params={"model": "vendor/fallback", "status": "completed"},
        ).json()

    assert run["status"] == "completed"
    assert run["segments"][0]["speaker_label"] == "Гость"
    assert persisted_job["latest_attribution"]["id"] == run["id"]
    assert persisted_job["saved_attribution"]["id"] == run["id"]
    assert persisted_list_item["saved_attribution"]["attributed_transcript_url"].endswith(
        "/artifacts/html"
    )
    assert routerai.calls == ["vendor/primary", "vendor/fallback"]
    assert usage["calls"] == 2
    assert usage["failed_calls"] == 1
    assert filtered["calls"] == 1
    assert filtered["items"][0]["actual_model"] == "vendor/fallback"


@pytest.mark.parametrize(
    ("processing_mode", "expected_calls"),
    [("fast", 4), ("precise", 8)],
)
def test_attribution_processing_modes(
    settings, fake_runner, processing_mode: str, expected_calls: int
) -> None:
    routerai = FakeRouterAI()
    app = create_app(
        settings,
        fake_runner,
        routerai=routerai,
        frame_extractor=FakeExtractor(),
    )
    with TestClient(app) as client:
        user = register_user(client, "admin@example.com")
        JobRepository(settings.database_path).set_user_role(user["id"], UserRole.ADMIN)
        client.patch(
            "/api/v1/admin/settings",
            json={
                "api_key": "routerai-test-secret",
                "provider_enabled": True,
                "primary_model_id": "vendor/primary",
                "fallback_model_id": "vendor/fallback",
                "allowed_model_ids": ["vendor/primary", "vendor/fallback"],
            },
        )
        created = client.post(
            "/api/v1/jobs",
            files={"file": ("meeting.webm", b"fake-webm", "video/webm")},
        ).json()
        job = _wait(client, f"/api/v1/jobs/{created['id']}", {"completed", "failed"})
        transcript = settings.jobs_dir / job["id"] / "transcript.html"
        transcript.write_text(
            '<a name="ts_0_1000_S00">S00: Короткая реплика</a>'
            '<a name="ts_1000_9000_S00">S00: Репрезентативная реплика</a>'
            '<a name="ts_9000_13000_S00">S00: Средняя реплика</a>'
            '<a name="ts_13000_16000_S01">S01: Другой говорящий</a>',
            encoding="utf-8",
        )

        started = client.post(
            f"/api/v1/jobs/{job['id']}/attribution-runs",
            json={
                "external_processing_consent": True,
                "processing_mode": processing_mode,
            },
        ).json()
        run = _wait(
            client,
            f"/api/v1/jobs/{job['id']}/attribution-runs/{started['id']}",
            {"completed", "failed"},
        )
        edited = client.patch(
            f"/api/v1/jobs/{job['id']}/attribution-runs/{run['id']}"
            f"/segments/{run['segments'][0]['id']}",
            json={"speaker_label": "Исправленный"},
        )
        assert edited.status_code == 200
        corrected = client.get(
            f"/api/v1/jobs/{job['id']}/attribution-runs/{run['id']}"
        ).json()

    assert run["status"] == "completed"
    assert run["processing_mode"] == processing_mode
    assert len(run["segments"]) == 4
    assert {item["speaker_label"] for item in run["segments"]} == {"Гость"}
    assert routerai.calls == ["vendor/primary", "vendor/fallback"] * (expected_calls // 2)
    if processing_mode == "fast":
        assert routerai.frame_counts == [5, 5, 2, 2]
    else:
        assert routerai.frame_counts == [1, 1, 2, 2, 2, 2, 2, 2]
    expected_manual = 3 if processing_mode == "fast" else 1
    assert sum(item["status"] == "manual" for item in corrected["segments"]) == expected_manual
    assert sum(
        item["manual_label"] == "Исправленный" for item in corrected["segments"]
    ) == expected_manual


def test_precise_manual_run_inherits_fast_labels_without_model_calls(
    settings, fake_runner
) -> None:
    routerai = FakeRouterAI()
    app = create_app(
        settings,
        fake_runner,
        routerai=routerai,
        frame_extractor=FakeExtractor(),
    )
    with TestClient(app) as client:
        user = register_user(client, "admin@example.com")
        JobRepository(settings.database_path).set_user_role(user["id"], UserRole.ADMIN)
        client.patch(
            "/api/v1/admin/settings",
            json={
                "api_key": "routerai-test-secret",
                "provider_enabled": True,
                "primary_model_id": "vendor/primary",
                "fallback_model_id": "vendor/fallback",
                "allowed_model_ids": ["vendor/primary", "vendor/fallback"],
            },
        )
        created = client.post(
            "/api/v1/jobs",
            files={"file": ("meeting.webm", b"fake-webm", "video/webm")},
        ).json()
        job = _wait(client, f"/api/v1/jobs/{created['id']}", {"completed", "failed"})
        fast = client.post(
            f"/api/v1/jobs/{job['id']}/attribution-runs",
            json={"external_processing_consent": True, "processing_mode": "fast"},
        ).json()
        fast = _wait(
            client,
            f"/api/v1/jobs/{job['id']}/attribution-runs/{fast['id']}",
            {"completed", "failed"},
        )
        calls_before_manual = len(routerai.calls)

        manual = client.post(
            f"/api/v1/jobs/{job['id']}/attribution-runs",
            json={
                "external_processing_consent": False,
                "processing_mode": "precise",
                "source_run_id": fast["id"],
            },
        ).json()

    assert manual["status"] == "completed"
    assert manual["processing_mode"] == "precise"
    assert len(routerai.calls) == calls_before_manual
    assert manual["segments"]
    assert {item["manual_label"] for item in manual["segments"]} == {"Гость"}


def test_incompatible_model_is_not_retried_for_every_segment(
    settings, fake_runner
) -> None:
    routerai = IncompatiblePrimaryRouterAI()
    app = create_app(
        settings,
        fake_runner,
        routerai=routerai,
        frame_extractor=FakeExtractor(),
    )
    with TestClient(app) as client:
        user = register_user(client, "admin@example.com")
        JobRepository(settings.database_path).set_user_role(user["id"], UserRole.ADMIN)
        client.patch(
            "/api/v1/admin/settings",
            json={
                "api_key": "routerai-test-secret",
                "provider_enabled": True,
                "primary_model_id": "vendor/primary",
                "fallback_model_id": "vendor/fallback",
                "allowed_model_ids": ["vendor/primary", "vendor/fallback"],
            },
        )
        created = client.post(
            "/api/v1/jobs",
            files={"file": ("meeting.webm", b"fake-webm", "video/webm")},
        ).json()
        job = _wait(client, f"/api/v1/jobs/{created['id']}", {"completed", "failed"})
        transcript = settings.jobs_dir / job["id"] / "transcript.html"
        transcript.write_text(
            '<a name="ts_0_1000_S00">S00: Первая реплика</a>'
            '<a name="ts_1000_2000_S01">S01: Вторая реплика</a>',
            encoding="utf-8",
        )
        started = client.post(
            f"/api/v1/jobs/{job['id']}/attribution-runs",
            json={"external_processing_consent": True, "processing_mode": "precise"},
        ).json()
        run = _wait(
            client,
            f"/api/v1/jobs/{job['id']}/attribution-runs/{started['id']}",
            {"completed", "failed"},
        )

    assert run["status"] == "completed"
    assert routerai.calls == ["vendor/primary", "vendor/fallback", "vendor/fallback"]


def test_interrupted_attribution_is_resumed_without_losing_completed_segments(
    settings, fake_runner
) -> None:
    with TestClient(create_app(settings, fake_runner)) as client:
        user = register_user(client)
        created = client.post(
            "/api/v1/jobs",
            files={"file": ("meeting.webm", b"fake-webm", "video/webm")},
        ).json()
        job = _wait(client, f"/api/v1/jobs/{created['id']}", {"completed", "failed"})

    repository = AttributionRepository(settings.database_path)
    run = repository.create_run(
        job["id"], user["id"], True, "precise", {}, None, "RUB"
    )
    repository.claim_next()
    segment = repository.replace_segments(
        run["id"],
        [
            {
                "id": "00000000-0000-0000-0000-000000000101",
                "ordinal": 0,
                "source_anchor": "ts_0_1000_S00",
                "source_label": "S00",
                "start_ms": 0,
                "end_ms": 1000,
                "text": "Готовая реплика",
            }
        ],
    )[0]
    repository.set_attribution(segment["id"], "detected", "Гость", 0.9)
    repository.finish(
        run["id"],
        "failed",
        "service_restarted",
        "Определение говорящих прервано перезапуском сервиса",
    )

    assert repository.recover_running() == 1
    recovered = repository.get_run(run["id"])
    recovered_segment = repository.list_segments(run["id"])[0]
    assert recovered["status"] == "queued"
    assert recovered["error_code"] is None
    assert recovered_segment["status"] == "detected"
    assert recovered_segment["speaker_label"] == "Гость"
