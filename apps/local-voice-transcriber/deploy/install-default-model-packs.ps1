[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [Parameter(Mandatory = $true)][string]$AppRoot,
    [Parameter(Mandatory = $true)][string]$UvPath
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-FileHash([string]$Path, [string]$ExpectedHash) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file is missing: $Path"
    }
    $actualHash = Get-Sha256 $Path
    if ($actualHash -ne $ExpectedHash.ToLowerInvariant()) {
        throw "SHA-256 mismatch for $Path"
    }
}

function Assert-RelativeName([string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Name) -or [System.IO.Path]::IsPathRooted($Name)) {
        throw "Model file name must be a relative path."
    }
    if ($Name.Replace("\", "/").Split("/") -contains "..") {
        throw "Model file name cannot escape its package directory."
    }
}

function Assert-FileSet([string]$Directory, [System.Collections.IDictionary]$Files) {
    foreach ($name in $Files.Keys) {
        Assert-RelativeName ([string]$name)
        Assert-FileHash (Join-Path $Directory $name) ([string]$Files[$name])
    }
}

function Get-OrDownload([string]$Uri, [string]$Destination, [string]$Sha256) {
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        Assert-FileHash $Destination $Sha256
        return
    }
    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $partial = "$Destination.partial-$PID"
    try {
        Write-Host "Downloading $(Split-Path -Leaf $Destination)..."
        Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $partial
        Assert-FileHash $partial $Sha256
        Move-Item -LiteralPath $partial -Destination $Destination -ErrorAction Stop
    }
    finally {
        if (Test-Path -LiteralPath $partial -PathType Leaf) {
            Remove-Item -LiteralPath $partial -Force
        }
    }
}

function Publish-ModelDirectory(
    [string]$StagedDirectory,
    [string]$DestinationDirectory,
    [System.Collections.IDictionary]$Files
) {
    if (Test-Path -LiteralPath $DestinationDirectory) {
        Assert-FileSet $DestinationDirectory $Files
        return
    }
    Assert-FileSet $StagedDirectory $Files
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DestinationDirectory) | Out-Null
    Move-Item -LiteralPath $StagedDirectory -Destination $DestinationDirectory -ErrorAction Stop
    Assert-FileSet $DestinationDirectory $Files
}

function Assert-FreeSpace([string]$Path) {
    $volumeRoot = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($Path))
    $driveName = $volumeRoot.TrimEnd("\\").TrimEnd(":")
    $drive = Get-PSDrive -Name $driveName -ErrorAction Stop
    if ($drive.Free -lt 5GB) {
        throw "At least 5 GB of free disk space is required on $volumeRoot."
    }
}

$dataDirectory = [System.IO.Path]::GetFullPath($DataRoot)
$modelsRoot = Join-Path $dataDirectory "models"
$toolsRoot = Join-Path $dataDirectory "tools"
$stagingRoot = Join-Path $dataDirectory (".transcription-next-model-staging-" + [guid]::NewGuid().ToString("N"))
Assert-FreeSpace $dataDirectory
New-Item -ItemType Directory -Force -Path $modelsRoot, $toolsRoot, $stagingRoot | Out-Null

$asrDirectoryName = "asr/gigaam-v3-ctc-322c3b29"
$asrFiles = [ordered]@{
    "config.json" = "0641fcf73f4af791c73f05083e38a658ff5dcbee3534a0c61396c10f4b97f0fe"
    "v3_ctc.onnx" = "1fb978d4f41e1334003fae9e29f2d9844d4132315a90553a4b986839051ba9d3"
    "v3_vocab.txt" = "a9143c30844d3c0bee3e9e927e4084774eb1b9eeaafc473b2c4521e4911a7c07"
}
$vadDirectoryName = "vad/silero-v6-b3e3ee3c"
$vadFiles = [ordered]@{
    "silero_vad.onnx" = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
}
$voiceDirectoryName = "diarization/pyannote-3-0-wespeaker-resnet34"
$voiceFiles = [ordered]@{
    "segmentation/model.onnx" = "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"
    "embedding/wespeaker_en_voxceleb_resnet34.onnx" = "5ef208a9da1453335308a6b6f4e6dfbd7e183a38b604de0a57664f45d257fe94"
}
$ffmpegDirectoryName = "ffmpeg-n8.1-20261021"
$ffmpegArchiveSha256 = "fe7ce100a275215b1f5eac9b55731186679f493bba0aba044283928e428106d4"

