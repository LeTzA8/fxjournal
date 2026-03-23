param(
    [string]$RepoRoot = "",
    [int]$Concurrency = 10,
    [string]$LogLevel = "INFO",
    [int]$RestartDelaySeconds = 5
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

$pythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Python executable not found at $pythonExe"
}

if ($Concurrency -lt 1) {
    throw "Concurrency must be at least 1."
}

Set-Location $RepoRoot

while ($true) {
    $startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$startedAt] Starting FX Journal MT5 sync worker pool=threads concurrency=$Concurrency"

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
