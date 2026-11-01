"""Install and control the panel as a per-user background service.

The service runs the panel in the foreground under the platform's own user-level
supervisor: a systemd ``--user`` unit on Linux, a launchd LaunchAgent on macOS,
and a logon-triggered scheduled task on Windows. Nothing outside the unit file
(the config directory, media, logs) is ever touched, so uninstalling the service
leaves the user's data intact.
"""

from __future__ import annotations

import getpass
import logging
import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from control import paths

logger = logging.getLogger(__name__)

SERVICE_NAME = "opencode-voice"
LAUNCHD_LABEL = "io.opencodevoice"
SYSTEMD_UNIT = "opencode-voice.service"

Runner = Callable[..., Any]

_SYSTEMD_UNIT_DIR = Path(".config") / "systemd" / "user"
_LAUNCHD_AGENT_DIR = Path("Library") / "LaunchAgents"


class ServiceError(RuntimeError):
    """User-facing failure while installing or controlling the service."""


@dataclass(frozen=True)
class ServiceResult:
    ok: bool
    action: str
    detail: str
    path: str | None = None


@dataclass(frozen=True)
class _Run:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def platform_key() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    raise ServiceError(f"Unsupported platform: {sys.platform}")


def unit_path() -> Path | None:
    key = platform_key()
    if key == "windows":
        return None
    if key == "macos":
        return Path.home() / _LAUNCHD_AGENT_DIR / f"{LAUNCHD_LABEL}.plist"
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "systemd" / "user" / SYSTEMD_UNIT


def _systemctl(*args: str) -> list[str]:
    return ["systemctl", "--user", *args]


def _loginctl(*args: str) -> list[str]:
    return ["loginctl", *args]


def _launchctl(*args: str) -> list[str]:
    return ["launchctl", *args]


def _schtasks(*args: str) -> list[str]:
    return ["schtasks", *args]


def _uid() -> int:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if getuid is not None else 0


def _posix_user() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or getpass.getuser()


def _windows_user() -> str:
    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN", "")
    if domain and user and "\\" not in user:
        return f"{domain}\\{user}"
    return user


def _windows_arg(part: str) -> str:
    return f'"{part}"' if any(char in part for char in (" ", "\t")) else part


