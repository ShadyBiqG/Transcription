from __future__ import annotations

import asyncio
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

LANGUAGE_PATTERN = re.compile(r"^(?:auto|[a-zA-Z]{2,8})$")


@dataclass(frozen=True, slots=True)
class NoScribeReadiness:
    ready: bool
    models: tuple[str, ...]
    message: str | None = None


@dataclass(frozen=True, slots=True)
class NoScribeResult:
    exit_code: int | None
    success: bool
    error_code: str | None = None
    error_message: str | None = None


class NoScribeRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build_arguments(
        self, input_path: Path, output_path: Path, language: str, model: str
    ) -> list[str]:
        if model not in self.settings.allowed_models:
            raise ValueError(f"Модель {model!r} не разрешена")
        if not LANGUAGE_PATTERN.fullmatch(language):
            raise ValueError("Код языка должен состоять из 2–8 латинских букв или быть auto")
        return [
            str(self.settings.noscribe_path),
            "--no-gui",
            "--language",
            language.lower(),
            "--model",
            model,
            "--speaker-detection",
            "none",
            "--timestamps",
            str(input_path),
            str(output_path),
        ]

    def check_readiness(self) -> NoScribeReadiness:
        executable = self.settings.noscribe_path
        if not executable.is_file():
            return NoScribeReadiness(False, (), "Исполняемый файл noScribe не найден")
        try:
            result = subprocess.run(
                [str(executable), "--help-models"],
                capture_output=True,
                timeout=30,
                check=False,
                shell=False,
                creationflags=_creation_flags(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return NoScribeReadiness(False, (), "Не удалось проверить модели noScribe")
        output = (result.stdout + result.stderr).decode(errors="replace")
        models = tuple(
            model
            for model in self.settings.allowed_models
            if re.search(rf"\b{re.escape(model)}\b", output)
        )
        if result.returncode != 0 or not models:
            return NoScribeReadiness(False, models, "noScribe не сообщил доступные модели")
        return NoScribeReadiness(True, models)

    async def run(
        self,
        input_path: Path,
        output_path: Path,
        log_path: Path,
        language: str,
        model: str,
    ) -> NoScribeResult:
        arguments = self.build_arguments(input_path, output_path, language, model)
        process: asyncio.subprocess.Process | None = None
        try:
            with log_path.open("wb") as log_file:
                process = await asyncio.create_subprocess_exec(
                    *arguments,
                    stdout=log_file,
                    stderr=asyncio.subprocess.STDOUT,
                    creationflags=_creation_flags(),
                )
                try:
                    exit_code = await asyncio.wait_for(
                        process.wait(), timeout=self.settings.noscribe_timeout_seconds
                    )
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    return NoScribeResult(
                        None, False, "noscribe_timeout", "noScribe превысил допустимое время"
                    )
        except asyncio.CancelledError:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            raise
        except OSError:
            return NoScribeResult(
                None, False, "noscribe_start_failed", "Не удалось запустить noScribe"
            )

        if exit_code != 0:
            return NoScribeResult(
                exit_code,
                False,
                "noscribe_failed",
                f"noScribe завершился с кодом {exit_code}",
            )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            return NoScribeResult(
                exit_code,
                False,
                "transcript_missing",
                "noScribe не создал непустую транскрипцию",
            )
        return NoScribeResult(exit_code, True)


def _creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
