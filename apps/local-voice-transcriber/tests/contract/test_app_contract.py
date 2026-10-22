from __future__ import annotations

from conftest import login, register
from fastapi.testclient import TestClient


def test_health_and_readiness_are_public_and_do_not_disclose_paths(client: TestClient) -> None:
    for endpoint in ("/health", "/ready", "/api/v1/health", "/api/v1/ready"):
        response = client.get(endpoint)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"] == "transcription-next"
        assert body["instance_id"] == "test-candidate"
        assert "path" not in str(body).casefold()
        assert body["checks"]["database"] == "ok"


def test_self_registration_accepts_email_login_and_creates_only_user(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "login": "New.User@Example.test",
            "password": "correct horse battery",
            "role": "admin",
            "email": "ignored@example.test",
        },
    )

    assert response.status_code == 201
    user = response.json()["user"]
    assert user["login"] == "new.user@example.test"
    assert user["role"] == "user"
    assert "email" not in user


def test_login_uses_isolated_http_only_session_and_csrf_cookie(client: TestClient) -> None:
    register(client)
    response = client.post(
        "/api/v1/auth/login", json={"login": "tester", "password": "correct horse battery"}
    )

    assert response.status_code == 200
    assert response.json()["csrf_header_name"] == "X-CSRF-Token"
    set_cookie = "; ".join(response.headers.get_list("set-cookie"))
    assert "local_voice_transcriber_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "local_voice_transcriber_csrf=" in set_cookie
    assert client.cookies.get("local_voice_transcriber_csrf")
    assert client.get("/api/v1/auth/me").status_code == 200


def test_login_accepts_email_identifier(client: TestClient) -> None:
    register(client, "operator@example.test")

    response = client.post(
        "/api/v1/auth/login",
        json={"login": "Operator@Example.test", "password": "correct horse battery"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["login"] == "operator@example.test"


def test_state_change_requires_valid_csrf(client: TestClient) -> None:
    register(client)
    csrf_token = login(client)

    missing = client.post("/api/v1/auth/logout")
    assert missing.status_code == 403
    assert missing.json()["code"] == "csrf_required"

    invalid = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "invalid"})
    assert invalid.status_code == 403
    assert invalid.json()["code"] == "csrf_invalid"

    success = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token})
    assert success.status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401


def test_admin_endpoint_rejects_regular_user(logged_in_client: TestClient) -> None:
    response = logged_in_client.get("/api/v1/admin/users")

    assert response.status_code == 403
    assert response.json()["code"] == "admin_required"


def test_voice_index_retry_is_available_only_to_administrator(client: TestClient) -> None:
    registered = client.post(
        "/api/v1/auth/register",
        json={"login": "local-admin", "password": "correct horse battery"},
    )
    user_id = registered.json()["user"]["id"]
    client.app.state.database.update_user(user_id=user_id, role="admin")
    csrf_token = login(client, "local-admin")

    response = client.post(
        "/api/v1/admin/voice-index/reconcile",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "voice_index_unavailable"
    assert "path" not in response.text.casefold()


def test_static_index_is_public_without_api_secrets(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Transcription Next" in response.text
    assert "local_voice_transcriber_session" not in response.text
