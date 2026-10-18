#requires -Version 5.1
#
# Manage the opencode-voice launcher (Telegram bot; waits for the external
# opencode server).
#
#   start  - launch `python -m telegram_bot` hidden; it waits for the
#            already-running opencode server and then runs the bot. Logs go
#            to logs/.
#   stop   - stop only the bot.
#   status - report launcher, listener, and /api/info health (with basic auth).
#
[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Logs = Join-Path $Root 'logs'
$Run = Join-Path $Root 'run'
$PidFile = Join-Path $Run 'bot.pid'

function Get-Python {
    $pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
    if (Test-Path -LiteralPath $pythonw) { return $pythonw }
    $python = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $python) { return $python }
    return 'python.exe'
}

function Get-Port {
    try {
        $py = Get-Python
        $out = & $py -c "import config,urllib.parse as u; p=u.urlparse(config.OPENCODE_BASE_URL); print(p.port or 4096)" 2>$null
        $port = 0
        $first = ($out | Select-Object -First 1)
        if ($first -and [int]::TryParse($first.ToString().Trim(), [ref]$port) -and $port -gt 0) {
            return $port
        }
    } catch {
    }
    return 4096
}

function Test-Health([int]$Port) {
    $py = Get-Python
    $script = Join-Path ([System.IO.Path]::GetTempPath()) "ocv_health_$PID.py"
    $code = @'
import os
import sys

sys.path.insert(0, os.getcwd())
import httpx
import config

creds = config.opencode_server_credentials()
auth = httpx.BasicAuth(*creds) if creds else None
port = sys.argv[1]
for host in ("localhost", "127.0.0.1"):
    try:
        response = httpx.get(f"http://{host}:{port}/api/info", auth=auth, timeout=3)
        if response.status_code == 200:
            sys.exit(0)
    except Exception:
        pass
sys.exit(1)
'@
    try {
        Set-Content -LiteralPath $script -Value $code -Encoding utf8
        Push-Location -LiteralPath $Root
        try {
            & $py $script "$Port" 2>$null | Out-Null
            return ($LASTEXITCODE -eq 0)
        } finally {
            Pop-Location
        }
    } catch {
        return $false
    } finally {
        Remove-Item -LiteralPath $script -Force -ErrorAction SilentlyContinue
    }
}

function Get-RunningPid {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $raw = Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    $procId = 0
    if ($raw -and [int]::TryParse($raw.ToString().Trim(), [ref]$procId)) {
        if (Get-Process -Id $procId -ErrorAction SilentlyContinue) { return $procId }
    }
    return $null
}

function Get-ListenerPids([int]$Port) {
    try {
        return @(
            Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
                Select-Object -ExpandProperty OwningProcess -Unique
        )
    } catch {
        return @()
    }
}

if ($Action -eq 'start') {
    $existing = Get-RunningPid
    if ($existing) {
        Write-Host "opencode-voice already running (pid $existing)"
        exit 0
    }
    New-Item -ItemType Directory -Force -Path $Logs | Out-Null
    New-Item -ItemType Directory -Force -Path $Run | Out-Null
    $py = Get-Python
    $outLog = Join-Path $Logs 'bot.out.log'
    $errLog = Join-Path $Logs 'bot.err.log'
    $process = Start-Process -FilePath $py -ArgumentList '-m', 'telegram_bot' `
        -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ascii
    Write-Host "Started opencode-voice (pid $($process.Id))."
    Write-Host "Logs: $outLog / $errLog"
    exit 0
}

if ($Action -eq 'stop') {
    $running = Get-RunningPid
    if ($running) {
        try {
            Stop-Process -Id $running -Force -ErrorAction Stop
            Write-Host "Stopped bot (pid $running)."
        } catch {
            Write-Warning "Could not stop bot pid $running"
        }
    } else {
        Write-Host "Bot is not running."
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

$port = Get-Port
$running = Get-RunningPid
$listening = (Get-ListenerPids $port).Count -gt 0
$healthy = Test-Health $port

if ($running) { Write-Host "launcher: running (pid $running)" } else { Write-Host "launcher: not running" }
if ($listening) { Write-Host "opencode server: listening on port $port" } else { Write-Host "opencode server: not listening on port $port" }
if ($healthy) { Write-Host "health: healthy" } else { Write-Host "health: down" }

if ($running -or $listening -or $healthy) { exit 0 }
exit 1
