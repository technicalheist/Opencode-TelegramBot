from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from control import manager, paths, settings


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    target = tmp_path / "cfg"
    target.mkdir()
    monkeypatch.setenv(paths.CONFIG_DIR_OVERRIDE, str(target))
    monkeypatch.setenv(paths.ENV_FILE_OVERRIDE, str(target / ".env"))
    return target


def write_settings(**overrides):
    values = settings.defaults()
    values.update(
        {
            "TELEGRAM_BOT_TOKEN": "123456789:AAFakeTokenValueForTests1234567890",
            "ADMIN_USER_ID": "123456789",
            "OPENCODE_SERVER_PASSWORD": "s3cret",
        }
    )
    values.update(overrides)
    return settings.write_env(values)


class FakeProcess:
    def __init__(self, pid: int = 4242, exit_code: int = 0) -> None:
        self.pid = pid
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False
        self.waits: list[float | None] = []
        self.alive = True

    def poll(self):
        return None if self.alive else self.exit_code

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.alive = False
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.killed = True
        self.alive = False


def make_probe(*responses):
    calls: list[tuple[str, tuple[str, str] | None, float]] = []

    def probe(base_url, credentials, timeout):
        calls.append((base_url, credentials, timeout))
        return responses[min(len(calls), len(responses)) - 1]

    probe.calls = calls
    return probe


def make_popen(process, calls=None):
    calls = [] if calls is None else calls

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return process

    popen.calls = calls
    return popen


def make_run(calls, *, returncode: int = 0, stdout: str = "", stderr: str = ""):
    def run(command, **kwargs):
        calls.append((list(command), kwargs))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return run


def healthy_probe():
    return make_probe((200, {"version": "2.2.5", "pid": 7}))


def make_manager(**kwargs):
    kwargs.setdefault("probe", healthy_probe())
    kwargs.setdefault("which", lambda name: "opencode")
    kwargs.setdefault("run", make_run([]))
    return manager.Manager(**kwargs)


def write_log(text: str) -> Path:
    path = paths.bot_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def test_snapshot_without_settings_is_unconfigured(config_dir):
    probe = make_probe((None, None))
    bot = manager.Manager(probe=probe, which=lambda name: None)

    snap = bot.snapshot()

    assert snap["configured"] is False
    assert snap["mode"] == "polling"
    assert snap["env_file"] == str(config_dir / ".env")
    assert snap["bot"] == {
        "status": "stopped",
        "pid": None,
        "started_at": None,
        "uptime": None,
        "exit_code": None,
        "detail": "",
    }
    assert snap["opencode"]["status"] == "missing"
    assert probe.calls and probe.calls[0][0] == "http://localhost:4096"


def test_snapshot_reports_the_server_and_webhook_mode():
    write_settings(
        TELEGRAM_WEBHOOK_URL="https://bot.example",
        TELEGRAM_WEBHOOK_PATH="hook",
        TELEGRAM_WEBHOOK_SECRET="shh",
    )
    probe = make_probe((200, {"version": "2.2.5", "pid": 7}))
    bot = manager.Manager(probe=probe, which=lambda name: "opencode")

    snap = bot.snapshot()

    assert snap["configured"] is True
    assert snap["mode"] == "webhook"
    assert snap["opencode"] == {
        "status": "running",
        "version": "2.2.5",
        "url": "http://localhost:4096",
        "detail": "opencode 2.2.5",
    }
    assert probe.calls[0][1] == ("opencode", "s3cret")


def test_snapshot_maps_unauthorized_and_missing_servers():
    unauthorized = make_manager(probe=make_probe((401, None)))
    assert unauthorized.snapshot()["opencode"]["status"] == "unauthorized"

    forbidden = make_manager(probe=make_probe((403, None)))
    assert forbidden.snapshot()["opencode"]["status"] == "unauthorized"

    missing = make_manager(probe=make_probe((None, None)), which=lambda name: None)
    state = missing.snapshot()["opencode"]
    assert state["status"] == "missing"
    assert "OPENCODE_COMMAND" in state["detail"]


