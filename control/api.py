"""Local HTTP API and web panel for the control plane.

The panel binds to loopback only and is guarded by a per-install admin token
printed as a one-time link by ``ocv open``. Three independent checks stand
between a browser and the bot: the token (cookie or ``?t=`` link), a Host
allowlist that defeats DNS rebinding, and a custom header on every mutating
request that blocks cross-site form posts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time
import webbrowser
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from control import __version__, paths, settings
from control.manager import Manager, ManagerError

logger = logging.getLogger(__name__)

COOKIE_NAME = "ocv_token"
CSRF_HEADER = "x-ocv"
WEB_DIR = Path(__file__).resolve().parent / "web"

POLL_INTERVAL = 0.5
HEARTBEAT_INTERVAL = 15.0
LOG_TAIL_BYTES = 256 * 1024
HEALTH_TIMEOUT = 0.5

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
LOG_SOURCES = ("bot", "panel")
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
LOOPBACK_HOSTS = ALLOWED_HOSTS
BOT_ACTIONS = {"start": "start_bot", "stop": "stop_bot", "restart": "restart_bot"}
OPENCODE_ACTIONS = {"start": "start_opencode", "stop": "stop_opencode"}

UNAUTHORIZED_TEXT = (
    "Unauthorized. Open the control panel with the one-time link printed by "
    "`ocv open`."
)

_Clock = Callable[[], float]


class SettingsBody(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    restart: bool = False


class SetupBody(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    start: bool = False


def _matches(candidate: str, expected: str) -> bool:
    if not candidate or not expected:
        return False
    return secrets.compare_digest(
        candidate.encode("utf-8"), expected.encode("utf-8")
    )


def _host_allowed(header: str) -> bool:
    host = (header or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return False
        name = host[: end + 1]
        rest = host[end + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return False
        return name in ALLOWED_HOSTS
    name, _, port = host.partition(":")
    if port and not port.isdigit():
        return False
    return name in ALLOWED_HOSTS


def _exempt(path: str) -> bool:
    return (
        path == "/api/health"
        or path == "/favicon.ico"
        or path == "/static"
        or path.startswith("/static/")
    )


def _bot_running(engine: Manager) -> bool:
    try:
        state = engine.snapshot().get("bot") or {}
    except Exception:
        logger.warning("Could not read the bot status", exc_info=True)
        return False
    return state.get("status") == "running"


def _validation_error(exc: ValueError) -> JSONResponse:
    detail = str(exc)
    return JSONResponse(
        status_code=400,
        content={"detail": detail, "errors": detail.splitlines()},
    )


def _invoke_action(
    engine: Manager, routes: Mapping[str, str], action: str, label: str
) -> dict:
    method = routes.get(action)
    if method is None:
        raise HTTPException(status_code=404, detail=f"Unknown {label} action: {action}")
    try:
        return getattr(engine, method)()
    except ManagerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _read_tail(path: Path, limit: int) -> list[str]:
    if limit <= 0:
        return []
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - LOG_TAIL_BYTES))
            data = stream.read()
    except OSError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()[-limit:]


def _read_since(path: Path, offset: int) -> tuple[int, list[str]]:
    try:
        size = path.stat().st_size
    except OSError:
        return 0, []
    start = offset if 0 <= offset <= size else 0
    try:
        with path.open("rb") as stream:
            stream.seek(start)
            data = stream.read()
    except OSError:
        return 0, []
    end = data.rfind(b"\n")
    if end < 0:
        return start, []
    text = data[: end + 1].decode("utf-8", errors="replace")
    return start + end + 1, text.splitlines()


def _log_lines(engine: Manager, source: str, limit: int) -> list[str]:
    if source == "bot":
        return engine.log_lines(limit=limit)
    return _read_tail(paths.panel_log(), limit)


def _log_size(engine: Manager, source: str) -> int:
    if source == "bot":
        return engine.log_size()
    try:
        return paths.panel_log().stat().st_size
    except OSError:
        return 0


def _log_since(engine: Manager, source: str, offset: int) -> tuple[int, list[str]]:
    if source == "bot":
        return engine.log_since(offset)
    return _read_since(paths.panel_log(), offset)


async def _stream_lines(
    engine: Manager, source: str, *, follow: bool = True, offset: int = 0
) -> AsyncIterator[str]:
    """Stream appended log lines from ``offset`` (0 replays the backlog)."""
    last_sent = time.monotonic()
    while True:
        offset, lines = _log_since(engine, source, offset)
        if lines:
            last_sent = time.monotonic()
            for line in lines:
                payload = json.dumps(line, ensure_ascii=False)
                yield f"event: line\ndata: {payload}\n\n"
        elif follow and time.monotonic() - last_sent >= HEARTBEAT_INTERVAL:
            last_sent = time.monotonic()
            yield ": keep-alive\n\n"
        if not follow:
            return
        await asyncio.sleep(POLL_INTERVAL)


def admin_token(*, create: bool = True) -> str:
    """Return the per-install panel token, creating it on first use."""
    path = paths.admin_token_file()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        token = ""
    if token:
        if os.name != "nt":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return token
    if not create:
        return ""

    path.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(24)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        handle = os.open(path, flags, 0o600)
    except FileExistsError:
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            existing = ""
        if existing:
            return existing
        try:
            path.write_text(generated + "\n", encoding="utf-8", newline="\n")
        except OSError:
            logger.exception("Could not write the admin token at %s", path)
            return ""
        return generated
    except OSError:
        logger.exception("Could not create the admin token at %s", path)
        return ""
    with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(generated + "\n")
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            logger.warning("Could not restrict permissions on %s", path)
    return generated


def create_app(
    *,
    manager: Manager | None = None,
    token: str | None = None,
    clock: _Clock | None = None,
    host_check: bool = True,
    port: int | None = None,
) -> FastAPI:
    """Build the panel app; dependencies are injectable for tests."""
    engine = manager if manager is not None else Manager()
    expected = token if token is not None else admin_token()
    now = clock or time.time
    started_at = now()
    panel_port = port if port is not None else paths.default_port()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            engine.shutdown()

    app = FastAPI(title="opencode-voice", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def guard(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if host_check and not _host_allowed(request.headers.get("host", "")):
            return JSONResponse({"detail": "Invalid Host header"}, status_code=403)

        path = request.url.path
        if request.method == "GET" and path == "/" and "t" in request.query_params:
            candidate = request.query_params["t"]
            if _matches(candidate, expected):
                redirect = RedirectResponse(url="/", status_code=302)
                redirect.set_cookie(
                    COOKIE_NAME,
                    expected,
                    httponly=True,
                    samesite="strict",
                    path="/",
                )
                return redirect

        if not _exempt(path):
            cookie = request.cookies.get(COOKIE_NAME, "")
            if not _matches(cookie, expected):
                if path == "/api" or path.startswith("/api/"):
                    return JSONResponse({"detail": "Unauthorized"}, status_code=401)
                return PlainTextResponse(UNAUTHORIZED_TEXT, status_code=401)

        if (
            request.method in MUTATING_METHODS
            and request.headers.get(CSRF_HEADER) != "1"
        ):
            return JSONResponse({"detail": "Missing X-OCV header"}, status_code=403)
        return await call_next(request)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": __version__}

    @app.get("/api/status")
    def status() -> dict:
        snapshot = dict(engine.snapshot())
        snapshot["panel"] = {
            "version": __version__,
            "port": panel_port,
            "config_dir": str(paths.config_dir()),
            "env_file": str(paths.env_file()),
            "uptime": max(0.0, now() - started_at),
        }
        return snapshot

    @app.post("/api/bot/{action}")
    def bot_action(action: str) -> dict:
        return _invoke_action(engine, BOT_ACTIONS, action, "bot")

    @app.post("/api/opencode/{action}")
    def opencode_action(action: str) -> dict:
        return _invoke_action(engine, OPENCODE_ACTIONS, action, "opencode")

    @app.get("/api/settings")
    def get_settings() -> dict:
        return settings.payload()

    @app.put("/api/settings")
    def put_settings(body: SettingsBody) -> Any:
        try:
            settings.save(body.values)
        except ValueError as exc:
            return _validation_error(exc)
        restarted = False
        restart_error: str | None = None
        if body.restart and _bot_running(engine):
            try:
                engine.restart_bot()
                restarted = True
            except ManagerError as exc:
                restart_error = str(exc)
        payload = {**settings.payload(), "restarted": restarted}
        if restart_error is not None:
            payload["restart_error"] = restart_error
        return payload

    @app.get("/api/setup")
    def get_setup() -> dict:
        values = {**settings.defaults(), **settings.load()}
        missing = settings.missing_required(values)
        return {"needed": bool(missing), "missing": missing, "configured": not missing}

    @app.post("/api/setup")
    def post_setup(body: SetupBody) -> Any:
        try:
            settings.save(body.values)
        except ValueError as exc:
            return _validation_error(exc)
        started = False
        start_error: str | None = None
        if body.start:
            try:
                engine.start_bot()
                started = True
            except ManagerError as exc:
                start_error = str(exc)
        return {**settings.payload(), "started": started, "start_error": start_error}

    @app.get("/api/logs")
    def logs(
        source: str = Query("bot"),
        lines: int = Query(200, ge=1, le=5000),
    ) -> dict:
        if source not in LOG_SOURCES:
            raise HTTPException(status_code=400, detail=f"Unknown log source: {source}")
        return {
            "lines": _log_lines(engine, source, lines),
            "offset": _log_size(engine, source),
        }

    @app.get("/api/logs/stream")
    async def log_stream(
        source: str = Query("bot"),
        follow: bool = Query(True),
        offset: int | None = Query(None, ge=0),
    ) -> StreamingResponse:
        if source not in LOG_SOURCES:
            raise HTTPException(status_code=400, detail=f"Unknown log source: {source}")
        start = offset if offset is not None else _log_size(engine, source)
        return StreamingResponse(
            _stream_lines(engine, source, follow=follow, offset=start),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.mount(
        "/static",
        StaticFiles(directory=str(WEB_DIR), check_dir=False),
        name="static",
    )
    return app


def _panel_running(url: str) -> bool:
    try:
        response = httpx.get(f"{url}/api/health", timeout=HEALTH_TIMEOUT, trust_env=False)
    except Exception:
        return False
    return response.status_code == 200


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        logger.warning("Could not open a browser (open the URL printed by `ocv open`)")


def _configure_file_logging() -> None:
    """Mirror ``control`` logs into ``logs/panel.log`` for the Logs tab."""
    root = logging.getLogger()
    if any(isinstance(handler, logging.FileHandler) for handler in root.handlers):
        return
    try:
        paths.ensure_dirs()
        handler = logging.FileHandler(paths.panel_log(), encoding="utf-8")
    except OSError:
        logger.warning("Could not open %s", paths.panel_log())
        return
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)


def run(
    *,
    port: int | None = None,
    host: str = "127.0.0.1",
    open_browser: bool = True,
) -> int:
    """Serve the panel, or focus the instance that is already running."""
    if host not in LOOPBACK_HOSTS:
        raise SystemExit(
            f"--host must be one of {', '.join(sorted(LOOPBACK_HOSTS))}; "
            "the panel is local-only by design."
        )
    paths.ensure_dirs()
    _configure_file_logging()
    resolved = port or paths.default_port()
    url = f"http://127.0.0.1:{resolved}"

    if _panel_running(url):
        logger.info("Control panel already running at %s", url)
        if open_browser:
            token = admin_token(create=False)
            _open_browser(f"{url}/?t={quote(token)}" if token else url)
        return 0

    token = admin_token()
    if open_browser:
        threading.Timer(
            1.0, _open_browser, args=(f"{url}/?t={quote(token)}",), daemon=True
        ).start()
    import uvicorn

    logger.info("Serving the control panel on %s:%s", host, resolved)
    uvicorn.run(
        create_app(port=resolved),
        host=host,
        port=resolved,
        log_level="info",
        access_log=False,
    )
    return 0
