from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import config
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler
from telegram_bot import bot, handlers

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


def test_build_application_registers_auth_gate_in_lower_group(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", FAKE_TOKEN)

    application = bot.build_application()

    assert -1 in application.handlers
    assert any(
        isinstance(handler, MessageHandler)
        and handler.callback is handlers.guard_unauthenticated
        for handler in application.handlers[-1]
    )


def test_build_application_registers_auth_callback(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", FAKE_TOKEN)

    application = bot.build_application()

    assert any(
        isinstance(handler, CallbackQueryHandler)
        and handler.callback is handlers.auth_callback
        for group in application.handlers.values()
        for handler in group
    )


def test_desired_event_subscriptions_includes_default_and_sessions(
    monkeypatch, tmp_path
):
    config_dir = tmp_path / "repo"
    monkeypatch.setattr(bot.config, "OPENCODE_DIRECTORY", config_dir)
    monkeypatch.setattr(bot.config, "OPENCODE_BASE_URL", "http://localhost:4096")
    rows = [
        {"directory": "D:/jcp", "is_authenticated": 1, "base_url": "http://other:4096"},
        {"directory": "D:/other", "is_authenticated": 1, "base_url": None},
        {"directory": "D:/unauthed", "is_authenticated": 0, "base_url": "http://x:1"},
        {"directory": "", "is_authenticated": 1, "base_url": "http://y:1"},
    ]
    monkeypatch.setattr(bot, "database", _FakeDB(rows))

    subscriptions = bot.desired_event_subscriptions()

    assert ("http://localhost:4096", str(config_dir)) in subscriptions
    assert ("http://other:4096", "D:/jcp") in subscriptions
    assert ("http://localhost:4096", "D:/other") in subscriptions
    assert ("http://x:1", "D:/unauthed") not in subscriptions
    assert not any(directory == "" for _, directory in subscriptions)


def test_desired_event_subscriptions_tolerates_db_error(monkeypatch, tmp_path):
    config_dir = tmp_path / "repo"
    monkeypatch.setattr(bot.config, "OPENCODE_DIRECTORY", config_dir)
    monkeypatch.setattr(bot.config, "OPENCODE_BASE_URL", "http://localhost:4096")

    class _BrokenDB:
        def list_opencode_sessions(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(bot, "database", _BrokenDB())

    assert bot.desired_event_subscriptions() == {
        ("http://localhost:4096", str(config_dir))
    }


def test_reconcile_event_tasks_adds_and_keeps_existing():
    application = _FakeApplication()
    subscriptions = {("http://a:1", "x"), ("http://b:2", "y")}

    bot.reconcile_event_tasks(application, subscriptions)
    assert set(application.bot_data["event_tasks"]) == {
        "http://a:1::x",
        "http://b:2::y",
    }
    assert sorted(application.created) == [
        "events:http://a:1::x",
        "events:http://b:2::y",
    ]

    application.created.clear()
    bot.reconcile_event_tasks(application, subscriptions)
    assert application.created == []


def test_reconcile_event_tasks_cancels_removed():
    application = _FakeApplication()
    bot.reconcile_event_tasks(
        application, {("http://a:1", "x"), ("http://b:2", "y")}
    )
    task_a = application.bot_data["event_tasks"]["http://a:1::x"]

    bot.reconcile_event_tasks(application, {("http://b:2", "y")})

    assert task_a.cancelled is True
    assert set(application.bot_data["event_tasks"]) == {"http://b:2::y"}


@pytest.mark.asyncio
async def test_listen_events_uses_per_base_url_client(monkeypatch):
    calls: list[str] = []

    def fake_client_for_base_url(context, base_url):
        calls.append(base_url)

        async def stream_events(directory=None):
            raise asyncio.CancelledError()
            yield  # pragma: no cover

        return SimpleNamespace(
            base_url=base_url, stream_events=stream_events
        )

    monkeypatch.setattr(handlers, "_client_for_base_url", fake_client_for_base_url)
    application = SimpleNamespace(bot_data={}, bot=SimpleNamespace())

    with pytest.raises(asyncio.CancelledError):
        await bot._listen_events(application, "http://other:4096", "D:/x")

    assert calls == ["http://other:4096"]


def test_build_application_registers_server_command(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", FAKE_TOKEN)

    application = bot.build_application()

    assert any(
        isinstance(handler, CommandHandler) and "server" in handler.commands
        for handler in application.handlers[0]
    )


@pytest.mark.parametrize("command", ["skip", "cancel"])
def test_build_application_registers_server_flow_commands(monkeypatch, command):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", FAKE_TOKEN)

    application = bot.build_application()

    assert any(
        isinstance(handler, CommandHandler) and command in handler.commands
        for handler in application.handlers[0]
    )


def test_build_application_registers_server_callback(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", FAKE_TOKEN)

    application = bot.build_application()

    assert any(
        isinstance(handler, CallbackQueryHandler)
        and handler.callback is handlers.server_callback
        for group in application.handlers.values()
        for handler in group
    )


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
