[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$DataRoot
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Test-MicrosoftSignature([string]$Path) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne "Valid") {
        throw "Microsoft Visual C++ Runtime signature is not valid: $($signature.Status)"
    }
    if ($null -eq $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notmatch 'CN=Microsoft Corporation') {
        throw "Microsoft Visual C++ Runtime is not signed by Microsoft Corporation."
    }
}

function Invoke-VisualCppSetup([string]$Action, [string]$InstallerPath, [string]$LogPath) {
    $argumentLine = '/' + $Action + ' /quiet /norestart /log "' + $LogPath + '"'
    $process = Start-Process -FilePath $InstallerPath -ArgumentList $argumentLine -Verb RunAs -Wait -PassThru
    return $process.ExitCode
}

$dataDirectory = [System.IO.Path]::GetFullPath($DataRoot)
$bootstrapDirectory = Join-Path $dataDirectory "tools\bootstrap"
$installerPath = Join-Path $bootstrapDirectory "vc_redist.x64.exe"
$partialPath = "$installerPath.partial-$PID"
$logPath = Join-Path $dataDirectory "logs\vc-redist-install.log"
New-Item -ItemType Directory -Force -Path $bootstrapDirectory | Out-Null

if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
    try {
        Write-Host "Downloading Microsoft Visual C++ Runtime prerequisite..."
        Invoke-WebRequest -UseBasicParsing -Uri "https://aka.ms/vc14/vc_redist.x64.exe" -OutFile $partialPath
        Test-MicrosoftSignature $partialPath
        Move-Item -LiteralPath $partialPath -Destination $installerPath -ErrorAction Stop
    }
    finally {
        if (Test-Path -LiteralPath $partialPath -PathType Leaf) {
            Remove-Item -LiteralPath $partialPath -Force
        }
    }
}

Test-MicrosoftSignature $installerPath
Write-Host "Installing Microsoft Visual C++ Runtime prerequisite..."
$exitCode = Invoke-VisualCppSetup "install" $installerPath $logPath
if ($exitCode -eq 1638) {
    Write-Host "Repairing the existing Microsoft Visual C++ Runtime..."
    $exitCode = Invoke-VisualCppSetup "repair" $installerPath $logPath
}
if ($exitCode -eq 3010) {
    throw "Microsoft Visual C++ Runtime was installed, but Windows must be restarted before continuing."
}
if ($exitCode -ne 0) {
    throw "Microsoft Visual C++ Runtime setup failed with exit code $exitCode. See $logPath"
}
