from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import config
from telegram.ext import CommandHandler
from telegram_bot import bot

FAKE_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def list_opencode_sessions(self):
        return self._rows


class _FakeTask:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def done(self):
        return False


class _FakeApplication:
    def __init__(self):
        self.bot_data = {"event_tasks": {}}
        self.created: list = []

    def create_task(self, coro, name=None):
        coro.close()
        self.created.append(name)
        return _FakeTask()


def test_build_application_uses_concurrent_updates(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", FAKE_TOKEN)

    application = bot.build_application()

    assert application.update_processor.max_concurrent_updates > 1


def test_build_application_registers_new_handlers(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", FAKE_TOKEN)

    application = bot.build_application()

    assert 0 in application.handlers
    assert any(
        isinstance(handler, CommandHandler) and "status" in handler.commands
        for handler in application.handlers[0]
    )


def test_desired_event_directories_includes_config_and_sessions(monkeypatch, tmp_path):
    config_dir = tmp_path / "repo"
    monkeypatch.setattr(bot.config, "OPENCODE_DIRECTORY", config_dir)
    rows = [
        {"directory": "D:/jcp", "is_authenticated": 1},
        {"directory": "D:/other", "is_authenticated": 1},
        {"directory": "D:/unauthed", "is_authenticated": 0},
        {"directory": "", "is_authenticated": 1},
    ]
    monkeypatch.setattr(bot, "database", _FakeDB(rows))

    directories = bot.desired_event_directories()

    assert str(config_dir) in directories
    assert "D:/jcp" in directories
    assert "D:/other" in directories
    assert "D:/unauthed" not in directories
    assert "" not in directories


def test_desired_event_directories_tolerates_db_error(monkeypatch, tmp_path):
    config_dir = tmp_path / "repo"
    monkeypatch.setattr(bot.config, "OPENCODE_DIRECTORY", config_dir)

    class _BrokenDB:
        def list_opencode_sessions(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(bot, "database", _BrokenDB())

    assert bot.desired_event_directories() == {str(config_dir)}


def test_reconcile_event_tasks_adds_and_keeps_existing():
    application = _FakeApplication()

    bot.reconcile_event_tasks(application, {"a", "b"})
    assert set(application.bot_data["event_tasks"]) == {"a", "b"}
    assert sorted(application.created) == ["events:a", "events:b"]

    application.created.clear()
    bot.reconcile_event_tasks(application, {"a", "b"})
    assert application.created == []


def test_reconcile_event_tasks_cancels_removed():
    application = _FakeApplication()
    bot.reconcile_event_tasks(application, {"a", "b"})
    task_a = application.bot_data["event_tasks"]["a"]

    bot.reconcile_event_tasks(application, {"b"})

    assert task_a.cancelled is True
    assert set(application.bot_data["event_tasks"]) == {"b"}


def _fake_run_app():
    return SimpleNamespace(run_webhook=MagicMock(), run_polling=MagicMock())


def test_main_uses_webhook_when_enabled(monkeypatch):
    monkeypatch.setattr(
        bot.config, "TELEGRAM_WEBHOOK_URL", "https://telegram-bot.shivrajan.com"
    )
    monkeypatch.setattr(bot.config, "TELEGRAM_WEBHOOK_PORT", 8080)
    monkeypatch.setattr(bot.config, "TELEGRAM_WEBHOOK_PATH", "ocw-abc")
    monkeypatch.setattr(bot.config, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    application = _fake_run_app()
    monkeypatch.setattr(bot, "build_application", lambda: application)

    bot.main()

    assert application.run_polling.called is False
    assert application.run_webhook.called is True
    kwargs = application.run_webhook.call_args.kwargs
    assert kwargs["listen"] == "127.0.0.1"
    assert kwargs["port"] == 8080
    assert kwargs["url_path"] == "ocw-abc"
    assert kwargs["webhook_url"] == (
        "https://telegram-bot.shivrajan.com/ocw-abc"
    )
    assert kwargs["secret_token"] == "s3cret"
    assert kwargs["drop_pending_updates"] is True


def test_main_uses_polling_without_webhook(monkeypatch):
    monkeypatch.setattr(bot.config, "TELEGRAM_WEBHOOK_URL", "")
    application = _fake_run_app()
    monkeypatch.setattr(bot, "build_application", lambda: application)

    bot.main()

    assert application.run_polling.called is True
    assert application.run_webhook.called is False


def test_webhook_url_strips_slashes(monkeypatch):
    monkeypatch.setattr(bot.config, "TELEGRAM_WEBHOOK_URL", "https://t.example/")
    monkeypatch.setattr(bot.config, "TELEGRAM_WEBHOOK_PATH", "/ocw/")
    assert bot._webhook_url() == "https://t.example/ocw"
