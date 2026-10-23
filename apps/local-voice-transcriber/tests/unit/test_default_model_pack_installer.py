from __future__ import annotations

from pathlib import Path


def test_default_installer_uses_pinned_gigaam_v3_rnnt_pack() -> None:
    installer = (
        Path(__file__).parents[2] / "deploy" / "install-default-model-packs.ps1"
    ).read_text(encoding="utf-8")

    assert '$asrDirectoryName = "asr/gigaam-v3-rnnt-322c3b29"' in installer
    assert (
        '"config.json" = '
        '"0641fcf73f4af791c73f05083e38a658ff5dcbee3534a0c61396c10f4b97f0fe"'
    ) in installer
    assert (
        '"v3_rnnt_encoder.onnx" = '
        '"7ae7509c3f1128369564df0b00e2ee4950adf539de2392ac5c800a5bc04c7132"'
    ) in installer
    assert (
        '"v3_rnnt_decoder.onnx" = '
        '"443c3b7bd42b453611618135d6b1e7d9467e5dd97c8a68501da4aa355750c0da"'
    ) in installer
    assert (
        '"v3_rnnt_joint.onnx" = '
        '"fd1d02f45c2ad3d6b67cc149811ad794ab4b020ed49a0a9e2790a8619d1cddd8"'
    ) in installer
    assert (
        '"v3_vocab.txt" = '
        '"a9143c30844d3c0bee3e9e927e4084774eb1b9eeaafc473b2c4521e4911a7c07"'
    ) in installer
    assert '"v3_rnnt_encoder.onnx" = 885084534' in installer
    assert '"v3_rnnt_decoder.onnx" = 3331429' in installer
    assert '"v3_rnnt_joint.onnx" = 1440448' in installer
    assert '"v3_vocab.txt" = 198' in installer
    assert '"config.json" = 135' in installer
    assert (
        "gigaam-v3-onnx/resolve/322c3b29492673eb7d0b434bfa9dfb8653e34d02/"
        "v3_rnnt_encoder.onnx"
    ) in installer
    assert 'pack_id = "gigaam-v3-rnnt-322c3b29"' in installer
    assert 'model_name = "gigaam-v3-rnnt"' in installer
    assert "Assert-FileSizes $asrDestination $asrFileSizes" in installer


def test_default_installer_does_not_download_optional_llm_cleaner_assets() -> None:
    installer = (
        Path(__file__).parents[2] / "deploy" / "install-default-model-packs.ps1"
    ).read_text(encoding="utf-8")

    assert "Qwen/Qwen2.5-1.5B-Instruct-GGUF" not in installer
    assert "ggml-org/llama.cpp/releases" not in installer
    assert "llama-server.exe" not in installer
    assert 'CleanerModelPath = ""' in installer
    assert 'CleanerLlamaServerPath = ""' in installer
    assert '$minimumDiskSpaceBytes = 7GB' in installer
    assert "Refusing an unapproved model download URL." in installer


def test_windows_installers_keep_local_cleaner_stopped_and_disabled_by_default() -> None:
    installer = (
        Path(__file__).parents[2] / "deploy" / "install-windows.ps1"
    ).read_text(encoding="utf-8")
    service_installer = (
        Path(__file__).parents[2] / "deploy" / "install-windows-service.ps1"
    ).read_text(encoding="utf-8")
    cleaner_disabler = (
        Path(__file__).parents[2] / "deploy" / "disable-windows-cleaner-service.ps1"
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
    assert 'disable-windows-cleaner-service.ps1' in installer
    assert 'disable-windows-cleaner-service.ps1' in service_installer
    assert 'install-windows-cleaner-service.ps1' not in service_installer
    assert 'Set-Service -Name $serviceName -StartupType Disabled' in cleaner_disabler
    assert 'Stop-Service -InputObject $cleanerService' in cleaner_disabler
    assert '$env:NEXT_CLEANER_ENABLED = "false"' in cleaner_disabler
    assert "TranscriptionNextCleanerService" in cleaner_disabler


def test_git_installer_stops_cleaner_before_replacing_model_files() -> None:
    installer = (
        Path(__file__).parents[2] / "deploy" / "install-from-git.bat"
    ).read_text(encoding="ascii")

    assert "TranscriptionNextCandidateService" in installer
    assert "TranscriptionNextCleanerService" in installer
    assert "Set-Service -Name $name -StartupType Disabled" in installer


def test_windows_service_installer_resolves_powershell_before_writing_winsw_xml() -> None:
    installer = (
        Path(__file__).parents[2] / "deploy" / "install-windows-service.ps1"
    ).read_text(encoding="utf-8")

    assert (
        "$systemDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::System)"
        in installer
    )
    assert "$powerShellPath = [System.IO.Path]::GetFullPath(" in installer
    assert 'Test-Path -LiteralPath $powerShellPath -PathType Leaf' in installer
    assert "The Windows PowerShell executable was not found: $powerShellPath" in installer
    assert installer.index("$powerShellPath = [System.IO.Path]::GetFullPath(") < installer.index(
        "<executable>$(ConvertTo-XmlValue $powerShellPath)</executable>"
    )
    assert "$env:SystemRoot" not in installer
