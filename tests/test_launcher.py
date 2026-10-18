from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import httpx

from telegram_bot import launcher


def _fake_get(handler):
    real_client = httpx.Client

    def fake_get(url, timeout=None, auth=None):
        with real_client(transport=httpx.MockTransport(handler)) as client:
            return client.get(url, auth=auth)

    return fake_get


def _basic_header(username: str, password: str) -> str:
    import base64

    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def test_is_healthy_true(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"healthy": True})

    monkeypatch.setattr(launcher.httpx, "get", _fake_get(handler))
    assert launcher.is_healthy("http://x") is True


def test_is_healthy_false(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"healthy": False})

    monkeypatch.setattr(launcher.httpx, "get", _fake_get(handler))
    assert launcher.is_healthy("http://x") is False


def test_is_healthy_accepts_v2_info(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"version": "2.0.18", "pid": 42})

    monkeypatch.setattr(launcher.httpx, "get", _fake_get(handler))
    assert launcher.is_healthy("http://x") is True
    assert seen == ["/api/info"]


def test_is_healthy_falls_back_to_api_health(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/info":
            return httpx.Response(404, text="missing")
        return httpx.Response(200, json={"healthy": True})

    monkeypatch.setattr(launcher.httpx, "get", _fake_get(handler))
    assert launcher.is_healthy("http://x") is True
    assert calls == ["/api/info", "/api/health"]


def test_is_healthy_swallows_errors(monkeypatch):
    def boom(url, timeout=None, auth=None):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(launcher.httpx, "get", boom)
    assert launcher.is_healthy("http://x") is False


def test_is_healthy_sends_config_basic_auth(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"healthy": True})

    monkeypatch.setattr(launcher.httpx, "get", _fake_get(handler))
    monkeypatch.setattr(launcher.config, "OPENCODE_SERVER_USERNAME", "opencode")
    monkeypatch.setattr(launcher.config, "OPENCODE_SERVER_PASSWORD", "s3cret")

    assert launcher.is_healthy("http://x") is True
    assert seen["authorization"] == _basic_header("opencode", "s3cret")


def test_is_healthy_no_auth_without_password(monkeypatch):
    seen: dict = {"authorization": "unset"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"healthy": True})

    monkeypatch.setattr(launcher.httpx, "get", _fake_get(handler))
    monkeypatch.setattr(launcher.config, "OPENCODE_SERVER_PASSWORD", "")

    assert launcher.is_healthy("http://x") is True
    assert seen["authorization"] is None


def test_popen_kwargs_posix(monkeypatch):
    monkeypatch.setattr(launcher.os, "name", "posix")
    assert launcher._popen_kwargs() == {"start_new_session": True}


def test_popen_kwargs_windows(monkeypatch):
    monkeypatch.setattr(launcher.os, "name", "nt")
    assert launcher._popen_kwargs() == {
        "creationflags": launcher.subprocess.CREATE_NEW_PROCESS_GROUP
    }


def test_kill_pid_os_posix_uses_process_group(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher.os, "name", "posix")
    monkeypatch.setattr(launcher.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(launcher.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        launcher.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)), raising=False
    )

    launcher._kill_pid_os(42)

    assert calls and calls[0][0] == 42


def test_terminate_pid_tree_uses_psutil(monkeypatch):
    terminated: list[int] = []
    killed: list[int] = []

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def children(self, recursive=False):
            return [FakeProcess(2)]

        def terminate(self):
            terminated.append(self.pid)

        def kill(self):
            killed.append(self.pid)

    fake_psutil = ModuleType("psutil")
    fake_psutil.CONN_LISTEN = "LISTEN"
    fake_psutil.Process = FakeProcess
    fake_psutil.wait_procs = lambda procs, timeout=None: (procs, [])
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    launcher._terminate_pid_tree(5)

    assert 5 in terminated
    assert 2 in terminated


def test_terminate_process_skips_finished(monkeypatch):
    terminated: list[int] = []
    monkeypatch.setattr(
        launcher, "_terminate_pid_tree", lambda pid: terminated.append(pid)
    )
    process = SimpleNamespace(pid=7, poll=lambda: 0)

    launcher.terminate_process(process)

    assert terminated == []


def test_terminate_process_kills_pid_tree(monkeypatch):
    terminated: list[int] = []
    waited: list[int] = []
    monkeypatch.setattr(
        launcher, "_terminate_pid_tree", lambda pid: terminated.append(pid)
    )

    class FakeProcess:
        pid = 7

        def poll(self):
            return None

        def wait(self, timeout=None):
            waited.append(self.pid)

    launcher.terminate_process(FakeProcess())

    assert terminated == [7]
    assert waited == [7]


