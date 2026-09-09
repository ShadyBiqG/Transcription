from __future__ import annotations

import pytest

from transcription_service.config import Settings


def test_default_model_must_be_allowed(tmp_path):
    with pytest.raises(ValueError, match="Модель по умолчанию"):
        Settings(
            data_dir=tmp_path,
            noscribe_path=tmp_path / "noScribe.exe",
            default_language="ru",
            default_model="large",
            allowed_models=("fast", "precise"),
            max_upload_bytes=10,
            noscribe_timeout_seconds=10,
            api_token=None,
        )


def test_paths_are_derived_from_data_directory(settings):
    assert settings.database_path == settings.data_dir / "jobs.sqlite3"
    assert settings.jobs_dir == settings.data_dir / "jobs"
    assert settings.temp_dir == settings.data_dir / "tmp"
