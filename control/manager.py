"""Lifecycle management for the bot child process and the opencode server.

The panel never imports ``config``: settings come from :mod:`control.settings`
and the resolved file is handed to the bot child through ``OCV_ENV_FILE``, so a
first run with no settings file can still open the panel.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from control import paths, settings

logger = logging.getLogger(__name__)

SERVER_DOWN_TEXT = (
    "opencode server is not running. Start it from the panel or run "
    "'opencode service start'."
)
MISSING_CLI_TEXT = (
    "Could not find the opencode CLI (OPENCODE_COMMAND). Is opencode installed?"
)
CREDENTIALS_TEXT = (
    "opencode rejected the configured password (HTTP 401). Check "
    "OPENCODE_SERVER_PASSWORD and how the server was started."
)

LOG_TAIL_BYTES = 256 * 1024
LOG_ROTATE_BYTES = 5 * 1024 * 1024
PARENT_PID_ENV = "OCV_PARENT_PID"
DEFAULT_BASE_URL = "http://localhost:4096"
PROBE_TIMEOUT = 2.0
START_TIMEOUT = 30.0
STOP_TIMEOUT = 10.0
SERVICE_TIMEOUT = 60.0
POLL_INTERVAL = 0.5
TERMINATE_TIMEOUT = 5.0
CREATE_NO_WINDOW = 0x08000000

Probe = Callable[[str, tuple[str, str] | None, float], tuple[int | None, dict | None]]


class ManagerError(RuntimeError):
    """User-facing reason an action was refused or failed."""


def _default_probe(
    base_url: str,
    credentials: tuple[str, str] | None,
    timeout: float,
) -> tuple[int | None, dict | None]:
    auth = httpx.BasicAuth(*credentials) if credentials else None
    try:
        response = httpx.get(
            base_url.rstrip("/") + "/api/info",
            timeout=timeout,
            auth=auth,
            trust_env=False,
        )
    except Exception:
        return None, None
    try:
        body = response.json()
    except Exception:
        body = None
    return response.status_code, body if isinstance(body, dict) else None


class Manager:
    """Runs the Telegram bot as a child process and drives the opencode CLI."""

    def __init__(
        self,
        *,
        popen: Callable[..., Any] | None = None,
        which: Callable[[str], str | None] | None = None,
        clock: Callable[[], float] | None = None,
        probe: Probe | None = None,
        run: Callable[..., Any] | None = None,
    ) -> None:
        self._popen = popen or subprocess.Popen
        self._which = which or shutil.which
        self._clock = clock or time.time
        self._probe = probe or _default_probe
        self._run = run or subprocess.run
        self._lock = threading.Lock()
        self._process: Any = None
        self._status = "stopped"
        self._pid: int | None = None
        self._started_at: float | None = None
        self._exit_code: int | None = None
        self._detail = ""
        self._cancel_start = False
        self._opencode_detail = ""

    def snapshot(self) -> dict:
        try:
            self._reconcile()
            values = self._values()
            return {
                "bot": self._bot_state(),
                "opencode": self._opencode_state(values),
                "mode": "webhook"
                if (values.get("TELEGRAM_WEBHOOK_URL") or "").strip()
                else "polling",
                "env_file": str(paths.env_file()),
                "configured": settings.missing_required(settings.load()) == [],
            }
        except Exception as exc:
            logger.exception("Could not build a status snapshot")
            return self._fallback_snapshot(str(exc))

    def start_bot(self) -> dict:
        self._reconcile()
        with self._lock:
            if self._status in ("starting", "running") or self._process is not None:
                raise ManagerError("Bot is already running.")
            self._status = "starting"
            self._detail = "Checking the opencode server..."
            self._pid = None
            self._started_at = None
            self._exit_code = None
            self._cancel_start = False
        try:
            values = self._values()
            base_url = self._base_url(values)
            credentials = self._credentials(values)
            status, _ = self._probe_safe(base_url, credentials, PROBE_TIMEOUT)
            if status in (401, 403):
                raise ManagerError(CREDENTIALS_TEXT)
            if status != 200:
                self._set_detail("Starting the opencode server...")
                try:
                    self.start_opencode()
                except ManagerError as exc:
                    logger.info("opencode start failed: %s", exc)
                    if self._resolve_cli(values) is None:
                        raise ManagerError(SERVER_DOWN_TEXT) from exc
                if not self._wait_until_healthy(base_url, credentials):
                    raise ManagerError(SERVER_DOWN_TEXT)
            self._set_detail("Starting the bot...")
            if self._cancel_start:
                raise ManagerError("Start cancelled.")
            process = self._spawn()
        except ManagerError as exc:
            self._fail(str(exc))
            raise
        except Exception as exc:
            logger.exception("Could not start the bot")
            message = f"Could not start the bot: {exc}"
            self._fail(message)
            raise ManagerError(message) from exc
        with self._lock:
            self._process = process
            self._pid = getattr(process, "pid", None)
            self._started_at = self._clock()
            self._status = "running"
            self._detail = ""
            self._exit_code = None
        return self.snapshot()

    def stop_bot(self) -> dict:
        self._cancel_start = True
        self._reconcile()
        with self._lock:
            process = self._process
            if process is not None:
                self._status = "stopping"
                self._detail = "Stopping the bot..."
        if process is None:
            return self.snapshot()
        self._terminate(process)
        exit_code = self._poll(process)
        with self._lock:
            self._process = None
            self._pid = None
            self._started_at = None
            self._status = "stopped"
            self._detail = ""
            if exit_code is not None:
                self._exit_code = exit_code
        self._clear_pid()
        return self.snapshot()

    def restart_bot(self) -> dict:
        self.stop_bot()
        return self.start_bot()

    def start_opencode(self) -> dict:
        values = self._values()
        base_url = self._base_url(values)
        credentials = self._credentials(values)
        status, _ = self._probe_safe(base_url, credentials, PROBE_TIMEOUT)
        if status == 200:
            return self.snapshot()
        cli = self._resolve_cli(values)
        if cli is None:
            self._set_opencode_detail(MISSING_CLI_TEXT)
            raise ManagerError(MISSING_CLI_TEXT)
        detail = ""
        try:
            result = self._run(
                self._cli_command(cli, ["service", "start"]),
                capture_output=True,
                text=True,
                timeout=SERVICE_TIMEOUT,
            )
            detail = self._output_detail(result)
        except Exception as exc:
            logger.warning("opencode service start failed: %s", exc)
            detail = str(exc)
        if not self._wait_until_healthy(base_url, credentials):
            message = "Could not start the opencode server."
            if detail:
                message = f"{message} {detail}"
            self._set_opencode_detail(detail or "The server did not start.")
            raise ManagerError(message)
        self._set_opencode_detail("")
        return self.snapshot()

    def stop_opencode(self) -> dict:
        values = self._values()
        base_url = self._base_url(values)
        credentials = self._credentials(values)
        cli = self._resolve_cli(values)
        if cli is None:
            self._set_opencode_detail(MISSING_CLI_TEXT)
            raise ManagerError(MISSING_CLI_TEXT)
        detail = ""
        try:
            result = self._run(
                self._cli_command(cli, ["service", "stop"]),
                capture_output=True,
                text=True,
                timeout=SERVICE_TIMEOUT,
            )
            detail = self._output_detail(result)
        except Exception as exc:
            logger.warning("opencode service stop failed: %s", exc)
            detail = str(exc)
        if not self._wait_until_stopped(base_url, credentials):
            message = "Could not stop the opencode server."
            if detail:
                message = f"{message} {detail}"
            self._set_opencode_detail(detail or "The server is still running.")
            raise ManagerError(message)
        self._set_opencode_detail("")
        return self.snapshot()

    def shutdown(self) -> None:
        self._cancel_start = True
        try:
            self.stop_bot()
        except Exception:
            logger.exception("Could not stop the bot during shutdown")

    def log_lines(self, *, limit: int = 200) -> list[str]:
        if limit <= 0:
            return []
        path = paths.bot_log()
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - LOG_TAIL_BYTES))
                data = stream.read()
        except OSError:
            return []
        return data.decode("utf-8", errors="replace").splitlines()[-limit:]

    def log_since(self, offset: int) -> tuple[int, list[str]]:
        path = paths.bot_log()
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
        new_offset = start + end + 1
        lines = data[: end + 1].decode("utf-8", errors="replace").splitlines()
        return new_offset, lines

    def log_size(self) -> int:
        try:
            return paths.bot_log().stat().st_size
        except OSError:
            return 0

    def _values(self) -> dict[str, str]:
        return {**settings.defaults(), **settings.load()}

    @staticmethod
    def _base_url(values: Mapping[str, str]) -> str:
        return (values.get("OPENCODE_BASE_URL") or "").strip() or DEFAULT_BASE_URL

    def _credentials(self, values: Mapping[str, str]) -> tuple[str, str] | None:
        password = (values.get("OPENCODE_SERVER_PASSWORD") or "").strip()
        if not password:
            return None
        username = (values.get("OPENCODE_SERVER_USERNAME") or "").strip() or "opencode"
        return username, password

    def _probe_safe(
        self,
        base_url: str,
        credentials: tuple[str, str] | None,
        timeout: float,
    ) -> tuple[int | None, dict | None]:
        try:
            status, body = self._probe(base_url, credentials, timeout)
        except Exception:
            logger.debug("opencode probe failed", exc_info=True)
            return None, None
        try:
            code = int(status) if status is not None else None
        except (TypeError, ValueError):
            code = None
        return code, body if isinstance(body, dict) else None

    def _wait_until_healthy(
        self, base_url: str, credentials: tuple[str, str] | None
    ) -> bool:
        return self._poll_until(
            lambda: self._probe_safe(base_url, credentials, PROBE_TIMEOUT)[0] == 200,
            timeout=START_TIMEOUT,
        )

    def _wait_until_stopped(
        self, base_url: str, credentials: tuple[str, str] | None
    ) -> bool:
        return self._poll_until(
            lambda: self._probe_safe(base_url, credentials, PROBE_TIMEOUT)[0] != 200,
            timeout=STOP_TIMEOUT,
        )

    def _poll_until(
        self,
        check: Callable[[], bool],
        *,
        timeout: float,
        interval: float | None = None,
    ) -> bool:
        step = POLL_INTERVAL if interval is None else interval
        deadline = time.monotonic() + timeout
        attempts = max(1, int(timeout / step) + 1) if step > 0 else 1
        for _ in range(attempts):
            if check():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(step)
        return False

    def _resolve_cli(self, values: Mapping[str, str]) -> str | None:
        raw = (values.get("OPENCODE_COMMAND") or "").strip() or "opencode"
        try:
            found = self._which(raw)
        except Exception:
            found = None
        if found:
            return str(found)
        try:
            if os.path.isfile(raw):
                return raw
        except OSError:
            pass
        return None

    def _cli_command(self, cli: str, args: list[str]) -> list[str]:
        if os.name == "nt" and cli.lower().endswith((".cmd", ".bat")):
            shell = os.environ.get("COMSPEC", "cmd.exe")
            return [shell, "/c", cli, *args]
        return [cli, *args]

    def _spawn(self) -> Any:
        paths.ensure_dirs()
        log_path = paths.bot_log()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_log(log_path)
        env = os.environ.copy()
        env[paths.ENV_FILE_OVERRIDE] = str(paths.env_file())
        env[paths.CONFIG_DIR_OVERRIDE] = str(paths.config_dir())
        # The child exits when the panel does (even on a hard kill), so a
        # restarted panel cannot end up with two Telegram pollers.
        env[PARENT_PID_ENV] = str(os.getpid())
        kwargs: dict[str, Any] = {
            "cwd": str(paths.command_cwd()),
            "env": env,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "nt":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        with log_path.open("ab") as stream:
            kwargs["stdout"] = stream
            process = self._popen(paths.bot_command(), **kwargs)
        self._write_pid(process)
        return process

    def _write_pid(self, process: Any) -> None:
        pid = getattr(process, "pid", None)
        if not pid:
            return
        try:
            paths.pid_file().write_text(str(pid), encoding="utf-8")
        except OSError:
            logger.warning("Could not write the bot pid file")

    def _clear_pid(self) -> None:
        try:
            paths.pid_file().unlink()
        except OSError:
            pass

    def _rotate_log(self, path: Path) -> None:
        try:
            if path.stat().st_size <= LOG_ROTATE_BYTES:
                return
        except OSError:
            return
        try:
            os.replace(path, path.parent / f"{path.name}.1")
        except OSError:
            logger.warning("Could not rotate the bot log at %s", path)

    def _terminate(self, process: Any) -> None:
        if self._poll(process) is not None:
            return
        pid = getattr(process, "pid", None)
        if isinstance(pid, int):
            self._terminate_pid_tree(pid)
        try:
            process.wait(timeout=TERMINATE_TIMEOUT)
            return
        except Exception:
            logger.debug("process.wait failed", exc_info=True)
        if self._poll(process) is not None:
            return
        try:
            process.terminate()
        except Exception:
            logger.debug("process.terminate failed", exc_info=True)
        try:
            process.wait(timeout=TERMINATE_TIMEOUT)
        except Exception:
            pass
        if self._poll(process) is not None:
            return
        try:
            process.kill()
        except Exception:
            logger.debug("process.kill failed", exc_info=True)
        if isinstance(pid, int) and os.name == "nt":
            self._kill_pid_os(pid)

    def _terminate_pid_tree(self, pid: int) -> None:
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
        self._kill_pid_os(pid)

    def _kill_pid_os(self, pid: int) -> None:
        try:
            if os.name == "nt":
                self._run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=10,
                )
            else:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except Exception:
                    self._run(
                        ["kill", "-9", str(pid)], capture_output=True, timeout=10
                    )
        except Exception:
            logger.warning("Could not kill pid %s", pid)

    @staticmethod
    def _poll(process: Any) -> int | None:
        try:
            return process.poll()
        except Exception:
            return None

    @staticmethod
    def _clean_output(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace").strip()
        return str(value or "").strip()

    def _output_detail(self, result: Any) -> str:
        text = self._clean_output(getattr(result, "stderr", "")) or self._clean_output(
            getattr(result, "stdout", "")
        )
        return text[-800:]

    def _reconcile(self) -> None:
        with self._lock:
            process = self._process
            if process is None:
                return
            code = self._poll(process)
            if code is None:
                if self._status == "starting":
                    self._status = "running"
                return
            self._process = None
            self._pid = None
            self._started_at = None
            self._status = "stopped"
            self._exit_code = code
            self._detail = f"exited with code {code}" if code != 0 else ""
        self._clear_pid()

    def _bot_state(self) -> dict:
        with self._lock:
            status = self._status
            live = status in ("running", "stopping")
            started_at = self._started_at if live else None
            uptime: float | None = None
            if started_at is not None:
                try:
                    uptime = max(0.0, self._clock() - started_at)
                except Exception:
                    uptime = None
            return {
                "status": status,
                "pid": self._pid if live else None,
                "started_at": started_at,
                "uptime": uptime,
                "exit_code": self._exit_code,
                "detail": self._detail,
            }

    def _opencode_state(self, values: Mapping[str, str]) -> dict:
        base_url = (values.get("OPENCODE_BASE_URL") or "").strip()
        if not base_url:
            return {
                "status": "unknown",
                "version": None,
                "url": "",
                "detail": "No opencode server URL is configured.",
            }
        status, body = self._probe_safe(
            base_url, self._credentials(values), PROBE_TIMEOUT
        )
        if status == 200:
            version = None
            if body:
                raw = body.get("version")
                version = str(raw) if raw else None
            detail = f"opencode {version}" if version else "Server responded."
            return {
                "status": "running",
                "version": version,
                "url": base_url,
                "detail": detail,
            }
        if status in (401, 403):
            return {
                "status": "unauthorized",
                "version": None,
                "url": base_url,
                "detail": "The server rejected the configured credentials.",
            }
        if self._resolve_cli(values) is None:
            return {
                "status": "missing",
                "version": None,
                "url": base_url,
                "detail": "opencode CLI not found; install it or set OPENCODE_COMMAND.",
            }
        with self._lock:
            pending = self._opencode_detail
        return {
            "status": "stopped",
            "version": None,
            "url": base_url,
            "detail": pending or "The server is not responding.",
        }

    def _fallback_snapshot(self, detail: str) -> dict:
        try:
            env_file = str(paths.env_file())
        except Exception:
            env_file = ""
        return {
            "bot": {
                "status": "error",
                "pid": None,
                "started_at": None,
                "uptime": None,
                "exit_code": None,
                "detail": detail,
            },
            "opencode": {
                "status": "unknown",
                "version": None,
                "url": "",
                "detail": detail,
            },
            "mode": "polling",
            "env_file": env_file,
            "configured": False,
        }

    def _set_detail(self, detail: str) -> None:
        with self._lock:
            self._detail = detail

    def _set_opencode_detail(self, detail: str) -> None:
        with self._lock:
            self._opencode_detail = detail

    def _fail(self, message: str) -> None:
        with self._lock:
            self._status = "error"
            self._detail = message
            self._pid = None
            self._started_at = None
