from __future__ import annotations

import config
from telegram.ext import CommandHandler
from telegram_bot import bot

FAKE_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


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
