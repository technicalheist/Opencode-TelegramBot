from __future__ import annotations

import json

import pytest

from control import cli, paths, settings


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("OCV_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("OCV_ENV_FILE", str(tmp_path / "cfg" / ".env"))
    monkeypatch.delenv("OCV_PORT", raising=False)
    settings.write_env(
        {
            **settings.defaults(),
            "TELEGRAM_BOT_TOKEN": "123456789:AAFakeTokenValueForTests1234567890",
            "ADMIN_USER_ID": "123456789",
            "OPENCODE_SERVER_PASSWORD": "secret",
        }
    )
    return tmp_path / "cfg"


class FakeManager:
    snapshot_value = {
        "bot": {"status": "running", "pid": 42, "uptime": 65.0, "detail": ""},
        "opencode": {"status": "running", "version": "2.2.5", "url": "http://x", "detail": ""},
        "mode": "polling",
        "env_file": "x",
        "configured": True,
    }

    def snapshot(self):
        return dict(self.snapshot_value)

    def log_lines(self, *, limit=200):
        return ["line one", "line two"][-limit:]


def test_bootstrap_sets_env_overrides(monkeypatch):
    monkeypatch.delenv("OCV_ENV_FILE", raising=False)
    monkeypatch.delenv("OCV_CONFIG_DIR", raising=False)
    monkeypatch.setattr(paths, "env_file", lambda: paths.Path("cfg") / ".env")
    monkeypatch.setattr(paths, "config_dir", lambda: paths.Path("cfg"))

    cli._bootstrap()

    import os

    assert os.environ["OCV_ENV_FILE"] == str(paths.Path("cfg") / ".env")
    assert os.environ["OCV_CONFIG_DIR"] == "cfg"


def test_version(capsys):
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == cli.__version__


def test_bare_invocation_runs_serve(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli.api, "run", lambda **kwargs: calls.update(kwargs) or 0)

    assert cli.main([]) == 0
    assert calls == {"port": None, "host": "127.0.0.1", "open_browser": True}


def test_leading_flag_is_forwarded_to_serve(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli.api, "run", lambda **kwargs: calls.update(kwargs) or 0)

    assert cli.main(["--port", "9000", "--no-open"]) == 0
    assert calls["port"] == 9000
    assert calls["open_browser"] is False


def test_config_path_prints_env_file(capsys, isolated):
    assert cli.main(["config", "path"]) == 0
    assert capsys.readouterr().out.strip() == str(isolated / ".env")


def test_config_set_then_get_round_trips(capsys, isolated):
    assert cli.main(["config", "set", "LOG_LEVEL=DEBUG"]) == 0
    capsys.readouterr()

    assert cli.main(["config", "get", "LOG_LEVEL"]) == 0
    assert capsys.readouterr().out.strip() == "DEBUG"
    assert settings.read_env()["LOG_LEVEL"] == "DEBUG"


def test_config_set_rejects_invalid_value(capsys, isolated):
    assert cli.main(["config", "set", "TELEGRAM_BOT_TOKEN="]) == 1
    assert "Bot token" in capsys.readouterr().err
    assert settings.read_env()["TELEGRAM_BOT_TOKEN"].startswith("123456789:")


def test_config_set_requires_key_value(capsys):
    assert cli.main(["config", "set", "NOPE"]) == 1
    assert "KEY=VALUE" in capsys.readouterr().err


def test_config_list_masks_secrets(capsys):
    assert cli.main(["config", "list"]) == 0
    out = capsys.readouterr().out

    assert "TELEGRAM_BOT_TOKEN=********" in out
    assert "ADMIN_USER_ID=123456789" in out
    assert "[Telegram]" in out


def test_config_get_unknown_key(capsys):
    assert cli.main(["config", "get", "NOPE"]) == 1
    assert "Unknown setting" in capsys.readouterr().err


def test_config_import_merges_file(capsys, tmp_path):
    source = tmp_path / "import.env"
    source.write_text("LOG_LEVEL=ERROR\nOPENCODE_AGENT=plan\n", encoding="utf-8")

    assert cli.main(["config", "import", str(source)]) == 0
    assert "Imported 2 setting(s)" in capsys.readouterr().out
    assert settings.read_env()["LOG_LEVEL"] == "ERROR"


def test_status_json(monkeypatch, capsys):
    monkeypatch.setattr(cli, "Manager", FakeManager)

    assert cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["bot"]["status"] == "running"


def test_status_human_output(monkeypatch, capsys):
    monkeypatch.setattr(cli, "Manager", FakeManager)

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "opencode" in out and "running" in out
    assert "2.2.5" in out
    assert "1m 05s" in out


def test_logs_prints_tail(capsys):
    log = paths.bot_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("line one\nline two\n", encoding="utf-8")

    assert cli.main(["logs"]) == 0
    assert "line two" in capsys.readouterr().out


def test_service_reports_failure(monkeypatch, capsys):
    class Result:
        ok = False
        detail = "systemctl failed: boom"

    monkeypatch.setattr(cli.service, "status", lambda: Result())

    assert cli.main(["service", "status"]) == 1
    assert "boom" in capsys.readouterr().out


def test_service_install_uses_port(monkeypatch, capsys):
    seen = {}

    class Result:
        ok = True
        detail = "installed"

    def fake_install(*, port):
        seen["port"] = port
        return Result()

    monkeypatch.setattr(cli.service, "install", fake_install)

    assert cli.main(["service", "install", "--port", "9100"]) == 0
    assert seen["port"] == 9100
    assert "installed" in capsys.readouterr().out


def test_manager_error_is_reported(monkeypatch, capsys):
    class Boom:
        def snapshot(self):
            from control.manager import ManagerError

            raise ManagerError("nope")

    monkeypatch.setattr(cli, "Manager", Boom)

    assert cli.main(["status"]) == 1
    assert "nope" in capsys.readouterr().err


def test_unknown_subcommand_exits(capsys):
    with pytest.raises(SystemExit):
        cli.main(["definitely-not-a-command"])

def test_config_set_rejects_unknown_keys(capsys, isolated):
    assert cli.main(["config", "set", "NOPE=1"]) == 1
    assert "Unknown setting" in capsys.readouterr().err
