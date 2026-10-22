"""Контракт общего справочника: видимость профиля без чужих источников."""

from __future__ import annotations

from conftest import login, register
from fastapi.testclient import TestClient


def _headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("local_voice_transcriber_csrf") or ""}


def test_profile_is_shared_but_does_not_reveal_private_provenance(client: TestClient) -> None:
    register(client, "first-user")
    login(client, "first-user")
    created = client.post(
        "/api/v1/voice-profiles",
        headers=_headers(client),
        json={"display_name": "Алексей", "aliases": ["Лёша"]},
    )

    assert created.status_code == 201, created.text
    profile = created.json()["profile"]
    assert profile["display_name"] == "Алексей"
    assert profile["aliases"] == ["Лёша"]
    assert "created_by" not in profile
    assert "source_job_id" not in profile
    assert "protected_path" not in profile
    assert "vector_id" not in profile

    other = TestClient(client.app)
    with other:
        register(other, "second-user")
        login(other, "second-user")
        response = other.get("/api/v1/voice-profiles?query=лёш")

        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [profile["id"]]


def test_any_user_can_correct_shared_name_but_not_archive(client: TestClient) -> None:
    register(client, "first-user")
    login(client, "first-user")
    created = client.post(
        "/api/v1/voice-profiles",
        headers=_headers(client),
        json={"display_name": "Неуточнённый"},
    )
    assert created.status_code == 201, created.text
    profile_id = created.json()["profile"]["id"]

    other = TestClient(client.app)
    with other:
        register(other, "second-user")
        login(other, "second-user")
        renamed = other.patch(
            f"/api/v1/voice-profiles/{profile_id}",
            headers=_headers(other),
            json={"display_name": "Марина"},
        )
        archive = other.post(
            f"/api/v1/voice-profiles/{profile_id}/archive",
            headers=_headers(other),
            json={"reason": "Проверка"},
        )

        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["profile"]["display_name"] == "Марина"
        assert archive.status_code == 403
        assert archive.json()["code"] == "admin_required"
