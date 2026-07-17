@echo off
title Telegram Video Duplicate Remover
cd /d "%~dp0"

echo ============================================================
echo   Telegram Video Duplicate Remover
echo ============================================================
echo.

REM Check Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python is not installed or not on PATH.
    echo     Please install it from https://www.python.org/downloads/
    echo     and tick "Add Python to PATH" during install, then run this again.
    echo.
    pause
    exit /b
)

echo Installing / checking requirements (first run only, please wait)...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install -r requirements.txt
echo.
echo Starting the app...
python gui.py

echo.
echo App closed. You can close this window.
pause
