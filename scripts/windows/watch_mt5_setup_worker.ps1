param(
    [string]$RepoRoot = "",
    [string]$PythonExe = "",
    [int]$CheckIntervalSeconds = 60,
    [int]$RestartDelaySeconds = 5,
    [switch]$RunOnce
)

$ErrorActionPreference = "Stop"
$script:PowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

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

function Write-WatchdogLog {
    param(
        [string]$Message,
        [string]$Level = "INFO"
    )

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] [$Level] $Message"
    Write-Host $line
    Add-Content -LiteralPath $script:WatchdogLogPath -Value $line
}

function Get-Mt5SetupWorkerProcesses {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -like "*-A celery_app.celery worker*" -and
            $_.CommandLine -like "*--queues=mt5_setup*"
        }
}

function Get-Mt5SetupLauncherProcesses {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -like "*run_mt5_setup_worker.ps1*"
        }
}

function Start-Mt5SetupLauncher {
    $launcherPath = Join-Path $RepoRoot "scripts\windows\run_mt5_setup_worker.ps1"
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $launcherPath,
        "-RepoRoot", $RepoRoot,
        "-PythonExe", $pythonExe,
        "-RestartDelaySeconds", $RestartDelaySeconds
    )
    $powerShellExe = $script:PowerShellExe
    if (-not (Test-Path $powerShellExe)) {
        $powerShellExe = "powershell.exe"
    }
    Start-Process -FilePath $powerShellExe -ArgumentList $arguments -WorkingDirectory $RepoRoot -WindowStyle Hidden | Out-Null
    Write-WatchdogLog "Started MT5 setup worker launcher."
}

function Ensure-Mt5SetupLauncher {
    $workerProcesses = @(Get-Mt5SetupWorkerProcesses)
    $launcherProcesses = @(Get-Mt5SetupLauncherProcesses)
    if ($workerProcesses.Count -gt 0 -or $launcherProcesses.Count -gt 0) {
        return
    }
    Start-Mt5SetupLauncher
}

function Restart-StaleMt5SetupWorker {
    $workerProcesses = @(Get-Mt5SetupWorkerProcesses)
    if ($workerProcesses.Count -eq 0) {
        Write-WatchdogLog "Stale worker detected but no active mt5_setup worker process was found. Ensuring launcher is running." "WARN"
        Ensure-Mt5SetupLauncher
        return
    }

    foreach ($process in $workerProcesses) {
        try {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
            Write-WatchdogLog "Stopped stale mt5_setup worker process id=$($process.ProcessId)."
        } catch {
            Write-WatchdogLog "Failed to stop stale worker process id=$($process.ProcessId): $($_.Exception.Message)" "WARN"
        }
    }

    Start-Sleep -Seconds 2

    if (@(Get-Mt5SetupLauncherProcesses).Count -eq 0) {
        Start-Mt5SetupLauncher
    } else {
        Write-WatchdogLog "Launcher is still running and will restart the worker automatically."
    }
}

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

$pythonExe = Resolve-PythonExe -RepoRoot $RepoRoot -PythonExe $PythonExe
$healthcheckScript = Join-Path $RepoRoot "scripts\windows\mt5_setup_healthcheck.py"
$watchdogLogDir = Join-Path $RepoRoot "logs\watchdog"
$script:WatchdogLogPath = Join-Path $watchdogLogDir "mt5-setup-watchdog.log"

New-Item -ItemType Directory -Path $watchdogLogDir -Force | Out-Null
Set-Location $RepoRoot

if ($CheckIntervalSeconds -lt 15) {
    throw "CheckIntervalSeconds must be at least 15."
}

if (-not $env:PYTHONUNBUFFERED) {
    $env:PYTHONUNBUFFERED = "1"
}

while ($true) {
    Ensure-Mt5SetupLauncher

    $healthOutput = & $pythonExe $healthcheckScript 2>&1
    $exitCode = $LASTEXITCODE
    $healthText = (($healthOutput | ForEach-Object { $_.ToString() }) -join "`n").Trim()

    if (-not $healthText) {
        Write-WatchdogLog "Healthcheck returned no output." "WARN"
    } else {
        Write-WatchdogLog "Healthcheck $healthText"
    }

    if ($exitCode -eq 2) {
        Restart-StaleMt5SetupWorker
    } elseif ($exitCode -ne 0) {
        Write-WatchdogLog "Healthcheck failed with exit code $exitCode." "WARN"
        Ensure-Mt5SetupLauncher
    }

    if ($RunOnce) {
        break
    }

    Start-Sleep -Seconds $CheckIntervalSeconds
}
