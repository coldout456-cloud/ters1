"""Сервер: вход по паролю + веб-интерфейс + проксирование запросов к агенту на даче.

Браузер общается только с этим сервером. Токен агента браузеру не передаётся.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import random
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import anyio
import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from dacha_installer import InstallerError, build_installer, build_oneliner, parse_agent_address
from security import verify_password, verify_totp

BASE = Path(__file__).parent
STATIC = BASE / "static"


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(BASE / ".env")

AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "12"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") == "1"  # 0 — только для проверки по http
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"  # 1 — если перед сервером стоит Caddy/nginx
HOST = os.environ.get("SERVER_HOST", "127.0.0.1")
PORT = int(os.environ.get("SERVER_PORT", "8000"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # https://адрес-сайта: нужен для установщика дачи
TAILSCALE_AUTHKEY = os.environ.get("TAILSCALE_AUTHKEY", "")  # необязательно: вшивается в установщик
GITHUB_REPO = os.environ.get("GITHUB_REPO", "coldout456-cloud/ters1")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
STATE_FILE = Path(os.environ.get("STATE_FILE", str(BASE / "agent_state.json")))  # список известных компьютеров
TOTP_SECRET = os.environ.get("TOTP_SECRET", "").strip()
# Если включён, но секрет не задан (битая настройка) — не запираем хозяина снаружи, просто не требуем код.
TOTP_ENABLED = os.environ.get("TOTP_ENABLED", "0") == "1" and bool(TOTP_SECRET)
ACTIVITY_LOG = Path(os.environ.get("ACTIVITY_LOG", str(BASE / "activity.log")))
ACTIVITY_MAX_LINES = 2000


def check_config() -> None:
    problems = []
    if len(AGENT_TOKEN) < 24:
        problems.append("AGENT_TOKEN не задан или короче 24 символов")
    if not ADMIN_PASSWORD_HASH:
        problems.append("ADMIN_PASSWORD_HASH не задан (получить: python security.py)")
    if len(SESSION_SECRET) < 32:
        problems.append("SESSION_SECRET не задан или короче 32 символов")
    if problems:
        sys.exit("Ошибка настройки server/.env:\n  - " + "\n  - ".join(problems))


# --------------------------------------------------------------------------- #
# Приложение
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Несколько компьютеров: каждый агент — отдельная запись со своим http-клиентом.
# device_id присваивает себе сам агент (хранит у себя в файле) и присылает его
# при каждой регистрации — так сайт отличает «дачу» от «квартиры», даже если
# у обоих один и тот же общий AGENT_TOKEN.
# --------------------------------------------------------------------------- #

DEVICES: dict[str, dict] = {}  # device_id -> {name, name_is_custom, url, updated, client}


def make_client(url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=url, headers={"X-Agent-Token": AGENT_TOKEN}, timeout=httpx.Timeout(10.0, read=30.0)
    )


def load_saved_devices() -> dict:
    """Записи из прошлого раза (пустой словарь, если файла нет, он испорчен или старого формата)."""
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        devices = raw.get("devices")
        if not isinstance(devices, dict):
            return {}
        out = {}
        for did, d in devices.items():
            if not isinstance(d, dict) or not re.fullmatch(r"[A-Za-z0-9]{1,64}", str(did)):
                continue
            try:
                parsed = urlparse(str(d.get("url", "")))
                parse_agent_address(parsed.hostname, parsed.port)  # файл мог быть подменён
            except Exception:
                continue
            out[did] = {
                "name": str(d.get("name") or did)[:60],
                "name_is_custom": bool(d.get("name_is_custom")),
                "url": str(d["url"]).rstrip("/"),
                "updated": float(d.get("updated", 0) or 0),
            }
        return out
    except Exception:
        return {}


def save_devices() -> None:
    try:
        data = {
            did: {"name": d["name"], "name_is_custom": d["name_is_custom"], "url": d["url"], "updated": d["updated"]}
            for did, d in DEVICES.items()
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"devices": data}), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError:
        pass  # не страшно: агенты сообщат о себе заново при следующей регистрации


def get_device(device_id: str) -> dict:
    d = DEVICES.get(device_id)
    if d is None:
        raise HTTPException(404, "Такой компьютер не найден (возможно, его уже удалили)")
    return d


@asynccontextmanager
async def lifespan(_: FastAPI):
    for did, d in load_saved_devices().items():
        DEVICES[did] = {**d, "client": make_client(d["url"])}
    try:
        yield
    finally:
        for d in DEVICES.values():
            await d["client"].aclose()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
if COOKIE_SECURE:
    SECURITY_HEADERS["Strict-Transport-Security"] = "max-age=31536000"


class SecurityHeaders:
    """Чистый ASGI-middleware (не ломает бесконечные стримы, в отличие от BaseHTTPMiddleware)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in SECURITY_HEADERS.items():
                    headers.setdefault(key, value)
                if scope["path"].startswith("/api/"):
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(SecurityHeaders)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET or "dev-only-not-checked-yet",
    session_cookie="dacha_session",
    max_age=SESSION_HOURS * 3600,
    same_site="strict",
    https_only=COOKIE_SECURE,
)


