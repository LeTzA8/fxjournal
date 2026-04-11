param(
    [string]$RepoRoot = "",
    [string]$PythonExe = "",
    [int]$Concurrency = 1,
    [string]$LogLevel = "INFO",
    [int]$RestartDelaySeconds = 5
)

$ErrorActionPreference = "Stop"

function Resolve-PythonExe {
    param(
        [string]$RepoRoot,
        [string]$PythonExe
    )

    if ($PythonExe) {
        if (Test-Path $PythonExe) {
            return (Resolve-Path $PythonExe).Path
        }

        $requestedCommand = Get-Command $PythonExe -ErrorAction SilentlyContinue
        if ($requestedCommand) {
            return $requestedCommand.Source
        }

        throw "Python executable not found for -PythonExe $PythonExe"
    }

    $venvCandidates = @(
        (Join-Path $RepoRoot ".venv\Scripts\python.exe"),
        (Join-Path $RepoRoot "venv\Scripts\python.exe")
    )

    foreach ($candidate in $venvCandidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    foreach ($commandName in @("python", "python.exe")) {
        $pythonCommand = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($pythonCommand) {
            return $pythonCommand.Source
        }
    }

    throw "Python executable not found. Install Python on the VM, add it to PATH, or pass -PythonExe with the full path."
}

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

$pythonExe = Resolve-PythonExe -RepoRoot $RepoRoot -PythonExe $PythonExe

if ($Concurrency -lt 1) {
    throw "Concurrency must be at least 1."
}

if ($Concurrency -ne 1) {
    Write-Warning "MT5 sync uses process-global MetaTrader5 session state. Forcing concurrency=1."
    $Concurrency = 1
}

Set-Location $RepoRoot

# Full-width ASCII table values (skip reasons, etc.); Render weekly worker keeps default 72-char cap.
if (-not $env:FXJ_ASCII_LOG_MAX_WIDTH) {
    $env:FXJ_ASCII_LOG_MAX_WIDTH = "0"
}

while ($true) {
    $host.UI.RawUI.WindowTitle = "MT5 Sync Window | Starting..."
    $startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$startedAt] Starting FX Journal MT5 sync worker pool=solo concurrency=$Concurrency python=$pythonExe"

    & $pythonExe -m celery -A celery_app.celery worker `
        --pool=solo `
        --concurrency=$Concurrency `
        --loglevel=$LogLevel `
        --queues=mt5_sync `
        --hostname="mt5-sync@$env:COMPUTERNAME"

    $exitCode = $LASTEXITCODE
    $stoppedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $host.UI.RawUI.WindowTitle = "MT5 Sync Window | Restarting in $RestartDelaySeconds s"
    Write-Warning "[$stoppedAt] FX Journal MT5 sync worker exited with code $exitCode. Restarting in $RestartDelaySeconds second(s)."

    Start-Sleep -Seconds $RestartDelaySeconds
}
