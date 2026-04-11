param(
    [string]$RepoRoot = "",
    [string]$PythonExe = "",
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

Set-Location $RepoRoot

if (-not $env:FXJ_ASCII_LOG_MAX_WIDTH) {
    $env:FXJ_ASCII_LOG_MAX_WIDTH = "0"
}

while ($true) {
    $host.UI.RawUI.WindowTitle = "MT5 Setup Window | Starting..."
    $startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$startedAt] Starting FX Journal MT5 setup worker pool=solo concurrency=1 python=$pythonExe"

    & $pythonExe -m celery -A celery_app.celery worker `
        --pool=solo `
        --concurrency=1 `
        --loglevel=$LogLevel `
        --queues=mt5_setup `
        --hostname="mt5-setup@$env:COMPUTERNAME"

    $exitCode = $LASTEXITCODE
    $stoppedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $host.UI.RawUI.WindowTitle = "MT5 Setup Window | Restarting in $RestartDelaySeconds s"
    Write-Warning "[$stoppedAt] FX Journal MT5 setup worker exited with code $exitCode. Restarting in $RestartDelaySeconds second(s)."

    Start-Sleep -Seconds $RestartDelaySeconds
}
