[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$DataRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Очиститель отделён от основного приложения: неисправный llama.cpp никогда не
# должен запрещать запуск Transcription Next с rule-based fallback.
$serviceName = "TranscriptionNextCleanerService"
$serviceDisplayName = "Transcription Next Local Cleaner"
$localServiceSid = "*S-1-5-19"
$cleanerHost = "127.0.0.1"
$cleanerPort = 8020
$winSwUrl = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"
$winSwSha256 = "05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA"

function Assert-Administrator {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated PowerShell session."
    }
}

function Assert-FileHash([string]$Path, [string]$ExpectedHash) {
    $actualHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if (-not [string]::Equals($actualHash, $ExpectedHash, [StringComparison]::OrdinalIgnoreCase)) {
        throw "SHA-256 mismatch for $Path"
    }
}

function ConvertTo-PowerShellLiteral([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

function ConvertTo-XmlValue([string]$Value) {
    return [System.Security.SecurityElement]::Escape($Value)
}

function Set-EnvironmentValue(
    [string]$EnvironmentFile,
    [string]$Name,
    [string]$Value
) {
    $assignment = '$env:' + $Name + ' = ' + (ConvertTo-PowerShellLiteral $Value)
    $lines = @(Get-Content -LiteralPath $EnvironmentFile)
    $pattern = '^\s*\$env:' + [regex]::Escape($Name) + '\s*='
    $updated = $false
    $rewritten = @(foreach ($line in $lines) {
        if ($line -match $pattern) {
            $updated = $true
            $assignment
        }
        else {
            $line
        }
    })
    if (-not $updated) {
        $rewritten += $assignment
    }
    $rewritten | Set-Content -LiteralPath $EnvironmentFile -Encoding utf8
}

function Get-ServiceLogTail([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return "(log file was not created)"
    }
    return (Get-Content -LiteralPath $Path -Tail 80 | Out-String).Trim()
}

function Wait-ServiceStatus([string]$ExpectedStatus, [int]$TimeoutSeconds = 30) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $service = Get-Service -Name $serviceName -ErrorAction Stop
        $matches = $service.Status.ToString() -eq $ExpectedStatus
        $service.Dispose()
        if ($matches) {
            return
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    throw "Service $serviceName did not reach $ExpectedStatus within $TimeoutSeconds seconds."
}

function Wait-ServiceRemoval([int]$TimeoutSeconds = 30) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        if ($null -eq $service) {
            return
        }
        $service.Dispose()
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    throw "Service $serviceName was not removed within $TimeoutSeconds seconds."
}

function Wait-CleanerPortAvailable([int]$TimeoutSeconds = 15) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $listeners = @(
            [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() |
                Where-Object { $_.Port -eq $cleanerPort }
        )
        if ($listeners.Count -eq 0) {
            return
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    $addresses = ($listeners | ForEach-Object { $_.Address.ToString() }) -join ", "
    throw "TCP port $cleanerPort is already in use ($addresses). Stop the manual llama-server before installing the cleaner service."
}

function Wait-CleanerHealth([int]$TimeoutSeconds = 240) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $healthUrl = "http://${cleanerHost}:$cleanerPort/health"
    $lastDetail = "No HTTP response."
    do {
        $service = Get-Service -Name $serviceName -ErrorAction Stop
        $stopped = $service.Status -eq "Stopped"
        $service.Dispose()
        if ($stopped) {
            throw "Service $serviceName exited before the health check completed."
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 5
            if ($response.StatusCode -eq 200) {
                return
            }
            $lastDetail = "HTTP $($response.StatusCode)"
        }
        catch {
            $lastDetail = $_.Exception.Message
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Service $serviceName did not pass the local health check: $healthUrl. Last response: $lastDetail"
}

function Grant-DirectoryReadExecute([string]$Path, [string]$Account) {
    $result = & icacls $Path /grant:r "${Account}:(OI)(CI)RX" /T /C 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not grant Read/Execute access to $Account for $Path. $($result | Out-String)"
    }
}

function Protect-ServiceFile([string]$Path) {
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $true)
    Set-Acl -LiteralPath $Path -AclObject $acl
    $result = & icacls $Path /grant:r "${localServiceSid}:RX" 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not protect the cleaner service file: $Path. $($result | Out-String)"
    }
}