# --------------------------------------------------------------------------- #
# Вход
# --------------------------------------------------------------------------- #

_failures: dict[str, list[float]] = {}
MAX_FAILS, FAIL_WINDOW = 5, 15 * 60


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def is_locked(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < FAIL_WINDOW]
    _failures[ip] = recent
    return len(recent) >= MAX_FAILS


def require_user(request: Request) -> None:
    if request.session.get("user") != ADMIN_USER:
        raise HTTPException(401, "Нужен вход")
    if request.method not in ("GET", "HEAD"):
        # Дополнительная защита от CSRF поверх SameSite=Strict
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            raise HTTPException(403, "Чужой источник запроса")


# --------------------------------------------------------------------------- #
# Журнал действий
# --------------------------------------------------------------------------- #

def log_activity(request: Request, action: str, **extra) -> None:
    try:
        entry = {
            "t": time.time(), "ip": client_ip(request),
            "user": request.session.get("user", ""), "action": action, **extra,
        }
        with ACTIVITY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if random.random() < 0.01:  # изредка подрезаем файл, не читая его на каждую запись
            trim_activity_log()
    except OSError:
        pass


def trim_activity_log() -> None:
    try:
        lines = ACTIVITY_LOG.read_text(encoding="utf-8").splitlines()
        if len(lines) > ACTIVITY_MAX_LINES:
            ACTIVITY_LOG.write_text("\n".join(lines[-ACTIVITY_MAX_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


@app.get("/api/login-info")
async def login_info():
    """Нужен ли код из приложения-аутентификатора — без входа, чтобы страница входа знала,
    показывать ли поле кода."""
    return {"totp_required": TOTP_ENABLED}


@app.post("/api/login")
async def login(request: Request):
    ip = client_ip(request)
    if is_locked(ip):
        raise HTTPException(429, "Слишком много попыток. Подожди 15 минут.")
    try:
        data = await request.json()
        username, password = str(data.get("username", "")), str(data.get("password", ""))
        code = str(data.get("code", "")).strip()
    except Exception:
        raise HTTPException(400, "Некорректный запрос")

    # Проверяем всегда все части, чтобы время ответа не выдавало, что именно неверно
    user_ok = hmac.compare_digest(username.encode(), ADMIN_USER.encode())
    pass_ok = verify_password(password, ADMIN_PASSWORD_HASH)
    code_ok = (not TOTP_ENABLED) or verify_totp(TOTP_SECRET, code)
    if not (user_ok and pass_ok and code_ok):
        _failures.setdefault(ip, []).append(time.time())
        await asyncio.sleep(1)  # замедляем перебор
        log_activity(request, "login_fail")
        if user_ok and pass_ok and not code_ok:
            raise HTTPException(401, "Неверный код из приложения-аутентификатора")
        raise HTTPException(401, "Неверный логин или пароль")

    _failures.pop(ip, None)
    request.session.clear()
    request.session["user"] = ADMIN_USER
    log_activity(request, "login_ok")
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request):
    log_activity(request, "logout")
    request.session.clear()
    return {"ok": True}


@app.get("/api/dacha/log", dependencies=[Depends(require_user)])
async def dacha_log(limit: int = 200):
    limit = max(1, min(limit, ACTIVITY_MAX_LINES))
    try:
        lines = ACTIVITY_LOG.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        lines = []
    entries = []
    for line in reversed(lines):
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return {"entries": entries}


# --------------------------------------------------------------------------- #
# Дача: регистрация агента и персональный установщик
# --------------------------------------------------------------------------- #

@app.post("/api/agent/register")
async def agent_register(request: Request):
    """Агент сам сообщает свой адрес в Tailscale и себя (device_id/имя). Вход на сайт не
    нужен, нужен общий токен — несколько компьютеров делят один и тот же AGENT_TOKEN,
    а отличает их device_id, который каждый агент хранит и присылает сам."""
    key = "agent:" + client_ip(request)
    if is_locked(key):
        raise HTTPException(429, "Слишком много попыток")
    sent = request.headers.get("x-agent-token", "")
    if not hmac.compare_digest(sent.encode(), AGENT_TOKEN.encode()):
        _failures.setdefault(key, []).append(time.time())
        await asyncio.sleep(1)
        raise HTTPException(401, "Неверный токен")
    try:
        data = await request.json()
        host, port = parse_agent_address(data.get("host"), data.get("port", 8765))
        device_id = str(data.get("device_id") or "").strip()
        reported_name = str(data.get("name") or "").strip()[:60]
    except Exception:
        raise HTTPException(400, "Некорректный адрес агента")
    if not re.fullmatch(r"[a-f0-9]{8,64}", device_id):
        device_id = "legacy"  # агент без device_id (старая версия) — одна запись на правах по умолчанию

    url = f"http://{host}:{port}"
    existing = DEVICES.get(device_id)
    if existing is None:
        DEVICES[device_id] = {
            "name": reported_name or device_id, "name_is_custom": False,
            "url": url, "updated": time.time(), "client": make_client(url),
        }
    else:
        if existing["url"] != url:
            old_client = existing["client"]
            existing["client"] = make_client(url)
            await old_client.aclose()
        if not existing["name_is_custom"] and reported_name:
            existing["name"] = reported_name
        existing["url"] = url
        existing["updated"] = time.time()
    save_devices()
    return {"ok": True, "device_id": device_id}


@app.get("/api/devices", dependencies=[Depends(require_user)])
async def devices_list():
    async def check_one(did: str, d: dict) -> tuple[str, bool]:
        try:
            r = await d["client"].get("/health", timeout=3.0)
            return did, r.status_code == 200
        except httpx.HTTPError:
            return did, False

    results = await asyncio.gather(*(check_one(did, d) for did, d in DEVICES.items())) if DEVICES else []
    online = dict(results)
    out = [
        {"id": did, "name": d["name"], "online": online.get(did, False), "last_seen": d["updated"]}
        for did, d in DEVICES.items()
    ]
    out.sort(key=lambda x: x["name"].lower())
    return {"devices": out}


@app.post("/api/devices/{device_id}/rename", dependencies=[Depends(require_user)])
async def device_rename(device_id: str, request: Request):
    d = get_device(device_id)
    try:
        data = await request.json()
        name = str(data.get("name", "")).strip()[:60]
    except Exception:
        raise HTTPException(400, "Некорректный запрос")
    if not name:
        raise HTTPException(400, "Имя не может быть пустым")
    d["name"], d["name_is_custom"] = name, True
    save_devices()
    log_activity(request, "device_rename", device=device_id, name=name)
    return {"ok": True}


@app.post("/api/devices/{device_id}/forget", dependencies=[Depends(require_user)])
async def device_forget(device_id: str, request: Request):
    d = DEVICES.pop(device_id, None)
    if d is None:
        raise HTTPException(404, "Такой компьютер не найден")
    await d["client"].aclose()
    save_devices()
    log_activity(request, "device_forget", device=device_id, name=d["name"])
    return {"ok": True}


@app.get("/api/dacha/info", dependencies=[Depends(require_user)])
async def dacha_info():
    return {"installer_ready": bool(PUBLIC_URL), "tailscale_key": bool(TAILSCALE_AUTHKEY)}


_RE_DEVICE_NAME = re.compile(r"^[A-Za-z0-9 _-]{1,40}$")


def _clean_device_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    if not _RE_DEVICE_NAME.match(name):
        raise HTTPException(400, "Имя компьютера: только латинские буквы, цифры, пробел, - и _ (до 40 символов)")
    return name


@app.get("/api/dacha/installer", dependencies=[Depends(require_user)])
async def dacha_installer(name: str = ""):
    """Готовый установщик для дачи: .bat с вшитыми адресом сайта, токеном, ключом Tailscale
    и (необязательно) именем компьютера, под которым он появится на сайте."""
    try:
        data = build_installer(
            PUBLIC_URL, AGENT_TOKEN, TAILSCALE_AUTHKEY, GITHUB_REPO, GITHUB_BRANCH, _clean_device_name(name)
        )
    except InstallerError as e:
        raise HTTPException(503, str(e))
    return Response(
        data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="dacha-install.bat"', "Cache-Control": "no-store"},
    )


@app.get("/api/dacha/oneliner", dependencies=[Depends(require_user)])
async def dacha_oneliner(name: str = ""):
    """Та же установка одной командой PowerShell — без файла, который остаётся на диске."""
    try:
        cmd = build_oneliner(
            PUBLIC_URL, AGENT_TOKEN, TAILSCALE_AUTHKEY, GITHUB_REPO, GITHUB_BRANCH, _clean_device_name(name)
        )
    except InstallerError as e:
        raise HTTPException(503, str(e))
    return JSONResponse({"command": cmd}, headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# Страницы
# --------------------------------------------------------------------------- #

def page(name: str) -> FileResponse:
    return FileResponse(STATIC / name, headers={"Cache-Control": "no-store"})


@app.get("/")
async def index(request: Request):
    if request.session.get("user") != ADMIN_USER:
        return RedirectResponse("/login", status_code=303)
    return page("index.html")


@app.get("/login")
async def login_page(request: Request):
    if request.session.get("user") == ADMIN_USER:
        return RedirectResponse("/", status_code=303)
    return page("login.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# --------------------------------------------------------------------------- #
# Прокси к агенту
# --------------------------------------------------------------------------- #

PASS_THROUGH_ERRORS = {400, 403, 404, 409, 413, 429, 503}


def agent_error(r: httpx.Response) -> HTTPException:
    """Ошибка агента -> ошибка для браузера. 401 от агента не пробрасываем:
    это проблема токена, а не сессии пользователя."""
    try:
        detail = r.json().get("detail", "Ошибка агента")
    except Exception:
        detail = "Ошибка агента"
    if r.status_code in PASS_THROUGH_ERRORS:
        return HTTPException(r.status_code, detail)
    return HTTPException(502, "Агент отклонил запрос (проверь токен)")


async def agent_json(client: httpx.AsyncClient, method: str, url: str, **kwargs):
    try:
        r = await client.request(method, url, **kwargs)
    except httpx.HTTPError:
        raise HTTPException(502, "Компьютер недоступен (агент не отвечает)")
    if r.status_code >= 400:
        raise agent_error(r)
    return r


async def agent_stream(client: httpx.AsyncClient, url: str, *, params: dict, headers: dict | None = None) -> httpx.Response:
    """Открывает потоковый ответ агента. Вызывающий обязан закрыть его (aclose)."""
    req = client.build_request(
        "GET", url, params=params, headers=headers,
        timeout=httpx.Timeout(10.0, read=None),  # стрим может идти сколько угодно
    )
    try:
        r = await client.send(req, stream=True)
    except httpx.HTTPError:
        raise HTTPException(502, "Компьютер недоступен (агент не отвечает)")
    if r.status_code >= 400:
        await r.aread()
        await r.aclose()
        raise agent_error(r)
    return r


async def relay(r: httpx.Response):
    try:
        async for chunk in r.aiter_raw():
            yield chunk
    finally:
        # При отключении клиента задача отменяется — закрываем соединение под щитом
        with anyio.CancelScope(shield=True):
            await r.aclose()


@app.get("/api/status", dependencies=[Depends(require_user)])
async def status(device: str):
    d = DEVICES.get(device)
    if d is None:
        return {"online": False, "reason": "unknown_device"}
    try:
        r = await d["client"].get("/health", timeout=5.0)
        if r.status_code == 200:
            return {"online": True, **r.json()}
        return {"online": False, "reason": "agent_auth"}
    except httpx.HTTPError:
        return {"online": False, "reason": "unreachable"}


@app.get("/api/files/list", dependencies=[Depends(require_user)])
async def files_list(device: str, path: str = ""):
    d = get_device(device)
    return (await agent_json(d["client"], "GET", "/files/list", params={"path": path})).json()


@app.get("/api/files/download", dependencies=[Depends(require_user)])
async def files_download(request: Request, device: str, path: str):
    d = get_device(device)
    headers = {"Range": request.headers["range"]} if "range" in request.headers else None
    r = await agent_stream(d["client"], "/files/download", params={"path": path}, headers=headers)
    out = {
        k: r.headers[k]
        for k in ("content-length", "content-disposition", "content-range", "accept-ranges")
        if k in r.headers
    }
    log_activity(request, "file_download", device=device, path=path)
    return StreamingResponse(
        relay(r), status_code=r.status_code, media_type="application/octet-stream", headers=out
    )


@app.post("/api/files/upload", dependencies=[Depends(require_user)])
async def files_upload(request: Request, device: str, path: str, name: str, overwrite: bool = False):
    d = get_device(device)

    async def body():
        async for chunk in request.stream():
            yield chunk

    try:
        r = await d["client"].post(
            "/files/upload",
            params={"path": path, "name": name, "overwrite": str(overwrite).lower()},
            content=body(),
            timeout=httpx.Timeout(10.0, read=600.0, write=600.0),
        )
    except httpx.HTTPError:
        raise HTTPException(502, "Компьютер недоступен (агент не отвечает)")
    if r.status_code >= 400:
        raise agent_error(r)
    log_activity(request, "file_upload", device=device, path=path, name=name)
    return r.json()


@app.post("/api/input/{action}", dependencies=[Depends(require_user)])
async def input_proxy(action: str, device: str, request: Request):
    if action not in {"move", "click", "scroll", "key"}:
        raise HTTPException(404, "Нет такого действия")
    d = get_device(device)
    body = await request.body()
    if len(body) > 4096:
        raise HTTPException(413, "Слишком большой запрос")
    r = await agent_json(
        d["client"], "POST", f"/input/{action}", content=body,
        headers={"content-type": "application/json"}, timeout=10.0,
    )
    return r.json()


@app.post("/api/dacha/self-destruct", dependencies=[Depends(require_user)])
async def self_destruct_proxy(device: str, request: Request):
    d = get_device(device)
    result = (await agent_json(d["client"], "POST", "/self-destruct", timeout=10.0)).json()
    # Агент под этим id больше не вернётся (папка сотрётся) — не ждём, пока
    # пользователь нажмёт «забыть компьютер» вручную, убираем запись сразу.
    name = d["name"]
    DEVICES.pop(device, None)
    await d["client"].aclose()
    save_devices()
    log_activity(request, "self_destruct", device=device, name=name)
    return result


@app.get("/api/screen/monitors", dependencies=[Depends(require_user)])
async def screen_monitors(device: str):
    d = get_device(device)
    return (await agent_json(d["client"], "GET", "/screen/monitors")).json()


@app.get("/api/screen/snapshot", dependencies=[Depends(require_user)])
async def screen_snapshot(device: str, quality: int = 80, scale: float = 1.0, monitor: int = 1):
    d = get_device(device)
    r = await agent_json(
        d["client"], "GET", "/screen/snapshot",
        params={"quality": quality, "scale": scale, "monitor": monitor},
        timeout=30.0,
    )
    return Response(
        r.content, media_type="image/jpeg",
        headers={"Content-Disposition": 'attachment; filename="screen.jpg"'},
    )


@app.get("/api/screen/stream", dependencies=[Depends(require_user)])
async def screen_stream(device: str, fps: float = 5, quality: int = 60, scale: float = 0.5, monitor: int = 1):
    d = get_device(device)
    r = await agent_stream(
        d["client"], "/screen/stream",
        params={"fps": fps, "quality": quality, "scale": scale, "monitor": monitor},
    )
    return StreamingResponse(
        relay(r),
        media_type=r.headers.get("content-type", "multipart/x-mixed-replace; boundary=frame"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/camera/snapshot", dependencies=[Depends(require_user)])
async def camera_snapshot(device: str, quality: int = 80, scale: float = 1.0, index: int = 0):
    d = get_device(device)
    r = await agent_json(
        d["client"], "GET", "/camera/snapshot",
        params={"quality": quality, "scale": scale, "index": index},
        timeout=30.0,
    )
    return Response(
        r.content, media_type="image/jpeg",
        headers={"Content-Disposition": 'attachment; filename="camera.jpg"'},
    )


@app.get("/api/camera/stream", dependencies=[Depends(require_user)])
async def camera_stream(device: str, fps: float = 3, quality: int = 60, scale: float = 0.5, index: int = 0):
    d = get_device(device)
    r = await agent_stream(
        d["client"], "/camera/stream",
        params={"fps": fps, "quality": quality, "scale": scale, "index": index},
    )
    return StreamingResponse(
        relay(r),
        media_type=r.headers.get("content-type", "multipart/x-mixed-replace; boundary=frame"),
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------- #

def main() -> None:
    check_config()
    print(f"Сервер: http://{HOST}:{PORT}  (компьютеров известно: {len(load_saved_devices())})")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", proxy_headers=False)


if __name__ == "__main__":
    main()
