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
    assert settings.cleaner_enabled is False
    assert settings.cleaner_model_path is None
    assert settings.cleaner_llama_server_path is None
    assert settings.cleaner_endpoint is None
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


def test_voice_auto_assignment_requires_explicit_calibrated_thresholds(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "NEXT_VOICE_CANDIDATE_MAX_DISTANCE": "0.35",
            "NEXT_VOICE_AUTO_MAX_DISTANCE": "0.20",
            "NEXT_VOICE_MIN_MARGIN": "0.04",
            "NEXT_VOICE_MIN_INTERVALS": "2",
            "NEXT_VOICE_MIN_COVERAGE": "0.7",
            "NEXT_VOICE_CALIBRATED": "true",
            "NEXT_VOICE_AUTO_ASSIGN_ENABLED": "true",
        },
        app_root=tmp_path,
    )

    assert settings.voice_matching_thresholds.calibrated is True
    assert settings.voice_matching_thresholds.auto_assign_enabled is True
    assert settings.voice_matching_thresholds.auto_max_distance == 0.2


def test_voice_auto_threshold_cannot_be_weaker_than_candidate(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="voice matching"):
        Settings.from_env(
            {
                "NEXT_VOICE_CANDIDATE_MAX_DISTANCE": "0.2",
                "NEXT_VOICE_AUTO_MAX_DISTANCE": "0.3",
            },
            app_root=tmp_path,
        )


def test_cleaner_configuration_accepts_only_local_endpoint_and_model_pack(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "NEXT_CLEANER_ENABLED": "true",
            "NEXT_CLEANER_MODEL_PATH": "qwen/cleaner.gguf",
            "NEXT_CLEANER_LLAMA_SERVER_PATH": "tools/llama-server.exe",
            "NEXT_CLEANER_ENDPOINT": "http://127.0.0.1:18080",
        },
        app_root=tmp_path,
    )

    assert settings.cleaner_enabled is True
    assert settings.cleaner_model_path == tmp_path / "data" / "models" / "qwen" / "cleaner.gguf"
    assert settings.cleaner_llama_server_path == tmp_path / "tools" / "llama-server.exe"
    assert settings.cleaner_endpoint == "http://127.0.0.1:18080"


def test_cleaner_configuration_rejects_nonlocal_endpoint(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="NEXT_CLEANER_ENDPOINT"):
        Settings.from_env(
            {"NEXT_CLEANER_ENDPOINT": "https://example.test/v1"},
            app_root=tmp_path,
        )
