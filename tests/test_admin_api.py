import sqlite3

import pytest
from fastapi.testclient import TestClient

from transcription_service.admin_repository import AdminRepository
from transcription_service.api import create_app
from transcription_service.database import JobRepository
from transcription_service.models import UserRole

from .conftest import register_user


def test_admin_routes_are_hidden_from_regular_users(settings, fake_runner) -> None:
    app = create_app(settings, fake_runner, start_worker=False)
    with TestClient(app) as client:
        register_user(client)
        response = client.get("/api/v1/admin/statistics/overview")
    assert response.status_code == 403


def test_admin_sees_three_backend_sections(settings, fake_runner) -> None:
    app = create_app(settings, fake_runner, start_worker=False)
    with TestClient(app) as client:
        user = register_user(client, "admin@example.com")
        JobRepository(settings.database_path).set_user_role(user["id"], UserRole.ADMIN)

        overview = client.get("/api/v1/admin/statistics/overview")
        usage = client.get("/api/v1/admin/external-usage")
        configuration = client.get("/api/v1/admin/settings")

    assert overview.status_code == 200
    assert overview.json()["users"] == 1
    assert overview.json()["by_user"][0]["email"] == "admin@example.com"
    assert overview.json()["by_user"][0]["jobs"] == 0
    assert usage.status_code == 200
    assert usage.json()["calls"] == 0
    assert configuration.status_code == 200
    assert configuration.json()["credential"]["configured"] is False


def test_last_admin_is_protected_and_setting_change_is_audited(settings, fake_runner) -> None:
    app = create_app(settings, fake_runner, start_worker=False)
    with TestClient(app) as client:
        user = register_user(client, "admin@example.com")
        repository = JobRepository(settings.database_path)
        repository.set_user_role(user["id"], UserRole.ADMIN)
        response = client.patch(
            "/api/v1/admin/settings", json={"provider_enabled": False}
        )
        assert response.status_code == 200
        with pytest.raises(ValueError, match="последнего"):
            repository.set_user_role(user["id"], UserRole.USER)

    with sqlite3.connect(settings.database_path) as connection:
        actions = [row[0] for row in connection.execute("SELECT action FROM admin_audit_events")]
    assert "settings.update" in actions


def test_personal_usage_never_contains_another_user(settings, fake_runner) -> None:
    app = create_app(settings, fake_runner, start_worker=False)
    with TestClient(app) as client:
        first = register_user(client, "first@example.com")
        client.post("/api/v1/auth/logout")
        second = register_user(client, "second@example.com")
        ledger = AdminRepository(settings.database_path)
        first_call = ledger.create_call("model-a", 2, user_id=first["id"])
        ledger.finish_call(first_call, status="completed", input_units=10)
        second_call = ledger.create_call("model-b", 3, user_id=second["id"])
        ledger.finish_call(second_call, status="completed", input_units=20)

        response = client.get("/api/v1/external-usage")

    assert response.status_code == 200
    payload = response.json()
    assert payload["calls"] == 1
    assert payload["input_units"] == 20
    assert payload["by_user"][0]["email"] == "second@example.com"
    assert payload["items"][0]["requested_model"] == "model-b"


def test_admin_can_configure_manual_openai_compatible_provider(
    settings, fake_runner
) -> None:
    app = create_app(settings, fake_runner, start_worker=False)
    with TestClient(app) as client:
        user = register_user(client, "admin@example.com")
        JobRepository(settings.database_path).set_user_role(user["id"], UserRole.ADMIN)

        response = client.patch(
            "/api/v1/admin/settings",
            json={
                "provider_name": "Локальный шлюз",
                "provider_base_url": "http://models.internal/v1/",
                "primary_model_id": "vision/manual-primary",
                "fallback_model_id": "vision/manual-fallback",
                "allowed_model_ids": [],
            },
        )
        catalog = client.get("/api/v1/admin/models")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider_name"] == "Локальный шлюз"
    assert payload["provider_base_url"] == "http://models.internal/v1"
    assert payload["allowed_model_ids"] == [
        "vision/manual-primary",
        "vision/manual-fallback",
    ]
    assert {item["model_id"] for item in catalog.json()["models"]} == {
        "vision/manual-primary",
        "vision/manual-fallback",
    }
