from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from tests.integration.test_job_flow import wait_for_terminal
from transcription_service.api import create_app


def test_completed_job_downloads_vtt_and_valid_manifest(settings, fake_runner):
    with TestClient(create_app(settings, fake_runner)) as client:
        created = client.post(
            "/api/v1/jobs",
            files={"file": ("meeting.webm", b"video", "video/webm")},
        ).json()
        job = wait_for_terminal(client, created["id"])
        transcript = client.get(job["transcript_url"])
        manifest_response = client.get(job["manifest_url"])

    assert transcript.status_code == 200
    assert transcript.text.startswith("WEBVTT")
    manifest = manifest_response.json()
    schema_path = (
        Path(__file__).parents[2]
        / "specs"
        / "002-local-transcription-service"
        / "contracts"
        / "job-manifest.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(manifest)
    assert manifest["artifacts"]["transcript_vtt"] == "transcript.vtt"


def test_queued_job_cannot_download_transcript(settings, fake_runner):
    with TestClient(create_app(settings, fake_runner, start_worker=False)) as client:
        created = client.post(
            "/api/v1/jobs",
            files={"file": ("meeting.webm", b"video", "video/webm")},
        ).json()
        response = client.get(f"/api/v1/jobs/{created['id']}/transcript")
    assert response.status_code == 409
