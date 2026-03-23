param(
    [string]$RepoRoot = "",
    [string]$PythonExe = "",
    [int]$Concurrency = 10,
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

Set-Location $RepoRoot

while ($true) {
    $startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$startedAt] Starting FX Journal MT5 sync worker pool=threads concurrency=$Concurrency python=$pythonExe"

    & $pythonExe -m celery -A celery_app.celery worker `
        --pool=threads `
        --concurrency=$Concurrency `
        --loglevel=$LogLevel `
        --queues=mt5_sync `
        --hostname="mt5-sync@$env:COMPUTERNAME"

    $exitCode = $LASTEXITCODE
    $stoppedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Warning "[$stoppedAt] FX Journal MT5 sync worker exited with code $exitCode. Restarting in $RestartDelaySeconds second(s)."

    Start-Sleep -Seconds $RestartDelaySeconds
}
