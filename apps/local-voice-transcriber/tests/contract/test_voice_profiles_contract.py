"""Контракт скрытого от обычных пользователей технического каталога говорящих."""

from __future__ import annotations

from conftest import login, register
from fastapi.testclient import TestClient


def _headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("local_voice_transcriber_csrf") or ""}


def _promote_to_admin(client: TestClient, login_name: str) -> None:
    user = client.app.state.database.get_user_by_login(login_name)
    assert user is not None
    client.app.state.database.update_user(user_id=user["id"], role="admin")
    login(client, login_name)


def test_regular_user_cannot_enumerate_or_change_shared_speaker_catalog(client: TestClient) -> None:
    register(client, "regular-user")
    login(client, "regular-user")

    listed = client.get("/api/v1/voice-profiles?query=мар")
    created = client.post(
        "/api/v1/voice-profiles",
        headers=_headers(client),
        json={"display_name": "Марина Иванова"},
    )

    assert listed.status_code == created.status_code == 403
    assert listed.json()["code"] == created.json()["code"] == "admin_required"


def test_administrator_keeps_catalog_operations_without_private_provenance(
    client: TestClient,
) -> None:
    register(client, "administrator")
    _promote_to_admin(client, "administrator")

    created = client.post(
        "/api/v1/voice-profiles",
        headers=_headers(client),
        json={"display_name": "Марина Иванова", "aliases": ["М. Иванова"]},
    )
    assert created.status_code == 201, created.text
    profile = created.json()["profile"]

    listed = client.get("/api/v1/voice-profiles?query=иван")

    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [profile["id"]]
    assert "created_by" not in profile
    assert "source_job_id" not in profile
    assert "protected_path" not in profile
    assert "vector_id" not in profile
