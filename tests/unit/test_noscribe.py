from __future__ import annotations

from pathlib import Path

import pytest

from transcription_service.noscribe import (
    NoScribeRunner,
    _drain_windows_pty,
    _noscribe_environment,
)


def test_build_arguments_uses_direct_headless_vtt_contract(settings):
    runner = NoScribeRunner(settings)
    arguments = runner.build_arguments(
        Path("meeting.webm"), Path("transcript.vtt"), "ru", "precise"
    )
    assert arguments[0] == str(settings.noscribe_path)
    assert "--no-gui" in arguments
    assert arguments[arguments.index("--speaker-detection") + 1] == "none"
    assert "--timestamps" in arguments
    assert arguments[-2:] == ["meeting.webm", "transcript.vtt"]


@pytest.mark.parametrize("language", ["ru;del", "", "русский", "r"])
def test_build_arguments_rejects_invalid_language(settings, language):
    with pytest.raises(ValueError):
        NoScribeRunner(settings).build_arguments(Path("a.webm"), Path("a.vtt"), language, "precise")


def test_readiness_reports_missing_executable(settings):
    settings.noscribe_path.unlink()
    readiness = NoScribeRunner(settings).check_readiness()
    assert readiness.ready is False
    assert readiness.models == ()


def test_noscribe_environment_forces_utf8(monkeypatch):
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")

    environment = _noscribe_environment()

    assert environment["PYTHONUTF8"] == "1"
    assert environment["PYTHONIOENCODING"] == "utf-8"


def test_windows_pty_log_is_written_as_utf8(tmp_path):
    class FakePtyProcess:
        def __init__(self):
            self.outputs = iter(["\x1b[1tРаспознанный текст\r\n"])

        def read(self, _size):
            try:
                return next(self.outputs)
            except StopIteration as error:
                raise EOFError from error

        def wait(self):
            return 0

    log_path = tmp_path / "noscribe.log"

    exit_code = _drain_windows_pty(FakePtyProcess(), log_path)

    assert exit_code == 0
    assert log_path.read_text(encoding="utf-8") == "Распознанный текст\n"
