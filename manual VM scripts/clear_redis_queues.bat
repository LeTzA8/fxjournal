@echo off
setlocal EnableExtensions
rem Clears pending Celery task messages from Redis broker queues used by FX Journal.
rem Does NOT flush Redis entirely (cache, result backend, worker state keys).
rem
rem Run from any directory; repo root is the parent of this folder.

set "REPO=%~dp0.."
cd /d "%REPO%"
if not exist "%REPO%\celery_app.py" (
    echo Repo root not found. Expected celery_app.py under:
    echo   %REPO%
    pause
    exit /b 1
)

set "PYTHON=python"
if exist "%REPO%\.venv\Scripts\python.exe" set "PYTHON=%REPO%\.venv\Scripts\python.exe"

echo ============================================
echo  FX Journal - Clear Celery broker queues
echo  Queues: mt5_sync, mt5_setup, celery
echo ============================================
echo.
echo  Stops nothing by itself. Optional: run stop_all_workers.bat first
echo  so workers are not consuming while you purge.
echo.
echo  Repo: %REPO%
echo  Python: %PYTHON%
echo.

"%PYTHON%" -m celery -A celery_app.celery purge -f -Q mt5_sync,mt5_setup,celery
set "EC=%ERRORLEVEL%"
echo.
if %EC% neq 0 (
    echo Purge failed ^(exit %EC%^).
    echo Check REDIS_URL in .env / FXJournal Main.env / FXJ_ENV_FILE and network to Redis.
) else (
    echo Purge finished.
)
echo.
pause
endlocal