def _main_env(monkeypatch, calls, *, health=True, bot_raises=False):
    monkeypatch.setattr(launcher.config, "CLOUDFLARED_TUNNEL_TOKEN", "")
    monkeypatch.setattr(
        launcher.opencode_setup,
        "install_all",
        lambda: calls.append("install") or {"mcp": True, "skill": True},
    )
    monkeypatch.setattr(
        launcher, "wait_for_health", lambda base, **kw: calls.append("wait") or health
    )
    monkeypatch.setattr(
        launcher, "terminate_process", lambda process: calls.append("terminate")
    )

    def fake_bot_main():
        calls.append("bot")
        if bot_raises:
            raise RuntimeError("boom")

    monkeypatch.setattr(launcher.bot, "main", fake_bot_main)


def test_main_runs_in_order_without_managing_the_server(monkeypatch):
    calls: list[str] = []
    _main_env(monkeypatch, calls)

    assert launcher.main() == 0
    assert calls == ["install", "wait", "bot"]
    assert calls.count("terminate") == 0


def test_main_health_failure_leaves_bot_stopped(monkeypatch):
    calls: list[str] = []
    _main_env(monkeypatch, calls, health=False)

    assert launcher.main() == 1
    assert calls == ["install", "wait"]
    assert "bot" not in calls
    assert calls.count("terminate") == 0


def test_main_returns_one_when_bot_raises(monkeypatch):
    calls: list[str] = []
    _main_env(monkeypatch, calls, bot_raises=True)

    assert launcher.main() == 1
    assert calls == ["install", "wait", "bot"]
    assert calls.count("terminate") == 0


def _enable_webhook(monkeypatch, token="tok"):
    monkeypatch.setattr(
        launcher.config, "TELEGRAM_WEBHOOK_URL", "https://t.example"
    )
    monkeypatch.setattr(launcher.config, "TELEGRAM_WEBHOOK_PORT", 8080)
    monkeypatch.setattr(launcher.config, "CLOUDFLARED_TUNNEL_TOKEN", token)


def test_build_cloudflared_command_disabled(monkeypatch):
    monkeypatch.setattr(launcher.config, "TELEGRAM_WEBHOOK_URL", "")
    assert launcher.build_cloudflared_command() is None


def test_build_cloudflared_command_enabled(monkeypatch):
    _enable_webhook(monkeypatch)
    monkeypatch.setattr(
        launcher.shutil, "which", lambda command: r"C:\cf\cloudflared.exe"
    )

    command = launcher.build_cloudflared_command()

    assert command == [
        r"C:\cf\cloudflared.exe",
        "tunnel",
        "--no-autoupdate",
        "run",
        "--token",
        "tok",
    ]


def test_build_cloudflared_command_cmd_shim(monkeypatch):
    _enable_webhook(monkeypatch)
    monkeypatch.setattr(launcher.os, "name", "nt")
    monkeypatch.setattr(
        launcher.shutil, "which", lambda command: r"C:\cf\cloudflared.cmd"
    )
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    command = launcher.build_cloudflared_command()

    assert command[:3] == [
        r"C:\Windows\System32\cmd.exe",
        "/c",
        r"C:\cf\cloudflared.cmd",
    ]
    assert command[-2:] == ["--token", "tok"]


def test_wait_for_tunnel_treats_any_response_as_reachable(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        launcher.httpx,
        "get",
        lambda url, timeout=None: seen.append(url)
        or SimpleNamespace(status_code=404),
    )

    assert launcher.wait_for_tunnel(
        "https://t.example", "ocw", timeout=5.0, interval=0
    ) is True
    assert seen == ["https://t.example/ocw"]


def test_wait_for_tunnel_returns_false_on_timeout(monkeypatch):
    def boom(url, timeout=None):
        raise httpx.ConnectError("no")

    monkeypatch.setattr(launcher.httpx, "get", boom)

    assert launcher.wait_for_tunnel(
        "https://t.example", "ocw", timeout=0.05, interval=0
    ) is False


def test_main_starts_and_stops_cloudflared_in_webhook_mode(monkeypatch):
    calls: list[str] = []
    _main_env(monkeypatch, calls)
    _enable_webhook(monkeypatch)
    monkeypatch.setattr(
        launcher,
        "build_cloudflared_command",
        lambda: calls.append("cf-build") or ["cloudflared", "tunnel"],
    )
    monkeypatch.setattr(
        launcher,
        "start_cloudflared",
        lambda command: calls.append("cf-start")
        or SimpleNamespace(pid=2, poll=lambda: None),
    )
    monkeypatch.setattr(
        launcher, "wait_for_tunnel", lambda base, path, **kw: calls.append("tunnel")
        or True
    )

    assert launcher.main() == 0
    assert calls == [
        "install",
        "wait",
        "cf-build",
        "cf-start",
        "tunnel",
        "bot",
        "terminate",
    ]
    assert calls.count("terminate") == 1


def test_main_skips_cloudflared_when_disabled(monkeypatch):
    calls: list[str] = []
    _main_env(monkeypatch, calls)
    monkeypatch.setattr(launcher.config, "TELEGRAM_WEBHOOK_URL", "")
    started: list = []
    monkeypatch.setattr(
        launcher, "start_cloudflared", lambda command: started.append(command)
    )

    assert launcher.main() == 0
    assert started == []
    assert calls.count("terminate") == 0
