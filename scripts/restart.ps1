#requires -Version 5.1
#
# Restart the opencode-voice bot stack in one shot:  stop -> start -> status
#
#   stop   - stops only the bot (the separately started opencode server is left
#            untouched)
#   start  - relaunches `python -m telegram_bot` (which waits for the running
#            opencode server)
#   status - reports launcher / listener / health
#
# This exists because `stop` tears down the very process tree that carries the
# Telegram conversation, so the three actions cannot be typed one at a time.
# Everything therefore runs from a single detached process that keeps going
# after the caller (and the bot) disappears.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restart.ps1
#   ... -DelaySeconds 30     # grace period before the destructive stop
#   ... -SettleSeconds 8     # pause between stop and start, and start/status
#
[CmdletBinding()]
param(
    # Grace period before running `stop`, so any in-flight reply can be delivered.
    [ValidateRange(0, 3600)]
    [int]$DelaySeconds = 20,

    # Pause after `stop` (the bot needs to exit) and after `start` (the bot
    # needs time to connect to the already-running opencode server) before we
    # read status.
    [ValidateRange(0, 300)]
    [int]$SettleSeconds = 8
)

$ErrorActionPreference = 'Continue'   # never let one bad step abort the rest

$Root = Split-Path -Parent $PSScriptRoot
$Logs = Join-Path $Root 'logs'
$Run = Join-Path $Root 'run'
$Bot = Join-Path $PSScriptRoot 'bot.ps1'
$LogFile = Join-Path $Logs 'restart.log'
$DoneFile = Join-Path $Run 'restart.done'

New-Item -ItemType Directory -Force -Path $Logs, $Run | Out-Null

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $line = "[$stamp] [$Level] $Message"
    try { Add-Content -LiteralPath $LogFile -Value $line -Encoding utf8 } catch { }
    Write-Host $line
}

function Invoke-Bot {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('start', 'stop', 'status')][string]$Action
    )

    Write-Log "==> $Action"

    # NOTE: do NOT capture this with `& powershell ... 2>&1`.
    # `bot.ps1 -Action start` spawns a long-lived detached pythonw launcher via
    # Start-Process. That grandchild inherits the inherited pipe handles, so the
    # pipe never reaches EOF and the capturing call blocks forever - the whole
    # restart then stalls right after "==> start". Redirecting to real files
    # sidesteps the EOF dependency entirely.
    $stepOut = Join-Path $Logs "restart-$Action.out.log"
    $stepErr = Join-Path $Logs "restart-$Action.err.log"

    $proc = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                      '-File', $Bot, '-Action', $Action `
        -Wait -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $stepOut -RedirectStandardError $stepErr

    $code = $proc.ExitCode

    foreach ($f in @($stepOut, $stepErr)) {
        if (-not (Test-Path -LiteralPath $f)) { continue }
        foreach ($line in (Get-Content -LiteralPath $f -ErrorAction SilentlyContinue)) {
            if ($null -ne $line -and "$line".Trim().Length -gt 0) {
                Write-Log "    $line"
            }
        }
    }

    Write-Log "<== $Action finished (exit $code)" $(if ($code -eq 0) { 'INFO' } else { 'WARN' })
    return $code
}

# --- fresh slate -----------------------------------------------------------
Remove-Item -LiteralPath $DoneFile -Force -ErrorAction SilentlyContinue

Write-Log "=================================================="
Write-Log "restart.ps1 starting (pid $PID)"
Write-Log "DelaySeconds=$DelaySeconds SettleSeconds=$SettleSeconds"

if (-not (Test-Path -LiteralPath $Bot)) {
    Write-Log "bot.ps1 not found at $Bot - aborting" 'ERROR'
    Set-Content -LiteralPath $DoneFile -Value "failed: bot.ps1 missing" -Encoding ascii
    exit 1
}

# --- grace period ----------------------------------------------------------
if ($DelaySeconds -gt 0) {
    Write-Log "Waiting $DelaySeconds s before stopping (grace period)..."
    Start-Sleep -Seconds $DelaySeconds
}

# --- 1. stop ---------------------------------------------------------------
$stopCode = Invoke-Bot -Action 'stop'

# --- settle: let the bot exit ----------------------------------------------
if ($SettleSeconds -gt 0) {
    Write-Log "Waiting $SettleSeconds s after stop..."
    Start-Sleep -Seconds $SettleSeconds
}

# --- 2. start --------------------------------------------------------------
$startCode = Invoke-Bot -Action 'start'

# --- settle: let the bot come up -------------------------------------------
if ($SettleSeconds -gt 0) {
    Write-Log "Waiting $SettleSeconds s after start..."
    Start-Sleep -Seconds $SettleSeconds
}

# --- 3. status -------------------------------------------------------------
$statusCode = Invoke-Bot -Action 'status'

# --- summary ---------------------------------------------------------------
Write-Log "=================================================="
Write-Log "RESULT  stop=$stopCode  start=$startCode  status=$statusCode"

$summary = switch ($true) {
    ($startCode -eq 0 -and $statusCode -eq 0) { 'RESTART OK' }
    ($startCode -ne 0) { 'RESTART FAILED (start did not succeed)' }
    default { 'RESTART INCOMPLETE (stack not fully healthy)' }
}
Write-Log "SUMMARY: $summary"

try { Set-Content -LiteralPath $DoneFile -Value $summary -Encoding ascii } catch { }
Write-Log "Done. Log: $LogFile"
exit $startCode
