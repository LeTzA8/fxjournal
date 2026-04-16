@echo off
setlocal EnableExtensions
rem MT5 broker/server discovery seeding (UI automation only — no login, no servers.dat edits).
rem List: manual VM scripts\mt5_broker_seeding\brokers_to_seed.txt
rem Config: manual VM scripts\mt5_broker_seeding\mt5_seed_brokers.py
rem Deps:   pip install -r "manual VM scripts\mt5_broker_seeding\requirements.txt"
rem
rem VM repo root (same as monitor.bat / Task Scheduler XMLs).

set "REPO=C:\Users\Administrator\fxjournal"
set "SCRIPT=%REPO%\manual VM scripts\mt5_broker_seeding\mt5_seed_brokers.py"
set "VENV_PY=%REPO%\.venv\Scripts\python.exe"

if not exist "%SCRIPT%" (
  echo Script not found:
  echo   "%SCRIPT%"
  pause
  exit /b 1
)

cd /d "%REPO%"

if exist "%VENV_PY%" (
  echo Using venv: "%VENV_PY%"
  "%VENV_PY%" -u "%SCRIPT%" %*
) else (
  echo Using python on PATH.
  python -u "%SCRIPT%" %*
)

echo.
pause
