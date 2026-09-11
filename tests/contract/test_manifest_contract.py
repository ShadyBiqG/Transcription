from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from tests.conftest import register_user
from tests.integration.test_job_flow import wait_for_terminal
from transcription_service.api import create_app


def test_completed_job_opens_html_and_has_valid_manifest(settings, fake_runner):
    with TestClient(create_app(settings, fake_runner)) as client:
        register_user(client)
        created = client.post(
            "/api/v1/jobs",
            files={"file": ("meeting.webm", b"video", "video/webm")},
        ).json()
        job = wait_for_terminal(client, created["id"])
        transcript = client.get(job["transcript_url"])
        manifest_response = client.get(job["manifest_url"])

    assert transcript.status_code == 200
    assert transcript.headers["content-type"].startswith("text/html")
    assert transcript.headers["content-disposition"].startswith("inline")
    assert transcript.headers["content-security-policy"].startswith("default-src 'none'")
    assert "S00:" in transcript.text
    manifest = manifest_response.json()
    schema_path = Path(__file__).parent / "schemas" / "job-manifest.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(manifest)
    assert manifest["schema_version"] == 2
    assert manifest["transcription"]["speaker_detection"] == "auto"
    assert manifest["artifacts"]["transcript_html"] == "transcript.html"


def test_queued_job_cannot_download_transcript(settings, fake_runner):
    with TestClient(create_app(settings, fake_runner, start_worker=False)) as client:
        register_user(client)
        created = client.post(
            "/api/v1/jobs",
            files={"file": ("meeting.webm", b"video", "video/webm")},
        ).json()
        response = client.get(f"/api/v1/jobs/{created['id']}/transcript")
    assert response.status_code == 409
