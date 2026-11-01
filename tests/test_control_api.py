from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from control import __version__, api, paths, settings

VALID = {
    "TELEGRAM_BOT_TOKEN": "123456789:AAFakeTokenValueForTests1234567890",
    "ADMIN_USER_ID": "123456789",
    "OPENCODE_BASE_URL": "http://localhost:4096",
    "OPENCODE_SERVER_PASSWORD": "secret",
}


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("OCV_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("OCV_ENV_FILE", str(tmp_path / "cfg" / ".env"))
    return tmp_path / "cfg"


def _snapshot(status: str = "stopped") -> dict:
    return {
        "bot": {
            "status": status,
            "pid": None,
            "started_at": None,
            "uptime": None,
            "exit_code": None,
            "detail": "",
        },
        "opencode": {
            "status": "stopped",
            "version": None,
            "url": "http://localhost:4096",
            "detail": "",
        },
        "mode": "polling",
        "env_file": "",
        "configured": False,
    }


class FakeManager:
    def __init__(self, status: str = "stopped") -> None:
        self.data = _snapshot(status)
        self.calls: list[str] = []
        self.error: str | None = None
        self.log = paths.bot_log()

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.error is not None:
            raise api.ManagerError(self.error)

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self.data))

    def start_bot(self) -> dict:
        self._record("start_bot")
        self.data["bot"]["status"] = "running"
        return self.snapshot()

    def stop_bot(self) -> dict:
        self._record("stop_bot")
        self.data["bot"]["status"] = "stopped"
        return self.snapshot()

    def restart_bot(self) -> dict:
        self._record("restart_bot")
        self.data["bot"]["status"] = "running"
        return self.snapshot()

    def start_opencode(self) -> dict:
        self._record("start_opencode")
        self.data["opencode"]["status"] = "running"
        return self.snapshot()

    def stop_opencode(self) -> dict:
        self._record("stop_opencode")
        self.data["opencode"]["status"] = "stopped"
        return self.snapshot()

    def shutdown(self) -> None:
        self.calls.append("shutdown")

    def log_lines(self, *, limit: int = 200) -> list[str]:
        try:
            return self.log.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []

    def log_since(self, offset: int) -> tuple[int, list[str]]:
        try:
            data = self.log.read_bytes()
        except OSError:
            return 0, []
        start = offset if 0 <= offset <= len(data) else 0
        chunk = data[start:]
        end = chunk.rfind(b"\n")
        if end < 0:
            return start, []
        lines = chunk[: end + 1].decode("utf-8", errors="replace").splitlines()
        return start + end + 1, lines

    def log_size(self) -> int:
        try:
            return self.log.stat().st_size
        except OSError:
            return 0


def make_client(manager: FakeManager | None = None, **kwargs) -> TestClient:
    engine = manager if manager is not None else FakeManager()
    app = api.create_app(manager=engine, token="tok", host_check=False, **kwargs)
    return TestClient(app)


def login(client: TestClient) -> None:
    response = client.get("/?t=tok", follow_redirects=False)
    assert response.status_code == 302


def test_health_needs_no_auth():
    client = make_client()

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "version": __version__}


