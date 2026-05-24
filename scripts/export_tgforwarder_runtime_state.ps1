param(
    [string]$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$DestinationRoot = "",
    [switch]$StopLocalCompose,
    [switch]$NoZip,
    [string]$DockerPath = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
)

$ErrorActionPreference = "Stop"

$runtimeEntries = @(
    ".env",
    "docker-compose.yml",
    "db",
    "sessions",
    "config",
    "ufb\config",
    "rss\data",
    "rss\media"
)

function Ensure-Directory($Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Copy-Entry($RelativePath, $SourceBase, $DestinationBase) {
    $source = Join-Path $SourceBase $RelativePath
    if (-not (Test-Path -LiteralPath $source)) {
        return [pscustomobject]@{
            path = $RelativePath
            copied = $false
            skipped = $true
            reason = "not present"
        }
    }

    $destination = Join-Path $DestinationBase $RelativePath
    $parent = Split-Path -Parent $destination
    Ensure-Directory $parent
    Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force

    return [pscustomobject]@{
        path = $RelativePath
        copied = $true
        skipped = $false
        reason = ""
    }
}

$SourceRoot = (Resolve-Path -LiteralPath $SourceRoot).Path
if ([string]::IsNullOrWhiteSpace($DestinationRoot)) {
    $DestinationRoot = Join-Path $SourceRoot "runtime_exports"
}
Ensure-Directory $DestinationRoot

if ($StopLocalCompose) {
    if (-not (Test-Path -LiteralPath $DockerPath)) {
        throw "Docker CLI not found: $DockerPath"
    }
    Push-Location $SourceRoot
    try {
        & $DockerPath compose down
        if ($LASTEXITCODE -ne 0) {
            throw "docker compose down failed with exit code $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$exportDir = Join-Path $DestinationRoot "tgForwarder-runtime-$timestamp"
Ensure-Directory $exportDir

$results = @()
foreach ($entry in $runtimeEntries) {
    $results += Copy-Entry $entry $SourceRoot $exportDir
}

$manifest = [ordered]@{
    created_at = (Get-Date).ToString("o")
    source_root = $SourceRoot
    export_dir = $exportDir
    stopped_local_compose = [bool]$StopLocalCompose
    entries = $runtimeEntries
    results = $results
}

$manifestPath = Join-Path $exportDir "manifest.json"
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

if (-not $NoZip) {
    $zipPath = "$exportDir.zip"
    Compress-Archive -Path (Join-Path $exportDir "*") -DestinationPath $zipPath -Force
    Write-Host "runtime export: $zipPath"
} else {
    Write-Host "runtime export: $exportDir"
}
