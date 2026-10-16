"""Персональный установщик для дачи и проверка адреса агента (без внешних зависимостей).

Установщик, который выдаёт сайт, — это .bat-файл с вшитыми адресом сайта, токеном агента
и (необязательно) ключом Tailscale. Все вшиваемые значения проверяются по строгим
шаблонам, поэтому в файл не может попасть ничего, кроме безопасных символов.
"""
from __future__ import annotations

import ipaddress
import re

# Только ASCII: cmd.exe плохо работает с не-латиницей в .bat-файлах.
#
# Запускается без единого видимого окна, кроме стандартного запроса Windows
# «Разрешить этому приложению вносить изменения?» — его показывает сама
# Windows поверх всего, и ни один скрипт не может ни скрыть его, ни пройти
# мимо него. Дальше всё (скачивание, установка Python/Tailscale, настройка
# агента) идёт в скрытом окне и только пишется в журнал %LOCALAPPDATA%\DachaAgent\install.log.
# Поэтому ни окна с вопросами, ни окна с «Готово»/с ошибкой тут нет: результат
# смотрят на сайте (появился ли компьютер) или в этом журнале.
# Сам агент ставится в %LOCALAPPDATA%\DachaAgent (AppData\Local) — эта папка скрыта
# в проводнике по умолчанию, в отличие от C:\ или рабочего стола.
_TEMPLATE = r"""@echo off
rem Dacha agent installer. Made by your own site.
rem THIS FILE CONTAINS SECRET KEYS - do not share it or upload it anywhere.
rem Runs with no visible window except the one Windows "allow this app" prompt.
set "DACHA_SERVER=@@SERVER@@"
set "DACHA_TOKEN=@@TOKEN@@"
set "DACHA_TSKEY=@@TSKEY@@"
set "DACHA_DEVICE_NAME=@@DEVNAME@@"

net session >nul 2>&1
if %errorlevel% equ 0 goto :admin
powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%~f0' -WindowStyle Hidden -Verb RunAs" >nul 2>&1
exit /b

:admin
if not exist "%LOCALAPPDATA%\DachaAgent" mkdir "%LOCALAPPDATA%\DachaAgent" >nul 2>&1
powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $ProgressPreference='SilentlyContinue'; $f=Join-Path $env:TEMP 'dacha-install.ps1'; try { Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/@@REPO@@/@@BRANCH@@/install/install.ps1' -OutFile $f -ErrorAction Stop } catch { $_.Exception.Message | Out-File -Append (Join-Path $env:LOCALAPPDATA 'DachaAgent\install.log') ; exit 1 }; & $f; exit $LASTEXITCODE" >nul 2>&1
if errorlevel 1 goto :failed
(goto) 2>nul & del "%~f0"
exit /b

:failed
exit /b 1
"""

RE_URL = re.compile(r"^https://[A-Za-z0-9.-]+(:\d{1,5})?$")
RE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{24,128}$")
RE_TSKEY = re.compile(r"^tskey-[A-Za-z0-9-]{10,200}$")
RE_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RE_BRANCH = re.compile(r"^[A-Za-z0-9_.\-/]{1,60}$")
RE_DEVICE_NAME = re.compile(r"^[A-Za-z0-9 _-]{1,40}$")  # для .bat нужна ASCII-латиница

# Адреса Tailscale — это 100.64.0.0/10 (CGNAT-диапазон)
TAILNET = ipaddress.ip_network("100.64.0.0/10")


class InstallerError(ValueError):
    """Нельзя собрать установщик (понятное пользователю сообщение)."""


def _validate(public_url: str, token: str, ts_key: str, repo: str, branch: str, device_name: str = "") -> None:
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
    if device_name and not RE_DEVICE_NAME.match(device_name):
        raise InstallerError("Имя компьютера: только латинские буквы, цифры, пробел, - и _ (до 40 символов).")


def build_installer(public_url: str, token: str, ts_key: str, repo: str, branch: str, device_name: str = "") -> bytes:
    """Собирает текст .bat (CRLF, ASCII). ts_key и device_name могут быть пустыми."""
    _validate(public_url, token, ts_key, repo, branch, device_name)

    text = (
        _TEMPLATE.replace("@@SERVER@@", public_url)
        .replace("@@TOKEN@@", token)
        .replace("@@TSKEY@@", ts_key or "")
        .replace("@@DEVNAME@@", device_name or "")
        .replace("@@REPO@@", repo)
        .replace("@@BRANCH@@", branch)
    )
    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("ascii")


def build_oneliner(public_url: str, token: str, ts_key: str, repo: str, branch: str, device_name: str = "") -> str:
    """Собирает одну команду PowerShell: ничего не скачивается на диск как отдельный файл —
    её нужно вставить в окно PowerShell/Terminal, открытое от имени администратора.
    Значения уже проверены строгими шаблонами (_validate), поэтому безопасны внутри
    одиночных кавычек PowerShell без дополнительного экранирования."""
    _validate(public_url, token, ts_key, repo, branch, device_name)

    url = f"https://raw.githubusercontent.com/{repo}/{branch}/install/install.ps1"
    return (
        f"$env:DACHA_SERVER='{public_url}'; $env:DACHA_TOKEN='{token}'; $env:DACHA_TSKEY='{ts_key or ''}'; "
        f"$env:DACHA_DEVICE_NAME='{device_name or ''}'; "
        "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; "
        f"iex (irm '{url}')"
    )


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
