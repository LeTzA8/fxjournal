@echo off
setlocal
echo ============================================
echo  FX Journal MT5 - Stop All Workers
echo ============================================
echo.

echo [1/4] Stopping Task Scheduler watchdogs...
schtasks /End /TN "FX Journal MT5 Sync Watchdog" >nul 2>&1
schtasks /End /TN "FX Journal MT5 Setup Watchdog" >nul 2>&1
echo       Done.

echo [2/3] Killing launcher scripts (run_mt5_*.ps1)...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*run_mt5_sync_worker.ps1*' -or $_.CommandLine -like '*run_mt5_setup_worker.ps1*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo       Done.

echo [3/3] Killing Celery workers...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*celery_app.celery worker*' -and ($_.CommandLine -like '*mt5_sync*' -or $_.CommandLine -like '*mt5_setup*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo       Done.

echo.
echo ============================================
echo  All MT5 worker processes stopped.
echo  MT5 terminals (terminal64.exe) left running
echo  - close them manually if needed.
echo  Task Scheduler watchdogs will NOT restart
echo  until triggered again (reboot or manual).
echo  To restart manually, run:
echo    watch_mt5_sync_worker.ps1
echo    watch_mt5_setup_worker.ps1
echo ============================================
echo.
pause
