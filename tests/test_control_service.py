from __future__ import annotations

import plistlib
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from control import paths, service


class FakeRunner:
    def __init__(self, *, result=None, handler=None):
        self.calls: list[list[str]] = []
        self._result = (
            result
            if result is not None
            else SimpleNamespace(returncode=0, stdout="", stderr="")
        )
        self._handler = handler

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        outcome = self._handler(command) if self._handler is not None else self._result
        if isinstance(outcome, dict):
            return SimpleNamespace(
                returncode=outcome.get("returncode", 0),
                stdout=outcome.get("stdout", ""),
                stderr=outcome.get("stderr", ""),
            )
        return outcome


def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.CONFIG_DIR_OVERRIDE, str(tmp_path / "cfg"))
    monkeypatch.setenv(paths.ENV_FILE_OVERRIDE, str(tmp_path / "cfg" / ".env"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


def _linux(monkeypatch, tmp_path):
    _hermetic(monkeypatch, tmp_path)
    monkeypatch.setattr(service.sys, "platform", "linux")


def _macos(monkeypatch, tmp_path):
    _hermetic(monkeypatch, tmp_path)
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "_uid", lambda: 501)


def _windows(monkeypatch, tmp_path):
    _hermetic(monkeypatch, tmp_path)
    monkeypatch.setattr(service.sys, "platform", "win32")


def _write_unit() -> Path:
    path = service.unit_path()
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("unit", encoding="utf-8")
    return path


def _write_plist() -> Path:
    path = service.unit_path()
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<plist/>", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("win32", "windows"),
        ("darwin", "macos"),
        ("linux", "linux"),
        ("linux2", "linux"),
    ],
)
def test_platform_key(monkeypatch, platform, expected):
    monkeypatch.setattr(service.sys, "platform", platform)

    assert service.platform_key() == expected


def test_platform_key_rejects_unknown(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "aix")

    with pytest.raises(service.ServiceError):
        service.platform_key()


def test_unit_path_windows(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "win32")

    assert service.unit_path() is None


def test_unit_path_linux(monkeypatch, tmp_path):
    _hermetic(monkeypatch, tmp_path)
    monkeypatch.setattr(service.sys, "platform", "linux")

    assert service.unit_path() == (
        tmp_path / ".config" / "systemd" / "user" / "opencode-voice.service"
    )


def test_unit_path_macos(monkeypatch, tmp_path):
    _hermetic(monkeypatch, tmp_path)
    monkeypatch.setattr(service.sys, "platform", "darwin")

    assert service.unit_path() == (
        tmp_path / "Library" / "LaunchAgents" / "io.opencodevoice.plist"
    )


def test_systemd_unit_quotes_arguments_and_sets_restart():
    command = ["/opt/my app/opencode-voice", "-m", "control", "serve", "--port", "8787"]
    unit = service.systemd_unit(command, working_dir="/home/u/My Config")

    assert (
        "ExecStart='/opt/my app/opencode-voice' -m control serve --port 8787"
        in unit
    )
    assert "WorkingDirectory='/home/u/My Config'" in unit
    for fragment in (
        "[Unit]",
        "[Service]",
        "[Install]",
        "Restart=on-failure",
        "RestartSec=5",
        "WantedBy=default.target",
    ):
        assert fragment in unit


def test_launchd_plist_has_program_arguments_and_logs():
    command = ["/usr/local/bin/ocv", "serve", "--no-open", "--port", "8787"]
    text = service.launchd_plist(
        command,
        working_dir="/Users/u/cfg",
        log_path="/Users/u/cfg/logs/panel.log",
    )

    payload = plistlib.loads(text.encode("utf-8"))
    assert payload["Label"] == service.LAUNCHD_LABEL
    assert payload["ProgramArguments"] == command
    assert payload["WorkingDirectory"] == "/Users/u/cfg"
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["StandardOutPath"] == "/Users/u/cfg/logs/panel.log"
    assert payload["StandardErrorPath"] == "/Users/u/cfg/logs/panel.log"
    assert "<true/>" in text


