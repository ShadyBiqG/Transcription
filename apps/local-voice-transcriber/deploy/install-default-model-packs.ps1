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

function Assert-FileSize([string]$Path, [Int64]$ExpectedSize) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file is missing: $Path"
    }
    $actualSize = (Get-Item -LiteralPath $Path -Force).Length
    if ($actualSize -ne $ExpectedSize) {
        throw "Unexpected file size for $Path"
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
    if ($script:ApprovedDownloadUris -notcontains $Uri) {
        throw "Refusing an unapproved model download URL."
    }
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

function Assert-SafeZipArchive([string]$ArchivePath) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName.Replace("\", "/")
            if ([string]::IsNullOrWhiteSpace($name)) {
                continue
            }
            if (
                [System.IO.Path]::IsPathRooted($name) -or
                $name -match '^[A-Za-z]:' -or
                $name -match '(^|/)\.\.(/|$)' -or
                $name.IndexOf([char]0) -ge 0
            ) {
                throw "Verified archive contains an unsafe path."
            }
        }
    }
    finally {
        $archive.Dispose()
    }
}

function Assert-LlamaServer([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "llama-server.exe was not installed."
    }
    & $Path --version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The installed llama-server.exe did not start."
    }
}

function Install-CleanerRuntime(
    [string]$ArchivePath,
    [string]$DestinationDirectory,
    [string]$StagingDirectory
) {
    $destinationExecutable = Join-Path $DestinationDirectory "llama-server.exe"
    if (Test-Path -LiteralPath $DestinationDirectory) {
        Assert-LlamaServer $destinationExecutable
        return [PSCustomObject]@{
            Path = $destinationExecutable
            Published = $false
        }
    }

    $published = $false
    try {
        Assert-SafeZipArchive $ArchivePath
        $expandedDirectory = Join-Path $StagingDirectory "expanded"
        Expand-Archive -LiteralPath $ArchivePath -DestinationPath $expandedDirectory -Force
        $serverCandidates = @(Get-ChildItem -LiteralPath $expandedDirectory -Filter "llama-server.exe" -File -Recurse)
        if ($serverCandidates.Count -ne 1) {
            throw "The verified llama.cpp archive has an unexpected layout."
        }
        $runtimeSourceDirectory = Split-Path -Parent $serverCandidates[0].FullName
        Assert-LlamaServer $serverCandidates[0].FullName
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DestinationDirectory) | Out-Null
        Move-Item -LiteralPath $runtimeSourceDirectory -Destination $DestinationDirectory -ErrorAction Stop
        $published = $true
        Assert-LlamaServer $destinationExecutable
        return [PSCustomObject]@{
            Path = $destinationExecutable
            Published = $true
        }
    }
    catch {
        if ($published -and (Test-Path -LiteralPath $DestinationDirectory)) {
            Remove-Item -LiteralPath $DestinationDirectory -Recurse -Force
        }
        throw
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

function Assert-FreeSpace([string]$Path, [Int64]$MinimumBytes) {
    $volumeRoot = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($Path))
    $driveName = $volumeRoot.TrimEnd("\\").TrimEnd(":")
    $drive = Get-PSDrive -Name $driveName -ErrorAction Stop
    if ($drive.Free -lt $MinimumBytes) {
        $minimumGigabytes = [Math]::Ceiling($MinimumBytes / 1GB)
        throw "At least $minimumGigabytes GB of free disk space is required on $volumeRoot."
    }
}

$dataDirectory = [System.IO.Path]::GetFullPath($DataRoot)
$modelsRoot = Join-Path $dataDirectory "models"
$toolsRoot = Join-Path $dataDirectory "tools"
$stagingRoot = Join-Path $dataDirectory (".transcription-next-model-staging-" + [guid]::NewGuid().ToString("N"))
$minimumDiskSpaceBytes = 7GB
Assert-FreeSpace $dataDirectory $minimumDiskSpaceBytes
New-Item -ItemType Directory -Force -Path $modelsRoot, $toolsRoot, $stagingRoot | Out-Null

