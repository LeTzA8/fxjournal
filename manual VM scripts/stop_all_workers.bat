@echo off
setlocal EnableExtensions
echo ============================================
echo  FX Journal MT5 - Stop All Workers
echo ============================================
echo.
echo  NOTE: MetaTrader terminals (terminal64.exe
echo  etc.) are NEVER stopped by this script.
echo.

REM --- 1) Scheduled-task watchdogs + direct worker tasks (stops Task Scheduler from holding runners) ---
echo [1/4] Stopping Task Scheduler MT5 tasks...
schtasks /End /TN "FX Journal MT5 Sync Watchdog" >nul 2>&1
schtasks /End /TN "FX Journal MT5 Setup Watchdog" >nul 2>&1
schtasks /End /TN "FX Journal MT5 Sync Worker Direct" >nul 2>&1
schtasks /End /TN "FX Journal MT5 Setup Worker Direct" >nul 2>&1
echo       Done.

REM --- 2) PowerShell: launchers + file watchers (by script name in command line) ---
echo.
echo [2/4] Stopping PowerShell launchers / watchers...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$killed = @(); " ^
  "Get-CimInstance Win32_Process | Where-Object { " ^
  "  ($_.Name -match '^(powershell|pwsh)\\.exe$') -and $_.CommandLine -and ( " ^
  "    $_.CommandLine -like '*run_mt5_sync_worker.ps1*' -or " ^
  "    $_.CommandLine -like '*run_mt5_setup_worker.ps1*' -or " ^
  "    $_.CommandLine -like '*watch_mt5_sync_worker.ps1*' -or " ^
  "    $_.CommandLine -like '*watch_mt5_setup_worker.ps1*' " ^
  "  ) " ^
  "} | ForEach-Object { " ^
  "  $s = if ($_.CommandLine.Length -gt 90) { $_.CommandLine.Substring(0,90) + '...' } else { $_.CommandLine }; " ^
  "  Write-Host ('       PID {0}: {1}' -f $_.ProcessId, $s); " ^
  "  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $killed += $_.ProcessId " ^
  "}; " ^
  "if ($killed.Count -eq 0) { Write-Host '       (none found)' }"

REM --- 3) Python: healthchecks / standalone monitor only (not all python.exe) ---
echo.
echo [3/4] Stopping MT5 helper Python (healthcheck / monitor)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$killed = @(); " ^
  "Get-CimInstance Win32_Process | Where-Object { " ^
  "  ($_.Name -match '^python(\\d+\\.\\d+)?\\.exe$') -and $_.CommandLine -and ( " ^
  "    $_.CommandLine -match 'mt5_sync_healthcheck\\.py|mt5_setup_healthcheck\\.py|mt5_monitor\\.py' " ^
  "  ) " ^
  "} | ForEach-Object { " ^
  "  $s = if ($_.CommandLine.Length -gt 90) { $_.CommandLine.Substring(0,90) + '...' } else { $_.CommandLine }; " ^
  "  Write-Host ('       PID {0}: {1}' -f $_.ProcessId, $s); " ^
  "  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $killed += $_.ProcessId " ^
  "}; " ^
  "if ($killed.Count -eq 0) { Write-Host '       (none found)' }"

REM --- 4) Celery workers for MT5 sync / setup queues ---
echo.
echo [4/4] Stopping Celery MT5 workers...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$killed = @(); " ^
  "Get-CimInstance Win32_Process | Where-Object { " ^
  "  $_.CommandLine -and ($_.CommandLine -like '*celery_app.celery worker*') -and ( " ^
  "    $_.CommandLine -like '*mt5_sync*' -or $_.CommandLine -like '*mt5_setup*' " ^
  "  ) " ^
  "} | ForEach-Object { " ^
  "  $s = if ($_.CommandLine.Length -gt 90) { $_.CommandLine.Substring(0,90) + '...' } else { $_.CommandLine }; " ^
  "  Write-Host ('       PID {0} ({1}): {2}' -f $_.ProcessId, $_.Name, $s); " ^
  "  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $killed += $_.ProcessId " ^
  "}; " ^
  "if ($killed.Count -eq 0) { Write-Host '       (none found)' }"

echo.
echo ============================================
echo  All MT5 worker-related processes stopped.
echo  MT5 terminals were not stopped.
echo  Task Scheduler watchdogs will NOT restart
echo  until triggered again (reboot or manual).
echo  To restart manually, run:
echo    watch_mt5_sync_worker.ps1
echo    watch_mt5_setup_worker.ps1
echo ============================================
echo.
pause
