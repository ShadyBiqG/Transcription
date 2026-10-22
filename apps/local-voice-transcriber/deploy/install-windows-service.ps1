[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ApplicationRoot,
    [Parameter(Mandatory)][string]$DataRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$serviceName = "TranscriptionNextCandidateService"
$serviceDisplayName = "Transcription Next Candidate"
$localServiceSid = "*S-1-5-19"
$Port = 0
$winSwUrl = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"
$winSwSha256 = "05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA"

function Assert-Administrator {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated PowerShell session."
    }
}

function Grant-DirectoryAccess([string]$Path, [string]$Account, [string]$Rights) {
    $accessRule = "${Account}:(OI)(CI)$Rights"
    $result = & icacls $Path /grant $accessRule /T /C 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not grant $Rights access to $Account for $Path. $($result | Out-String)"
    }
}

function Assert-FileHash([string]$Path, [string]$ExpectedHash) {
    $actualHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if (-not [string]::Equals($actualHash, $ExpectedHash, [StringComparison]::OrdinalIgnoreCase)) {
        throw "SHA-256 mismatch for $Path"
    }
}

function ConvertTo-XmlValue([string]$Value) {
    return [System.Security.SecurityElement]::Escape($Value)
}

function Wait-ServiceStatus([string]$ExpectedStatus, [int]$TimeoutSeconds = 30) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $service = Get-Service -Name $serviceName -ErrorAction Stop
        if ($service.Status.ToString() -eq $ExpectedStatus) {
            return
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    throw "Service $serviceName did not reach $ExpectedStatus within $TimeoutSeconds seconds."
}

function Wait-ServiceRemoval([int]$TimeoutSeconds = 30) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if ($null -eq (Get-Service -Name $serviceName -ErrorAction SilentlyContinue)) {
            return
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    throw "Service $serviceName was not removed within $TimeoutSeconds seconds."
}

function Wait-TcpPortAvailable([int]$TimeoutSeconds = 15) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $listeners = @(
            [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() |
                Where-Object { $_.Port -eq $Port }
        )
        if ($listeners.Count -eq 0) {
            return
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    $addresses = ($listeners | ForEach-Object { $_.Address.ToString() }) -join ", "
    throw "TCP port $Port is already in use ($addresses). Stop the manual candidate launcher before installing the service."
}

function Wait-Health([int]$TimeoutSeconds = 180) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $healthUrl = "http://127.0.0.1:$Port/health"
    do {
        try {
            $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5
            if ($health.status -eq "ok") {
                return
            }
        }
        catch {
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Service $serviceName did not pass the local health check: $healthUrl"
}

function Get-ServiceLogTail([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return "(log file was not created)"
    }
    return (Get-Content -LiteralPath $Path -Tail 80 | Out-String).Trim()
}

Assert-Administrator

$applicationRootPath = (Resolve-Path -LiteralPath $ApplicationRoot).Path
$dataRootPath = [System.IO.Path]::GetFullPath($DataRoot)
$launcherPath = Join-Path $dataRootPath "start-transcription-next.ps1"
if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    throw "The candidate launcher was not found: $launcherPath"
}
$environmentFile = Join-Path $dataRootPath "candidate.env.ps1"
if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
    throw "The candidate environment file was not found: $environmentFile"
}
. $environmentFile
if (-not [int]::TryParse($env:NEXT_PORT, [ref]$Port)) {
    throw "NEXT_PORT in $environmentFile must be a valid TCP port."
}
if ($Port -lt 1 -or $Port -gt 65535) {
    throw "NEXT_PORT in $environmentFile is outside the TCP port range."
}
$pythonLauncher = Join-Path $applicationRootPath ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonLauncher -PathType Leaf)) {
    throw "The candidate Python launcher was not found: $pythonLauncher"
}
$pythonRuntimePath = (& $pythonLauncher -c "import sys; print(sys.base_prefix)" | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($pythonRuntimePath)) {
    throw "Could not determine the Python runtime used by the candidate virtual environment."
}
$pythonRuntimeRoot = (Resolve-Path -LiteralPath $pythonRuntimePath).Path

$serviceRoot = Join-Path $dataRootPath "windows-service"
$serviceLogPath = Join-Path $serviceRoot "logs"
$wrapperPath = Join-Path $serviceRoot "$serviceName.exe"
$configurationPath = Join-Path $serviceRoot "$serviceName.xml"
New-Item -ItemType Directory -Force -Path $serviceRoot, $serviceLogPath | Out-Null

Grant-DirectoryAccess $dataRootPath $localServiceSid "M"
Grant-DirectoryAccess $applicationRootPath $localServiceSid "RX"
Grant-DirectoryAccess $pythonRuntimeRoot $localServiceSid "RX"

if (Test-Path -LiteralPath $wrapperPath -PathType Leaf) {
    Assert-FileHash $wrapperPath $winSwSha256
}
else {
    $temporaryWrapperPath = "$wrapperPath.download-$PID"
    try {
        Write-Host "Downloading the verified Windows service wrapper..."
        Invoke-WebRequest -UseBasicParsing $winSwUrl -OutFile $temporaryWrapperPath
        Assert-FileHash $temporaryWrapperPath $winSwSha256
        Move-Item -LiteralPath $temporaryWrapperPath -Destination $wrapperPath
    }
    finally {
        if (Test-Path -LiteralPath $temporaryWrapperPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryWrapperPath -Force
        }
    }
}

$powerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $launcherPath + '"'
$configuration = @"
<?xml version="1.0" encoding="utf-8"?>
<service>
  <id>$serviceName</id>
  <name>$serviceDisplayName</name>
  <description>Isolated local Transcription Next candidate service.</description>
  <executable>$(ConvertTo-XmlValue $powerShellPath)</executable>
  <arguments>$(ConvertTo-XmlValue $arguments)</arguments>
  <workingdirectory>$(ConvertTo-XmlValue $dataRootPath)</workingdirectory>
  <env name="PYTHONUTF8" value="1" />
  <env name="PYTHONIOENCODING" value="utf-8" />
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
$configuration | Set-Content -LiteralPath $configurationPath -Encoding utf8

$existingService = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($null -ne $existingService) {
    if ($existingService.Status -eq "Running") {
        & $wrapperPath stop
        if ($LASTEXITCODE -ne 0) {
            throw "Could not stop Windows service $serviceName for replacement."
        }
        Wait-ServiceStatus "Stopped"
    }
    & $wrapperPath uninstall
    if ($LASTEXITCODE -ne 0) {
        throw "Could not remove Windows service $serviceName for replacement."
    }
    Wait-ServiceRemoval
}

Wait-TcpPortAvailable
& $wrapperPath install
if ($LASTEXITCODE -ne 0) {
    throw "Could not install Windows service $serviceName."
}

& $wrapperPath start
if ($LASTEXITCODE -ne 0) {
    throw "Could not start Windows service $serviceName."
}
Wait-ServiceStatus "Running"
try {
    Wait-Health
}
catch {
    & $wrapperPath stop | Out-Null
    $errorLog = Get-ServiceLogTail (Join-Path $serviceLogPath "$serviceName.err.log")
    $outputLog = Get-ServiceLogTail (Join-Path $serviceLogPath "$serviceName.out.log")
    throw "$($_.Exception.Message)`nService error log:`n$errorLog`nService output log:`n$outputLog"
}

Write-Host "Windows service $serviceName is running and configured for delayed automatic start."
Write-Host "Logs: $serviceLogPath"
