@echo off
rem ClearScreen setup - double-click to install, upgrade or repair.
rem Asks for administrator rights, then runs installer\Install-ClearScreen.ps1.
setlocal
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
title ClearScreen setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\Install-ClearScreen.ps1" %*
echo.
pause
