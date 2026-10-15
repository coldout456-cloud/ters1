@echo off
rem Removes the Dacha agent from this computer.
title Dacha - uninstall

net session >nul 2>&1
if %errorlevel% equ 0 goto :admin
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:admin
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