def test_windows_task_xml_renders_settings_and_arguments():
    xml = service.windows_task_xml(
        [
            r"C:\Program Files\ocv\ocv.exe",
            "serve",
            "--no-open",
            "--port",
            "8787",
            "--title",
            "My Panel",
        ],
        working_dir=r"C:\Users\u\My Config",
        user=r"DOM\user",
    )

    assert '<Command>"C:\\Program Files\\ocv\\ocv.exe"</Command>' in xml
    assert '<Arguments>serve --no-open --port 8787 --title "My Panel"</Arguments>' in xml
    assert (
        "<WorkingDirectory>C:\\Users\\u\\My Config</WorkingDirectory>" in xml
    )
    assert "<UserId>DOM\\user</UserId>" in xml
    assert "<Enabled>true</Enabled>" in xml
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
    assert "<RestartOnFailure>" in xml
    assert "<Interval>PT1M</Interval>" in xml
    assert "<Count>3</Count>" in xml
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml


def test_windows_task_xml_escapes_special_characters():
    xml = service.windows_task_xml(
        [r"C:\a&b\ocv.exe", "serve"],
        working_dir=r"C:\x<y>",
        user="D&U<ser>",
    )

    assert "C:\\a&amp;b\\ocv.exe" in xml
    assert "C:\\x&lt;y&gt;" in xml
    assert "D&amp;U&lt;ser&gt;" in xml


def test_systemctl_helper_builds_exact_argv():
    assert service._systemctl("start", service.SYSTEMD_UNIT) == [
        "systemctl",
        "--user",
        "start",
        "opencode-voice.service",
    ]


def test_install_linux_writes_unit_and_enables(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    runner = FakeRunner()

    result = service.install(port=9123, runner=runner)

    unit = tmp_path / ".config" / "systemd" / "user" / "opencode-voice.service"
    assert result.ok is True
    assert result.action == "install"
    assert result.path == str(unit)
    text = unit.read_text(encoding="utf-8")
    assert "--port 9123" in text
    assert f"WorkingDirectory={shlex.quote(str(paths.command_cwd()))}" in text
    assert runner.calls[:2] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "opencode-voice.service"],
    ]
    assert runner.calls[2][0] == "loginctl"
    assert "enable-linger" in runner.calls[2]
    assert (tmp_path / "cfg" / "logs").is_dir()


def test_install_linux_reports_systemctl_failure(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)

    def handler(command):
        if "enable" in command:
            return {
                "returncode": 1,
                "stderr": "Job for opencode-voice.service failed.\nSee logs.",
            }
        return {"returncode": 0}

    runner = FakeRunner(handler=handler)

    result = service.install(port=8787, runner=runner)

    assert result.ok is False
    assert result.detail == "systemctl failed: Job for opencode-voice.service failed."


def test_install_linux_survives_missing_systemctl(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)

    def runner(command, **kwargs):
        raise FileNotFoundError("systemctl")

    result = service.install(port=8787, runner=runner)

    assert result.ok is False
    assert result.detail.startswith("systemctl failed:")


def test_install_macos_writes_plist_and_bootstraps(monkeypatch, tmp_path):
    _macos(monkeypatch, tmp_path)
    runner = FakeRunner()

    result = service.install(port=8787, runner=runner)

    plist = tmp_path / "Library" / "LaunchAgents" / "io.opencodevoice.plist"
    assert result.ok is True
    assert result.path == str(plist)
    payload = plistlib.loads(plist.read_bytes())
    assert payload["ProgramArguments"][-2:] == ["--port", "8787"]
    assert payload["StandardOutPath"] == str(tmp_path / "cfg" / "logs" / "panel.log")
    assert runner.calls == [
        ["launchctl", "bootout", "gui/501", str(plist)],
        ["launchctl", "bootstrap", "gui/501", str(plist)],
        ["launchctl", "kickstart", "-k", "gui/501/io.opencodevoice"],
    ]