$asrDirectoryName = "asr/gigaam-v3-e2e-ctc-322c3b29"
$asrFiles = [ordered]@{
    "config.json" = "0641fcf73f4af791c73f05083e38a658ff5dcbee3534a0c61396c10f4b97f0fe"
    "v3_e2e_ctc.onnx" = "377701bd33568f4733feec2db5b2dc12544fd09a5a5dfa69ccf55d161f84027a"
    "v3_e2e_ctc_vocab.txt" = "142de7570b3de5b3035ce111a89c228e80e6085273731d944093ddf24fa539cd"
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
$cleanerModelDirectoryName = "cleaner/qwen2.5-1.5b-instruct-q4-k-m-91cad511"
$cleanerModelFileName = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
$cleanerModelRevision = "91cad51170dc346986eccefdc2dd33a9da36ead9"
$cleanerModelSha256 = "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"
$cleanerModelSize = 1117320736
$cleanerRuntimeDirectoryName = "llama.cpp-b5092-win-noavx-x64"
$cleanerRuntimeArchiveName = "llama-b5092-bin-win-noavx-x64.zip"
$cleanerRuntimeArchiveSha256 = "4a4c6aef72ff13122d56a9ab59acf697603ce9084087427400ae7d1b7ebaef71"
$cleanerModelUri = "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/$cleanerModelRevision/$cleanerModelFileName"
$cleanerRuntimeUri = "https://github.com/ggml-org/llama.cpp/releases/download/b5092/$cleanerRuntimeArchiveName"
$script:ApprovedDownloadUris = @(
    "https://huggingface.co/istupakov/gigaam-v3-onnx/resolve/322c3b29492673eb7d0b434bfa9dfb8653e34d02/config.json",
    "https://huggingface.co/istupakov/gigaam-v3-onnx/resolve/322c3b29492673eb7d0b434bfa9dfb8653e34d02/v3_e2e_ctc.onnx",
    "https://huggingface.co/istupakov/gigaam-v3-onnx/resolve/322c3b29492673eb7d0b434bfa9dfb8653e34d02/v3_e2e_ctc_vocab.txt",
    "https://huggingface.co/istupakov/silero-vad-onnx/resolve/b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6/silero_vad.onnx",
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34.onnx",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-gpl-shared-8.1.zip",
    $cleanerModelUri,
    $cleanerRuntimeUri
)
$publishedCleanerModel = $false
$publishedCleanerRuntime = $false
$installationCompleted = $false

try {
    $stagedAsr = Join-Path $stagingRoot "asr"
    $asrDestination = Join-Path $modelsRoot $asrDirectoryName
    if (Test-Path -LiteralPath $asrDestination) {
        Assert-FileSet $asrDestination $asrFiles
    }
    else {
        foreach ($name in $asrFiles.Keys) {
            Get-OrDownload (
                "https://huggingface.co/istupakov/gigaam-v3-onnx/resolve/322c3b29492673eb7d0b434bfa9dfb8653e34d02/$name"
            ) (Join-Path $stagedAsr $name) $asrFiles[$name]
        }
        Publish-ModelDirectory $stagedAsr $asrDestination $asrFiles
    }

    $stagedVad = Join-Path $stagingRoot "vad"
    $vadDestination = Join-Path $modelsRoot $vadDirectoryName
    if (Test-Path -LiteralPath $vadDestination) {
        Assert-FileSet $vadDestination $vadFiles
    }
    else {
        Get-OrDownload (
            "https://huggingface.co/istupakov/silero-vad-onnx/resolve/b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6/silero_vad.onnx"
        ) (Join-Path $stagedVad "silero_vad.onnx") $vadFiles["silero_vad.onnx"]
        Publish-ModelDirectory $stagedVad $vadDestination $vadFiles
    }

    $stagedVoice = Join-Path $stagingRoot "voice"
    $voiceDestination = Join-Path $modelsRoot $voiceDirectoryName
    if (Test-Path -LiteralPath $voiceDestination) {
        Assert-FileSet $voiceDestination $voiceFiles
    }
    else {
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
        Publish-ModelDirectory $stagedVoice $voiceDestination $voiceFiles
    }

    $stagedCleanerModel = Join-Path $stagingRoot "cleaner-model"
    $cleanerModelDestination = Join-Path $modelsRoot $cleanerModelDirectoryName
    $cleanerModelPath = Join-Path $cleanerModelDestination $cleanerModelFileName
    if (Test-Path -LiteralPath $cleanerModelDestination) {
        Assert-FileHash $cleanerModelPath $cleanerModelSha256
        Assert-FileSize $cleanerModelPath $cleanerModelSize
    }
    else {
        $stagedCleanerModelPath = Join-Path $stagedCleanerModel $cleanerModelFileName
        Get-OrDownload $cleanerModelUri $stagedCleanerModelPath $cleanerModelSha256
        Assert-FileSize $stagedCleanerModelPath $cleanerModelSize
        Publish-ModelDirectory $stagedCleanerModel $cleanerModelDestination ([ordered]@{
            $cleanerModelFileName = $cleanerModelSha256
        })
        $publishedCleanerModel = $true
        Assert-FileSize $cleanerModelPath $cleanerModelSize
    }

    $cleanerRuntimeDirectory = Join-Path $toolsRoot $cleanerRuntimeDirectoryName
    $cleanerRuntimeArchive = Join-Path $stagingRoot $cleanerRuntimeArchiveName
    if (-not (Test-Path -LiteralPath $cleanerRuntimeDirectory)) {
        Get-OrDownload $cleanerRuntimeUri $cleanerRuntimeArchive $cleanerRuntimeArchiveSha256
    }
    $cleanerRuntimeInstallation = Install-CleanerRuntime `
        -ArchivePath $cleanerRuntimeArchive `
        -DestinationDirectory $cleanerRuntimeDirectory `
        -StagingDirectory (Join-Path $stagingRoot "cleaner-runtime")
    $cleanerLlamaServerPath = $cleanerRuntimeInstallation.Path
    $publishedCleanerRuntime = $cleanerRuntimeInstallation.Published

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
            pack_id = "gigaam-v3-e2e-ctc-322c3b29"
            model_name = "gigaam-v3-e2e-ctc"
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

    $installationCompleted = $true
    [PSCustomObject]@{
        ModelsRoot = $modelsRoot
        ManifestPath = $manifestPath
        FfmpegPath = $ffmpegExecutable
        CleanerModelPath = $cleanerModelPath
        CleanerLlamaServerPath = $cleanerLlamaServerPath
    }
}
finally {
    if (-not $installationCompleted -and $publishedCleanerRuntime -and (Test-Path -LiteralPath $cleanerRuntimeDirectory)) {
        Remove-Item -LiteralPath $cleanerRuntimeDirectory -Recurse -Force
    }
    if (-not $installationCompleted -and $publishedCleanerModel -and (Test-Path -LiteralPath $cleanerModelDestination)) {
        Remove-Item -LiteralPath $cleanerModelDestination -Recurse -Force
    }
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
}
