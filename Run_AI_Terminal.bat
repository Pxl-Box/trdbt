@echo off
title AI Trading Terminal
cd /d "%~dp0"
echo Starting AI Trading Terminal (Native Setup)...
python scanner_desktop.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ❌ The application crashed or failed to start.
    echo Please check the error message above.
    pause
)