def test_install_windows_registers_task_and_removes_temp_xml(monkeypatch, tmp_path):
    _windows(monkeypatch, tmp_path)
    captured: dict = {}
    temp_paths: list[Path] = []

    def handler(command):
        if command[:2] == ["schtasks", "/Create"]:
            xml_path = Path(command[command.index("/XML") + 1])
            temp_paths.append(xml_path)
            captured["exists"] = xml_path.exists()
            captured["xml"] = xml_path.read_text(encoding="utf-16")
        return {"returncode": 0}

    runner = FakeRunner(handler=handler)

    result = service.install(port=8787, runner=runner)

    assert result.ok is True
    assert result.path == service.SERVICE_NAME
    assert runner.calls[0][:5] == [
        "schtasks",
        "/Create",
        "/TN",
        "opencode-voice",
        "/XML",
    ]
    assert runner.calls[0][-1] == "/F"
    assert captured["exists"] is True
    assert "<Command>" in captured["xml"]
    assert "<Arguments>-m control serve --no-open --port 8787</Arguments>" in captured[
        "xml"
    ]
    assert temp_paths[0].exists() is False


def test_linux_start_stop_restart_use_exact_argv(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    _write_unit()
    runner = FakeRunner()

    assert service.start(runner=runner).ok is True
    assert runner.calls[-1] == [
        "systemctl",
        "--user",
        "start",
        "opencode-voice.service",
    ]
    assert service.stop(runner=runner).ok is True
    assert runner.calls[-1] == [
        "systemctl",
        "--user",
        "stop",
        "opencode-voice.service",
    ]
    assert service.restart(runner=runner).ok is True
    assert runner.calls[-1] == [
        "systemctl",
        "--user",
        "restart",
        "opencode-voice.service",
    ]


def test_linux_status_ok_only_when_active(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    _write_unit()

    active = FakeRunner(
        result=SimpleNamespace(returncode=0, stdout="active\n", stderr="")
    )
    result = service.status(runner=active)
    assert result.ok is True
    assert active.calls == [
        ["systemctl", "--user", "is-active", "opencode-voice.service"]
    ]

    inactive = FakeRunner(
        result=SimpleNamespace(returncode=3, stdout="inactive\n", stderr="")
    )
    result = service.status(runner=inactive)
    assert result.ok is False
    assert "inactive" in result.detail


def test_linux_status_accepts_bytes_output(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    _write_unit()
    runner = FakeRunner(
        result=SimpleNamespace(returncode=0, stdout=b"active\n", stderr=b"")
    )

    assert service.status(runner=runner).ok is True


def test_linux_actions_report_not_installed(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    runner = FakeRunner()

    for action in (service.start, service.stop, service.restart, service.status):
        result = action(runner=runner)
        assert result.ok is False
        assert result.detail == "Service is not installed."

    assert runner.calls == []


def test_linux_missing_systemctl_is_reported_not_raised(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    _write_unit()

    def runner(command, **kwargs):
        raise FileNotFoundError("systemctl")

    result = service.start(runner=runner)

    assert result.ok is False
    assert result.detail.startswith("systemctl failed:")


def test_macos_start_stop_use_gui_domain(monkeypatch, tmp_path):
    _macos(monkeypatch, tmp_path)
    _write_plist()
    runner = FakeRunner()

    assert service.start(runner=runner).ok is True
    assert runner.calls[-1] == [
        "launchctl",
        "kickstart",
        "-k",
        "gui/501/io.opencodevoice",
    ]
    assert service.restart(runner=runner).ok is True
    assert runner.calls[-1] == [
        "launchctl",
        "kickstart",
        "-k",
        "gui/501/io.opencodevoice",
    ]
    assert service.stop(runner=runner).ok is True
    assert runner.calls[-1] == [
        "launchctl",
        "bootout",
        "gui/501/io.opencodevoice",
    ]


def test_macos_status_uses_returncode(monkeypatch, tmp_path):
    _macos(monkeypatch, tmp_path)
    _write_plist()

    running = FakeRunner(result=SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert service.status(runner=running).ok is True
    assert running.calls == [
        ["launchctl", "print", "gui/501/io.opencodevoice"]
    ]

    stopped = FakeRunner(result=SimpleNamespace(returncode=1, stdout="", stderr=""))
    assert service.status(runner=stopped).ok is False


def test_macos_missing_plist_is_not_installed(monkeypatch, tmp_path):
    _macos(monkeypatch, tmp_path)
    runner = FakeRunner()

    result = service.start(runner=runner)

    assert result.ok is False
    assert result.detail == "Service is not installed."
    assert runner.calls == []


def test_windows_start_stop_restart_use_exact_argv(monkeypatch, tmp_path):
    _windows(monkeypatch, tmp_path)
    runner = FakeRunner()

    assert service.start(runner=runner).ok is True
    assert runner.calls[-1] == ["schtasks", "/Run", "/TN", "opencode-voice"]
    assert service.stop(runner=runner).ok is True
    assert runner.calls[-1] == ["schtasks", "/End", "/TN", "opencode-voice"]
    assert service.restart(runner=runner).ok is True
    assert runner.calls[-2:] == [
        ["schtasks", "/End", "/TN", "opencode-voice"],
        ["schtasks", "/Run", "/TN", "opencode-voice"],
    ]


def test_windows_status_queries_task(monkeypatch, tmp_path):
    _windows(monkeypatch, tmp_path)
    runner = FakeRunner(
        result=SimpleNamespace(returncode=0, stdout="TaskName: opencode-voice", stderr="")
    )

    result = service.status(runner=runner)

    assert result.ok is True
    assert runner.calls == [
        ["schtasks", "/Query", "/TN", "opencode-voice", "/V", "/FO", "LIST"]
    ]


def test_windows_missing_task_is_not_installed(monkeypatch, tmp_path):
    _windows(monkeypatch, tmp_path)
    runner = FakeRunner(
        result=SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="ERROR: The system cannot find the task specified.",
        )
    )

    result = service.status(runner=runner)

    assert result.ok is False
    assert result.detail == "Service is not installed."


def test_uninstall_linux_removes_unit_but_keeps_data(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    paths.ensure_dirs()
    unit = _write_unit()
    marker = tmp_path / "cfg" / "data" / "bot.db"
    marker.write_text("keep", encoding="utf-8")
    runner = FakeRunner()

    result = service.uninstall(runner=runner)

    assert result.ok is True
    assert unit.exists() is False
    assert marker.read_text(encoding="utf-8") == "keep"
    assert ["systemctl", "--user", "disable", "--now", "opencode-voice.service"] in (
        runner.calls
    )
    assert ["systemctl", "--user", "daemon-reload"] in runner.calls


def test_uninstall_linux_when_not_installed(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    runner = FakeRunner()

    result = service.uninstall(runner=runner)

    assert result.ok is False
    assert result.detail == "Service is not installed."


def test_uninstall_macos_removes_plist(monkeypatch, tmp_path):
    _macos(monkeypatch, tmp_path)
    plist = _write_plist()
    runner = FakeRunner()

    result = service.uninstall(runner=runner)

    assert result.ok is True
    assert plist.exists() is False
    assert ["launchctl", "bootout", "gui/501", str(plist)] in runner.calls


def test_uninstall_windows_deletes_task(monkeypatch, tmp_path):
    _windows(monkeypatch, tmp_path)
    runner = FakeRunner()

    result = service.uninstall(runner=runner)

    assert result.ok is True
    assert runner.calls[-1] == [
        "schtasks",
        "/Delete",
        "/TN",
        "opencode-voice",
        "/F",
    ]
