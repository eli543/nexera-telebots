# launch-channel.ps1 - start one fleet bot on Windows. EXPERIMENTAL, UNTESTED.
#
# Reads the bot's token from its per-bot .env, sets TELEGRAM_STATE_DIR, and
# launches `claude --channels` in a new Windows Terminal tab in the bot's
# working directory. Mirrors the macOS fleet-launch engine.
#
# Usage:  .\launch-channel.ps1 -Key main [-ResumeId <uuid>]
# Env:    FLEET_HOME (default %USERPROFILE%\.claude\telegram-fleet)

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Key,
    [string] $ResumeId = ""
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
if (-not (Test-Path $ConfigPath)) {
    Write-Error "fleet.json not found at $ConfigPath"
    exit 1
}

$config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
$bot = $config.bots | Where-Object { $_.key -eq $Key } | Select-Object -First 1
if (-not $bot) {
    Write-Error "no bot with key '$Key' in fleet.json"
    exit 1
}

$workdir = Expand-FleetPath $bot.workdir
$stateDir = Expand-FleetPath $bot.state_dir

# Read the token from the per-bot .env only. Never inline a token here.
$envFile = Join-Path $stateDir ".env"
if (-not (Test-Path $envFile)) {
    Write-Error "no .env for bot '$Key' at $envFile"
    exit 1
}
$token = ""
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*TELEGRAM_BOT_TOKEN=(.+)$') {
        $token = $Matches[1].Trim()
    }
}
if (-not $token) {
    Write-Error "TELEGRAM_BOT_TOKEN not set in $envFile"
    exit 1
}

# Optional model-state (two lines: MODEL=, EFFORT=).
$model = ""
$effort = ""
$modelState = Join-Path $stateDir "model-state"
if (Test-Path $modelState) {
    foreach ($line in Get-Content $modelState) {
        if ($line -match '^MODEL=(.+)$') { $model = $Matches[1].Trim() }
        if ($line -match '^EFFORT=(.+)$') { $effort = $Matches[1].Trim() }
    }
}

# Permission stance -> claude flag.
$stanceFile = Join-Path $FleetHome "permission-stance"
$stance = if (Test-Path $stanceFile) { (Get-Content $stanceFile -Raw).Trim() } else { "safe" }
$permFlag = switch ($stance) {
    "full"   { "--dangerously-skip-permissions" }
    "scoped" { "--permission-mode auto" }
    default  { "" }
}

# Build the claude command line.
$claudeArgs = @("--channels", "plugin:telegram@claude-plugins-official")
if ($permFlag) { $claudeArgs += $permFlag.Split(" ") }
if ($model)  { $claudeArgs += @("--model", $model) }
if ($effort) { $claudeArgs += @("--effort", $effort) }
if ($ResumeId) { $claudeArgs += @("--resume", $ResumeId) }

$argLine = $claudeArgs -join " "
$inner = "`$env:TELEGRAM_STATE_DIR='$stateDir'; `$env:TELEGRAM_BOT_TOKEN='$token'; `$env:CLAUDE_CODE_NO_FLICKER='1'; claude $argLine"

# Launch in a new Windows Terminal tab if available, else a new PowerShell window.
if (Get-Command wt.exe -ErrorAction SilentlyContinue) {
    Start-Process wt.exe -ArgumentList @("-w", "0", "new-tab", "-d", "`"$workdir`"", "powershell", "-NoExit", "-Command", $inner)
}
else {
    Start-Process powershell -ArgumentList @("-NoExit", "-Command", "Set-Location '$workdir'; $inner")
}

Write-Host "launched bot '$Key' in $workdir"