function Ensure-VerifiedWinSw([string]$WrapperPath) {
    if (Test-Path -LiteralPath $WrapperPath -PathType Leaf) {
        Assert-FileHash $WrapperPath $winSwSha256
        return
    }
    $temporaryWrapperPath = "$WrapperPath.download-$PID"
    try {
        Write-Host "Downloading the verified Windows service wrapper for the local cleaner..."
        Invoke-WebRequest -UseBasicParsing $winSwUrl -OutFile $temporaryWrapperPath
        Assert-FileHash $temporaryWrapperPath $winSwSha256
        Move-Item -LiteralPath $temporaryWrapperPath -Destination $WrapperPath
    }
    finally {
        if (Test-Path -LiteralPath $temporaryWrapperPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryWrapperPath -Force
        }
    }
}

function Archive-ServiceLogs([string]$ServiceLogPath) {
    $attemptId = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ") + "-" + [guid]::NewGuid().ToString("N")
    $archivePath = Join-Path $ServiceLogPath ("previous-" + $attemptId)
    foreach ($logName in @("$serviceName.err.log", "$serviceName.out.log", "$serviceName.wrapper.log")) {
        $logPath = Join-Path $ServiceLogPath $logName
        if (Test-Path -LiteralPath $logPath -PathType Leaf) {
            New-Item -ItemType Directory -Force -Path $archivePath | Out-Null
            Move-Item -LiteralPath $logPath -Destination (Join-Path $archivePath $logName)
        }
    }
    return $archivePath
}

function Get-CleanerThreadCount {
    return [Math]::Max(1, [Math]::Min(4, [Environment]::ProcessorCount - 1))
}

$environmentFile = Join-Path ([System.IO.Path]::GetFullPath($DataRoot)) "candidate.env.ps1"
$dataRootPath = [System.IO.Path]::GetFullPath($DataRoot)
$serviceRoot = Join-Path $dataRootPath "windows-service"
$serviceLogPath = Join-Path $serviceRoot "logs"
$wrapperPath = Join-Path $serviceRoot "$serviceName.exe"
$configurationPath = Join-Path $serviceRoot "$serviceName.xml"

try {
    Assert-Administrator
    if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
        throw "The candidate environment file was not found: $environmentFile"
    }
    . $environmentFile
    # Не сохраняем устаревшее включённое состояние при замене процесса.
    Set-EnvironmentValue $environmentFile "NEXT_CLEANER_ENDPOINT" "http://${cleanerHost}:$cleanerPort"
    Set-EnvironmentValue $environmentFile "NEXT_CLEANER_ENABLED" "false"

    $llamaServerPath = $env:NEXT_CLEANER_LLAMA_SERVER_PATH
    $modelPath = $env:NEXT_CLEANER_MODEL_PATH
    if ([string]::IsNullOrWhiteSpace($llamaServerPath) -or -not (Test-Path -LiteralPath $llamaServerPath -PathType Leaf)) {
        throw "NEXT_CLEANER_LLAMA_SERVER_PATH does not point to installed llama-server.exe."
    }
    if ([string]::IsNullOrWhiteSpace($modelPath) -or -not (Test-Path -LiteralPath $modelPath -PathType Leaf)) {
        throw "NEXT_CLEANER_MODEL_PATH does not point to the installed GGUF model."
    }

    New-Item -ItemType Directory -Force -Path $serviceRoot, $serviceLogPath | Out-Null
    Grant-DirectoryReadExecute (Split-Path -Parent $llamaServerPath) $localServiceSid
    Grant-DirectoryReadExecute (Split-Path -Parent $modelPath) $localServiceSid
    Ensure-VerifiedWinSw $wrapperPath
    Protect-ServiceFile $wrapperPath

    $existingService = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if ($null -ne $existingService -and $existingService.Status -ne "Stopped") {
        Stop-Service -InputObject $existingService -ErrorAction Stop
        Wait-ServiceStatus "Stopped" 60
    }
    Wait-CleanerPortAvailable

    $threads = Get-CleanerThreadCount
    # Аргументы llama.cpp составляются только из установленных проверенных путей.
    $arguments = '--model "' + $modelPath.Replace('"', '') + '" --host ' + $cleanerHost +
        ' --port ' + $cleanerPort + ' --ctx-size 4096 --threads ' + $threads + ' --parallel 1'
    $configuration = @"
