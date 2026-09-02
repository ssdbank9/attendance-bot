@echo off
:: TimeIn Bot - One-Click Installer
:: Your friend can run JUST this file. It downloads everything else.
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
echo  This installs automatic attendance marking.
echo  Takes about 5 minutes. Needs internet.
echo.
pause

:: Download install.ps1 from GitHub if not already present
if not exist "%~dp0install.ps1" (
    echo  Downloading installer script...
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/ssdbank9/attendance-bot/main/install.ps1' -OutFile '%~dp0install.ps1' -UseBasicParsing"
    if %errorlevel% neq 0 (
        echo  ERROR: Could not download installer. Check your internet connection.
        pause
        exit /b 1
    )
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