def systemd_unit(command: list[str], *, working_dir: str) -> str:
    exec_start = " ".join(shlex.quote(part) for part in command)
    return "\n".join(
        [
            "[Unit]",
            "Description=opencode-voice control panel",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={exec_start}",
            f"WorkingDirectory={shlex.quote(working_dir)}",
            "Restart=on-failure",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def launchd_plist(command: list[str], *, working_dir: str, log_path: str) -> str:
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": list(command),
        "WorkingDirectory": working_dir,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    return plistlib.dumps(payload, sort_keys=False).decode("utf-8")


def windows_task_xml(command: list[str], *, working_dir: str, user: str) -> str:
    executable = _windows_arg(command[0]) if command else ""
    arguments = " ".join(_windows_arg(part) for part in command[1:])
    escaped_user = escape(user)
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>opencode-voice control panel</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"      <UserId>{escaped_user}</UserId>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{escaped_user}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <RestartOnFailure>\n"
        "      <Interval>PT1M</Interval>\n"
        "      <Count>3</Count>\n"
        "    </RestartOnFailure>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{escape(executable)}</Command>\n"
        f"      <Arguments>{escape(arguments)}</Arguments>\n"
        f"      <WorkingDirectory>{escape(working_dir)}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _runner(runner: Runner | None) -> Runner:
    return subprocess.run if runner is None else runner


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _invoke(runner: Runner, command: list[str]) -> _Run:
    try:
        completed = runner(command, capture_output=True, text=True)
    except FileNotFoundError:
        return _Run(127, "", f"{command[0]}: command not found")
    except OSError as exc:
        return _Run(127, "", str(exc))
    code = getattr(completed, "returncode", 1)
    try:
        returncode = 1 if code is None else int(code)
    except (TypeError, ValueError):
        returncode = 1
    return _Run(
        returncode,
        _text(getattr(completed, "stdout", None)),
        _text(getattr(completed, "stderr", None)),
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _failure(tool: str, result: _Run) -> str:
    reason = (
        _first_line(result.stderr)
        or _first_line(result.stdout)
        or f"exit code {result.returncode}"
    )
    return f"{tool} failed: {reason}"


def _not_installed(action: str) -> ServiceResult:
    return ServiceResult(False, action, "Service is not installed.")


def _missing_task(result: _Run) -> bool:
    combined = f"{result.stdout}\n{result.stderr}".lower()
    return any(
        marker in combined
        for marker in ("cannot find", "does not exist", "no such", "not found")
    )


def _write_file(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, mode)


def _install_command(port: int) -> list[str]:
    return paths.panel_command(port=port)


def _install_linux(command: list[str], runner: Runner) -> ServiceResult:
    path = unit_path()
    if path is None:
        raise ServiceError("Linux install requires a systemd unit path")
    try:
        _write_file(path, systemd_unit(command, working_dir=str(paths.command_cwd())))
    except OSError as exc:
        return ServiceResult(
            False, "install", f"Could not write the unit file: {exc}", str(path)
        )
    reload_result = _invoke(runner, _systemctl("daemon-reload"))
    if not reload_result.ok:
        return ServiceResult(
            False, "install", _failure("systemctl", reload_result), str(path)
        )
    enable_result = _invoke(runner, _systemctl("enable", "--now", SYSTEMD_UNIT))
    if not enable_result.ok:
        return ServiceResult(
            False, "install", _failure("systemctl", enable_result), str(path)
        )
    linger_result = _invoke(runner, _loginctl("enable-linger", _posix_user()))
    if not linger_result.ok:
        logger.debug(
            "loginctl enable-linger failed: %s", _failure("loginctl", linger_result)
        )
    return ServiceResult(
        True, "install", "Installed and started (systemd user unit).", str(path)
    )


def _install_macos(command: list[str], runner: Runner) -> ServiceResult:
    path = unit_path()
    if path is None:
        raise ServiceError("macOS install requires a LaunchAgent path")
    plist = launchd_plist(
        command,
        working_dir=str(paths.command_cwd()),
        log_path=str(paths.logs_dir() / "panel.log"),
    )
    try:
        _write_file(path, plist)
    except OSError as exc:
        return ServiceResult(
            False, "install", f"Could not write the LaunchAgent: {exc}", str(path)
        )
    domain = f"gui/{_uid()}"
    _invoke(runner, _launchctl("bootout", domain, str(path)))
    bootstrap_result = _invoke(runner, _launchctl("bootstrap", domain, str(path)))
    if not bootstrap_result.ok:
        return ServiceResult(
            False, "install", _failure("launchctl", bootstrap_result), str(path)
        )
    kickstart_result = _invoke(
        runner, _launchctl("kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}")
    )
    if not kickstart_result.ok:
        return ServiceResult(
            False, "install", _failure("launchctl", kickstart_result), str(path)
        )
    return ServiceResult(
        True, "install", "Installed and started (launchd LaunchAgent).", str(path)
    )


def _install_windows(command: list[str], runner: Runner) -> ServiceResult:
    xml = windows_task_xml(
        command, working_dir=str(paths.command_cwd()), user=_windows_user()
    )
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".xml", delete=False, encoding="utf-16"
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(xml)
    except OSError as exc:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return ServiceResult(
            False, "install", f"Could not write the task XML: {exc}", SERVICE_NAME
        )
    try:
        result = _invoke(
            runner,
            _schtasks("/Create", "/TN", SERVICE_NAME, "/XML", str(temp_path), "/F"),
        )
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove temporary task XML at %s", temp_path)
    if not result.ok:
        return ServiceResult(
            False, "install", _failure("schtasks", result), SERVICE_NAME
        )
    return ServiceResult(
        True, "install", "Installed (scheduled task; starts at logon).", SERVICE_NAME
    )


def install(*, port: int, runner: Runner | None = None) -> ServiceResult:
    run = _runner(runner)
    try:
        paths.ensure_dirs()
        command = _install_command(port)
    except OSError as exc:
        return ServiceResult(False, "install", f"Could not prepare the install: {exc}")

    key = platform_key()
    if key == "linux":
        return _install_linux(command, run)
    if key == "macos":
        return _install_macos(command, run)
    return _install_windows(command, run)


def _linux_action(action: str, runner: Runner) -> ServiceResult:
    path = unit_path()
    if path is None or not path.is_file():
        return _not_installed(action)
    result = _invoke(runner, _systemctl(action, SYSTEMD_UNIT))
    if not result.ok:
        return ServiceResult(False, action, _failure("systemctl", result))
    detail = {
        "start": "Service started.",
        "stop": "Service stopped.",
        "restart": "Service restarted.",
    }[action]
    return ServiceResult(True, action, detail)


def _linux_status(runner: Runner) -> ServiceResult:
    path = unit_path()
    if path is None or not path.is_file():
        return _not_installed("status")
    result = _invoke(runner, _systemctl("is-active", SYSTEMD_UNIT))
    state = result.stdout.strip()
    if state == "active":
        return ServiceResult(True, "status", "Service is active.")
    reason = state or _first_line(result.stderr) or "unknown"
    return ServiceResult(False, "status", f"Service is not active ({reason}).")


def _macos_targets() -> tuple[str, str]:
    domain = f"gui/{_uid()}"
    return domain, f"{domain}/{LAUNCHD_LABEL}"


def _macos_action(action: str, runner: Runner) -> ServiceResult:
    path = unit_path()
    if path is None or not path.is_file():
        return _not_installed(action)
    domain, target = _macos_targets()
    if action == "stop":
        command = _launchctl("bootout", target)
        result = _invoke(runner, command)
        if not result.ok:
            return ServiceResult(False, action, _failure("launchctl", result))
        return ServiceResult(True, action, "Service stopped.")

    # ``bootout`` unloads the job, so a bare kickstart fails after a stop;
    # bootstrap the plist again when the job is not loaded.
    loaded = _invoke(runner, _launchctl("print", target)).ok
    if not loaded:
        bootstrapped = _invoke(runner, _launchctl("bootstrap", domain, str(path)))
        if not bootstrapped.ok:
            return ServiceResult(False, action, _failure("launchctl", bootstrapped))
    result = _invoke(runner, _launchctl("kickstart", "-k", target))
    if not result.ok:
        return ServiceResult(False, action, _failure("launchctl", result))
    detail = {
        "start": "Service started.",
        "restart": "Service restarted.",
    }[action]
    return ServiceResult(True, action, detail)


def _macos_status(runner: Runner) -> ServiceResult:
    path = unit_path()
    if path is None or not path.is_file():
        return _not_installed("status")
    _, target = _macos_targets()
    result = _invoke(runner, _launchctl("print", target))
    if result.ok:
        return ServiceResult(True, "status", "Service is running.")
    return ServiceResult(False, "status", "Service is not running.")


def _windows_action(action: str, runner: Runner) -> ServiceResult:
    if action == "restart":
        _invoke(runner, _schtasks("/End", "/TN", SERVICE_NAME))
        result = _invoke(runner, _schtasks("/Run", "/TN", SERVICE_NAME))
    else:
        verb = "/Run" if action == "start" else "/End"
        result = _invoke(runner, _schtasks(verb, "/TN", SERVICE_NAME))
    if not result.ok:
        if _missing_task(result):
            return _not_installed(action)
        return ServiceResult(False, action, _failure("schtasks", result))
    detail = {
        "start": "Service started.",
        "stop": "Service stopped.",
        "restart": "Service restarted.",
    }[action]
    return ServiceResult(True, action, detail)


def _windows_status(runner: Runner) -> ServiceResult:
    result = _invoke(
        runner, _schtasks("/Query", "/TN", SERVICE_NAME, "/V", "/FO", "LIST")
    )
    if result.ok:
        text = f"{result.stdout or ''}"
        running = "running" in text.lower()
        detail = (
            "Service is running."
            if running
            else "Service is installed but not running."
        )
        return ServiceResult(True, "status", detail)
    if _missing_task(result):
        return _not_installed("status")
    return ServiceResult(False, "status", _failure("schtasks", result))


def start(*, runner: Runner | None = None) -> ServiceResult:
    key = platform_key()
    if key == "linux":
        return _linux_action("start", _runner(runner))
    if key == "macos":
        return _macos_action("start", _runner(runner))
    return _windows_action("start", _runner(runner))


def stop(*, runner: Runner | None = None) -> ServiceResult:
    key = platform_key()
    if key == "linux":
        return _linux_action("stop", _runner(runner))
    if key == "macos":
        return _macos_action("stop", _runner(runner))
    return _windows_action("stop", _runner(runner))


def restart(*, runner: Runner | None = None) -> ServiceResult:
    key = platform_key()
    if key == "linux":
        return _linux_action("restart", _runner(runner))
    if key == "macos":
        return _macos_action("restart", _runner(runner))
    return _windows_action("restart", _runner(runner))


def status(*, runner: Runner | None = None) -> ServiceResult:
    key = platform_key()
    if key == "linux":
        return _linux_status(_runner(runner))
    if key == "macos":
        return _macos_status(_runner(runner))
    return _windows_status(_runner(runner))


def _uninstall_linux(runner: Runner) -> ServiceResult:
    path = unit_path()
    if path is None:
        raise ServiceError("Linux uninstall requires a systemd unit path")
    _invoke(runner, _systemctl("stop", SYSTEMD_UNIT))
    existed = path.is_file()
    if existed:
        try:
            path.unlink()
        except OSError as exc:
            return ServiceResult(
                False, "uninstall", f"Could not remove the unit file: {exc}", str(path)
            )
    _invoke(runner, _systemctl("disable", "--now", SYSTEMD_UNIT))
    _invoke(runner, _systemctl("daemon-reload"))
    if not existed:
        return _not_installed("uninstall")
    return ServiceResult(True, "uninstall", "Service uninstalled.", str(path))


def _uninstall_macos(runner: Runner) -> ServiceResult:
    path = unit_path()
    if path is None:
        raise ServiceError("macOS uninstall requires a LaunchAgent path")
    existed = path.is_file()
    _invoke(runner, _launchctl("bootout", f"gui/{_uid()}", str(path)))
    if existed:
        try:
            path.unlink()
        except OSError as exc:
            return ServiceResult(
                False,
                "uninstall",
                f"Could not remove the LaunchAgent: {exc}",
                str(path),
            )
        return ServiceResult(True, "uninstall", "Service uninstalled.", str(path))
    return _not_installed("uninstall")


def _uninstall_windows(runner: Runner) -> ServiceResult:
    _invoke(runner, _schtasks("/End", "/TN", SERVICE_NAME))
    result = _invoke(runner, _schtasks("/Delete", "/TN", SERVICE_NAME, "/F"))
    if result.ok:
        return ServiceResult(True, "uninstall", "Service uninstalled.", SERVICE_NAME)
    if _missing_task(result):
        return _not_installed("uninstall")
    return ServiceResult(False, "uninstall", _failure("schtasks", result), SERVICE_NAME)


def uninstall(*, runner: Runner | None = None) -> ServiceResult:
    key = platform_key()
    if key == "linux":
        return _uninstall_linux(_runner(runner))
    if key == "macos":
        return _uninstall_macos(_runner(runner))
    return _uninstall_windows(_runner(runner))
