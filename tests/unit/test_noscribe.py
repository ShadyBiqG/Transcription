from __future__ import annotations

from pathlib import Path

import pytest

from transcription_service.noscribe import NoScribeRunner


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