<?xml version="1.0" encoding="utf-8"?>
<service>
  <id>$serviceName</id>
  <name>$serviceDisplayName</name>
  <description>Isolated loopback-only local LLM cleaner for Transcription Next.</description>
  <executable>$(ConvertTo-XmlValue $llamaServerPath)</executable>
  <arguments>$(ConvertTo-XmlValue $arguments)</arguments>
  <workingdirectory>$(ConvertTo-XmlValue $dataRootPath)</workingdirectory>
  <startmode>Automatic</startmode>
  <delayedAutoStart />
  <stoptimeout>30 sec</stoptimeout>
  <stopparentprocessfirst>true</stopparentprocessfirst>
  <logpath>$(ConvertTo-XmlValue $serviceLogPath)</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>5</keepFiles>
  </log>
  <onfailure action="restart" delay="10 sec" />
  <resetfailure>1 hour</resetfailure>
  <serviceaccount>
    <domain>NT AUTHORITY</domain>
    <user>LocalService</user>
  </serviceaccount>
</service>
"@
    if ($null -ne $existingService) {
        $existingService.Dispose()
        & $wrapperPath uninstall
        if ($LASTEXITCODE -ne 0) {
            throw "Could not remove Windows service $serviceName for replacement."
        }
        Wait-ServiceRemoval
    }

    $configuration | Set-Content -LiteralPath $configurationPath -Encoding utf8
    Protect-ServiceFile $configurationPath
    Wait-CleanerPortAvailable
    & $wrapperPath install
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install Windows service $serviceName."
    }

    $archivePath = Archive-ServiceLogs $serviceLogPath
    & $wrapperPath start
    if ($LASTEXITCODE -ne 0) {
        throw "Could not start Windows service $serviceName."
    }
    Wait-ServiceStatus "Running"
    $installedService = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
    $installedAccountSid = ([System.Security.Principal.NTAccount]::new($installedService.StartName).Translate(
        [System.Security.Principal.SecurityIdentifier]
    )).Value
    if ($installedAccountSid -ne "S-1-5-19") {
        throw "The cleaner service was not installed as LocalService: $($installedService.StartName)"
    }
    Wait-CleanerHealth

    # Родительский установщик запускает приложение только после этой метки.
    # Поэтому работающий cleaner не может быть объявлен доступным преждевременно.
    Set-EnvironmentValue $environmentFile "NEXT_CLEANER_ENABLED" "true"
    Write-Host "Local cleaner service $serviceName is running at http://${cleanerHost}:$cleanerPort."
}
catch {
    $failure = $_.Exception.Message
    try {
        $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        if ($null -ne $service) {
            if ($service.Status -ne "Stopped") {
                Stop-Service -InputObject $service -ErrorAction Stop
                Wait-ServiceStatus "Stopped" 60
            }
            $service.Dispose()
        }
    }
    catch {
        $failure += "`nCould not stop the failed cleaner service: $($_.Exception.Message)"
    }
    if (Test-Path -LiteralPath $environmentFile -PathType Leaf) {
        Set-EnvironmentValue $environmentFile "NEXT_CLEANER_ENABLED" "false"
    }
    $errorLog = Get-ServiceLogTail (Join-Path $serviceLogPath "$serviceName.err.log")
    $outputLog = Get-ServiceLogTail (Join-Path $serviceLogPath "$serviceName.out.log")
    Write-Warning "Локальный очиститель не запущен. Основная служба будет установлена с rule-based очисткой. Причина: $failure`nCleaner stderr:`n$errorLog`nCleaner stdout:`n$outputLog"
}

# Родительский установщик намеренно продолжает работу при сбое этой необязательной
# локальной модели: основная служба имеет детерминированный offline fallback.
exit 0
