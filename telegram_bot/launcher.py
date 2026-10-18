from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging
import os
import shutil
import signal
import subprocess
import time
from typing import Optional

import httpx

import config
from telegram_bot import bot, opencode_setup

logger = logging.getLogger(__name__)

HEALTH_PATHS = ("/api/info", "/api/health", "/global/health")


def _health_auth() -> Optional[httpx.BasicAuth]:
    credentials = config.opencode_server_credentials()
    if credentials is None:
        return None
    username, password = credentials
    return httpx.BasicAuth(username, password)


def is_healthy(base_url: str, *, timeout: float = 2.0) -> bool:
    base = (base_url or "").rstrip("/")
    auth = _health_auth()
    for path in HEALTH_PATHS:
        try:
            response = httpx.get(f"{base}{path}", timeout=timeout, auth=auth)
        except Exception:
            return False
        if response.status_code >= 400:
            continue
        try:
            body = response.json()
        except Exception:
            continue
        if not isinstance(body, dict):
            continue
        # v2 exposes ``/api/info`` (version/pid/urls); v1 used ``healthy``.
        if body.get("healthy") is True or "version" in body or "pid" in body:
            return True
    return False


def _kill_pid_os(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:
                subprocess.run(
                    ["kill", "-9", str(pid)], capture_output=True, timeout=10
                )
    except Exception:
        logger.warning("Could not kill pid %s", pid)


def _terminate_pid_tree(pid: int) -> None:
    try:
        import psutil
    except Exception:
        psutil = None
    if psutil is not None:
        try:
            process = psutil.Process(pid)
            children = process.children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except Exception:
                    pass
            try:
                process.terminate()
            except Exception:
                pass
            _, alive = psutil.wait_procs([process, *children], timeout=5)
            for survivor in alive:
                try:
                    survivor.kill()
                except Exception:
                    pass
            return
        except Exception:
            logger.warning("psutil terminate failed for pid %s; using OS kill", pid)
    _kill_pid_os(pid)


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def wait_for_health(
    base_url: str, *, timeout: float = 60.0, interval: float = 0.5
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_healthy(base_url):
            return True
        time.sleep(interval)
    return False


def build_cloudflared_command() -> Optional[list[str]]:
    if not config.cloudflared_enabled():
        return None
    resolved = shutil.which(config.CLOUDFLARED_COMMAND)
    if resolved is None and os.path.isfile(config.CLOUDFLARED_COMMAND):
        resolved = config.CLOUDFLARED_COMMAND
    if resolved is None:
        raise RuntimeError(
            f"Could not find cloudflared command: {config.CLOUDFLARED_COMMAND!r}"
        )
    tunnel_args = [
        "tunnel",
        "--no-autoupdate",
        "run",
        "--token",
        config.CLOUDFLARED_TUNNEL_TOKEN,
    ]
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        shell = os.environ.get("COMSPEC", "cmd.exe")
        return [shell, "/c", resolved, *tunnel_args]
    return [resolved, *tunnel_args]


def start_cloudflared(command: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        command, cwd=str(_ROOT), env=os.environ.copy(), **_popen_kwargs()
    )


def wait_for_tunnel(
    base_url: str,
    path: str,
    *,
    timeout: float = 60.0,
    interval: float = 2.0,
) -> bool:
    base = (base_url or "").rstrip("/")
    clean_path = (path or "").strip("/")
    url = f"{base}/{clean_path}" if clean_path else base
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=5.0)
            return True
        except Exception:
            time.sleep(interval)
    logger.warning("Tunnel did not respond at %s within %ss", url, timeout)
    return False


def terminate_process(process: subprocess.Popen) -> None:
    try:
        if process.poll() is not None:
            return
        _terminate_pid_tree(process.pid)
        try:
            process.wait(timeout=10)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    except Exception:
        logger.exception("Failed to terminate process")


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    base_url = config.OPENCODE_BASE_URL
    try:
        result = opencode_setup.install_all()
        logger.info("opencode setup install result: %s", result)
    except Exception:
        logger.exception("opencode setup install failed")
    cloudflared_process: Optional[subprocess.Popen] = None
    try:
        if not wait_for_health(base_url, timeout=30.0):
            logger.error(
                "No running opencode server at %s; start it first (`opencode service start`)",
                base_url,
            )
            return 1
        logger.info("opencode server healthy at %s", base_url)
        if config.cloudflared_enabled():
            try:
                cloudflared_command = build_cloudflared_command()
            except Exception:
                logger.exception("Could not build cloudflared command")
                cloudflared_command = None
            if cloudflared_command is not None:
                logger.info("Starting cloudflared tunnel")
                cloudflared_process = start_cloudflared(cloudflared_command)
                if not wait_for_tunnel(
                    config.TELEGRAM_WEBHOOK_URL, config.TELEGRAM_WEBHOOK_PATH
                ):
                    logger.warning(
                        "cloudflared tunnel not reachable yet; continuing anyway"
                    )
        bot.main()
        return 0
    except Exception:
        logger.exception("Launcher failed")
        return 1
    finally:
        if cloudflared_process is not None:
            terminate_process(cloudflared_process)


if __name__ == "__main__":
    raise SystemExit(main())
