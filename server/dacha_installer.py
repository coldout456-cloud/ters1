"""Персональный установщик для дачи и проверка адреса агента (без внешних зависимостей).

Установщик, который выдаёт сайт, — это .bat-файл с вшитыми адресом сайта, токеном агента
и (необязательно) ключом Tailscale. Все вшиваемые значения проверяются по строгим
шаблонам, поэтому в файл не может попасть ничего, кроме безопасных символов.
"""
from __future__ import annotations

import ipaddress
import re

# Только ASCII: cmd.exe плохо работает с не-латиницей в .bat-файлах.
_TEMPLATE = r"""@echo off
rem Dacha agent installer. Made by your own site.
rem THIS FILE CONTAINS SECRET KEYS - do not share it or upload it anywhere.
title Dacha - install
set "DACHA_SERVER=@@SERVER@@"
set "DACHA_TOKEN=@@TOKEN@@"
set "DACHA_TSKEY=@@TSKEY@@"

net session >nul 2>&1
if %errorlevel% equ 0 goto :admin
echo Requesting administrator rights...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:admin
echo Downloading the installer from GitHub...
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $ProgressPreference='SilentlyContinue'; $f=Join-Path $env:TEMP 'dacha-install.ps1'; try { Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/@@REPO@@/@@BRANCH@@/install/install.ps1' -OutFile $f -ErrorAction Stop } catch { Write-Host 'Could not download the installer:' $_.Exception.Message; exit 1 }; & $f; exit $LASTEXITCODE"
if errorlevel 1 goto :failed
echo.
echo Done. This file contains secret keys, so it deletes itself now.
pause
(goto) 2>nul & del "%~f0"

:failed
echo.
echo Something went wrong. The file was NOT deleted, you can run it again.
pause
"""

RE_URL = re.compile(r"^https://[A-Za-z0-9.-]+(:\d{1,5})?$")
RE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{24,128}$")
RE_TSKEY = re.compile(r"^tskey-[A-Za-z0-9-]{10,200}$")
RE_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RE_BRANCH = re.compile(r"^[A-Za-z0-9_.\-/]{1,60}$")

# Адреса Tailscale — это 100.64.0.0/10 (CGNAT-диапазон)
TAILNET = ipaddress.ip_network("100.64.0.0/10")


class InstallerError(ValueError):
    """Нельзя собрать установщик (понятное пользователю сообщение)."""


def build_installer(public_url: str, token: str, ts_key: str, repo: str, branch: str) -> bytes:
    """Собирает текст .bat (CRLF, ASCII). ts_key может быть пустым."""
    if not RE_URL.match(public_url or ""):
        raise InstallerError(
            "На сервере не задан PUBLIC_URL (адрес сайта вида https://имя.duckdns.org). "
            "Запусти скрипт установки сервера ещё раз."
        )
    if not RE_TOKEN.match(token or ""):
        raise InstallerError("В настройках сервера неверный AGENT_TOKEN.")
    if ts_key and not RE_TSKEY.match(ts_key):
        raise InstallerError("В настройках сервера неверный TAILSCALE_AUTHKEY (должен начинаться с tskey-).")
    if not RE_REPO.match(repo or "") or not RE_BRANCH.match(branch or ""):
        raise InstallerError("В настройках сервера неверные GITHUB_REPO / GITHUB_BRANCH.")

    text = (
        _TEMPLATE.replace("@@SERVER@@", public_url)
        .replace("@@TOKEN@@", token)
        .replace("@@TSKEY@@", ts_key or "")
        .replace("@@REPO@@", repo)
        .replace("@@BRANCH@@", branch)
    )
    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("ascii")


def parse_agent_address(host, port) -> tuple[str, int]:
    """Проверяет адрес, который сообщил агент: только IPv4 из сети Tailscale и нормальный порт.

    Это защита от подмены: даже с верным токеном нельзя направить сервер на произвольный адрес.
    """
    ip = ipaddress.ip_address(str(host).strip())
    if ip.version != 4 or ip not in TAILNET:
        raise ValueError("адрес не из сети Tailscale")
    p = int(port)
    if not 1 <= p <= 65535:
        raise ValueError("неверный порт")
    return str(ip), p
