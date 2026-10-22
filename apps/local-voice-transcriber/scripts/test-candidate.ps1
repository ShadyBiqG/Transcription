[CmdletBinding()]
param(
    [ValidateSet("ui", "jobs", "editor", "auth", "full")]
    [string] $Suite = "full"
)

$ErrorActionPreference = "Stop"
$applicationRoot = Split-Path -Parent $PSScriptRoot

function Invoke-CheckedCommand {
    param([string] $FilePath, [string[]] $Arguments)

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Проверка завершилась с кодом ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

Push-Location $applicationRoot
try {
    Invoke-CheckedCommand "uv" @("run", "--no-sync", "ruff", "check", ".")

    switch ($Suite) {
        "ui" {
            Invoke-CheckedCommand "node" @("--check", "src/local_voice_transcriber/static/app.js")
        }
        "jobs" {
            Invoke-CheckedCommand "uv" @(
                "run", "--no-sync", "pytest", "-x",
                "tests/unit/test_jobs_repository.py",
                "tests/unit/test_jobs_dispatcher.py",
                "tests/unit/test_jobs_pipeline.py",
                "tests/contract/test_jobs_contract.py"
            )
        }
        "editor" {
            Invoke-CheckedCommand "uv" @(
                "run", "--no-sync", "pytest", "-x",
                "tests/unit/test_editor_revisions.py",
                "tests/unit/test_editor_publisher.py",
                "tests/contract/test_editor_contract.py",
                "tests/contract/test_vocabulary_contract.py"
            )
        }
        "auth" {
            Invoke-CheckedCommand "uv" @(
                "run", "--no-sync", "pytest", "-x",
                "tests/unit/test_auth.py",
                "tests/unit/test_admin_user_transactions.py",
                "tests/contract/test_app_contract.py",
                "tests/contract/test_admin_users_contract.py"
            )
        }
        "full" {
            Invoke-CheckedCommand "uv" @("run", "--no-sync", "pytest", "--durations=15")
        }
    }
}
finally {
    Pop-Location
}
