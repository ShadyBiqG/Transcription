from __future__ import annotations

import sqlite3
import time

import pytest
from conftest import login, register
from fastapi.testclient import TestClient

from local_voice_transcriber import api as api_module
from local_voice_transcriber.api import create_app
from local_voice_transcriber.config import Settings
from local_voice_transcriber.jobs.dispatcher import OneWorkerDispatcher


class _NoopProcessor:
    async def process(self, _job, _run, _cancellation_requested) -> None:
        return None


def _wait_for_worker(client: TestClient, expected: str) -> None:
    deadline = time.monotonic() + 2
    while client.app.state.dispatcher_status() != expected and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client.app.state.dispatcher_status() == expected


def test_health_and_readiness_are_public_and_do_not_disclose_paths(client: TestClient) -> None:
    for endpoint in ("/health", "/ready", "/api/v1/health", "/api/v1/ready"):
        response = client.get(endpoint)

        assert response.status_code == (503 if "ready" in endpoint else 200)
        body = response.json()
        assert body["status"] == ("error" if "ready" in endpoint else "ok")
        assert body["service"] == "transcription-next"
        assert body["instance_id"] == "test-candidate"
        assert "path" not in str(body).casefold()
        assert body["checks"]["database"] == "ok"
        assert body["checks"]["worker"] == "stopped"


def test_readiness_and_admin_overview_follow_live_worker_state(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def configured_worker(_settings, repository, *_args):
        dispatcher = OneWorkerDispatcher(
            repository, _NoopProcessor(), worker_id="test-worker", poll_seconds=0.01
        )
        return dispatcher, "verified-pack", None, None, None, None, None

    monkeypatch.setattr(api_module, "_configure_asr_worker", configured_worker)

    with TestClient(create_app(settings)) as client:
        dispatcher = client.app.state.asr_dispatcher
        repository = client.app.state.jobs_repository
        assert dispatcher is not None
        _wait_for_worker(client, "running")
        assert client.get("/ready").status_code == 200
        assert client.get("/ready").json()["checks"]["worker"] == "running"

        registered = client.post(
            "/api/v1/auth/register",
            json={"login": "operator", "password": "correct horse battery"},
        )
        client.app.state.database.update_user(user_id=registered.json()["user"]["id"], role="admin")
        login(client, "operator")

        original_claim = repository.claim_next_run

        def temporary_failure(**_kwargs):
            raise sqlite3.OperationalError("private-content /private/path")

        monkeypatch.setattr(repository, "claim_next_run", temporary_failure)
        _wait_for_worker(client, "degraded")
        degraded = client.get("/ready")
        assert degraded.status_code == 503
        assert degraded.json()["checks"]["worker"] == "degraded"
        assert "private-content" not in degraded.text
        overview = client.get("/api/v1/admin/overview").json()
        assert overview["queue"]["worker"] == "degraded"
        assert overview["asr"]["ready"] is False
        assert "private-content" not in str(overview)
        assert client.get("/health").status_code == 200

        monkeypatch.setattr(repository, "claim_next_run", original_claim)
        _wait_for_worker(client, "running")
        assert client.get("/ready").status_code == 200

        def fatal_failure(**_kwargs):
            raise RuntimeError("private-content /private/path")

        monkeypatch.setattr(repository, "claim_next_run", fatal_failure)
        _wait_for_worker(client, "stopped")
        stopped = client.get("/ready")
        assert stopped.status_code == 503
        assert stopped.json()["checks"]["worker"] == "stopped"
        overview = client.get("/api/v1/admin/overview").json()
        assert overview["queue"]["worker"] == "stopped"
        assert overview["asr"]["ready"] is False


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
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert 'id="add-recording-button"' in response.text
    assert 'id="recording-dropzone"' in response.text
    assert 'id="workspace-menu"' in response.text
    assert 'id="go-to-jobs"' in response.text
    assert 'class="menu-item" data-view="jobs">Записи</button>' in response.text
    assert 'data-view="profiles"' not in response.text
    assert 'id="profiles-view"' not in response.text
    assert 'id="download-text-button"' in response.text
    assert 'id="open-speakers-button"' in response.text
    assert 'id="speakers-dialog"' in response.text
    assert 'id="speaker-timeline-dialog"' in response.text
    assert 'id="clean-draft-button"' in response.text
    assert 'id="raw-draft-button"' in response.text
    assert 'Изменить спикеров' in response.text
    assert 'Чистый черновик' in response.text
    assert 'Дословный оригинал' in response.text
    assert 'Говорящие · 0' not in response.text

    script = client.get("/static/app.js")

    assert script.status_code == 200
    assert script.headers["cache-control"] == "no-store, max-age=0"
    assert "function selectMediaFile" in script.text
    assert 'if (file && state.user) activateView("jobs");' in script.text
    assert 'function openMediaPicker()' in script.text
    assert 'byId("go-to-jobs").addEventListener("click", goToJobs);' in script.text
    assert 'addEventListener("drop"' in script.text
    assert 'audio-player").addEventListener("error"' in script.text
    assert '"/identity"' in script.text
    assert '"/transcript/groups/merge"' in script.text
    assert '"/transcript/groups/unmerge"' in script.text
    assert '"/voice-profiles"' not in script.text
    assert "Объединить локальные группы" in script.text
    assert "Сопоставить с другой группой" not in script.text
    assert "speaker-picker" not in script.text
    assert "function playSample" in script.text
    assert "function stopSamplePlayback" in script.text
    assert 'if (!player.paused) player.pause();' in script.text
    assert "function isGenericGroupLabel" in script.text
    assert 'isGenericGroupLabel(group.label) ? "" : group.label' in script.text
    assert "function renderSpeakerPanel" in script.text
    assert "function renderSpeakerTimeline" in script.text
    assert "function openSpeakerTimeline" in script.text
    assert "function setDraftMode" in script.text
    assert "function syncDraftModeControls" in script.text
    assert '"/transcript/view-preference"' in script.text
    assert '"/transcript?view="' in script.text
    assert 'preference: "last_selected"' in script.text
    assert "editor.revision.cleaning" not in script.text
    assert "hiddenByCleaning" in script.text
    assert '"Дословный оригинал ASR сохранён неизменным' in script.text
    assert "Добавить образец голоса" in script.text
    assert "function enrollSpeakerSample" in script.text
    assert 'transcript/export?format=txt' in script.text
    assert "Выбирайте только из групп этой записи" in script.text
    assert '"Говорящие · "' not in script.text
