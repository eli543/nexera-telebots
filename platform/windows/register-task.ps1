# register-task.ps1 - register the fleet watchdog as a Scheduled Task.
# EXPERIMENTAL, UNTESTED.
#
# Registers a task that runs a keepalive loop at logon, calling watchdog.ps1
# every 60 seconds. A loop is used rather than a repeating trigger so cadence
# does not depend on a timer that can stall after sleep, matching the macOS
# KeepAlive design.
#
# Usage:  .\register-task.ps1            register (or replace) the task
#         .\register-task.ps1 -Remove    unregister the task
# Env:    FLEET_HOME (default %USERPROFILE%\.claude\telegram-fleet)

[CmdletBinding()]
param(
    [switch] $Remove
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskName = "ClaudeFleetWatchdog"
$watchdog = Join-Path $PSScriptRoot "watchdog.ps1"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "removed scheduled task $TaskName"
    }
    else {
        Write-Host "no scheduled task named $TaskName"
    }
    return
}

if (-not (Test-Path $watchdog)) {
    Write-Error "watchdog.ps1 not found next to this script at $watchdog"
    exit 1
}

# The loop body: run one watchdog pass, sleep 60s, repeat.
$loop = "while (`$true) { try { & '$watchdog' } catch { } ; Start-Sleep -Seconds 60 }"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$loop`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

# Replace any existing task with the same name.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Keepalive loop for the claude-telegram-fleet bots (experimental)." | Out-Null

Write-Host "registered scheduled task $TaskName (runs at logon; watchdog every 60s)"
Write-Host "remove it with: .\register-task.ps1 -Remove"
