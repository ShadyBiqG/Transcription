from __future__ import annotations

from pathlib import Path


def test_default_installer_uses_pinned_gigaam_v3_e2e_ctc_pack() -> None:
    installer = (
        Path(__file__).parents[2] / "deploy" / "install-default-model-packs.ps1"
    ).read_text(encoding="utf-8")

    assert '$asrDirectoryName = "asr/gigaam-v3-e2e-ctc-322c3b29"' in installer
    assert (
        '"v3_e2e_ctc.onnx" = '
        '"377701bd33568f4733feec2db5b2dc12544fd09a5a5dfa69ccf55d161f84027a"'
    ) in installer
    assert (
        '"v3_e2e_ctc_vocab.txt" = '
        '"142de7570b3de5b3035ce111a89c228e80e6085273731d944093ddf24fa539cd"'
    ) in installer
    assert 'pack_id = "gigaam-v3-e2e-ctc-322c3b29"' in installer
    assert 'model_name = "gigaam-v3-e2e-ctc"' in installer


def test_default_installer_uses_verified_local_qwen_cleaner_and_llama_runtime() -> None:
    installer = (
        Path(__file__).parents[2] / "deploy" / "install-default-model-packs.ps1"
    ).read_text(encoding="utf-8")

    assert (
        '$cleanerModelDirectoryName = "cleaner/qwen2.5-1.5b-instruct-q4-k-m-91cad511"'
        in installer
    )
    assert '$cleanerModelRevision = "91cad51170dc346986eccefdc2dd33a9da36ead9"' in installer
    assert (
        '$cleanerModelSha256 = '
        '"6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"'
    ) in installer
    assert "$cleanerModelSize = 1117320736" in installer
    assert 'Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/$cleanerModelRevision' in installer
    assert '$cleanerRuntimeDirectoryName = "llama.cpp-b5092-win-noavx-x64"' in installer
    assert '$cleanerRuntimeArchiveName = "llama-b5092-bin-win-noavx-x64.zip"' in installer
    assert (
        '$cleanerRuntimeArchiveSha256 = '
        '"4a4c6aef72ff13122d56a9ab59acf697603ce9084087427400ae7d1b7ebaef71"'
    ) in installer
    assert (
        "ggml-org/llama.cpp/releases/download/b5092/$cleanerRuntimeArchiveName" in installer
    )
    assert "Assert-SafeZipArchive $ArchivePath" in installer
    assert "Assert-LlamaServer $destinationExecutable" in installer
    assert '$minimumDiskSpaceBytes = 7GB' in installer
    assert "Refusing an unapproved model download URL." in installer


def test_windows_installer_disables_cleaner_until_its_service_is_healthy() -> None:
    installer = (
        Path(__file__).parents[2] / "deploy" / "install-windows.ps1"
    ).read_text(encoding="utf-8")
    service_installer = (
        Path(__file__).parents[2] / "deploy" / "install-windows-service.ps1"
    ).read_text(encoding="utf-8")
    cleaner_installer = (
        Path(__file__).parents[2] / "deploy" / "install-windows-cleaner-service.ps1"
    ).read_text(encoding="utf-8")

    assert "NEXT_CLEANER_MODEL_PATH = $modelInstallation.CleanerModelPath" in installer
    assert (
        "NEXT_CLEANER_LLAMA_SERVER_PATH = $modelInstallation.CleanerLlamaServerPath"
        in installer
    )
    assert 'NEXT_CLEANER_ENDPOINT = "http://127.0.0.1:8020"' in installer
    assert 'NEXT_CLEANER_ENABLED = "false"' in installer
    assert (
        'Ensure-EnvironmentValue $environmentFile '
        '"NEXT_CLEANER_ENDPOINT" "http://127.0.0.1:8020"'
    ) in installer
    assert 'Ensure-EnvironmentValue $environmentFile "NEXT_CLEANER_ENABLED" "false"' in installer
    assert 'install-windows-cleaner-service.ps1' in service_installer
    assert "TranscriptionNextCleanerService" in cleaner_installer
    assert '$cleanerHost = "127.0.0.1"' in cleaner_installer
    assert "$cleanerPort = 8020" in cleaner_installer
    assert "--ctx-size 4096" in cleaner_installer
    assert "--parallel 1" in cleaner_installer
    assert "Wait-CleanerHealth" in cleaner_installer
    assert "rule-based очисткой" in cleaner_installer
    assert "exit 0" in cleaner_installer
    health_call = cleaner_installer.index("    Wait-CleanerHealth\n")
    assert cleaner_installer.index(
        'Set-EnvironmentValue $environmentFile "NEXT_CLEANER_ENABLED" "false"'
    ) < health_call < cleaner_installer.index(
        'Set-EnvironmentValue $environmentFile "NEXT_CLEANER_ENABLED" "true"'
    )


def test_git_installer_stops_cleaner_before_replacing_model_files() -> None:
    installer = (
        Path(__file__).parents[2] / "deploy" / "install-from-git.bat"
    ).read_text(encoding="ascii")

    assert "TranscriptionNextCandidateService" in installer
    assert "TranscriptionNextCleanerService" in installer
