"""Контракт явного обучения профиля из локальной группы."""

from __future__ import annotations

from conftest import login, register
from fastapi.testclient import TestClient


def _headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("local_voice_transcriber_csrf") or ""}


def test_group_sample_is_rejected_until_verified_local_voice_pack_is_configured(
    client: TestClient,
) -> None:
    register(client, "owner")
    login(client, "owner")

    response = client.post(
        "/api/v1/jobs/unknown/transcript/groups/local-speaker:0/samples",
        headers=_headers(client),
        json={"profile_id": "profile-1"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "voice_pack_unavailable"