def test_api_requires_auth():
    client = make_client()

    response = client.get("/api/status")

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_token_link_sets_cookie_and_redirects():
    client = make_client()

    response = client.get("/?t=tok", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"].lower()
    assert "ocv_token=tok" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    assert client.get("/api/status").status_code == 200


def test_wrong_token_is_rejected():
    client = make_client()

    response = client.get("/?t=nope")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("text/plain")
    assert "ocv open" in response.text
    assert "ocv_token" not in client.cookies


def test_status_reports_the_panel():
    client = make_client(port=9123)
    login(client)

    body = client.get("/api/status").json()

    assert body["bot"]["status"] == "stopped"
    assert body["panel"]["version"] == __version__
    assert body["panel"]["port"] == 9123
    assert body["panel"]["config_dir"] == str(paths.config_dir())
    assert body["panel"]["env_file"] == str(paths.env_file())
    assert body["panel"]["uptime"] >= 0


def test_mutating_requests_need_the_header():
    fake = FakeManager()
    client = make_client(fake)
    login(client)

    response = client.post("/api/bot/start")

    assert response.status_code == 403
    assert response.json() == {"detail": "Missing X-OCV header"}
    assert fake.calls == []


def test_bot_actions_return_the_snapshot():
    fake = FakeManager()
    client = make_client(fake)
    login(client)

    response = client.post("/api/bot/start", headers={"X-OCV": "1"})

    assert response.status_code == 200
    assert response.json()["bot"]["status"] == "running"
    assert fake.calls == ["start_bot"]


def test_opencode_actions_return_the_snapshot():
    fake = FakeManager()
    client = make_client(fake)
    login(client)

    response = client.post("/api/opencode/start", headers={"X-OCV": "1"})

    assert response.status_code == 200
    assert response.json()["opencode"]["status"] == "running"
    assert fake.calls == ["start_opencode"]


def test_manager_error_becomes_conflict():
    fake = FakeManager()
    fake.error = "Bot is already running."
    client = make_client(fake)
    login(client)

    response = client.post("/api/bot/start", headers={"X-OCV": "1"})

    assert response.status_code == 409
    assert response.json() == {"detail": "Bot is already running."}


def test_host_allowlist_rejects_other_names():
    app = api.create_app(manager=FakeManager(), token="tok", host_check=True)

    with TestClient(app, base_url="http://evil.test") as client:
        assert client.get("/api/health").status_code == 403
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        assert client.get("/api/health").status_code == 200
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/api/health").status_code == 200


def test_put_settings_saves_and_restarts():
    fake = FakeManager(status="running")
    client = make_client(fake)
    login(client)

    response = client.put(
        "/api/settings",
        json={"values": VALID, "restart": True},
        headers={"X-OCV": "1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["restarted"] is True
    assert body["values"]["TELEGRAM_BOT_TOKEN"] == ""
    assert body["secrets_set"]["TELEGRAM_BOT_TOKEN"] is True
    assert fake.calls == ["restart_bot"]
    stored = settings.read_env(paths.env_file())
    assert stored["TELEGRAM_BOT_TOKEN"] == VALID["TELEGRAM_BOT_TOKEN"]
    assert stored["ADMIN_USER_ID"] == VALID["ADMIN_USER_ID"]


def test_put_settings_skips_restart_when_stopped():
    fake = FakeManager()
    client = make_client(fake)
    login(client)

    body = client.put(
        "/api/settings",
        json={"values": VALID, "restart": True},
        headers={"X-OCV": "1"},
    ).json()

    assert body["restarted"] is False
    assert "restart_error" not in body
    assert fake.calls == []


def test_put_settings_reports_restart_failure():
    fake = FakeManager(status="running")
    fake.error = "Could not start the bot."
    client = make_client(fake)
    login(client)

    body = client.put(
        "/api/settings",
        json={"values": VALID, "restart": True},
        headers={"X-OCV": "1"},
    ).json()

    assert body["restarted"] is False
    assert body["restart_error"] == "Could not start the bot."


def test_put_settings_rejects_invalid_values():
    client = make_client()
    login(client)

    response = client.put(
        "/api/settings",
        json={"values": {**VALID, "TELEGRAM_BOT_TOKEN": "nope"}},
        headers={"X-OCV": "1"},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["detail"]
    assert any("Bot token" in line for line in body["errors"])
    assert settings.read_env(paths.env_file()) == {}


def test_setup_reports_missing_required_values():
    client = make_client()
    login(client)

    body = client.get("/api/setup").json()

    assert body == {
        "needed": True,
        "missing": ["TELEGRAM_BOT_TOKEN", "ADMIN_USER_ID"],
        "configured": False,
    }


def test_setup_reports_configured_once_saved():
    settings.save(VALID)
    client = make_client()
    login(client)

    body = client.get("/api/setup").json()

    assert body == {"needed": False, "missing": [], "configured": True}


def test_setup_saves_and_starts_the_bot():
    fake = FakeManager()
    client = make_client(fake)
    login(client)

    response = client.post(
        "/api/setup",
        json={"values": VALID, "start": True},
        headers={"X-OCV": "1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["started"] is True
    assert body["start_error"] is None
    assert body["configured"] is True
    assert fake.calls == ["start_bot"]
    assert settings.read_env(paths.env_file())["ADMIN_USER_ID"] == "123456789"


def test_setup_reports_a_start_failure():
    fake = FakeManager()
    fake.error = "opencode server is not running."
    client = make_client(fake)
    login(client)

    body = client.post(
        "/api/setup",
        json={"values": VALID, "start": True},
        headers={"X-OCV": "1"},
    ).json()

    assert body["started"] is False
    assert body["start_error"] == "opencode server is not running."


def test_setup_rejects_invalid_values():
    client = make_client()
    login(client)

    response = client.post(
        "/api/setup",
        json={"values": {"ADMIN_USER_ID": "-1"}},
        headers={"X-OCV": "1"},
    )

    assert response.status_code == 400
    assert response.json()["errors"]


def test_logs_return_the_bot_tail():
    path = paths.bot_log()
    path.parent.mkdir(parents=True)
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    client = make_client()
    login(client)

    response = client.get("/api/logs", params={"source": "bot", "lines": 2})

    assert response.status_code == 200
    assert response.json() == {"lines": ["two", "three"], "offset": path.stat().st_size}


def test_logs_read_the_panel_file():
    path = paths.panel_log()
    path.parent.mkdir(parents=True)
    path.write_text("panel line\n", encoding="utf-8")
    client = make_client()
    login(client)

    body = client.get("/api/logs", params={"source": "panel"}).json()

    assert body["lines"] == ["panel line"]
    assert body["offset"] == path.stat().st_size


def test_logs_reject_unknown_sources():
    client = make_client()
    login(client)

    response = client.get("/api/logs", params={"source": "nope"})

    assert response.status_code == 400
    assert "Unknown log source" in response.json()["detail"]


def test_log_stream_emits_existing_lines():
    path = paths.bot_log()
    path.parent.mkdir(parents=True)
    path.write_text("hello\nworld\n", encoding="utf-8")
    client = make_client()
    login(client)

    with client.stream(
        "GET",
        "/api/logs/stream",
        params={"source": "bot", "follow": "false", "offset": 0},
        timeout=10.0,
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        payloads = [
            json.loads(line[len("data: ") :])
            for line in response.iter_lines()
            if line.startswith("data: ")
        ]

    assert payloads == ["hello", "world"]


def test_static_files_are_public(monkeypatch, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "app.js").write_text("console.log(1);", encoding="utf-8")
    monkeypatch.setattr(api, "WEB_DIR", web)
    client = make_client()

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert response.text == "console.log(1);"


def test_admin_token_is_created_once():
    token = api.admin_token()

    assert token
    path = paths.admin_token_file()
    assert path.read_text(encoding="utf-8").strip() == token
    assert api.admin_token() == token
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600


def test_admin_token_without_create_returns_empty():
    assert api.admin_token(create=False) == ""
    assert not paths.admin_token_file().exists()
