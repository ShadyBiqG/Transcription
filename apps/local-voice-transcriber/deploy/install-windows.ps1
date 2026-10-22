[CmdletBinding()]
param(
    [string]$RepositoryPath = (Join-Path $PSScriptRoot ".."),
    [string]$DataPath = "C:\ProgramData\TranscriptionNext",
    [ValidateRange(1, 65535)][int]$Port = 8010,
    [string]$BootstrapAdminLogin,
    [securestring]$BootstrapAdminPassword,
    [switch]$UpdateRepository,
    [switch]$OpenFirewall
)

$ErrorActionPreference = "Stop"

function Require-Command([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "Не найдена команда '$Name'."
    }
    $command.Source
}

$appRoot = (Resolve-Path -LiteralPath $RepositoryPath).Path
if (-not (Test-Path -LiteralPath (Join-Path $appRoot "pyproject.toml") -PathType Leaf)) {
    throw "В '$appRoot' не найден pyproject.toml Transcription Next."
}

$uv = Require-Command "uv"
if ($UpdateRepository) {
    $git = Require-Command "git"
    & $git -C $appRoot pull --ff-only
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось безопасно обновить репозиторий."
    }
}

$dataRoot = [System.IO.Path]::GetFullPath($DataPath)
New-Item -ItemType Directory -Force -Path $dataRoot, (Join-Path $dataRoot "jobs"), (Join-Path $dataRoot "models"), (Join-Path $dataRoot "logs") | Out-Null

& $uv python install 3.12
if ($LASTEXITCODE -ne 0) {
    throw "Не удалось подготовить Python 3.12."
}
& $uv sync --project $appRoot --locked --no-dev
if ($LASTEXITCODE -ne 0) {
    throw "Не удалось установить зависимости приложения."
}

$environmentFile = Join-Path $dataRoot "candidate.env.ps1"
if (-not (Test-Path -LiteralPath $environmentFile)) {
    if ([string]::IsNullOrWhiteSpace($BootstrapAdminLogin)) {
        $BootstrapAdminLogin = Read-Host "Логин первого администратора"
    }
    if ($null -eq $BootstrapAdminPassword) {
        $BootstrapAdminPassword = Read-Host "Пароль первого администратора" -AsSecureString
    }
    if ($null -eq $BootstrapAdminPassword -or $BootstrapAdminPassword.Length -lt 12) {
        throw "Пароль первого администратора должен содержать не менее 12 символов."
    }
    $environmentLines = @(
        "# Создано install-windows.ps1. Секреты и manifests не хранятся в репозитории."
        ('$env:NEXT_DATA_DIR = "' + $dataRoot + '"')
        ('$env:NEXT_DATABASE_PATH = "' + (Join-Path $dataRoot "transcription-next.sqlite3") + '"')
        ('$env:NEXT_ZEUSDB_DIR = "' + (Join-Path $dataRoot "zeusdb") + '"')
        ('$env:NEXT_MODELS_DIR = "' + (Join-Path $dataRoot "models") + '"')
        '$env:NEXT_HOST = "127.0.0.1"'
        ('$env:NEXT_PORT = "' + $Port + '"')
        '$env:NEXT_INSTANCE_ID = "transcription-next-candidate"'
        '$env:NEXT_COOKIE_SECURE = "true"'
    )
    $environmentLines | Set-Content -LiteralPath $environmentFile -Encoding utf8

    . $environmentFile
    $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($BootstrapAdminPassword)
    try {
        $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
        $env:NEXT_ADMIN_PASSWORD_INPUT = $plainPassword
        & $uv run --project $appRoot --no-sync local-voice-transcriber --create-admin $BootstrapAdminLogin
        if ($LASTEXITCODE -ne 0) {
            throw "Не удалось создать первого администратора."
        }
    }
    finally {
        Remove-Item Env:NEXT_ADMIN_PASSWORD_INPUT -ErrorAction SilentlyContinue
        if ($null -ne $passwordPointer) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
        }
    }
}

$launcher = Join-Path $dataRoot "start-transcription-next.ps1"
'$ErrorActionPreference = "Stop"' | Set-Content -LiteralPath $launcher -Encoding utf8
('. "' + $environmentFile + '"') | Add-Content -LiteralPath $launcher -Encoding utf8
('& "' + $uv + '" run --project "' + $appRoot + '" --no-sync local-voice-transcriber') | Add-Content -LiteralPath $launcher -Encoding utf8

if ($OpenFirewall) {
    $ruleName = "Transcription Next candidate TCP $Port"
    if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
    }
}

Write-Host "Transcription Next подготовлен."
Write-Host "Данные: $dataRoot"
Write-Host "Конфигурация: $environmentFile"
Write-Host "Запуск: powershell -ExecutionPolicy Bypass -File $launcher"
Write-Host "Используйте отдельную service account без ACL на legacy data."
