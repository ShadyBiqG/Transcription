[CmdletBinding()]
param(
    [string]$RepositoryPath = (Join-Path $PSScriptRoot ".."),
    [string]$DataPath = "C:\ProgramData\TranscriptionNext",
    [ValidateRange(1, 65535)][int]$Port = 8010,
    [string]$ServiceAccount,
    [string]$BootstrapAdminLogin,
    [securestring]$BootstrapAdminPassword,
    [switch]$UpdateRepository,
    [switch]$OpenFirewall
)

$ErrorActionPreference = "Stop"

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated PowerShell session."
}

if ([string]::IsNullOrWhiteSpace($ServiceAccount)) {
    $ServiceAccount = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
}

function Require-Command([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "Required command was not found: $Name"
    }
    return $command.Source
}

function Grant-DataRootModifyAccess([string]$DataRoot, [string]$Account) {
    $accountSid = ([System.Security.Principal.NTAccount]::new($Account).Translate(
        [System.Security.Principal.SecurityIdentifier]
    )).Value
    $accessRule = "*${accountSid}:(OI)(CI)M"
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $result = & icacls $DataRoot /grant:r $accessRule /T /C 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Could not grant Modify access to $Account for $DataRoot. $($result | Out-String)"
        }
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

function ConvertTo-PowerShellLiteral([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

function Assert-ResolvableWindowsAccount([string]$Account) {
    try {
        [void]([System.Security.Principal.NTAccount]::new($Account).Translate(
            [System.Security.Principal.SecurityIdentifier]
        ))
    }
    catch {
        throw "The Windows service account could not be resolved: $Account"
    }
}

function Ensure-EnvironmentValue(
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

function Test-OnnxRuntimeImport([string]$PythonPath) {
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $PythonPath -I -X utf8 -c "import onnxruntime" 1>$null 2>$null
        return $LASTEXITCODE -eq 0
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

function Get-OnnxRuntimeDiagnostic([string]$PythonPath) {
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $detail = & $PythonPath -I -X utf8 -c "import onnxruntime" 2>&1
        return (($detail | Out-String).Trim())
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

function Test-ActiveAdministrator([string]$PythonPath) {
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $detail = & $PythonPath -I -X utf8 -c "from local_voice_transcriber.config import Settings; from local_voice_transcriber.database import Database; database = Database(Settings.from_env().database_path); database.initialize(); raise SystemExit(0 if database.count_active_admins() else 3)" 2>&1
        if ($LASTEXITCODE -notin @(0, 3)) {
            throw "Could not check the candidate administrator. $($detail | Out-String)"
        }
        return $LASTEXITCODE -eq 0
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

function Assert-InstalledInterfaceAssets([string]$PythonPath, [string]$ApplicationRoot) {
    $verificationProgram = @'
from pathlib import Path
import sys
import local_voice_transcriber

source_root = Path(sys.argv[1]).resolve() / "src" / "local_voice_transcriber" / "static"
installed_root = Path(local_voice_transcriber.__file__).resolve().parent / "static"
names = ("index.html", "app.js", "styles.css")
mismatches = [
    name for name in names
    if not (source_root / name).is_file()
    or not (installed_root / name).is_file()
    or (source_root / name).read_bytes() != (installed_root / name).read_bytes()
]
if mismatches:
    raise SystemExit("Файлы интерфейса отличаются: " + ", ".join(mismatches))
print("Файлы интерфейса проверены.")
'@
    $detail = & $PythonPath -I -X utf8 -c $verificationProgram $ApplicationRoot 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Установленные файлы интерфейса не совпадают с обновлёнными исходниками. $($detail | Out-String)"
    }
    Write-Host (($detail | Out-String).Trim())
}

function Get-WindowsDescription() {
    try {
        $operatingSystem = Get-CimInstance -ClassName Win32_OperatingSystem
        return "$($operatingSystem.Caption), version $($operatingSystem.Version), build $($operatingSystem.BuildNumber)"
    }
    catch {
        return "Windows version could not be determined."
    }
}

$appRoot = (Resolve-Path -LiteralPath $RepositoryPath).Path
foreach ($requiredFile in @("pyproject.toml", "uv.lock", "src\local_voice_transcriber\__main__.py")) {
    if (-not (Test-Path -LiteralPath (Join-Path $appRoot $requiredFile) -PathType Leaf)) {
        throw "The candidate application file was not found: $appRoot\$requiredFile"
    }
}

$uv = Require-Command "uv"

$dataRoot = [System.IO.Path]::GetFullPath($DataPath)
if ($dataRoot.TrimEnd('\') -eq [System.IO.Path]::GetPathRoot($dataRoot).TrimEnd('\') -or
    $dataRoot.TrimEnd('\') -eq $appRoot.TrimEnd('\') -or
    $dataRoot.StartsWith($appRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $appRoot.StartsWith($dataRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "DataPath must be a separate candidate directory, outside the application directory and volume root."
}
$candidateService = Get-Service -Name "TranscriptionNextCandidateService" -ErrorAction SilentlyContinue
if ($null -ne $candidateService -and $candidateService.Status -ne "Stopped") {
    Write-Host "Stopping the candidate service before replacing its virtual environment..."
    Stop-Service -InputObject $candidateService -ErrorAction Stop
    $candidateService.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(60))
}
if ($null -ne $candidateService) {
    $candidateService.Dispose()
}
$portDeadline = (Get-Date).AddSeconds(15)
do {
    $listeners = @([Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() |
        Where-Object { $_.Port -eq $Port })
    if ($listeners.Count -eq 0) {
        break
    }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $portDeadline)
if ($listeners.Count -ne 0) {
    throw "Candidate TCP port $Port is still in use. Stop the manual candidate launcher before rebuilding its environment."
}
if ($UpdateRepository) {
    $git = Require-Command "git"
    $worktreeStatus = & $git -C $appRoot status --porcelain
    if ($LASTEXITCODE -ne 0 -or -not [string]::IsNullOrWhiteSpace(($worktreeStatus | Out-String))) {
        throw "The candidate Git clone must be clean before updating."
    }
    & $git -C $appRoot pull --ff-only
    if ($LASTEXITCODE -ne 0) {
        throw "Could not safely update the repository."
    }
}
New-Item -ItemType Directory -Force -Path $dataRoot, (Join-Path $dataRoot "jobs"), (Join-Path $dataRoot "models"), (Join-Path $dataRoot "logs") | Out-Null
Assert-ResolvableWindowsAccount $ServiceAccount
Grant-DataRootModifyAccess $dataRoot $ServiceAccount

$pythonInstallationRoot = Join-Path $dataRoot "python-runtime"
$virtualEnvironmentPath = Join-Path $appRoot ".venv"
$existingVenv = Get-Item -LiteralPath $virtualEnvironmentPath -Force -ErrorAction SilentlyContinue
if ($null -ne $existingVenv -and ($existingVenv.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Refusing to rebuild a candidate virtual environment that is a link: $virtualEnvironmentPath"
}
$savedEnvironment = @{}
foreach ($name in @("UV_PYTHON_INSTALL_DIR", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV")) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}
try {
    # The service cannot rely on a Python runtime stored in an interactive user's profile.
    $env:UV_PYTHON_INSTALL_DIR = $pythonInstallationRoot
    $env:UV_PROJECT_ENVIRONMENT = $virtualEnvironmentPath
    Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
    & $uv python install 3.12 --no-bin --no-registry
    if ($LASTEXITCODE -ne 0) {
        throw "Could not prepare the isolated Python 3.12 runtime."
    }
    $managedPython = (& $uv python find 3.12 --managed-python --system --no-project --no-python-downloads | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $managedPython.StartsWith(
        $pythonInstallationRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "uv did not select Python from the isolated runtime directory: $managedPython"
    }
    & $uv venv $virtualEnvironmentPath --clear --no-project --python $managedPython --no-python-downloads
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the candidate virtual environment."
    }
    & $uv sync --project $appRoot --locked --no-dev --no-editable --link-mode copy `
        --reinstall-package local-voice-transcriber --refresh-package local-voice-transcriber `
        --python $managedPython --no-python-downloads
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install the application dependencies."
    }
}
finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
    }
}

$applicationLauncher = Join-Path $virtualEnvironmentPath "Scripts\python.exe"
& $applicationLauncher -I -X utf8 -c "import pathlib, sys, local_voice_transcriber; package = pathlib.Path(local_voice_transcriber.__file__).resolve(); expected = pathlib.Path(sys.prefix, 'Lib', 'site-packages').resolve(); assert package.is_relative_to(expected), (package, expected); print('Candidate Python:', sys.executable); print('Base runtime:', sys.base_prefix); print('Installed package:', package)"
if ($LASTEXITCODE -ne 0) {
    throw "The installed candidate package cannot be imported by the exact service Python."
}
Assert-InstalledInterfaceAssets $applicationLauncher $appRoot

if (-not (Test-OnnxRuntimeImport $applicationLauncher)) {
    $runtimePrerequisiteInstaller = Join-Path $PSScriptRoot "install-onnxruntime-prerequisite.ps1"
    if (-not (Test-Path -LiteralPath $runtimePrerequisiteInstaller -PathType Leaf)) {
        throw "The ONNX Runtime prerequisite installer was not found: $runtimePrerequisiteInstaller"
    }
    & $runtimePrerequisiteInstaller -DataRoot $dataRoot
    if ($LASTEXITCODE -ne 0 -or -not (Test-OnnxRuntimeImport $applicationLauncher)) {
        $runtimeDiagnostic = Get-OnnxRuntimeDiagnostic $applicationLauncher
        $windowsDescription = Get-WindowsDescription
        throw "ONNX Runtime could not load after the Microsoft Visual C++ Runtime installation. Windows: $windowsDescription. VC++ log: $(Join-Path $dataRoot 'logs\vc-redist-install.log'). Import detail: $runtimeDiagnostic"
    }
}

$modelInstaller = Join-Path $PSScriptRoot "install-default-model-packs.ps1"
if (-not (Test-Path -LiteralPath $modelInstaller -PathType Leaf)) {
    throw "The default model installer was not found: $modelInstaller"
}
Write-Host "Installing verified local ASR, VAD, voice, and FFmpeg packs..."
$previousProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    $env:UV_PROJECT_ENVIRONMENT = $virtualEnvironmentPath
    $modelInstallation = & $modelInstaller -DataRoot $dataRoot -AppRoot $appRoot -UvPath $uv
}
finally {
    [Environment]::SetEnvironmentVariable("UV_PROJECT_ENVIRONMENT", $previousProjectEnvironment, "Process")
}
if ($LASTEXITCODE -ne 0 -or $null -eq $modelInstallation) {
    throw "The default model installation failed."
}

$runtimeVerifier = Join-Path $PSScriptRoot "verify-default-runtime.py"
& $applicationLauncher -I -X utf8 $runtimeVerifier `
    --models-root $modelInstallation.ModelsRoot `
    --manifest $modelInstallation.ManifestPath `
    --ffmpeg $modelInstallation.FfmpegPath
if ($LASTEXITCODE -ne 0) {
    throw "The downloaded local runtime did not pass verification."
}

$environmentFile = Join-Path $dataRoot "candidate.env.ps1"
if (-not (Test-Path -LiteralPath $environmentFile)) {
    $environmentValues = [ordered]@{
        NEXT_DATA_DIR = $dataRoot
        NEXT_DATABASE_PATH = (Join-Path $dataRoot "transcription-next.sqlite3")
        NEXT_ZEUSDB_DIR = (Join-Path $dataRoot "zeusdb")
        NEXT_MODELS_DIR = $modelInstallation.ModelsRoot
        NEXT_ASR_MANIFEST = $modelInstallation.ManifestPath
        NEXT_FFMPEG_PATH = $modelInstallation.FfmpegPath
        NEXT_HOST = "127.0.0.1"
        NEXT_PORT = "$Port"
        NEXT_INSTANCE_ID = "transcription-next-candidate"
        NEXT_COOKIE_SECURE = "false"
    }
    $environmentLines = @("# Generated by install-windows.ps1. Do not commit this file.")
    $environmentLines += foreach ($name in $environmentValues.Keys) {
        '$env:' + $name + ' = ' + (ConvertTo-PowerShellLiteral $environmentValues[$name])
    }
    $environmentLines | Set-Content -LiteralPath $environmentFile -Encoding utf8
}
else {
    Ensure-EnvironmentValue $environmentFile "NEXT_MODELS_DIR" $modelInstallation.ModelsRoot
    Ensure-EnvironmentValue $environmentFile "NEXT_ASR_MANIFEST" $modelInstallation.ManifestPath
    Ensure-EnvironmentValue $environmentFile "NEXT_FFMPEG_PATH" $modelInstallation.FfmpegPath
}

. $environmentFile
& $applicationLauncher -I -X utf8 -c "import pathlib, sys; from local_voice_transcriber.config import Settings; actual = Settings.from_env().data_dir.resolve(); expected = pathlib.Path(sys.argv[1]).resolve(); assert actual == expected, ('NEXT_DATA_DIR does not match DataPath', actual, expected)" $dataRoot
if ($LASTEXITCODE -ne 0) {
    throw "The candidate environment file does not match the selected data directory."
}
if (-not (Test-ActiveAdministrator $applicationLauncher)) {
    if ([string]::IsNullOrWhiteSpace($BootstrapAdminLogin)) {
        $BootstrapAdminLogin = Read-Host "First administrator login or e-mail"
    }
    if ($null -eq $BootstrapAdminPassword) {
        $BootstrapAdminPassword = Read-Host "First administrator password" -AsSecureString
    }
    if ($null -eq $BootstrapAdminPassword -or $BootstrapAdminPassword.Length -lt 12) {
        throw "The first administrator password must contain at least 12 characters."
    }
    $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($BootstrapAdminPassword)
    try {
        $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
        $env:NEXT_ADMIN_PASSWORD_INPUT = $plainPassword
        & $applicationLauncher -I -X utf8 -m local_voice_transcriber --create-admin $BootstrapAdminLogin
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create the first administrator."
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
if (-not (Test-Path -LiteralPath $applicationLauncher -PathType Leaf)) {
    throw "The application Python runtime was not found: $applicationLauncher"
}
$launcherLines = @(
    '$ErrorActionPreference = "Stop"'
    ('. ' + (ConvertTo-PowerShellLiteral $environmentFile))
    ('Set-Location -LiteralPath ' + (ConvertTo-PowerShellLiteral $dataRoot))
    ('& ' + (ConvertTo-PowerShellLiteral $applicationLauncher) + ' -I -X utf8 -u -m local_voice_transcriber')
    'exit $LASTEXITCODE'
)
$launcherLines | Set-Content -LiteralPath $launcher -Encoding utf8

if ($OpenFirewall) {
    $ruleName = "Transcription Next candidate TCP $Port"
    if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
    }
}

Write-Host "Transcription Next is ready."
Write-Host "Data: $dataRoot"
Write-Host "Data access account: $ServiceAccount"
Write-Host "Configuration: $environmentFile"
Write-Host "Launch: powershell -ExecutionPolicy Bypass -File $launcher"
Write-Host "Use a separate service account without access to legacy data."
