@echo off
rem FX Journal MT5 Monitor — opens a new terminal window with the live dashboard.
rem
rem Usage:
rem   monitor.bat              (5s refresh, default)
rem   monitor.bat --interval 10
rem
rem Run from any directory; paths are resolved from this script's location.

set REPO=C:\Users\Administrator\fxjournal
set PYTHON=python
set SCRIPT=%REPO%\scripts\windows\mt5_monitor.py

if not exist "%SCRIPT%" (
    echo Monitor script not found at %SCRIPT%
    pause
    exit /b 1
)

start "FX Journal Monitor" /D "%REPO%" cmd /k ""%PYTHON%" "%SCRIPT%" %*"
