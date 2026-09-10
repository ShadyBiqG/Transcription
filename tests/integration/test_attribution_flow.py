from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from tests.conftest import register_user
from transcription_service.api import create_app
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
            Path(__file__).parents[2]
            / "specs/003-speaker-frame-attribution/contracts/attribution.schema.json"
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
        usage = client.get("/api/v1/admin/external-usage").json()
        filtered = client.get(
            "/api/v1/admin/external-usage",
            params={"model": "vendor/fallback", "status": "completed"},
        ).json()

    assert run["status"] == "completed"
    assert run["segments"][0]["speaker_label"] == "Гость"
    assert routerai.calls == ["vendor/primary", "vendor/fallback"]
    assert usage["calls"] == 2
    assert usage["failed_calls"] == 1
    assert filtered["calls"] == 1
    assert filtered["items"][0]["actual_model"] == "vendor/fallback"
