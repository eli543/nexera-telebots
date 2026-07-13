# watchdog.ps1 - one fleet health pass on Windows. EXPERIMENTAL, UNTESTED.
#
# For each bot in fleet.json, checks whether a claude channel session for that
# bot is alive, and restarts an unhealthy bot through launch-channel.ps1.
# Processes are matched by a verified command-line substring, never killed by a
# broad name pattern.
#
# Usage:  .\watchdog.ps1 [-DryRun]
# Env:    FLEET_HOME (default %USERPROFILE%\.claude\telegram-fleet)

[CmdletBinding()]
param(
    [switch] $DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Expand-FleetPath([string] $p) {
    if ($p.StartsWith("~")) {
        return (Join-Path $env:USERPROFILE ($p.Substring(1).TrimStart('\', '/')))
    }
    return $p
}

$FleetHome = if ($env:FLEET_HOME) { $env:FLEET_HOME } else { Join-Path $env:USERPROFILE ".claude\telegram-fleet" }
$ConfigPath = Join-Path $FleetHome "fleet.json"
$LogDir = Join-Path $FleetHome "logs"
$LogFile = Join-Path $LogDir "watchdog.log"

if (-not (Test-Path $ConfigPath)) {
    Write-Error "fleet.json not found at $ConfigPath"
    exit 1
}
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

function Write-Log([string] $msg) {
    $stamp = (Get-Date).ToString("s")
    $line = "$stamp $msg"
    Write-Host $line
    if (-not $DryRun) { Add-Content -Path $LogFile -Value $line }
}

$config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
$launcher = Join-Path $PSScriptRoot "launch-channel.ps1"

# Snapshot every process command line once (Win32_Process).
$procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine }

foreach ($bot in $config.bots) {
    $key = $bot.key
    $stateDir = Expand-FleetPath $bot.state_dir

    # A bot is up if some process's command line references the channels plugin
    # AND this bot's state dir. This is a verified match, not a name pattern.
    $sessionUp = $false
    foreach ($p in $procs) {
        if ($p.CommandLine -like "*--channels*plugin:telegram@claude-plugins-official*" -and
            $p.CommandLine -like "*$stateDir*") {
            $sessionUp = $true
            break
        }
    }

    # Heartbeat freshness (written by the poller): under 180s is healthy.
    $heartbeatFresh = $false
    $hb = Join-Path $stateDir "poller.heartbeat"
    if (Test-Path $hb) {
        $age = (Get-Date) - (Get-Item $hb).LastWriteTime
        if ($age.TotalSeconds -lt 180) { $heartbeatFresh = $true }
    }

    if ($sessionUp -and $heartbeatFresh) {
        Write-Log "ok    $key (session up, heartbeat fresh)"
        continue
    }

    # Respect a per-bot disable marker (park switch).
    $disabled = Join-Path $FleetHome "watchdog-state\$key.disabled"
    if (Test-Path $disabled) {
        Write-Log "skip  $key (disabled marker present)"
        continue
    }

    Write-Log "down  $key (sessionUp=$sessionUp heartbeatFresh=$heartbeatFresh) -> restart"
    if ($DryRun) {
        Write-Log "dry   would run: $launcher -Key $key"
    }
    else {
        try {
            & $launcher -Key $key
        }
        catch {
            Write-Log "error $key restart failed: $_"
        }
    }
}
