[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$ForceSync,
    [string]$HealthUrl = "http://127.0.0.1:8000/api/v1/health"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$LogDirectory = Join-Path $PSScriptRoot "update-logs"
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDirectory "update-$Timestamp.log"
$LockPath = Join-Path $env:TEMP "LocalTranscriptionService-update.lock"
$ServiceName = "LocalTranscriptionService"
$ServiceExecutable = Join-Path $PSScriptRoot "TranscriptionService.exe"
$PythonExecutable = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$OldCommit = $null
$CodeUpdated = $false
$ServiceWasRunning = $false
$LockCreated = $false
$TranscriptStarted = $false
$ExitCode = 0

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message" -ForegroundColor Cyan
}

function Invoke-Native {
    param(
        [string]$Description,
        [string]$FilePath,
        [string[]]$ArgumentList
    )
    Write-Step $Description
    Write-Host "> $FilePath $($ArgumentList -join ' ')"
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Description завершено с кодом $LASTEXITCODE"
    }
}

function Get-DataDirectory {
    if ($env:TRANSCRIPTION_DATA_DIR) {
        $ConfiguredPath = $env:TRANSCRIPTION_DATA_DIR.Trim().Trim('"').Trim("'")
    }
    else {
        $EnvironmentFile = Join-Path $ProjectRoot ".env"
        if (-not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) {
            return Join-Path $ProjectRoot "data"
        }
        $Setting = Get-Content -LiteralPath $EnvironmentFile -Encoding UTF8 |
            Where-Object { $_ -match '^\s*TRANSCRIPTION_DATA_DIR\s*=' } |
            Select-Object -Last 1
        if (-not $Setting) {
            return Join-Path $ProjectRoot "data"
        }
        $ConfiguredPath = ($Setting -split '=', 2)[1].Trim().Trim('"').Trim("'")
    }

    if ([IO.Path]::IsPathRooted($ConfiguredPath)) {
        return [IO.Path]::GetFullPath($ConfiguredPath)
    }
    return [IO.Path]::GetFullPath((Join-Path $ProjectRoot $ConfiguredPath))
}

function Wait-ServiceState {
    param(
        [string]$ExpectedStatus,
        [int]$TimeoutSeconds = 30
    )
    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $Current = Get-Service -Name $ServiceName -ErrorAction Stop
        if ($Current.Status.ToString() -eq $ExpectedStatus) {
            return
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $Deadline)
    throw "Служба не перешла в состояние $ExpectedStatus за $TimeoutSeconds секунд"
}

function Start-OriginalServiceAfterPreUpdateFailure {
    if (-not $ServiceWasRunning -or $CodeUpdated) {
        return
    }
    try {
        $Current = Get-Service -Name $ServiceName -ErrorAction Stop
        if ($Current.Status -ne 'Running') {
            Write-Step "Возврат исходной версии службы в работу"
            & $ServiceExecutable start
            Wait-ServiceState -ExpectedStatus "Running"
        }
    }
    catch {
        Write-Warning "Не удалось повторно запустить исходную версию службы: $($_.Exception.Message)"
    }
}

