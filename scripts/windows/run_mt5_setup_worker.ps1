param(
    [string]$RepoRoot = "",
    [string]$PythonExe = "",
    [string]$LogLevel = "INFO",
    [int]$RestartDelaySeconds = 5
)

$ErrorActionPreference = "Stop"

function Set-ConsoleTitleSafely {
    param(
        [string]$Title
    )

    try {
        if ($host -and $host.UI -and $host.UI.RawUI) {
            $host.UI.RawUI.WindowTitle = $Title
        }
    } catch {
        return
    }
}

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

if (-not $env:CELERY_POOL) {
    $env:CELERY_POOL = "solo"
}
if (-not $env:PYTHONUNBUFFERED) {
    $env:PYTHONUNBUFFERED = "1"
}
if (-not $env:FXJ_ASCII_LOG_LAYOUT) {
    $env:FXJ_ASCII_LOG_LAYOUT = "narrow"
}
if (-not $env:FXJ_ASCII_LOG_LINE_MAX) {
    $env:FXJ_ASCII_LOG_LINE_MAX = "100"
}

function Get-Mt5VmQueueSlug {
    $name = [string]$env:COMPUTERNAME
    if (-not $name) {
        return "unknown"
    }
    $slug = $name.ToLowerInvariant()
    $slug = [regex]::Replace($slug, '[^a-z0-9]+', '-')
    $slug = [regex]::Replace($slug, '-+', '-').Trim('-')
    if ($slug.Length -gt 48) {
        $slug = $slug.Substring(0, 48)
    }
    if (-not $slug) {
        return "unknown"
    }
    return $slug
}

$VmSlug = Get-Mt5VmQueueSlug
$SetupQueues = "mt5_setup.$VmSlug,mt5_setup"

while ($true) {
    Set-ConsoleTitleSafely "MT5 Setup Window | Starting..."
    $startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$startedAt] Starting FX Journal MT5 setup worker pool=solo concurrency=1 python=$pythonExe"

    & $pythonExe -m celery -A celery_app.celery worker `
        --pool=solo `
        --concurrency=1 `
        --loglevel=$LogLevel `
        --queues=$SetupQueues `
        --hostname="mt5-setup@$env:COMPUTERNAME"

    $exitCode = $LASTEXITCODE
    $stoppedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Set-ConsoleTitleSafely "MT5 Setup Window | Restarting in $RestartDelaySeconds s"
    Write-Warning "[$stoppedAt] FX Journal MT5 setup worker exited with code $exitCode. Restarting in $RestartDelaySeconds second(s)."

    Start-Sleep -Seconds $RestartDelaySeconds
}
