@echo off
rem Dacha agent installer. Double-click this file and answer "Yes" in the Windows prompt.
rem (ASCII only on purpose: cmd.exe handles non-Latin text in .bat files badly.)
title Dacha - install

net session >nul 2>&1
if %errorlevel% equ 0 goto :admin
echo Requesting administrator rights...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:admin
echo Downloading the installer from GitHub...
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $ProgressPreference='SilentlyContinue'; $f=Join-Path $env:TEMP 'dacha-install.ps1'; try { Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/coldout456-cloud/ters1/main/install/install.ps1' -OutFile $f -ErrorAction Stop } catch { Write-Host 'Could not download the installer:' $_.Exception.Message; exit 1 }; & $f"
echo.
pause