def test_snapshot_survives_internal_failures(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("settings exploded")

    monkeypatch.setattr(manager.settings, "missing_required", boom)
    bot = make_manager()

    snap = bot.snapshot()

    assert snap["bot"]["status"] == "error"
    assert "settings exploded" in snap["bot"]["detail"]
    assert snap["configured"] is False


def test_start_bot_spawns_with_env_overrides_and_log(config_dir):
    write_settings()
    process = FakeProcess()
    popen = make_popen(process)
    bot = manager.Manager(
        popen=popen,
        which=lambda name: "opencode",
        probe=healthy_probe(),
        run=make_run([]),
        clock=lambda: 1000.0,
    )

    snap = bot.start_bot()

    assert len(popen.calls) == 1
    command, kwargs = popen.calls[0]
    assert command == paths.bot_command()
    assert kwargs["cwd"] == str(paths.command_cwd())
    assert kwargs["env"]["OCV_ENV_FILE"] == str(config_dir / ".env")
    assert kwargs["env"]["OCV_CONFIG_DIR"] == str(config_dir)
    assert kwargs["stderr"] is subprocess.STDOUT
    assert Path(kwargs["stdout"].name) == paths.bot_log()
    assert paths.bot_log().exists()
    if os.name == "nt":
        assert kwargs["creationflags"] == manager.CREATE_NO_WINDOW
        assert "start_new_session" not in kwargs
    else:
        assert kwargs["start_new_session"] is True
    assert snap["bot"]["status"] == "running"
    assert snap["bot"]["pid"] == 4242
    assert snap["bot"]["started_at"] == 1000.0
    assert snap["bot"]["uptime"] == 0.0


def test_start_bot_rotates_a_large_log(monkeypatch):
    monkeypatch.setattr(manager, "LOG_ROTATE_BYTES", 3)
    log_path = write_log("old\n")

    bot = make_manager(popen=make_popen(FakeProcess()))
    bot.start_bot()

    rotated = log_path.parent / f"{log_path.name}.1"
    assert rotated.read_text(encoding="utf-8") == "old\n"
    assert log_path.exists()


def test_start_bot_twice_is_refused():
    popen = make_popen(FakeProcess())
    bot = make_manager(popen=popen)

    bot.start_bot()
    with pytest.raises(manager.ManagerError, match="already running"):
        bot.start_bot()

    assert len(popen.calls) == 1


def test_start_bot_skips_the_service_cli_when_server_is_running():
    runs: list = []
    popen = make_popen(FakeProcess())
    bot = make_manager(popen=popen, run=make_run(runs))

    bot.start_bot()

    assert runs == []
    assert len(popen.calls) == 1


def test_start_bot_starts_opencode_and_refuses_when_it_stays_down(monkeypatch):
    monkeypatch.setattr(manager, "START_TIMEOUT", 0.0)
    runs: list = []
    popen = make_popen(FakeProcess())
    bot = manager.Manager(
        popen=popen,
        which=lambda name: "opencode",
        probe=make_probe((None, None)),
        run=make_run(runs, returncode=1, stderr="boom"),
    )

    with pytest.raises(manager.ManagerError, match="opencode server is not running"):
        bot.start_bot()

    assert [command for command, _ in runs] == [["opencode", "service", "start"]]
    assert popen.calls == []
    snap = bot.snapshot()
    assert snap["bot"]["status"] == "error"
    assert snap["opencode"]["status"] == "stopped"
    assert "boom" in snap["opencode"]["detail"]


def test_start_bot_without_the_cli_fails_fast(monkeypatch):
    monkeypatch.setattr(manager, "START_TIMEOUT", 30.0)
    runs: list = []
    bot = manager.Manager(
        popen=make_popen(FakeProcess()),
        which=lambda name: None,
        probe=make_probe((None, None)),
        run=make_run(runs),
    )

    with pytest.raises(manager.ManagerError, match="opencode server is not running"):
        bot.start_bot()

    assert runs == []


def test_stop_bot_terminates_the_tree_and_reports_exit(monkeypatch):
    terminated: list[int] = []
    monkeypatch.setattr(
        manager.Manager, "_terminate_pid_tree", lambda self, pid: terminated.append(pid)
    )
    process = FakeProcess()
    bot = make_manager(popen=make_popen(process))
    bot.start_bot()

    snap = bot.stop_bot()

    assert terminated == [process.pid]
    assert snap["bot"]["status"] == "stopped"
    assert snap["bot"]["pid"] is None
    assert snap["bot"]["exit_code"] == 0
    assert "4242" not in snap["bot"]["detail"]


def test_stop_bot_is_a_noop_when_stopped():
    popen = make_popen(FakeProcess())
    bot = make_manager(popen=popen)

    snap = bot.stop_bot()

    assert snap["bot"]["status"] == "stopped"
    assert popen.calls == []


def test_snapshot_reports_a_child_that_exited():
    process = FakeProcess(pid=99, exit_code=3)
    bot = make_manager(popen=make_popen(process))
    bot.start_bot()

    process.alive = False
    snap = bot.snapshot()

    assert snap["bot"]["status"] == "stopped"
    assert snap["bot"]["exit_code"] == 3
    assert snap["bot"]["detail"] == "exited with code 3"
    assert snap["bot"]["pid"] is None


def test_restart_bot_replaces_the_child(monkeypatch):
    monkeypatch.setattr(manager.Manager, "_terminate_pid_tree", lambda self, pid: None)
    processes = [FakeProcess(pid=1), FakeProcess(pid=2)]
    commands: list = []

    def popen(command, **kwargs):
        commands.append(command)
        return processes[len(commands) - 1]

    bot = manager.Manager(
        popen=popen,
        which=lambda name: "opencode",
        probe=healthy_probe(),
        run=make_run([]),
    )
    bot.start_bot()

    snap = bot.restart_bot()

    assert len(commands) == 2
    assert snap["bot"]["pid"] == 2


def test_shutdown_stops_the_child(monkeypatch):
    terminated: list[int] = []
    monkeypatch.setattr(
        manager.Manager, "_terminate_pid_tree", lambda self, pid: terminated.append(pid)
    )
    process = FakeProcess()
    bot = make_manager(popen=make_popen(process))
    bot.start_bot()

    bot.shutdown()

    assert terminated == [process.pid]
    assert bot.snapshot()["bot"]["status"] == "stopped"


def test_start_opencode_runs_service_start():
    runs: list = []
    bot = manager.Manager(
        probe=make_probe((None, None), (200, {"version": "2.2.5"})),
        which=lambda name: "opencode",
        run=make_run(runs),
    )

    snap = bot.start_opencode()

    assert [command for command, _ in runs] == [["opencode", "service", "start"]]
    assert runs[0][1] == {
        "capture_output": True,
        "text": True,
        "timeout": manager.SERVICE_TIMEOUT,
    }
    assert snap["opencode"]["status"] == "running"


def test_start_opencode_is_a_noop_when_already_healthy():
    runs: list = []
    bot = manager.Manager(
        probe=healthy_probe(), which=lambda name: "opencode", run=make_run(runs)
    )

    snap = bot.start_opencode()

    assert runs == []
    assert snap["opencode"]["status"] == "running"


def test_start_opencode_uses_comspec_for_cmd_shims(monkeypatch):
    if os.name != "nt":
        pytest.skip(".cmd shims only exist on Windows")
    monkeypatch.setattr(manager.os, "name", "nt")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    runs: list = []
    bot = manager.Manager(
        probe=make_probe((None, None), (200, {"version": "2.2.5"})),
        which=lambda name: r"C:\bin\opencode.cmd",
        run=make_run(runs),
    )

    bot.start_opencode()

    assert runs[0][0] == [
        r"C:\Windows\System32\cmd.exe",
        "/c",
        r"C:\bin\opencode.cmd",
        "service",
        "start",
    ]


def test_start_opencode_without_the_cli_raises():
    runs: list = []
    bot = manager.Manager(
        probe=make_probe((None, None)), which=lambda name: None, run=make_run(runs)
    )

    with pytest.raises(manager.ManagerError, match="Could not find the opencode CLI"):
        bot.start_opencode()

    assert runs == []


def test_start_opencode_falls_back_to_an_existing_cli_path(tmp_path):
    cli = tmp_path / "opencode"
    cli.write_text("", encoding="utf-8")
    write_settings(OPENCODE_COMMAND=str(cli))
    runs: list = []
    bot = manager.Manager(
        probe=make_probe((None, None), (200, {"version": "2.2.5"})),
        which=lambda name: None,
        run=make_run(runs),
    )

    bot.start_opencode()

    assert runs[0][0] == [str(cli), "service", "start"]


def test_stop_opencode_runs_service_stop(monkeypatch):
    monkeypatch.setattr(manager, "POLL_INTERVAL", 0.001)
    runs: list = []
    bot = manager.Manager(
        probe=make_probe((200, {"version": "2.2.5"}), (None, None)),
        which=lambda name: "opencode",
        run=make_run(runs),
    )

    snap = bot.stop_opencode()

    assert [command for command, _ in runs] == [["opencode", "service", "stop"]]
    assert snap["opencode"]["status"] == "stopped"


def test_stop_opencode_raises_when_the_server_stays_up(monkeypatch):
    monkeypatch.setattr(manager, "STOP_TIMEOUT", 0.0)
    runs: list = []
    bot = manager.Manager(
        probe=healthy_probe(), which=lambda name: "opencode", run=make_run(runs)
    )

    with pytest.raises(manager.ManagerError, match="Could not stop the opencode server"):
        bot.stop_opencode()

    assert [command for command, _ in runs] == [["opencode", "service", "stop"]]


def test_stop_opencode_without_the_cli_raises():
    bot = manager.Manager(probe=make_probe((None, None)), which=lambda name: None)

    with pytest.raises(manager.ManagerError, match="Could not find the opencode CLI"):
        bot.stop_opencode()


def test_terminate_pid_tree_uses_psutil(monkeypatch):
    terminated: list[int] = []
    killed: list[int] = []

    class FakePsutilProcess:
        def __init__(self, pid):
            self.pid = pid

        def children(self, recursive=False):
            return [FakePsutilProcess(2)]

        def terminate(self):
            terminated.append(self.pid)

        def kill(self):
            killed.append(self.pid)

    fake = ModuleType("psutil")
    fake.Process = FakePsutilProcess
    fake.wait_procs = lambda procs, timeout=None: (procs, [])
    monkeypatch.setitem(sys.modules, "psutil", fake)
    runs: list = []
    bot = manager.Manager(probe=healthy_probe(), which=lambda name: None, run=make_run(runs))

    bot._terminate_pid_tree(5)

    assert terminated == [2, 5]
    assert runs == []


def test_terminate_pid_tree_falls_back_to_taskkill(monkeypatch):
    class BrokenProcess:
        def __init__(self, pid):
            raise RuntimeError("gone")

    fake = ModuleType("psutil")
    fake.Process = BrokenProcess
    fake.wait_procs = lambda procs, timeout=None: (procs, [])
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(manager.os, "name", "nt")
    runs: list = []
    bot = manager.Manager(probe=healthy_probe(), which=lambda name: None, run=make_run(runs))

    bot._terminate_pid_tree(5)

    assert runs[0][0] == ["taskkill", "/F", "/T", "/PID", "5"]


def test_default_probe_uses_api_info_and_basic_auth(monkeypatch):
    seen: dict = {}

    def fake_get(url, timeout=None, auth=None, trust_env=None):
        seen.update(url=url, timeout=timeout, auth=auth, trust_env=trust_env)
        return httpx.Response(200, json={"version": "2.2.5"})

    monkeypatch.setattr(manager.httpx, "get", fake_get)

    status, body = manager._default_probe(
        "http://localhost:4096/", ("opencode", "pw"), 1.5
    )

    assert status == 200
    assert body == {"version": "2.2.5"}
    assert seen["url"] == "http://localhost:4096/api/info"
    assert seen["trust_env"] is False
    assert isinstance(seen["auth"], httpx.BasicAuth)


def test_default_probe_reports_connection_failures(monkeypatch):
    def boom(url, timeout=None, auth=None, trust_env=None):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(manager.httpx, "get", boom)

    assert manager._default_probe("http://x", None, 0.1) == (None, None)


def test_log_lines_returns_the_tail():
    path = write_log("".join(f"line {i}\n" for i in range(1, 11)))
    bot = make_manager()

    assert bot.log_lines(limit=3) == ["line 8", "line 9", "line 10"]
    assert bot.log_lines(limit=0) == []
    assert bot.log_size() == path.stat().st_size


def test_log_helpers_tolerate_a_missing_file():
    bot = make_manager()

    assert bot.log_lines() == []
    assert bot.log_since(0) == (0, [])
    assert bot.log_size() == 0


def test_log_since_reads_only_complete_lines():
    path = write_log("alpha\nbeta\ngamma")
    bot = make_manager()

    offset, lines = bot.log_since(0)

    assert lines == ["alpha", "beta"]
    assert offset == len("alpha\nbeta\n")
    assert bot.log_since(offset) == (offset, [])

    with path.open("ab") as stream:
        stream.write(b"\n")
    offset2, lines2 = bot.log_since(offset)
    assert lines2 == ["gamma"]
    assert offset2 == path.stat().st_size


def test_log_since_restarts_when_the_file_shrank():
    path = write_log("fresh\n")
    bot = make_manager()

    offset, lines = bot.log_since(9999)

    assert lines == ["fresh"]
    assert offset == path.stat().st_size


def test_log_since_at_the_end_reads_nothing():
    path = write_log("one\n")
    bot = make_manager()

    assert bot.log_since(path.stat().st_size) == (path.stat().st_size, [])

def test_start_bot_reports_bad_credentials_without_waiting(monkeypatch, tmp_path):
    monkeypatch.setenv("OCV_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("OCV_ENV_FILE", raising=False)
    calls = []

    def probe(base_url, credentials, timeout):
        calls.append(base_url)
        return 401, {"message": "Authentication required"}

    engine = manager.Manager(probe=probe)

    try:
        engine.start_bot()
    except manager.ManagerError as exc:
        assert "rejected the configured password" in str(exc)
    else:
        raise AssertionError("expected ManagerError")
    assert len(calls) == 1
