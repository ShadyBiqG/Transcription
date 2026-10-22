from __future__ import annotations
from pathlib import Path

import pytest

from local_voice_transcriber.config import ConfigurationError, Settings


def test_default_configuration_uses_app_local_data_root(tmp_path: Path) -> None:
    settings = Settings.from_env({}, app_root=tmp_path)

    assert settings.data_dir == tmp_path / "data"
    assert settings.database_path == tmp_path / "data" / "transcription-next.sqlite3"
    assert settings.zeusdb_dir == tmp_path / "data" / "zeusdb"
    assert settings.models_dir == tmp_path / "data" / "models"
    assert settings.asr_manifest_path is None
    assert settings.ffmpeg_path is None
    assert settings.port == 8010
    assert settings.cookie_name == "local_voice_transcriber_session"


def test_database_path_cannot_escape_candidate_data_root(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="NEXT_DATABASE_PATH"):
        Settings.from_env(
            {"NEXT_DATA_DIR": "candidate", "NEXT_DATABASE_PATH": "../legacy.sqlite3"},
            app_root=tmp_path,
        )


def test_zeusdb_path_cannot_escape_candidate_data_root(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="NEXT_ZEUSDB_DIR"):
        Settings.from_env(
            {"NEXT_DATA_DIR": "candidate", "NEXT_ZEUSDB_DIR": "../shared-zeusdb"},
            app_root=tmp_path,
        )


def test_asr_manifest_cannot_escape_models_directory(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="NEXT_ASR_MANIFEST"):
        Settings.from_env(
            {
                "NEXT_DATA_DIR": "candidate",
                "NEXT_ASR_MANIFEST": "../../untrusted/asr-pack.json",
            },
            app_root=tmp_path,
        )


@pytest.mark.parametrize("value", ["", "0", "abc"])
def test_port_must_be_positive_integer(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigurationError, match="NEXT_PORT"):
        Settings.from_env({"NEXT_PORT": value}, app_root=tmp_path)


def test_boolean_values_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="NEXT_COOKIE_SECURE"):
        Settings.from_env({"NEXT_COOKIE_SECURE": "perhaps"}, app_root=tmp_path)
