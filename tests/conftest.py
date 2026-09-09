from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from transcription_service.config import Settings
from transcription_service.noscribe import NoScribeReadiness, NoScribeResult


def register_user(client, email: str = "user@example.com", password: str = "password123"):
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    )
    assert response.status_code == 201, response.text
    return response.json()


class FakeNoScribeRunner:
    def __init__(self, *, fail: bool = False, delay: float = 0.01) -> None:
        self.fail = fail
        self.delay = delay
        self.calls: list[list[str]] = []

    def check_readiness(self) -> NoScribeReadiness:
        return NoScribeReadiness(True, ("fast", "precise"))

    def build_arguments(
        self,
        input_path: Path,
        output_path: Path,
        language: str,
        model: str,
        speaker_detection: str,
    ) -> list[str]:
        if model not in {"fast", "precise"}:
            raise ValueError("Недоступная модель")
        if not language:
            raise ValueError("Пустой язык")
        return [str(input_path), str(output_path), language, model, speaker_detection]

    async def run(
        self,
        input_path: Path,
        output_path: Path,
        log_path: Path,
        language: str,
        model: str,
        speaker_detection: str,
    ) -> NoScribeResult:
        self.calls.append(
            self.build_arguments(input_path, output_path, language, model, speaker_detection)
        )
        await asyncio.sleep(self.delay)
        log_path.write_text("fake noScribe\n", encoding="utf-8")
        if self.fail:
            return NoScribeResult(2, False, "noscribe_failed", "Тестовая ошибка")
        output_path.write_text(
            '<!doctype html><html><meta charset="UTF-8"><body>'
            '<a name="ts_0_1000_S00">S00: [00:00:00] Тестовая реплика</a>'
            "</body></html>",
            encoding="utf-8",
        )
        return NoScribeResult(0, True)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    executable = tmp_path / "noScribe.exe"
    executable.write_bytes(b"fake")
    return Settings(
        data_dir=tmp_path / "data",
        noscribe_path=executable,
        default_language="ru",
        default_model="precise",
        allowed_models=("fast", "precise"),
        max_upload_bytes=1024 * 1024,
        noscribe_timeout_seconds=10,
    )


@pytest.fixture
def fake_runner() -> FakeNoScribeRunner:
    return FakeNoScribeRunner()
