@echo off
rem ClearScreen uninstall - removes the services, program files, shortcuts and firewall rule.
rem Configuration (clients, keys, settings) and the database are KEPT unless you choose otherwise.
setlocal
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
title ClearScreen uninstall
set "SCRIPT=%~dp0installer\Uninstall-ClearScreen.ps1"
if not exist "%SCRIPT%" set "SCRIPT=%~dp0services\Uninstall-ClearScreen.ps1"
rem Run a copy from TEMP, so the program folder can be removed while it runs.
copy /y "%SCRIPT%" "%TEMP%\Uninstall-ClearScreen.ps1" >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\Uninstall-ClearScreen.ps1"
echo.
pause
