@echo off
:: TimeIn Bot Installer
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)
title TimeIn Bot Installer
color 0A
echo.
echo  ============================================
echo    TimeIn Bot - One-Click Installer
echo  ============================================
echo.
echo  This installs automatic attendance.
echo  Takes about 5 minutes.
echo.
pause
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
