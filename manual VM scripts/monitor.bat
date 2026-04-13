@echo off
rem FX Journal MT5 Monitor — opens a new terminal window with the live dashboard.
rem
rem Usage:
rem   monitor.bat              (5s refresh, default)
rem   monitor.bat --interval 10
rem
rem Run from any directory; paths are resolved from this script's location.

set REPO=C:\Users\Administrator\fxjournal
set PYTHON=%REPO%\.venv\Scripts\python.exe
set SCRIPT=%REPO%\scripts\windows\mt5_monitor.py

if not exist "%PYTHON%" (
    echo Python not found at %PYTHON%
    echo Check that the venv is set up at %REPO%\.venv
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo Monitor script not found at %SCRIPT%
    pause
    exit /b 1
)

start "FX Journal Monitor" cmd /k ""%PYTHON%" "%SCRIPT%" %*"