try {
    $stagedAsr = Join-Path $stagingRoot "asr"
    foreach ($name in $asrFiles.Keys) {
        Get-OrDownload (
            "https://huggingface.co/istupakov/gigaam-v3-onnx/resolve/322c3b29492673eb7d0b434bfa9dfb8653e34d02/$name"
        ) (Join-Path $stagedAsr $name) $asrFiles[$name]
    }
    Publish-ModelDirectory $stagedAsr (Join-Path $modelsRoot $asrDirectoryName) $asrFiles

    $stagedVad = Join-Path $stagingRoot "vad"
    Get-OrDownload (
        "https://huggingface.co/istupakov/silero-vad-onnx/resolve/b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6/silero_vad.onnx"
    ) (Join-Path $stagedVad "silero_vad.onnx") $vadFiles["silero_vad.onnx"]
    Publish-ModelDirectory $stagedVad (Join-Path $modelsRoot $vadDirectoryName) $vadFiles

    $stagedVoice = Join-Path $stagingRoot "voice"
    $segmentationArchive = Join-Path $stagingRoot "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
    Get-OrDownload (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
    ) $segmentationArchive "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488"
    $expandedSegmentation = Join-Path $stagingRoot "segmentation-expanded"
    & $UvPath run --project $AppRoot --no-sync python (Join-Path $PSScriptRoot "extract-model-archive.py") `
        --archive $segmentationArchive --destination $expandedSegmentation
    if ($LASTEXITCODE -ne 0) {
        throw "Could not extract the verified speaker segmentation archive."
    }
    $segmentationSource = Join-Path $expandedSegmentation "sherpa-onnx-pyannote-segmentation-3-0\model.onnx"
    Assert-FileHash $segmentationSource $voiceFiles["segmentation/model.onnx"]
    New-Item -ItemType Directory -Force -Path (Join-Path $stagedVoice "segmentation") | Out-Null
    Copy-Item -LiteralPath $segmentationSource -Destination (Join-Path $stagedVoice "segmentation\model.onnx")
    Get-OrDownload (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34.onnx"
    ) (Join-Path $stagedVoice "embedding\wespeaker_en_voxceleb_resnet34.onnx") $voiceFiles["embedding/wespeaker_en_voxceleb_resnet34.onnx"]
    Publish-ModelDirectory $stagedVoice (Join-Path $modelsRoot $voiceDirectoryName) $voiceFiles

    $ffmpegDirectory = Join-Path $toolsRoot $ffmpegDirectoryName
    $ffmpegExecutable = Join-Path $ffmpegDirectory "bin\ffmpeg.exe"
    if (-not (Test-Path -LiteralPath $ffmpegExecutable -PathType Leaf)) {
        if (Test-Path -LiteralPath $ffmpegDirectory) {
            throw "Existing FFmpeg directory is incomplete: $ffmpegDirectory"
        }
        $ffmpegArchive = Join-Path $stagingRoot "ffmpeg.zip"
        Get-OrDownload (
            "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-gpl-shared-8.1.zip"
        ) $ffmpegArchive $ffmpegArchiveSha256
        $expandedFfmpeg = Join-Path $stagingRoot "ffmpeg-expanded"
        Expand-Archive -LiteralPath $ffmpegArchive -DestinationPath $expandedFfmpeg -Force
        $candidates = @(Get-ChildItem -LiteralPath $expandedFfmpeg -Filter "ffmpeg.exe" -File -Recurse)
        if ($candidates.Count -ne 1) {
            throw "The verified FFmpeg archive has an unexpected layout."
        }
        $sourceDirectory = Split-Path -Parent (Split-Path -Parent $candidates[0].FullName)
        Move-Item -LiteralPath $sourceDirectory -Destination $ffmpegDirectory -ErrorAction Stop
    }
    if (-not (Test-Path -LiteralPath $ffmpegExecutable -PathType Leaf)) {
        throw "FFmpeg executable was not installed."
    }
    & $ffmpegExecutable -version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The installed FFmpeg executable did not start."
    }

    $manifest = [ordered]@{
        schema_version = 1
        language = "ru"
        asr = [ordered]@{
            pack_id = "gigaam-v3-ctc-322c3b29"
            model_name = "gigaam-v3-ctc"
            directory = $asrDirectoryName
            files = $asrFiles
        }
        vad = [ordered]@{
            pack_id = "silero-v6-b3e3ee3c"
            model_name = "silero"
            directory = $vadDirectoryName
            files = $vadFiles
        }
        diarization = [ordered]@{
            pack_id = "sherpa-pyannote-3-0-wespeaker-resnet34"
            directory = $voiceDirectoryName
            segmentation_model = "segmentation/model.onnx"
            embedding_model = "embedding/wespeaker_en_voxceleb_resnet34.onnx"
            embedding_dimension = 256
            files = $voiceFiles
        }
    }
    $manifestPath = Join-Path $modelsRoot "asr-pack.json"
    $manifestText = $manifest | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText(
        $manifestPath,
        $manifestText + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )

    [PSCustomObject]@{
        ModelsRoot = $modelsRoot
        ManifestPath = $manifestPath
        FfmpegPath = $ffmpegExecutable
    }
}
finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
}
