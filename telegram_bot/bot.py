from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncio
import logging

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from opencode_client import OpenCodeClient
from telegram_bot import database, handlers, opencode_setup, storage

logger = logging.getLogger(__name__)

EVENT_RETRY_DELAY = 5.0
EVENT_DIR_REFRESH_INTERVAL = 10.0

BOT_COMMANDS = [
    BotCommand("start", "Start the bot"),
    BotCommand("help", "Show available commands"),
    BotCommand("new", "Start a fresh opencode session"),
    BotCommand("voice", "Show voice mode (/voice on | off)"),
    BotCommand("stop", "Stop the running opencode request"),
    BotCommand("models", "Choose the model opencode uses"),
    BotCommand("session", "Switch the active opencode session"),
    BotCommand("workdir", "Browse or set the working directory"),
    BotCommand("compact", "Compact the active session"),
    BotCommand("mcp", "List MCP servers, status, and tools"),
    BotCommand("status", "Show the current task status"),
    BotCommand("whoami", "Show your profile and settings"),
    BotCommand("id", "Show your Telegram ID"),
    BotCommand("list", "List your stored media"),
    BotCommand("get", "Send a stored media item by id"),
]

MEDIA_FILTER = (
    filters.PHOTO
    | filters.VIDEO
    | filters.AUDIO
    | filters.VOICE
    | filters.Document.ALL
    | filters.ANIMATION
    | filters.VIDEO_NOTE
    | filters.Sticker.ALL
)


async def _listen_events(application: Application, directory: str) -> None:
    client = application.bot_data["opencode"]
    while True:
        try:
            async for event in client.stream_events(directory=directory):
                await handlers.handle_event(
                    event, bot=application.bot, client=client
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "opencode event stream error for %s; reconnecting in %ss",
                directory,
                EVENT_RETRY_DELAY,
            )
        await asyncio.sleep(EVENT_RETRY_DELAY)


def desired_event_directories() -> set[str]:
    directories = {str(config.OPENCODE_DIRECTORY)}
    try:
        rows = database.list_opencode_sessions()
    except Exception:
        logger.exception("Could not list sessions for event subscriptions")
        return directories
    for row in rows:
        if not bool(row["is_authenticated"]):
            continue
        directory = row["directory"]
        if directory:
            directories.add(str(directory))
    return directories


def reconcile_event_tasks(application: Application, directories: set[str]) -> None:
    tasks: dict[str, asyncio.Task] = application.bot_data.setdefault(
        "event_tasks", {}
    )
    wanted = set(directories)
    for directory in list(tasks):
        if directory not in wanted:
            task = tasks.pop(directory)
            task.cancel()
    for directory in wanted:
        existing = tasks.get(directory)
        if existing is not None and not existing.done():
            continue
        tasks[directory] = application.create_task(
            _listen_events(application, directory), name=f"events:{directory}"
        )


async def _supervise_events(application: Application) -> None:
    while True:
        try:
            reconcile_event_tasks(application, desired_event_directories())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Event supervisor iteration failed")
        refresh: asyncio.Event = application.bot_data["refresh_events"]
        try:
            await asyncio.wait_for(
                refresh.wait(), timeout=EVENT_DIR_REFRESH_INTERVAL
            )
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            raise
        refresh.clear()


async def post_init(application: Application) -> None:
    config.ensure_dirs()
    database.init_db()
    database.seed_admin(config.ADMIN_USER_ID)
    try:
        setup_result = opencode_setup.install_all()
        logger.info("opencode setup install result: %s", setup_result)
    except Exception:
        logger.exception("opencode setup install failed")
    application.bot_data["opencode"] = OpenCodeClient()
    application.bot_data["user_locks"] = handlers.USER_LOCKS
    application.bot_data["refresh_events"] = asyncio.Event()
    application.bot_data["event_tasks"] = {}
    application.bot_data["sse_supervisor"] = application.create_task(
        _supervise_events(application), name="opencode-event-supervisor"
    )
    application.bot_data["reconcile_task"] = application.create_task(
        handlers.reconcile_questions(application),
        name="opencode-question-reconcile",
    )
    await application.bot.set_my_commands(BOT_COMMANDS)
    logger.info("Bot started")


async def post_shutdown(application: Application) -> None:
    for key, label in (
        ("sse_supervisor", "event supervisor"),
        ("reconcile_task", "question reconciler"),
    ):
        task = application.bot_data.pop(key, None)
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error while stopping the %s", label)
    event_tasks = application.bot_data.pop("event_tasks", {}) or {}
    for task in event_tasks.values():
        task.cancel()
    for directory, task in event_tasks.items():
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(
                "Error while stopping event listener for %s", directory
            )
    client = application.bot_data.pop("opencode", None)
    if client is not None:
        try:
            await client.close()
        except Exception:
            logger.exception("Error while closing the opencode client")
    logger.info("Bot stopped")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(
        "Update %s caused error %s",
        update,
        context.error,
        exc_info=context.error,
    )


def build_application() -> Application:
    application = (
        ApplicationBuilder()
        .token(config.require_telegram_token())
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("id", handlers.id_command))
    application.add_handler(CommandHandler("whoami", handlers.whoami))
    application.add_handler(CommandHandler("voice", handlers.voice_command))
    application.add_handler(CommandHandler("new", handlers.new_command))
    application.add_handler(CommandHandler("stop", handlers.stop_command))
    application.add_handler(CommandHandler("models", handlers.models_command))
    application.add_handler(CommandHandler("session", handlers.session_command))
    application.add_handler(CommandHandler("workdir", handlers.workdir_command))
    application.add_handler(CommandHandler("compact", handlers.compact_command))
    application.add_handler(CommandHandler("mcp", handlers.mcp_command))
    application.add_handler(CommandHandler("status", handlers.status_command))
    application.add_handler(CommandHandler("list", handlers.list_command))
    application.add_handler(CommandHandler("get", handlers.get_command))
    application.add_handler(
        CallbackQueryHandler(handlers.permission_callback, pattern=r"^perm:")
    )
    application.add_handler(
        CallbackQueryHandler(handlers.model_callback, pattern=r"^mdl:")
    )
    application.add_handler(
        CallbackQueryHandler(
            handlers.session_callback, pattern=r"^ses:|^sesnew$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(handlers.workdir_callback, pattern=r"^wdir:")
    )
    application.add_handler(
        CallbackQueryHandler(handlers.voice_callback, pattern=r"^voice:(on|off)$")
    )
    application.add_handler(
        CallbackQueryHandler(handlers.media_callback, pattern=r"^media:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(handlers.question_callback, pattern=r"^q:")
    )
    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, handlers.voice_message)
    )
    application.add_handler(
        MessageHandler(MEDIA_FILTER & ~filters.COMMAND, handlers.media_handler)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.text_message)
    )
    application.add_error_handler(on_error)
    return application


def _webhook_url() -> str:
    base = config.TELEGRAM_WEBHOOK_URL.rstrip("/")
    path = config.TELEGRAM_WEBHOOK_PATH.strip("/")
    return f"{base}/{path}" if path else base


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    application = build_application()
    if config.webhook_enabled():
        application.run_webhook(
            listen="127.0.0.1",
            port=config.TELEGRAM_WEBHOOK_PORT,
            url_path=config.TELEGRAM_WEBHOOK_PATH.strip("/"),
            webhook_url=_webhook_url(),
            secret_token=config.TELEGRAM_WEBHOOK_SECRET or None,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
