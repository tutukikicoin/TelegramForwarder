param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"

function Assert-True($Condition, $Message) {
    if (-not $Condition) {
        throw $Message
    }
}

$exportScript = Join-Path $RepoRoot "scripts\export_tgforwarder_runtime_state.ps1"
$deployScript = Join-Path $RepoRoot "scripts\small_host_deploy.ps1"
$taskScript = Join-Path $RepoRoot "scripts\install_small_host_deploy_task.ps1"

$root = Join-Path ([System.IO.Path]::GetTempPath()) ("tgforwarder-script-test-" + [guid]::NewGuid().ToString("N"))
$source = Join-Path $root "source"
$exportRoot = Join-Path $root "export"

try {
    New-Item -ItemType Directory -Path (Join-Path $source "db") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $source "sessions") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $source "config") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $source "ufb\config") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $source "rss\data") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $source "rss\media") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $source "logs") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $source "temp") -Force | Out-Null

    "env" | Set-Content -LiteralPath (Join-Path $source ".env") -Encoding UTF8
    "compose" | Set-Content -LiteralPath (Join-Path $source "docker-compose.yml") -Encoding UTF8
    "routes" | Set-Content -LiteralPath (Join-Path $source "db\feishu_routes.json") -Encoding UTF8
    "session" | Set-Content -LiteralPath (Join-Path $source "sessions\user.session") -Encoding UTF8
    "log" | Set-Content -LiteralPath (Join-Path $source "logs\service.log") -Encoding UTF8
    "tmp" | Set-Content -LiteralPath (Join-Path $source "temp\file.tmp") -Encoding UTF8

    & $exportScript -SourceRoot $source -DestinationRoot $exportRoot -NoZip
    if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "export script exited with $LASTEXITCODE"
    }

    $latest = Get-ChildItem -LiteralPath $exportRoot -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    Assert-True $latest "export directory was not created"
    Assert-True (Test-Path -LiteralPath (Join-Path $latest.FullName ".env")) ".env was not exported"
    Assert-True (Test-Path -LiteralPath (Join-Path $latest.FullName "db\feishu_routes.json")) "db state was not exported"
    Assert-True (Test-Path -LiteralPath (Join-Path $latest.FullName "sessions\user.session")) "session state was not exported"
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $latest.FullName "logs\service.log"))) "logs should not be exported"
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $latest.FullName "temp\file.tmp"))) "temp should not be exported"
    Assert-True (Test-Path -LiteralPath (Join-Path $latest.FullName "manifest.json")) "manifest was not written"

    $deployText = Get-Content -LiteralPath $deployScript -Raw
    Assert-True ($deployText -match "git pull") "deploy script should pull code"
    Assert-True ($deployText -match "compose up -d") "deploy script should start compose stack"
    Assert-True ($deployText -match "RuntimeStatePath") "deploy script should support runtime state restore"

    $taskText = Get-Content -LiteralPath $taskScript -Raw
    Assert-True ($taskText -match "Register-ScheduledTask") "task installer should register a scheduled task"
    Assert-True ($taskText -match "small_host_deploy.ps1") "task installer should invoke small_host_deploy.ps1"

    Write-Host "PASS small host scripts"
} finally {
    if (Test-Path -LiteralPath $root) {
        Remove-Item -LiteralPath $root -Recurse -Force
    }
}
