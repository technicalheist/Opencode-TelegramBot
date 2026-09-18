from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging
import os
import shutil
import subprocess
import time
import urllib.parse
from typing import Optional

import httpx

import config
from telegram_bot import bot, opencode_setup

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4096
HEALTH_PATHS = ("/global/health", "/api/health")


def parse_base_url(base_url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(base_url or "")
    host = parsed.hostname or DEFAULT_HOST
    port = parsed.port or DEFAULT_PORT
    return host, int(port)


def is_healthy(base_url: str, *, timeout: float = 2.0) -> bool:
    base = (base_url or "").rstrip("/")
    for path in HEALTH_PATHS:
        try:
            response = httpx.get(f"{base}{path}", timeout=timeout)
        except Exception:
            return False
        if response.status_code >= 400:
            continue
        try:
            body = response.json()
        except Exception:
            continue
        if isinstance(body, dict) and body.get("healthy") is True:
            return True
    return False


def _find_pid_via_os(port: int) -> Optional[int]:
    if os.name == "nt":
        command = ["netstat", "-ano", "-p", "tcp"]
    else:
        command = ["lsof", "-ti", f"tcp:{port}"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=10
        )
    except Exception:
        logger.warning("Could not inspect listening sockets for port %s", port)
        return None
    if os.name != "nt":
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
        return None
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING":
            continue
        local_address = parts[1]
        if ":" not in local_address:
            continue
        local_port = local_address.rsplit(":", 1)[1]
        if local_port == str(port) and parts[4].isdigit():
            return int(parts[4])
    return None


def find_listening_pid(port: int) -> Optional[int]:
    try:
        import psutil
    except Exception:
        psutil = None
    if psutil is not None:
        try:
            for connection in psutil.net_connections(kind="tcp"):
                laddr = getattr(connection, "laddr", None)
                if laddr is None:
                    continue
                if (
                    getattr(laddr, "port", None) == port
                    and getattr(connection, "status", None) == psutil.CONN_LISTEN
                    and connection.pid
                ):
                    return int(connection.pid)
        except Exception:
            logger.warning("psutil net_connections failed; using OS fallback")
    return _find_pid_via_os(port)


def _kill_pid_os(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True, timeout=10)
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


def _wait_for_port_free(port: int, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if find_listening_pid(port) is None:
            return True
        time.sleep(0.25)
    return False


def stop_existing_server(base_url: str, port: int) -> bool:
    try:
        pid = find_listening_pid(port)
        if pid is None:
            return False
        logger.info("Stopping existing opencode server on port %s (pid %s)", port, pid)
        _terminate_pid_tree(pid)
        if not _wait_for_port_free(port):
            logger.warning("Port %s still reported as listening after stop", port)
        return True
    except Exception:
        logger.exception("Failed while stopping existing server on port %s", port)
        return False


def build_serve_command(host: str, port: int) -> list[str]:
    command = config.OPENCODE_SERVE_COMMAND
    resolved = shutil.which(command)
    if resolved is None:
        raise RuntimeError(f"Could not find opencode command: {command!r}")
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        shell = os.environ.get("COMSPEC", "cmd.exe")
        return [
            shell,
            "/c",
            resolved,
            "serve",
            "--port",
            str(port),
            "--hostname",
            host,
        ]
    return [resolved, "serve", "--port", str(port), "--hostname", host]


def start_server(command: list[str]) -> subprocess.Popen:
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(command, cwd=str(_ROOT), creationflags=creationflags)


def wait_for_health(
    base_url: str, *, timeout: float = 60.0, interval: float = 0.5
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_healthy(base_url):
            return True
        time.sleep(interval)
    return False


def terminate_server(process: subprocess.Popen, port: int) -> None:
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
        logger.exception("Failed to terminate opencode server")


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    base_url = config.OPENCODE_BASE_URL
    host, port = parse_base_url(base_url)
    try:
        result = opencode_setup.install_all()
        logger.info("opencode setup install result: %s", result)
    except Exception:
        logger.exception("opencode setup install failed")
    process: Optional[subprocess.Popen] = None
    try:
        stop_existing_server(base_url, port)
        command = build_serve_command(host, port)
        logger.info("Starting opencode server on %s:%s", host, port)
        process = start_server(command)
        if not wait_for_health(base_url):
            logger.error("opencode server did not become healthy at %s", base_url)
            terminate_server(process, port)
            process = None
            return 1
        logger.info("opencode server healthy at %s", base_url)
        bot.main()
        return 0
    except Exception:
        logger.exception("Launcher failed")
        return 1
    finally:
        if process is not None:
            terminate_server(process, port)


if __name__ == "__main__":
    raise SystemExit(main())
