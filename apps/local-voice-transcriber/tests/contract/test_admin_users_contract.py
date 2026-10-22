"""Контракты административных агрегатов и управления доступом."""

from __future__ import annotations

import pytest
from conftest import login, register
from fastapi.testclient import TestClient

from local_voice_transcriber.editor import GroupSnapshot, SourceSegmentSnapshot
from local_voice_transcriber.jobs import JobStatus


def _create_admin(client: TestClient) -> tuple[str, dict[str, str]]:
    register(client, "administrator")
    database = client.app.state.database
    user = database.get_user_by_login("administrator")
    assert user is not None
    database.update_user(user_id=user["id"], role="admin")
    return user["id"], {"X-CSRF-Token": login(client, "administrator")}


@pytest.mark.parametrize("endpoint", ["/api/v1/admin/users", "/api/v1/admin/overview"])
def test_administrative_statistics_require_admin(
    logged_in_client: TestClient, endpoint: str
) -> None:
    response = logged_in_client.get(endpoint)

    assert response.status_code == 403
    assert response.json()["code"] == "admin_required"


def test_statistics_count_all_records_without_disclosing_private_content(
    client: TestClient,
) -> None:
    admin_id, _ = _create_admin(client)
    database = client.app.state.database
    owner = database.create_user(login="recording-owner", password_hash="unused")
    inactive = database.create_user(login="inactive-user", password_hash="unused")
    database.update_user(user_id=inactive["id"], is_active=False)
    secret = "UNIQUE_PRIVATE_MEETING_83239"
    audio_path = client.app.state.settings.data_dir / f"{secret}.webm"
    audio_path.write_bytes(b"\x1a\x45\xdf\xa3private-audio")
    statuses = [status.value for status in JobStatus] * 15 + [JobStatus.UPLOADED.value] * 2
    with database.connection() as connection:
        connection.executemany(
            """
            INSERT INTO next_jobs(
                id, owner_id, source_name, source_path, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"private-job-{index}",
                    owner["id"],
                    f"{secret}.webm",
                    str(audio_path) if index == 0 else f"private-path/{secret}/{index}",
                    status,
                    "2026-10-23T00:00:00+00:00",
                    "2026-10-23T00:00:00+00:00",
                )
                for index, status in enumerate(statuses)
            ],
        )
    client.app.state.editor_repository.create_baseline(
        owner_id=owner["id"],
        job_id="private-job-0",
        job_run_id="private-run-1",
        groups=(GroupSnapshot(speaker_group_id="private-group", label_snapshot="Секрет"),),
        source_segments=(
            SourceSegmentSnapshot(
                source_segment_id="private-segment",
                ordinal=0,
                text=secret,
                primary_group_id="private-group",
                speaker_group_ids=("private-group",),
            ),
        ),
    )

    users = client.get("/api/v1/admin/users")
    overview = client.get("/api/v1/admin/overview")

    assert users.status_code == overview.status_code == 200
    items = {item["id"]: item for item in users.json()["items"]}
    assert set(items) == {admin_id, owner["id"], inactive["id"]}
    expected = {status.value: statuses.count(status.value) for status in JobStatus}
    assert items[owner["id"]]["statistics"] == {
        "recordings_total": 107,
        "by_status": expected,
    }
    for user_id in (admin_id, inactive["id"]):
        assert items[user_id]["statistics"] == {
            "recordings_total": 0,
            "by_status": dict.fromkeys(JobStatus, 0),
        }
    for item in items.values():
        assert set(item) == {"id", "login", "role", "is_active", "created_at", "statistics"}
        assert set(item["statistics"]) == {"recordings_total", "by_status"}
    assert items[inactive["id"]]["is_active"] is False
    totals = overview.json()
    assert totals["users_count"] == totals["users_total"] == 3
    assert totals["users_active"] == 2
    assert totals["users_admins"] == 1
    assert totals["recordings_total"] == 107
    assert totals["by_status"] == expected
    for response in (users, overview):
        for private_value in (secret, "private-job-", "private-path", "source_name", "source_path"):
            assert private_value not in response.text
        for private_field in ("transcript", "audio", "error_message", "password_hash"):
            assert f'"{private_field}":' not in response.text
    assert client.get("/api/v1/jobs").json()["items"] == []
    for suffix in ("", "/audio", "/source", "/transcript", "/transcript/revisions"):
        assert client.get(f"/api/v1/jobs/private-job-0{suffix}").status_code == 404


def test_role_changes_require_csrf_and_take_effect_in_existing_session(client: TestClient) -> None:
    admin_id, headers = _create_admin(client)
    register(client, "target-user")
    database = client.app.state.database
    target = database.get_user_by_login("target-user")
    assert target is not None
    endpoint = f"/api/v1/admin/users/{target['id']}"

    missing = client.patch(endpoint, json={"role": "admin"})
    invalid = client.patch(endpoint, headers={"X-CSRF-Token": "invalid"}, json={"role": "admin"})
    assert missing.status_code == invalid.status_code == 403
    assert missing.json()["code"] == "csrf_required"
    assert invalid.json()["code"] == "csrf_invalid"
    assert database.get_user(target["id"])["role"] == "user"

    with TestClient(client.app) as target_client:
        target_headers = {"X-CSRF-Token": login(target_client, "target-user")}
        denied = target_client.patch(endpoint, headers=target_headers, json={"role": "admin"})
        assert denied.status_code == 403
        assert denied.json()["code"] == "admin_required"
        granted = client.patch(endpoint, headers=headers, json={"role": "admin"})
        assert granted.status_code == 200
        assert granted.json()["user"]["role"] == "admin"
        assert target_client.get("/api/v1/admin/users").status_code == 200
        revoked = client.patch(endpoint, headers=headers, json={"role": "user"})
        assert revoked.status_code == 200
        assert revoked.json()["user"]["role"] == "user"
        assert target_client.get("/api/v1/admin/users").status_code == 403

    with database.connection() as connection:
        events = connection.execute(
            "SELECT actor_id FROM audit_events WHERE action = ? AND target_id = ?",
            ("admin_updated_user", target["id"]),
        ).fetchall()
    assert [row["actor_id"] for row in events] == [admin_id, admin_id]


@pytest.mark.parametrize("payload", [{"role": "user"}, {"is_active": False}])
def test_last_active_admin_cannot_be_removed(
    client: TestClient, payload: dict[str, object]
) -> None:
    admin_id, headers = _create_admin(client)
    database = client.app.state.database
    disabled_admin = database.create_user(
        login="disabled-admin", password_hash="unused", role="admin"
    )
    database.update_user(user_id=disabled_admin["id"], is_active=False)

    response = client.patch(f"/api/v1/admin/users/{admin_id}", headers=headers, json=payload)

    assert response.status_code == 409
    assert response.json()["code"] == "last_admin"
    assert database.count_active_admins() == 1
    assert client.get("/api/v1/admin/users").status_code == 200
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE action = 'admin_updated_user'"
            ).fetchone()[0]
            == 0
        )
