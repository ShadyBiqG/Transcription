[CmdletBinding()]
param(
    [string]$EnvironmentFile
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$serviceName = "TranscriptionNextCleanerService"

function Set-CleanerDisabledEnvironmentValue([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }

    $assignment = '$env:NEXT_CLEANER_ENABLED = "false"'
    $pattern = '^\s*\$env:NEXT_CLEANER_ENABLED\s*='
    $updated = $false
    $rewritten = @(foreach ($line in (Get-Content -LiteralPath $Path)) {
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
    $rewritten | Set-Content -LiteralPath $Path -Encoding utf8
}

$cleanerService = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($null -ne $cleanerService) {
    try {
        if ($cleanerService.Status -ne "Stopped") {
            Write-Host "Stopping the obsolete local cleaner service..."
            Stop-Service -InputObject $cleanerService -ErrorAction Stop
            $cleanerService.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(60))
        }
    }
    finally {
        $cleanerService.Dispose()
    }

    Set-Service -Name $serviceName -StartupType Disabled -ErrorAction Stop
    $cleanerConfiguration = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
    if ($null -eq $cleanerConfiguration -or $cleanerConfiguration.StartMode -ne "Disabled") {
        throw "The local cleaner service could not be disabled: $serviceName"
    }
    Write-Host "Local cleaner service $serviceName is stopped and disabled."
}

Set-CleanerDisabledEnvironmentValue $EnvironmentFile
