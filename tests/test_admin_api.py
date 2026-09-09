import sqlite3

import pytest
from fastapi.testclient import TestClient

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