try {
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
    Start-Transcript -LiteralPath $LogPath -Append | Out-Null
    $TranscriptStarted = $true
    Write-Host "Журнал обновления: $LogPath" -ForegroundColor Green

    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
    $IsAdministrator = $Principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
    if (-not $IsAdministrator) {
        throw "Запустите update-project.cmd из PowerShell или cmd от имени администратора"
    }

    try {
        New-Item -ItemType Directory -Path $LockPath -ErrorAction Stop | Out-Null
        $LockCreated = $true
    }
    catch {
        throw "Обновление уже запущено либо остался lock-каталог: $LockPath"
    }

    Write-Step "Предварительная проверка"
    Write-Host "Пользователь: $($Identity.Name)"
    Write-Host "Каталог проекта: $ProjectRoot"
    foreach ($Command in @("git.exe", "uv.exe")) {
        if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
            throw "Команда $Command не найдена в PATH"
        }
    }
    if (-not (Test-Path -LiteralPath $ServiceExecutable -PathType Leaf)) {
        throw "Не найден служебный файл: $ServiceExecutable"
    }
    if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
        throw "Не найден Python виртуального окружения: $PythonExecutable"
    }

    $ActualRoot = (& git.exe -C $ProjectRoot rev-parse --show-toplevel).Trim()
    if ($LASTEXITCODE -ne 0 -or
        [IO.Path]::GetFullPath($ActualRoot) -ne [IO.Path]::GetFullPath($ProjectRoot)) {
        throw "Каталог не является корнем ожидаемого Git-репозитория"
    }

    $GitStatus = @(& git.exe -C $ProjectRoot status --porcelain)
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось проверить состояние Git"
    }
    if ($GitStatus.Count -gt 0) {
        Write-Host ($GitStatus -join [Environment]::NewLine)
        throw "В репозитории есть несохранённые изменения. Обновление отменено без изменения файлов"
    }

    $Branch = (& git.exe -C $ProjectRoot branch --show-current).Trim()
    if (-not $Branch) {
        throw "Репозиторий находится в detached HEAD. Переключитесь на рабочую ветку"
    }
    $OldCommit = (& git.exe -C $ProjectRoot rev-parse HEAD).Trim()
    $Upstream = (& git.exe -C $ProjectRoot rev-parse --abbrev-ref --symbolic-full-name '@{upstream}').Trim()
    if ($LASTEXITCODE -ne 0 -or -not $Upstream) {
        throw "Для ветки $Branch не настроена upstream-ветка"
    }
    Write-Host "Ветка: $Branch; upstream: $Upstream; текущий commit: $OldCommit"

    $env:GIT_TERMINAL_PROMPT = "0"
    Invoke-Native "Получение сведений об обновлениях" "git.exe" @(
        "-C", $ProjectRoot, "fetch", "--prune"
    )
    & git.exe -C $ProjectRoot merge-base --is-ancestor HEAD $Upstream
    if ($LASTEXITCODE -ne 0) {
        throw "Локальная и удалённая ветки разошлись либо есть локальные commits. Автоматическое обновление запрещено"
    }

    $RemoteCommit = (& git.exe -C $ProjectRoot rev-parse $Upstream).Trim()
    if ($OldCommit -eq $RemoteCommit -and -not $ForceSync) {
        Write-Step "Новых commits нет"
        Write-Host "Для принудительной синхронизации зависимостей используйте: update-project.cmd -ForceSync"
        return
    }

    $DataDirectory = Get-DataDirectory
    $BackupDirectory = Join-Path $DataDirectory "backups\update-$Timestamp"
    New-Item -ItemType Directory -Path $BackupDirectory -Force | Out-Null
    Write-Host "Резервная копия: $BackupDirectory"

    Invoke-Native "Создание Git bundle исходной версии" "git.exe" @(
        "-C", $ProjectRoot, "bundle", "create",
        (Join-Path $BackupDirectory "repository.bundle"), "--all"
    )
    if (Test-Path -LiteralPath (Join-Path $ProjectRoot ".env") -PathType Leaf) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot ".env") `
            -Destination (Join-Path $BackupDirectory ".env")
    }

    $Service = Get-Service -Name $ServiceName -ErrorAction Stop
    $ServiceWasRunning = $Service.Status -eq 'Running'
    if ($ServiceWasRunning) {
        Invoke-Native "Остановка Windows-службы" $ServiceExecutable @("stop")
        Wait-ServiceState -ExpectedStatus "Stopped"
    }
    else {
        Write-Step "Служба уже остановлена; это состояние будет сохранено"
    }

    Write-Step "Резервное копирование SQLite"
    $DatabasePath = Join-Path $DataDirectory "jobs.sqlite3"
    if (Test-Path -LiteralPath $DatabasePath -PathType Leaf) {
        foreach ($Suffix in @("", "-wal", "-shm")) {
            $Source = "$DatabasePath$Suffix"
            if (Test-Path -LiteralPath $Source -PathType Leaf) {
                Copy-Item -LiteralPath $Source -Destination $BackupDirectory
            }
        }
        $DatabaseBackup = Join-Path $BackupDirectory "jobs.sqlite3"
        $CheckCode = "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); " +
            "r=c.execute('PRAGMA quick_check').fetchone()[0]; c.close(); " +
            "print('SQLite quick_check:', r); raise SystemExit(0 if r == 'ok' else 1)"
        Invoke-Native "Проверка резервной копии SQLite" $PythonExecutable @(
            "-c", $CheckCode, $DatabaseBackup
        )
        Get-ChildItem -LiteralPath $BackupDirectory -Filter "jobs.sqlite3*" -File |
            Get-FileHash | Format-List Algorithm, Hash, Path
    }
    else {
        Write-Warning "База данных не найдена: $DatabasePath"
    }

    Invoke-Native "Обновление исходного кода без merge commit" "git.exe" @(
        "-C", $ProjectRoot, "pull", "--ff-only"
    )
    $CodeUpdated = $OldCommit -ne ((& git.exe -C $ProjectRoot rev-parse HEAD).Trim())

    if (-not $SkipTests) {
        Invoke-Native "Установка зависимостей для проверки" "uv.exe" @(
            "sync", "--directory", $ProjectRoot, "--frozen"
        )
        Invoke-Native "Проверка Ruff" "uv.exe" @(
            "run", "--directory", $ProjectRoot, "ruff", "check", "."
        )
        Invoke-Native "Запуск pytest" "uv.exe" @(
            "run", "--directory", $ProjectRoot, "pytest"
        )
    }

    Invoke-Native "Синхронизация production-зависимостей" "uv.exe" @(
        "sync", "--directory", $ProjectRoot, "--frozen", "--no-dev"
    )

    if ($ServiceWasRunning) {
        Invoke-Native "Запуск Windows-службы" $ServiceExecutable @("start")
        Wait-ServiceState -ExpectedStatus "Running"

        Write-Step "Проверка API"
        $Healthy = $false
        for ($Attempt = 1; $Attempt -le 15; $Attempt++) {
            try {
                $Health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5
                if ($Health.service -eq "ok") {
                    $Healthy = $true
                    $Health | ConvertTo-Json -Depth 5
                    break
                }
            }
            catch {
                Write-Host "Health-check $Attempt/15: $($_.Exception.Message)"
            }
            Start-Sleep -Seconds 2
        }
        if (-not $Healthy) {
            & $ServiceExecutable stop
            throw "Новая версия не прошла health-check; служба остановлена"
        }
    }

    $NewCommit = (& git.exe -C $ProjectRoot rev-parse HEAD).Trim()
    Write-Step "Обновление успешно завершено"
    Write-Host "Было:  $OldCommit"
    Write-Host "Стало: $NewCommit"
    Write-Host "Backup: $BackupDirectory"
}
catch {
    $ExitCode = 1
    Write-Host ""
    Write-Host "[ОШИБКА] $($_.Exception.Message)" -ForegroundColor Red
    Start-OriginalServiceAfterPreUpdateFailure
    if ($CodeUpdated) {
        Write-Warning "Код уже обновлён. Автоматический разрушительный rollback не выполнялся. Используйте Git bundle и backup из журнала для ручного восстановления."
    }
}
finally {
    if ($LockCreated -and (Test-Path -LiteralPath $LockPath -PathType Container)) {
        Remove-Item -LiteralPath $LockPath -Force
    }
    if ($TranscriptStarted) {
        Stop-Transcript | Out-Null
    }
    Write-Host "Журнал обновления: $LogPath"
}

exit $ExitCode
