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
$argumentLine = '/install /quiet /norestart /log "' + $logPath + '"'
$process = Start-Process -FilePath $installerPath -ArgumentList $argumentLine -Verb RunAs -Wait -PassThru
if ($process.ExitCode -eq 3010) {
    throw "Microsoft Visual C++ Runtime was installed, but Windows must be restarted before continuing."
}
if ($process.ExitCode -notin @(0, 1638)) {
    throw "Microsoft Visual C++ Runtime setup failed with exit code $($process.ExitCode)."
}
