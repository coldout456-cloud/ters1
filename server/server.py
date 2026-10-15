"""Сервер: вход по паролю + веб-интерфейс + проксирование запросов к агенту на даче.

Браузер общается только с этим сервером. Токен агента браузеру не передаётся.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import anyio
import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from dacha_installer import InstallerError, build_installer, parse_agent_address
from security import verify_password

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

AGENT_URL = os.environ.get("AGENT_URL", "http://127.0.0.1:8765").rstrip("/")
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
STATE_FILE = Path(os.environ.get("STATE_FILE", str(BASE / "agent_state.json")))  # последний адрес агента


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

agent: httpx.AsyncClient  # создаётся при старте


def load_saved_agent_url() -> str:
    """Адрес агента, который он сам сообщил в прошлый раз (пусто, если файла нет или он испорчен)."""
    try:
        url = str(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("agent_url", ""))
        parsed = urlparse(url)
        parse_agent_address(parsed.hostname, parsed.port)  # повторная проверка: файл мог быть подменён
        return url
    except Exception:
        return ""


def save_agent_url(url: str) -> None:
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"agent_url": url, "updated": time.time()}), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError:
        pass  # не страшно: агент сообщит адрес заново при следующей регистрации


@asynccontextmanager
async def lifespan(_: FastAPI):
    global agent
    agent = httpx.AsyncClient(
        base_url=load_saved_agent_url() or AGENT_URL,
        headers={"X-Agent-Token": AGENT_TOKEN},
        timeout=httpx.Timeout(10.0, read=30.0),
    )
    try:
        yield
    finally:
        await agent.aclose()


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


@app.post("/api/login")
async def login(request: Request):
    ip = client_ip(request)
    if is_locked(ip):
        raise HTTPException(429, "Слишком много попыток. Подожди 15 минут.")
    try:
        data = await request.json()
        username, password = str(data.get("username", "")), str(data.get("password", ""))
    except Exception:
        raise HTTPException(400, "Некорректный запрос")

    # Проверяем всегда обе части, чтобы время ответа не выдавало, что неверно
    user_ok = hmac.compare_digest(username.encode(), ADMIN_USER.encode())
    pass_ok = verify_password(password, ADMIN_PASSWORD_HASH)
    if not (user_ok and pass_ok):
        _failures.setdefault(ip, []).append(time.time())
        await asyncio.sleep(1)  # замедляем перебор
        raise HTTPException(401, "Неверный логин или пароль")

    _failures.pop(ip, None)
    request.session.clear()
    request.session["user"] = ADMIN_USER
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Дача: регистрация агента и персональный установщик
# --------------------------------------------------------------------------- #

@app.post("/api/agent/register")
async def agent_register(request: Request):
    """Агент на даче сам сообщает свой адрес в Tailscale. Вход на сайт не нужен, нужен токен."""
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
    except Exception:
        raise HTTPException(400, "Некорректный адрес агента")
    url = f"http://{host}:{port}"
    if str(agent.base_url).rstrip("/") != url:
        agent.base_url = url
        save_agent_url(url)
    return {"ok": True}


@app.get("/api/dacha/info", dependencies=[Depends(require_user)])
async def dacha_info():
    return {"installer_ready": bool(PUBLIC_URL), "tailscale_key": bool(TAILSCALE_AUTHKEY)}


@app.get("/api/dacha/installer", dependencies=[Depends(require_user)])
async def dacha_installer():
    """Готовый установщик для дачи: .bat с вшитыми адресом сайта, токеном и ключом Tailscale."""
    try:
        data = build_installer(PUBLIC_URL, AGENT_TOKEN, TAILSCALE_AUTHKEY, GITHUB_REPO, GITHUB_BRANCH)
    except InstallerError as e:
        raise HTTPException(503, str(e))
    return Response(
        data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="dacha-install.bat"', "Cache-Control": "no-store"},
    )


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


async def agent_json(method: str, url: str, **kwargs):
    try:
        r = await agent.request(method, url, **kwargs)
    except httpx.HTTPError:
        raise HTTPException(502, "Дача недоступна (агент не отвечает)")
    if r.status_code >= 400:
        raise agent_error(r)
    return r


async def agent_stream(url: str, *, params: dict, headers: dict | None = None) -> httpx.Response:
    """Открывает потоковый ответ агента. Вызывающий обязан закрыть его (aclose)."""
    req = agent.build_request(
        "GET", url, params=params, headers=headers,
        timeout=httpx.Timeout(10.0, read=None),  # стрим может идти сколько угодно
    )
    try:
        r = await agent.send(req, stream=True)
    except httpx.HTTPError:
        raise HTTPException(502, "Дача недоступна (агент не отвечает)")
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
async def status():
    try:
        r = await agent.get("/health", timeout=5.0)
        if r.status_code == 200:
            return {"online": True, **r.json()}
        return {"online": False, "reason": "agent_auth"}
    except httpx.HTTPError:
        return {"online": False, "reason": "unreachable"}


@app.get("/api/files/list", dependencies=[Depends(require_user)])
async def files_list(path: str = ""):
    return (await agent_json("GET", "/files/list", params={"path": path})).json()


@app.get("/api/files/download", dependencies=[Depends(require_user)])
async def files_download(request: Request, path: str):
    headers = {"Range": request.headers["range"]} if "range" in request.headers else None
    r = await agent_stream("/files/download", params={"path": path}, headers=headers)
    out = {
        k: r.headers[k]
        for k in ("content-length", "content-disposition", "content-range", "accept-ranges")
        if k in r.headers
    }
    return StreamingResponse(
        relay(r), status_code=r.status_code, media_type="application/octet-stream", headers=out
    )


@app.post("/api/files/upload", dependencies=[Depends(require_user)])
async def files_upload(request: Request, path: str, name: str, overwrite: bool = False):
    async def body():
        async for chunk in request.stream():
            yield chunk

    try:
        r = await agent.post(
            "/files/upload",
            params={"path": path, "name": name, "overwrite": str(overwrite).lower()},
            content=body(),
            timeout=httpx.Timeout(10.0, read=600.0, write=600.0),
        )
    except httpx.HTTPError:
        raise HTTPException(502, "Дача недоступна (агент не отвечает)")
    if r.status_code >= 400:
        raise agent_error(r)
    return r.json()


@app.get("/api/screen/monitors", dependencies=[Depends(require_user)])
async def screen_monitors():
    return (await agent_json("GET", "/screen/monitors")).json()


@app.get("/api/screen/snapshot", dependencies=[Depends(require_user)])
async def screen_snapshot(quality: int = 80, scale: float = 1.0, monitor: int = 1):
    r = await agent_json(
        "GET", "/screen/snapshot",
        params={"quality": quality, "scale": scale, "monitor": monitor},
        timeout=30.0,
    )
    return Response(
        r.content, media_type="image/jpeg",
        headers={"Content-Disposition": 'attachment; filename="screen.jpg"'},
    )


@app.get("/api/screen/stream", dependencies=[Depends(require_user)])
async def screen_stream(fps: float = 5, quality: int = 60, scale: float = 0.5, monitor: int = 1):
    r = await agent_stream(
        "/screen/stream",
        params={"fps": fps, "quality": quality, "scale": scale, "monitor": monitor},
    )
    return StreamingResponse(
        relay(r),
        media_type=r.headers.get("content-type", "multipart/x-mixed-replace; boundary=frame"),
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------- #

def main() -> None:
    check_config()
    print(f"Сервер: http://{HOST}:{PORT}  (агент: {AGENT_URL})")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", proxy_headers=False)


if __name__ == "__main__":
    main()
