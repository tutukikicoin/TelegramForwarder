param(
    [string]$RepoPath = "E:\services\tgForwarder",
    [string]$TaskName = "ThinkDeep-tgForwarder-Deploy-Every30Minutes",
    [int]$IntervalMinutes = 30
)

$ErrorActionPreference = "Stop"

$deployScript = Join-Path $RepoPath "scripts\small_host_deploy.ps1"
if (-not (Test-Path -LiteralPath $deployScript)) {
    throw "Deploy script not found: $deployScript"
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $deployScript + '"')

$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -AllowStartIfOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Pull code and redeploy tgForwarder on the small host." | Out-Null

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,TaskPath,State
