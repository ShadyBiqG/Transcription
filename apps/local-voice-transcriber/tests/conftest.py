from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from local_voice_transcriber.api import create_app
from local_voice_transcriber.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "candidate-data"
    return Settings(
        data_dir=data_dir,
        database_path=data_dir / "transcription-next.sqlite3",
        host="127.0.0.1",
        port=8010,
        instance_id="test-candidate",
        cookie_secure=False,
        session_ttl_days=14,
        auth_rate_limit=50,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def register(
    client: TestClient, login: str = "tester", password: str = "correct horse battery"
) -> None:
    response = client.post("/api/v1/auth/register", json={"login": login, "password": password})
    assert response.status_code == 201, response.text


def login(
    client: TestClient, login_name: str = "tester", password: str = "correct horse battery"
) -> str:
    response = client.post("/api/v1/auth/login", json={"login": login_name, "password": password})
    assert response.status_code == 200, response.text
    return client.cookies.get("local_voice_transcriber_csrf") or ""


@pytest.fixture
def logged_in_client(client: TestClient) -> TestClient:
    register(client)
    csrf_token = login(client)
    assert csrf_token
    return client
