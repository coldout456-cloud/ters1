"""Агент для дачного компьютера (Windows).

Отдаёт файлы из разрешённых папок и картинку экрана. Слушает только адрес из
AGENT_HOST (по умолчанию 127.0.0.1; в боевой схеме это адрес внутри
WireGuard/Tailscale) и требует токен в заголовке X-Agent-Token.

Важно: запускать нужно в сессии залогиненного пользователя (например, через
Планировщик заданий «при входе в систему»), а не как настоящую службу Windows:
служба работает в изолированной сессии и не видит рабочий стол и камеру.
"""
from __future__ import annotations

import asyncio
import hmac
import io
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from PIL import Image, ImageDraw

# Вывод в файл/консоль Windows может быть не в UTF-8: не даём русскому тексту уронить агента
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

def load_env(path: Path) -> None:
    """Мини-загрузчик .env (KEY=VALUE), без экранирования — пути Windows как есть."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(Path(__file__).with_name(".env"))

def _flag(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


TOKEN = os.environ.get("AGENT_TOKEN", "")
HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENT_PORT", "8765"))
SCREEN_BACKEND = os.environ.get("SCREEN_BACKEND", "mss")  # mss | test
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2048"))
MAX_STREAMS = 3
SERVER_URL = os.environ.get("SERVER_URL", "").rstrip("/")  # адрес сайта: сюда агент сообщает свой адрес
ALLOW_ALL_FILES = _flag("ALLOW_ALL_FILES")  # True — виден весь диск, а не только папки из ALLOWED_DIRS
INPUT_ENABLED = _flag("INPUT_ENABLED", "1")  # управление мышью (по умолчанию включено)
INSTALL_DIR = Path(__file__).resolve().parent.parent  # ...\DachaAgent (в ней лежит uninstall.ps1)


def parse_roots(raw: str) -> dict[str, Path]:
    """'docs=C:\\Docs;photos=D:\\Photos' -> {'docs': Path, 'photos': Path}."""
    roots: dict[str, Path] = {}
    for part in filter(None, (p.strip() for p in raw.split(";"))):
        alias, sep, folder = part.partition("=")
        alias, folder = alias.strip(), folder.strip()
        if not sep or not re.fullmatch(r"[\w\-]+", alias):
            sys.exit(f"ALLOWED_DIRS: неверная запись '{part}'. Формат: имя=путь;имя2=путь2")
        p = Path(folder).expanduser()
        if not p.is_dir():
            sys.exit(f"ALLOWED_DIRS: папки не существует: {folder}")
        roots[alias] = p.resolve()
    return roots


ROOTS = parse_roots(os.environ.get("ALLOWED_DIRS", ""))


# --------------------------------------------------------------------------- #
# Авторизация и безопасные пути
# --------------------------------------------------------------------------- #

def require_token(x_agent_token: str = Header(default="")) -> None:
    if not TOKEN or not hmac.compare_digest(x_agent_token.encode(), TOKEN.encode()):
        raise HTTPException(401, "bad token")


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    dependencies=[Depends(require_token)],  # токен нужен на всех маршрутах
)


def resolve_path(vpath: str) -> Path:
    """Виртуальный путь 'имя/подпапка/файл' -> реальный путь внутри разрешённой папки.

    Путь нормализуется (resolve раскрывает '..' и симлинки), и только потом
    проверяется, что он остался внутри корня. Выйти за пределы нельзя.
    """
    vpath = vpath.replace("\\", "/").strip("/")
    if not vpath:
        raise HTTPException(400, "Пустой путь")
    alias, _, rest = vpath.partition("/")
    root = ROOTS.get(alias)
    if root is None:
        raise HTTPException(404, "Неизвестная папка")
    try:
        target = (root / rest).resolve() if rest else root
    except (OSError, ValueError):
        raise HTTPException(400, "Некорректный путь")
    if target != root and not target.is_relative_to(root):
        raise HTTPException(403, "Путь вне разрешённой папки")
    return target


def list_drives() -> list[str]:
    """Буквы дисков Windows: ['C:', 'D:']. На прочих ОС — корень '/'."""
    if os.name != "nt":
        return ["/"]
    import string

    drives = []
    try:
        from ctypes import windll  # type: ignore

        bitmask = windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if bitmask & (1 << i):
                drives.append(f"{letter}:")
    except Exception:
        for letter in string.ascii_uppercase:
            if os.path.exists(f"{letter}:\\"):
                drives.append(f"{letter}:")
    return drives


def resolve_any(vpath: str) -> Path:
    """Режим «весь диск»: 'C:/Users/Me/файл' -> реальный путь. Разрешён только
    абсолютный путь на локальном диске (UNC-пути \\\\сервер\\шара отклоняются)."""
    vpath = vpath.replace("\\", "/").strip("/")
    if not vpath:
        raise HTTPException(400, "Пустой путь")
    if os.name == "nt":
        first, _, rest = vpath.partition("/")
        if not re.fullmatch(r"[A-Za-z]:", first):
            raise HTTPException(400, "Путь должен начинаться с диска, например C:/Users")
        raw = first + "\\" + rest.replace("/", "\\") if rest else first + "\\"
        try:
            target = Path(raw).resolve()
        except (OSError, ValueError):
            raise HTTPException(400, "Некорректный путь")
        if not re.fullmatch(r"[A-Za-z]:", target.drive):  # отсекаем UNC и прочее
            raise HTTPException(403, "Недопустимый путь")
        return target
    try:
        return Path("/" + vpath).resolve()
    except (OSError, ValueError):
        raise HTTPException(400, "Некорректный путь")


def resolve(vpath: str) -> Path:
    """Выбирает способ в зависимости от режима доступа."""
    return resolve_any(vpath) if ALLOW_ALL_FILES else resolve_path(vpath)


_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def validate_name(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or _INVALID_NAME.search(name)
        or name.endswith((" ", "."))
        or name.split(".")[0].upper() in _WIN_RESERVED
        or len(name) > 200
    ):
        raise HTTPException(400, "Недопустимое имя файла")


# --------------------------------------------------------------------------- #
# Общее
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    return {
        "ok": True,
        "hostname": socket.gethostname(),
        "roots": list(ROOTS),
        "all_files": ALLOW_ALL_FILES,
        "input": INPUT_ENABLED,
        "screen": SCREEN_BACKEND,
        "time": time.time(),
    }


# --------------------------------------------------------------------------- #
# Файлы
# --------------------------------------------------------------------------- #

@app.get("/files/list")
def files_list(path: str = ""):
    if not path.strip("/\\ "):
        names = list_drives() if ALLOW_ALL_FILES else list(ROOTS)
        return {
            "path": "",
            "entries": [{"name": a, "is_dir": True, "size": None, "mtime": None} for a in names],
        }
    target = resolve(path)
    if not target.is_dir():
        raise HTTPException(400, "Это не папка")
    entries = []
    try:
        with os.scandir(target) as it:
            for e in it:
                try:
                    st = e.stat()
                    is_dir = e.is_dir()
                except OSError:
                    continue
                entries.append({
                    "name": e.name,
                    "is_dir": is_dir,
                    "size": None if is_dir else st.st_size,
                    "mtime": st.st_mtime,
                })
    except PermissionError:
        raise HTTPException(403, "Нет доступа к папке")
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {"path": path.replace("\\", "/").strip("/"), "entries": entries}


@app.get("/files/download")
def files_download(path: str):
    target = resolve(path)
    if not target.is_file():
        raise HTTPException(404, "Файл не найден")
    # Всегда «скачать», никогда не открывать в браузере: так HTML-файл из
    # папки не сможет выполниться на нашем сайте.
    return FileResponse(
        target,
        filename=target.name,
        media_type="application/octet-stream",
        content_disposition_type="attachment",
    )


@app.post("/files/upload")
async def files_upload(request: Request, path: str, name: str, overwrite: bool = False):
    """Тело запроса — сырые байты файла (без multipart)."""
    validate_name(name)
    dest = resolve(f"{path.strip('/')}/{name}")
    folder = dest.parent
    if not folder.is_dir():
        raise HTTPException(400, "Папка не найдена")
    if dest.is_dir():
        raise HTTPException(409, "На этом месте уже есть папка")
    if dest.exists() and not overwrite:
        raise HTTPException(409, "Файл уже существует")

    limit = MAX_UPLOAD_MB * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "Файл слишком большой")

    tmp = folder / f".{secrets.token_hex(6)}.part"
    size = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, "Файл слишком большой")
                f.write(chunk)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return {"ok": True, "name": name, "size": size}


# --------------------------------------------------------------------------- #
# Экран
# --------------------------------------------------------------------------- #

class MssGrabber:
    """Захват настоящего экрана (mss). Объект нужно использовать из одного потока."""

    def __init__(self, monitor: int):
        import mss

        self.sct = mss.mss()
        self.mon = self.sct.monitors[monitor]

    def grab(self) -> Image.Image:
        shot = self.sct.grab(self.mon)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def close(self) -> None:
        self.sct.close()


class TestGrabber:
    """Картинка-заглушка без реального экрана — проверить всю цепочку."""

    def __init__(self, monitor: int = 1):
        self.n = 0

    def grab(self) -> Image.Image:
        self.n += 1
        img = Image.new("RGB", (1280, 720), (20 + self.n % 200, 60, 120))
        ImageDraw.Draw(img).text(
            (40, 40), f"TEST SCREEN  frame {self.n}  {time.strftime('%H:%M:%S')}", fill=(255, 255, 255)
        )
        return img

    def close(self) -> None:
        pass


def make_grabber(monitor: int):
    return TestGrabber(monitor) if SCREEN_BACKEND == "test" else MssGrabber(monitor)


def list_monitors() -> list[dict]:
    if SCREEN_BACKEND == "test":
        return [{"index": 1, "width": 1280, "height": 720}]
    import mss

    with mss.mss() as sct:
        return [
            {"index": i, "width": m["width"], "height": m["height"]}
            for i, m in enumerate(sct.monitors)
            if i > 0  # 0 — «все мониторы сразу», пропускаем
        ]


def check_screen(monitor: int) -> None:
    try:
        indexes = [m["index"] for m in list_monitors()]
    except Exception as e:  # нет дисплея, нет прав и т.п.
        raise HTTPException(503, f"Захват экрана недоступен: {e}")
    if monitor not in indexes:
        raise HTTPException(400, "Нет такого монитора")


def grab_and_encode(grabber, quality: int, scale: float) -> bytes:
    img = grabber.grab()
    if scale < 1.0:
        img = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.Resampling.BILINEAR,
        )
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


@app.get("/screen/monitors")
def screen_monitors():
    try:
        return {"monitors": list_monitors()}
    except Exception as e:
        raise HTTPException(503, f"Захват экрана недоступен: {e}")


@app.get("/screen/snapshot")
def screen_snapshot(quality: int = 80, scale: float = 1.0, monitor: int = 1):
    quality, scale = int(_clamp(quality, 10, 95)), _clamp(scale, 0.1, 1.0)
    check_screen(monitor)
    grabber = make_grabber(monitor)
    try:
        data = grab_and_encode(grabber, quality, scale)
    finally:
        grabber.close()
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


_active_streams = 0


@app.get("/screen/stream")
async def screen_stream(fps: float = 5, quality: int = 60, scale: float = 0.5, monitor: int = 1):
    """MJPEG-поток (multipart/x-mixed-replace): браузер показывает его прямо в <img>."""
    global _active_streams
    fps, quality, scale = _clamp(fps, 0.5, 30), int(_clamp(quality, 10, 95)), _clamp(scale, 0.1, 1.0)
    check_screen(monitor)
    if _active_streams >= MAX_STREAMS:
        raise HTTPException(429, "Слишком много одновременных потоков")
    _active_streams += 1  # уменьшится в finally генератора

    async def gen():
        global _active_streams
        loop = asyncio.get_running_loop()
        # Один выделенный поток на весь стрим: захвату экрана нужен один и тот же поток.
        pool = ThreadPoolExecutor(max_workers=1)
        grabber = None
        interval = 1.0 / fps
        try:
            grabber = await loop.run_in_executor(pool, make_grabber, monitor)
            while True:
                t0 = time.monotonic()
                data = await loop.run_in_executor(pool, grab_and_encode, grabber, quality, scale)
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(data)).encode()
                    + b"\r\n\r\n"
                    + data
                    + b"\r\n"
                )
                await asyncio.sleep(max(0.0, interval - (time.monotonic() - t0)))
        finally:
            _active_streams -= 1
            if grabber is not None:
                pool.submit(grabber.close)
            pool.shutdown(wait=False)

    return StreamingResponse(
        gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------- #
# Управление мышью
# --------------------------------------------------------------------------- #

_mouse = None          # (Controller, Button) — создаётся один раз при первом запросе
_mouse_error = ""      # если pynput недоступен, запоминаем причину и больше не пытаемся


def mouse_controller():
    global _mouse, _mouse_error
    if _mouse is not None:
        return _mouse
    if _mouse_error:
        raise HTTPException(503, _mouse_error)
    try:
        from pynput.mouse import Button, Controller

        _mouse = (Controller(), Button)
        return _mouse
    except Exception as e:  # библиотека не установлена, нет сессии рабочего стола и т.п.
        _mouse_error = f"Управление мышью недоступно: {e}"
        raise HTTPException(503, _mouse_error)


def monitor_rect(monitor: int) -> dict:
    """Границы монитора в координатах виртуального рабочего стола (left/top/width/height)."""
    if SCREEN_BACKEND == "test":
        return {"left": 0, "top": 0, "width": 1280, "height": 720}
    import mss

    with mss.mss() as sct:
        if monitor < 1 or monitor >= len(sct.monitors):
            raise HTTPException(400, "Нет такого монитора")
        m = sct.monitors[monitor]
        return {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}


def point_from_norm(nx: float, ny: float, monitor: int) -> tuple[int, int]:
    """Доля 0..1 по выбранному монитору -> абсолютная точка на рабочем столе."""
    r = monitor_rect(monitor)
    x = int(round(r["left"] + _clamp(nx, 0.0, 1.0) * (r["width"] - 1)))
    y = int(round(r["top"] + _clamp(ny, 0.0, 1.0) * (r["height"] - 1)))
    return x, y


async def _input_body(request: Request) -> dict:
    if not INPUT_ENABLED:
        raise HTTPException(403, "Управление мышью отключено")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Нужен JSON")
    if not isinstance(data, dict):
        raise HTTPException(400, "Нужен объект JSON")
    return data


@app.post("/input/move")
async def input_move(request: Request):
    d = await _input_body(request)
    ctl, _ = mouse_controller()
    ctl.position = point_from_norm(float(d.get("x", 0)), float(d.get("y", 0)), int(d.get("monitor", 1)))
    return {"ok": True}


@app.post("/input/click")
async def input_click(request: Request):
    d = await _input_body(request)
    ctl, Button = mouse_controller()
    buttons = {"left": Button.left, "right": Button.right, "middle": Button.middle}
    btn = buttons.get(str(d.get("button", "left")), Button.left)
    count = 2 if d.get("double") else 1
    ctl.position = point_from_norm(float(d.get("x", 0)), float(d.get("y", 0)), int(d.get("monitor", 1)))
    ctl.click(btn, count)
    return {"ok": True}


@app.post("/input/scroll")
async def input_scroll(request: Request):
    d = await _input_body(request)
    ctl, _ = mouse_controller()
    ctl.position = point_from_norm(float(d.get("x", 0)), float(d.get("y", 0)), int(d.get("monitor", 1)))
    dy = int(_clamp(float(d.get("dy", 0)), -10, 10))
    dx = int(_clamp(float(d.get("dx", 0)), -10, 10))
    ctl.scroll(dx, dy)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Самоуничтожение: удалить агента с компьютера по кнопке с сайта
# --------------------------------------------------------------------------- #

@app.post("/self-destruct")
def self_destruct():
    """Запускает uninstall.ps1 в отдельном процессе и выходит. Скрипт снимает
    автозапуск и правило брандмауэра (если хватает прав), завершает агента и
    удаляет всю папку установки."""
    uninstall = INSTALL_DIR / "uninstall.ps1"
    if os.name != "nt" or not uninstall.exists():
        raise HTTPException(503, "Самоуничтожение доступно только на установленной копии (Windows)")
    import subprocess

    DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
             "-File", str(uninstall), "-Yes"],
            cwd=str(INSTALL_DIR), creationflags=DETACHED, close_fds=True,
        )
    except Exception as e:
        raise HTTPException(500, f"Не удалось запустить удаление: {e}")
    # Даём ответу уйти на сайт, затем выходим — uninstall.ps1 подождёт пару секунд и доделает остальное.
    threading.Timer(1.5, lambda: os._exit(0)).start()
    return {"ok": True, "message": "Агент удаляется с компьютера"}


# --------------------------------------------------------------------------- #

def register_once(server_url: str, token: str, host: str, port: int, timeout: float = 10.0) -> bool:
    """Сообщает сайту адрес агента (внутри VPN). True, если сайт принял."""
    req = urllib.request.Request(
        server_url + "/api/agent/register",
        data=json.dumps({"host": host, "port": port}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-Agent-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"Не удалось сообщить адрес сайту: {e}", flush=True)
        return False


def _register_loop() -> None:
    """Сообщает адрес при запуске и потом раз в 5 минут (если сайт был недоступен, пробует чаще)."""
    while True:
        ok = register_once(SERVER_URL, TOKEN, HOST, PORT)
        time.sleep(300 if ok else 60)


def main() -> None:
    if len(TOKEN) < 24:
        sys.exit(
            "AGENT_TOKEN не задан или короче 24 символов.\n"
            'Сгенерируй: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    if not ROOTS and not ALLOW_ALL_FILES:
        sys.exit("ALLOWED_DIRS не задан. Пример: ALLOWED_DIRS=docs=C:\\Users\\Me\\Documents")
    where = "весь диск" if ALLOW_ALL_FILES else ", ".join(ROOTS)
    print(f"Агент слушает {HOST}:{PORT}, файлы: {where}, мышь: {INPUT_ENABLED}, экран: {SCREEN_BACKEND}")
    if SERVER_URL and not HOST.startswith("127."):
        threading.Thread(target=_register_loop, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
