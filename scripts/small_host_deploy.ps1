param(
    [string]$RepoPath = "E:\services\tgForwarder",
    [string]$RuntimeStatePath = "",
    [string]$LogDir = "E:\logs\tgForwarder-deploy",
    [string]$DockerPath = "C:\Program Files\Docker\Docker\resources\bin\docker.exe",
    [string]$GitPath = "git",
    [switch]$SkipGitPull,
    [switch]$SkipBuild,
    [switch]$SkipRuntimeRestore
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

function Resolve-RuntimeSource($Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return ""
    }

    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
    if ((Get-Item -LiteralPath $resolved.Path).PSIsContainer) {
        return $resolved.Path
    }

    $extractRoot = Join-Path $env:TEMP ("tgforwarder-runtime-" + [guid]::NewGuid().ToString("N"))
    Ensure-Directory $extractRoot
    Expand-Archive -LiteralPath $resolved.Path -DestinationPath $extractRoot -Force
    return $extractRoot
}

function Restore-RuntimeEntry($RelativePath, $RuntimeSource, $RepoRoot, $BackupRoot) {
    $source = Join-Path $RuntimeSource $RelativePath
    if (-not (Test-Path -LiteralPath $source)) {
        return
    }

    $destination = Join-Path $RepoRoot $RelativePath
    if (Test-Path -LiteralPath $destination) {
        $backup = Join-Path $BackupRoot $RelativePath
        Ensure-Directory (Split-Path -Parent $backup)
        Copy-Item -LiteralPath $destination -Destination $backup -Recurse -Force
    }

    Ensure-Directory (Split-Path -Parent $destination)
    Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force
}

Ensure-Directory $LogDir
$log = Join-Path $LogDir ("deploy-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")
Start-Transcript -LiteralPath $log | Out-Null

try {
    if (-not (Test-Path -LiteralPath $RepoPath)) {
        throw "RepoPath does not exist: $RepoPath"
    }
    if (-not (Test-Path -LiteralPath $DockerPath)) {
        throw "Docker CLI not found: $DockerPath"
    }

    $RepoPath = (Resolve-Path -LiteralPath $RepoPath).Path

    if (-not $SkipRuntimeRestore -and -not [string]::IsNullOrWhiteSpace($RuntimeStatePath)) {
        $runtimeSource = Resolve-RuntimeSource $RuntimeStatePath
        $backupRoot = Join-Path $LogDir ("runtime-backup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
        Ensure-Directory $backupRoot
        foreach ($entry in $runtimeEntries) {
            Restore-RuntimeEntry $entry $runtimeSource $RepoPath $backupRoot
        }
        Write-Host "runtime restored from $runtimeSource"
        Write-Host "previous runtime backup: $backupRoot"
    }

    Push-Location $RepoPath
    try {
        if (-not $SkipGitPull) {
            & $GitPath pull --ff-only
            if ($LASTEXITCODE -ne 0) {
                throw "git pull failed with exit code $LASTEXITCODE"
            }
        }

        if ($SkipBuild) {
            & $DockerPath compose up -d
        } else {
            & $DockerPath compose up -d --build
        }
        if ($LASTEXITCODE -ne 0) {
            throw "docker compose up failed with exit code $LASTEXITCODE"
        }

        & $DockerPath compose ps
        if ($LASTEXITCODE -ne 0) {
            throw "docker compose ps failed with exit code $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }

    Write-Host "deploy ok: $RepoPath"
} finally {
    Stop-Transcript | Out-Null
    Write-Host "deploy log: $log"
}
