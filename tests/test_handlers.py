from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden
from telegram.ext import ApplicationHandlerStop

import config
from telegram_bot import handlers


class FakeMessage:
    def __init__(self, text=None, voice=None, chat_id=111):
        self.text = text
        self.voice = voice
        self.chat_id = chat_id
        self.sent: list[str] = []
        self.edits: list[str] = []
        self.voices: list[str] = []
        self.documents: list[str] = []
        self.photos: list[str] = []
        self.reply_markups: list = []
        self.edit_markups: list = []
        self.reply_text = AsyncMock(side_effect=self._reply)
        self.edit_text = AsyncMock(side_effect=self._edit)
        self.reply_voice = AsyncMock(side_effect=self._voice)
        self.reply_document = AsyncMock(side_effect=self._document)
        self.reply_photo = AsyncMock(side_effect=self._photo)
        self.delete = AsyncMock()

    async def _reply(self, text, **kwargs):
        self.sent.append(text)
        self.reply_markups.append(kwargs.get("reply_markup"))
        return self

    async def _edit(self, text, **kwargs):
        self.edits.append(text)
        self.edit_markups.append(kwargs.get("reply_markup"))

    async def _voice(self, voice, **kwargs):
        self.voices.append(voice)

    async def _document(self, document, **kwargs):
        self.documents.append(document)

    async def _photo(self, photo, **kwargs):
        self.photos.append(photo)


class FakeQuery:
    def __init__(self, data, text="opencode", from_user=None):
        self.data = data
        self.from_user = from_user if from_user is not None else SimpleNamespace(id=111)
        self.message = FakeMessage(text=text)
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()
        self.edit_message_reply_markup = AsyncMock()


class FakeApplication:
    def __init__(self):
        self.tasks: list = []

    def create_task(self, coro, name=None):
        self.tasks.append(coro)
        return coro


class FakeClient:
    def __init__(self, reply="hello from opencode"):
        self.directory = "D:/repo"
        self.base_url = handlers.config.OPENCODE_BASE_URL
        self.create_session = AsyncMock(return_value={"id": "ses_new"})
        self.send_prompt = AsyncMock(return_value=reply)
        self.abort = AsyncMock()
        self.reply_permission = AsyncMock()
        self.list_models = AsyncMock(return_value=[])
        self.list_sessions = AsyncMock(return_value=[])
        self.compact_session = AsyncMock()
        self.get_mcp_status = AsyncMock(return_value={})
        self.list_tool_ids = AsyncMock(return_value=[])
        self.get_session_status = AsyncMock(return_value={})
        self.get_session_todos = AsyncMock(return_value=[])
        self.get_last_activity = AsyncMock(return_value=None)
        self.get_last_assistant_message = AsyncMock(return_value=None)
        self.get_turn_assistant_parts = AsyncMock(return_value=[])
        self.get_session_diff = AsyncMock(return_value=[])
        self.list_questions = AsyncMock(return_value=[])
        self.reply_question = AsyncMock()
        self.reject_question = AsyncMock()
        self.get_path = AsyncMock(return_value={})
        self.default_directory = AsyncMock(
            return_value=str(handlers.config.OPENCODE_DIRECTORY)
        )
        self.list_directory = AsyncMock(return_value=[])


class FakeDB:
    def __init__(self, row, *, voice=False):
        self.row = row
        self.voice = voice
        self.sessions: dict[int, dict] = {}
        self.workdirs: dict[int, str] = {}
        self.user_workdirs: dict[tuple[int, int], str] = {}
        self.models: dict[int, tuple[str, str]] = {}
        self.user_models: dict[tuple[int, int], tuple[str, str]] = {}
        self.media: dict[int, dict] = {}
        self.session_rows: list[dict] = []
        self.approval_requested_at = row.get("approval_requested_at")
        self.servers: dict[int, dict] = {}
        self.next_server_id = 1
        self.user_servers: dict[int, int] = {}

    def upsert_user(self, *args, **kwargs):
        return self.row

    def get_user_by_telegram_id(self, telegram_id):
        return self.row

    def set_approval_requested(self, telegram_id, value):
        self.approval_requested_at = "2026-01-01T00:00:00+00:00" if value else None
        self.row["approval_requested_at"] = self.approval_requested_at

    def has_pending_approval(self, telegram_id):
        return bool(self.approval_requested_at) and not bool(
            self.row["is_authenticated"]
        )

    def set_authenticated(self, telegram_id, value):
        self.row["is_authenticated"] = 1 if value else 0
        return True

    def is_authenticated(self, telegram_id):
        return bool(self.row["is_authenticated"])

    def get_voice_mode(self, telegram_id):
        return self.voice

    def set_voice_mode(self, telegram_id, enabled):
        self.voice = enabled
        return True

    def get_workdir(self, telegram_id, server_id=None):
        if server_id is None:
            return self.workdirs.get(int(telegram_id))
        key = (int(telegram_id), int(server_id))
        if key in self.user_workdirs:
            return self.user_workdirs[key]
        if self.default_server_id() == int(server_id):
            return self.workdirs.get(int(telegram_id))
        return None

    def set_workdir(self, telegram_id, directory, server_id=None):
        if server_id is None:
            self.workdirs[int(telegram_id)] = directory
            return
        self.user_workdirs[(int(telegram_id), int(server_id))] = directory
        if self.default_server_id() == int(server_id):
            self.workdirs[int(telegram_id)] = directory

    def default_server_id(self):
        ids = sorted(self.servers)
        return ids[0] if ids else None

    def get_model(self, telegram_id, server_id=None):
        if server_id is None:
            return self.models.get(int(telegram_id))
        key = (int(telegram_id), int(server_id))
        if key in self.user_models:
            return self.user_models[key]
        if self.default_server_id() == int(server_id):
            return self.models.get(int(telegram_id))
        return None

    def set_model(self, telegram_id, provider_id, model_id, server_id=None):
        if server_id is None:
            self.models[int(telegram_id)] = (provider_id, model_id)
            return
        self.user_models[(int(telegram_id), int(server_id))] = (
            provider_id,
            model_id,
        )
        if self.default_server_id() == int(server_id):
            self.models[int(telegram_id)] = (provider_id, model_id)

    def get_opencode_session(self, user_id):
        return self.sessions.get(user_id)

    def set_opencode_session(self, user_id, session_id, directory, server_id=None):
        self.sessions[user_id] = {
            "session_id": session_id,
            "directory": directory,
            "server_id": server_id,
        }

    def delete_opencode_session(self, user_id):
        return self.sessions.pop(user_id, None) is not None

    def ensure_default_server(self, base_url, label="Local"):
        return self.add_server(label, base_url)

    def add_server(
        self, label, base_url, created_by=None, username=None, password=None
    ):
        base = (base_url or "").rstrip("/")
        if any(row["base_url"] == base for row in self.servers.values()):
            return None
        server_id = self.next_server_id
        self.next_server_id += 1
        row = {
            "id": server_id,
            "label": label,
            "base_url": base,
            "created_by": created_by,
            "username": username,
            "password": password,
        }
        self.servers[server_id] = row
        return row

    def list_servers(self):
        return [self.servers[key] for key in sorted(self.servers)]

    def get_server(self, server_id):
        return self.servers.get(int(server_id))

    def get_server_by_url(self, base_url):
        base = (base_url or "").rstrip("/")
        for row in self.servers.values():
            if row["base_url"] == base:
                return row
        return None

    def delete_server(self, server_id):
        server_id = int(server_id)
        if len(self.servers) <= 1 or server_id not in self.servers:
            return False
        self.servers.pop(server_id)
        for telegram_id, active in list(self.user_servers.items()):
            if active == server_id:
                self.user_servers.pop(telegram_id)
        return True

    def get_user_server(self, telegram_id):
        server_id = self.user_servers.get(int(telegram_id))
        if server_id is not None and server_id in self.servers:
            return self.servers[server_id]
        servers = self.list_servers()
        return servers[0] if servers else None

    def set_user_server(self, telegram_id, server_id):
        self.user_servers[int(telegram_id)] = int(server_id)
        return True

    def get_user_by_session_id(self, session_id):
        for session in self.sessions.values():
            if session["session_id"] == session_id:
                return self.row
        return None

    def insert_media(self, **kwargs):
        return 1

    def get_media(self, media_id):
        return self.media.get(int(media_id))

    def list_media(self, user_id=None, limit=50):
        rows = list(self.media.values())
        if user_id is not None:
            rows = [row for row in rows if row.get("user_id") == user_id]
        return rows[:limit]

    def list_opencode_sessions(self):
        return list(self.session_rows)


def _row(*, telegram_id=111, authenticated=1):
    return {
        "id": 1,
        "telegram_id": telegram_id,
        "username": "alice",
        "first_name": "Alice",
        "last_name": "A",
        "language_code": "en",
        "is_authenticated": authenticated,
        "role": "user",
        "voice_mode": 0,
        "approval_requested_at": None,
    }


class FakeBot:
    def __init__(self, error=None):
        self.messages: list[dict] = []
        self.error = error

    async def send_message(self, chat_id=None, text=None, reply_markup=None, **kwargs):
        if self.error is not None:
            raise self.error
        self.messages.append(
            {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        )
        return SimpleNamespace(message_id=len(self.messages))


def _context_with_bot(client, bot):
    return SimpleNamespace(
        bot_data={"opencode": client},
        bot=bot,
        application=FakeApplication(),
    )


def _update(text=None, voice=None, telegram_id=111):
    user = SimpleNamespace(
        id=telegram_id,
        username="alice",
        first_name="Alice",
        last_name="A",
        language_code="en",
    )
    message = FakeMessage(text=text, voice=voice, chat_id=telegram_id)
    return SimpleNamespace(
        effective_user=user,
        effective_message=message,
        effective_chat=SimpleNamespace(id=telegram_id),
    )


def _context(client, application=None):
    return SimpleNamespace(
        bot_data={"opencode": client},
        bot=AsyncMock(),
        application=application if application is not None else FakeApplication(),
    )


@pytest.fixture(autouse=True)
def _clear_locks():
    handlers.USER_LOCKS.clear()
    handlers.MODEL_CHOICES.clear()
    handlers.MODEL_LABELS.clear()
    handlers.WORKDIR_BROWSE.clear()
    handlers.ACTIVE_TASKS.clear()
    handlers.SENT_MEDIA.clear()
    handlers.PENDING_QUESTIONS.clear()
    handlers.PENDING_SERVER_INPUT.clear()
    handlers.PERMISSION_SERVERS.clear()
    yield
    handlers.USER_LOCKS.clear()
    handlers.MODEL_CHOICES.clear()
    handlers.MODEL_LABELS.clear()
    handlers.WORKDIR_BROWSE.clear()
    handlers.ACTIVE_TASKS.clear()
    handlers.SENT_MEDIA.clear()
    handlers.PENDING_QUESTIONS.clear()
    handlers.PENDING_SERVER_INPUT.clear()
    handlers.PERMISSION_SERVERS.clear()


def test_split_message_at_exact_limit():
    text = "a" * 4096
    assert handlers.split_message(text) == [text]


def test_split_message_splits_long_text():
    text = "a" * 4096 + "bcd"
    chunks = handlers.split_message(text)
    assert [len(chunk) for chunk in chunks] == [4096, 3]
    assert "".join(chunks) == text


def test_split_message_empty():
    assert handlers.split_message("") == []


def test_build_permission_keyboard_callback_data():
    request = SimpleNamespace(id="per_1")
    keyboard = handlers.build_permission_keyboard(request)
    data = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert data == ["perm:once:per_1", "perm:always:per_1", "perm:reject:per_1"]


def test_parse_permission_callback_round_trips():
    assert handlers.parse_permission_callback("perm:once:per_1") == ("once", "per_1")
    assert handlers.parse_permission_callback("perm:always:per_2") == ("always", "per_2")
    assert handlers.parse_permission_callback("perm:reject:per_3") == ("reject", "per_3")


def test_parse_permission_callback_malformed():
    assert handlers.parse_permission_callback("") is None
    assert handlers.parse_permission_callback("perm") is None
    assert handlers.parse_permission_callback("perm:once") is None
    assert handlers.parse_permission_callback("perm:bogus:per_1") is None
    assert handlers.parse_permission_callback("perm:once:") is None
    assert handlers.parse_permission_callback("other:once:per_1") is None


@pytest.mark.asyncio
async def test_text_flow_voice_off_never_calls_tts(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    synthesize = AsyncMock()
    monkeypatch.setattr(handlers.tts, "synthesize", synthesize)

    client = FakeClient(reply="Hello there")
    update = _update(text="hi")
    await handlers.text_message(update, _context(client))

    client.send_prompt.assert_awaited_once()
    synthesize.assert_not_awaited()
    assert update.effective_message.voices == []
    assert "Hello there" in update.effective_message.edits


@pytest.mark.asyncio
async def test_text_flow_voice_on_synthesizes_and_sends_voice(monkeypatch):
    db = FakeDB(_row(), voice=True)
    monkeypatch.setattr(handlers, "database", db)
    synthesize = AsyncMock()
    monkeypatch.setattr(handlers.tts, "synthesize", synthesize)

    client = FakeClient(reply="Spoken reply")
    update = _update(text="hi")
    await handlers.text_message(update, _context(client))

    client.send_prompt.assert_awaited_once()
    synthesize.assert_awaited_once()
    assert len(update.effective_message.voices) == 1
    assert "Spoken reply" in update.effective_message.edits


@pytest.mark.asyncio
async def test_text_flow_unauthenticated_is_refused(monkeypatch):
    db = FakeDB(_row(authenticated=0))
    monkeypatch.setattr(handlers, "database", db)
    synthesize = AsyncMock()
    monkeypatch.setattr(handlers.tts, "synthesize", synthesize)

    client = FakeClient()
    update = _update(text="hi")
    await handlers.text_message(update, _context(client))

    client.send_prompt.assert_not_awaited()
    client.create_session.assert_not_awaited()
    synthesize.assert_not_awaited()
    assert handlers.ACCESS_PENDING_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_text_flow_reports_opencode_error(monkeypatch):
    from opencode_client import OpenCodeError

    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)

    client = FakeClient()
    client.send_prompt = AsyncMock(side_effect=OpenCodeError("boom"))
    update = _update(text="hi")
    await handlers.text_message(update, _context(client))

    assert handlers.OPENCODE_ERROR_TEXT in update.effective_message.edits


@pytest.mark.asyncio
async def test_voice_flow_transcribes_and_prompts_without_tts(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)

    info = SimpleNamespace(
        media_type="voice",
        file_id="f1",
        file_unique_id="u1",
        file_name=None,
        mime_type="audio/ogg",
        file_size=10,
    )
    fake_storage = SimpleNamespace(
        extract_media=lambda message: info,
        download_media=AsyncMock(return_value=Path("media/111/u1.ogg")),
        to_relative_path=lambda path: "111/u1.ogg",
    )
    monkeypatch.setattr(handlers, "storage", fake_storage)

    transcribe = AsyncMock(return_value="make a file")
    monkeypatch.setattr(handlers.stt, "transcribe", transcribe)
    synthesize = AsyncMock()
    monkeypatch.setattr(handlers.tts, "synthesize", synthesize)

    client = FakeClient(reply="done")
    update = _update(voice=SimpleNamespace(file_id="f1"))
    await handlers.voice_message(update, _context(client))

    transcribe.assert_awaited_once()
    client.send_prompt.assert_awaited_once()
    assert client.send_prompt.await_args.args[1] == "make a file"
    synthesize.assert_not_awaited()
    assert "📝 make a file" in update.effective_message.sent


@pytest.mark.asyncio
async def test_voice_flow_empty_transcript_stops(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)

    info = SimpleNamespace(
        media_type="voice",
        file_id="f1",
        file_unique_id="u1",
        file_name=None,
        mime_type="audio/ogg",
        file_size=10,
    )
    fake_storage = SimpleNamespace(
        extract_media=lambda message: info,
        download_media=AsyncMock(return_value=Path("media/111/u1.ogg")),
        to_relative_path=lambda path: "111/u1.ogg",
    )
    monkeypatch.setattr(handlers, "storage", fake_storage)
    monkeypatch.setattr(handlers.stt, "transcribe", AsyncMock(return_value="   "))

    client = FakeClient()
    update = _update(voice=SimpleNamespace(file_id="f1"))
    await handlers.voice_message(update, _context(client))

    client.send_prompt.assert_not_awaited()
    assert handlers.EMPTY_TRANSCRIPT_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_permission_event_sends_inline_keyboard(monkeypatch):
    row = _row()
    db = FakeDB(row)
    db.get_user_by_session_id = lambda session_id: row
    monkeypatch.setattr(handlers, "database", db)

    bot = SimpleNamespace(send_message=AsyncMock())
    event = {
        "id": "evt_1",
        "type": "permission.asked",
        "properties": {
            "id": "per_1",
            "sessionID": "ses_1",
            "permission": "bash",
            "patterns": ["git push"],
        },
    }
    await handlers.handle_event(event, bot=bot, client=FakeClient())

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 111
    assert "bash" in kwargs["text"]
    assert "git push" in kwargs["text"]
    data = [
        button.callback_data
        for button_row in kwargs["reply_markup"].inline_keyboard
        for button in button_row
    ]
    assert data == ["perm:once:per_1", "perm:always:per_1", "perm:reject:per_1"]


@pytest.mark.asyncio
async def test_unrelated_event_is_ignored(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    bot = SimpleNamespace(send_message=AsyncMock())

    await handlers.handle_event(
        {"type": "message.updated", "properties": {"id": "m1"}},
        bot=bot,
        client=FakeClient(),
    )

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_callback_replies_and_edits(monkeypatch):
    client = FakeClient()
    query = SimpleNamespace(
        data="perm:always:per_1",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(text="opencode needs permission"),
    )
    update = SimpleNamespace(callback_query=query)

    await handlers.permission_callback(update, _context(client))

    client.reply_permission.assert_awaited_once_with("per_1", "always")
    query.answer.assert_awaited()
    edit_kwargs = query.edit_message_text.await_args.kwargs
    assert edit_kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_permission_callback_malformed_data_does_nothing(monkeypatch):
    client = FakeClient()
    query = SimpleNamespace(
        data="perm:bogus:per_1",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(text="x"),
    )
    update = SimpleNamespace(callback_query=query)

    await handlers.permission_callback(update, _context(client))

    client.reply_permission.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_session_creates_and_persists(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    session_id = await handlers.ensure_session(db.row, client)

    assert session_id == "ses_new"
    client.create_session.assert_awaited_once()
    assert db.sessions[1]["session_id"] == "ses_new"


@pytest.mark.asyncio
async def test_ensure_session_reuses_existing(monkeypatch):
    db = FakeDB(_row())
    directory = str(handlers.config.OPENCODE_DIRECTORY)
    db.sessions[1] = {"session_id": "ses_existing", "directory": directory}
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    session_id = await handlers.ensure_session(db.row, client)

    assert session_id == "ses_existing"
    client.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_session_recreates_when_directory_differs(monkeypatch):
    db = FakeDB(_row())
    db.sessions[1] = {"session_id": "ses_old", "directory": "/stale/path"}
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    session_id = await handlers.ensure_session(db.row, client)

    assert session_id == "ses_new"
    client.create_session.assert_awaited_once()
    assert db.sessions[1]["directory"] == str(handlers.config.OPENCODE_DIRECTORY)


@pytest.mark.asyncio
async def test_ensure_session_heals_invalid_stored_workdir(monkeypatch):
    db = FakeDB(_row())
    db.workdirs[111] = "/does/not/exist"
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.default_directory = AsyncMock(return_value="/Users/nix")

    async def list_directory(path):
        if path != "/Users/nix":
            from opencode_client import OpenCodeError

            raise OpenCodeError("UnknownError")
        return []

    client.list_directory = AsyncMock(side_effect=list_directory)

    session_id = await handlers.ensure_session(db.row, client, directory="/does/not/exist")

    assert session_id == "ses_new"
    assert db.workdirs[111] == "/Users/nix"
    assert db.sessions[1]["directory"] == "/Users/nix"
    create_kwargs = client.create_session.await_args.kwargs
    assert create_kwargs["directory"] == "/Users/nix"


@pytest.mark.asyncio
async def test_ensure_session_replaces_legacy_server_session(monkeypatch):
    db = FakeDB(_row())
    directory = str(handlers.config.OPENCODE_DIRECTORY)
    db.sessions[1] = {
        "session_id": "ses_legacy",
        "directory": directory,
        "server_id": None,
    }
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    session_id = await handlers.ensure_session(db.row, client, server_id=2)

    assert session_id == "ses_new"
    client.create_session.assert_awaited_once()
    assert db.sessions[1]["server_id"] == 2


@pytest.mark.asyncio
async def test_ensure_session_reuses_matching_server_session(monkeypatch):
    db = FakeDB(_row())
    directory = str(handlers.config.OPENCODE_DIRECTORY)
    db.sessions[1] = {
        "session_id": "ses_existing",
        "directory": directory,
        "server_id": 2,
    }
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    session_id = await handlers.ensure_session(db.row, client, server_id=2)

    assert session_id == "ses_existing"
    client.create_session.assert_not_awaited()


def _models(count):
    return [
        {
            "providerID": f"p{index % 3}",
            "modelID": f"m{index:03d}",
            "name": f"Model {index}",
        }
        for index in range(count)
    ]


def _assert_callback_data_within_limit(keyboard):
    for row in keyboard.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode("utf-8")) <= 64


def _command_context(client, args=None, application=None):
    return SimpleNamespace(
        bot_data={"opencode": client},
        bot=AsyncMock(),
        args=args,
        application=application if application is not None else FakeApplication(),
    )


def _patch_check_server(monkeypatch, healthy, calls=None):
    async def check(base_url, username, password):
        if calls is not None:
            calls.append((base_url, username, password))
        return healthy

    monkeypatch.setattr(handlers, "_check_server", check)
    return check


def test_remember_models_and_keyboard_pagination_round_trip():
    handlers.remember_models(111, _models(20))

    assert handlers.MODEL_CHOICES[111]["m0"] == ("p0", "m000")
    assert handlers.MODEL_LABELS[111]["m0"] == "Model 0"
    assert handlers.model_page_count(111) == 3

    first = handlers.build_models_keyboard(111, 0)
    first_data = [
        button.callback_data for row in first.inline_keyboard for button in row
    ]
    assert [d for d in first_data if d.startswith("mdl:m")] == [
        f"mdl:m{index}" for index in range(8)
    ]
    assert "mdl:pg:1" in first_data

    second = handlers.build_models_keyboard(111, 1)
    second_data = [
        button.callback_data for row in second.inline_keyboard for button in row
    ]
    assert "mdl:pg:0" in second_data
    assert "mdl:pg:2" in second_data

    last = handlers.build_models_keyboard(111, 2)
    last_data = [
        button.callback_data for row in last.inline_keyboard for button in row
    ]
    assert "mdl:pg:2" not in last_data
    assert "mdl:pg:1" in last_data

    for keyboard in (first, second, last):
        _assert_callback_data_within_limit(keyboard)


def test_remember_models_caps_tokens_per_user():
    handlers.remember_models(111, _models(handlers.MAX_MODEL_TOKENS + 25))
    assert len(handlers.MODEL_CHOICES[111]) == handlers.MAX_MODEL_TOKENS


def test_parse_model_callback_round_trips():
    assert handlers.parse_model_callback("mdl:m0") == ("m0", "m0")
    assert handlers.parse_model_callback("mdl:pg:2") == ("pg", "2")
    assert handlers.parse_model_callback("mdl:pg:x") is None
    assert handlers.parse_model_callback("mdl:") is None
    assert handlers.parse_model_callback("perm:once:x") is None
    assert handlers.parse_model_callback("") is None


def test_parse_session_callback_round_trips():
    assert handlers.parse_session_callback("sesnew") == handlers.SESSION_NEW_CALLBACK
    assert handlers.parse_session_callback("ses:ses_abc") == "ses_abc"
    assert handlers.parse_session_callback("ses:") is None
    assert handlers.parse_session_callback("mdl:m0") is None
    assert handlers.parse_session_callback("") is None


@pytest.mark.asyncio
async def test_current_directory_uses_stored_workdir(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.default_directory = AsyncMock(return_value="/srv/default")

    assert await handlers.current_directory(db.row, client) == "/srv/default"

    db.workdirs[111] = "D:/projects/demo"
    assert await handlers.current_directory(db.row, client) == "D:/projects/demo"
    client.default_directory.assert_awaited_once()


@pytest.mark.asyncio
async def test_current_directory_does_not_fall_back_to_config_on_error(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.default_directory = AsyncMock(side_effect=RuntimeError("down"))

    with pytest.raises(RuntimeError):
        await handlers.current_directory(db.row, client)


@pytest.mark.asyncio
async def test_current_directory_uses_per_server_workdir(monkeypatch):
    db = FakeDB(_row())
    first = db.add_server("A", "http://a:1")
    second = db.add_server("B", "http://b:2")
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.default_directory = AsyncMock(return_value="/server/default")

    assert await handlers.current_directory(
        db.row, client, int(first["id"])
    ) == "/server/default"

    db.user_workdirs[(111, int(first["id"]))] = "/server/a"
    assert await handlers.current_directory(
        db.row, client, int(first["id"])
    ) == "/server/a"
    assert await handlers.current_directory(
        db.row, client, int(second["id"])
    ) == "/server/default"


@pytest.mark.asyncio
async def test_current_directory_switching_servers_uses_other_default(monkeypatch):
    db = FakeDB(_row())
    first = db.add_server("A", "http://a:1")
    second = db.add_server("B", "http://b:2")
    db.workdirs[111] = "/legacy-default"
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.default_directory = AsyncMock(return_value="/server/b-default")

    assert await handlers.current_directory(
        db.row, client, int(first["id"])
    ) == "/legacy-default"
    assert await handlers.current_directory(
        db.row, client, int(second["id"])
    ) == "/server/b-default"


@pytest.mark.asyncio
async def test_browsable_workdir_heals_and_persists_per_server(monkeypatch):
    from opencode_client import OpenCodeError

    db = FakeDB(_row())
    first = db.add_server("A", "http://a:1")
    second = db.add_server("B", "http://b:2")
    db.user_workdirs[(111, int(first["id"]))] = "/stale"
    db.user_workdirs[(111, int(second["id"]))] = "/keep"
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.default_directory = AsyncMock(return_value="/server/a-default")

    async def list_directory(path):
        if path == "/stale":
            raise OpenCodeError("UnknownError")
        return []

    client.list_directory = AsyncMock(side_effect=list_directory)

    result = await handlers._browsable_workdir(db.row, client, int(first["id"]))

    assert result == "/server/a-default"
    assert db.get_workdir(111, int(first["id"])) == "/server/a-default"
    assert db.get_workdir(111, int(second["id"])) == "/keep"


@pytest.mark.asyncio
async def test_workdir_command_no_args_heals_stale_workdir(monkeypatch):
    from opencode_client import OpenCodeError

    db = FakeDB(_row())
    first = db.add_server("A", handlers.config.OPENCODE_BASE_URL)
    db.user_workdirs[(111, int(first["id"]))] = "/Users/nix"
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.default_directory = AsyncMock(return_value="/srv/default")

    async def list_directory(path):
        if path == "/Users/nix":
            raise OpenCodeError("UnknownError")
        return []

    client.list_directory = AsyncMock(side_effect=list_directory)

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=[]))

    assert handlers.WORKDIR_BROWSE[111]["path"] == "/srv/default"
    assert db.get_workdir(111, int(first["id"])) == "/srv/default"
    assert (
        handlers.WORKDIR_BROWSE_ERROR_TEXT.format(path="/Users/nix")
        not in update.effective_message.sent
    )


@pytest.mark.asyncio
async def test_ensure_session_heals_into_correct_per_server_slot(monkeypatch):
    from opencode_client import OpenCodeError

    db = FakeDB(_row())
    first = db.add_server("A", "http://a:1")
    second = db.add_server("B", "http://b:2")
    db.user_workdirs[(111, int(first["id"]))] = "/bad"
    db.user_workdirs[(111, int(second["id"]))] = "/keep"
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.default_directory = AsyncMock(return_value="/srv/default")

    async def list_directory(path):
        if path == "/bad":
            raise OpenCodeError("UnknownError")
        return []

    client.list_directory = AsyncMock(side_effect=list_directory)

    session_id = await handlers.ensure_session(
        db.row, client, directory="/bad", server_id=int(first["id"])
    )

    assert session_id == "ses_new"
    assert db.sessions[1]["directory"] == "/srv/default"
    assert db.get_workdir(111, int(first["id"])) == "/srv/default"
    assert db.get_workdir(111, int(second["id"])) == "/keep"


@pytest.mark.asyncio
async def test_whoami_shows_active_server_workdir(monkeypatch):
    db = FakeDB(_row())
    first = db.add_server("A", "http://a:1")
    second = db.add_server("B", "http://b:2")
    db.set_user_server(111, int(second["id"]))
    db.user_workdirs[(111, int(first["id"]))] = "/server/a"
    db.user_workdirs[(111, int(second["id"]))] = "/server/b"
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.whoami(update, _context(FakeClient()))

    assert "workdir: /server/b" in update.effective_message.sent[-1]


@pytest.mark.asyncio
async def test_whoami_shows_active_server_model(monkeypatch):
    db = FakeDB(_row())
    first = db.add_server("A", "http://a:1")
    second = db.add_server("B", "http://b:2")
    db.set_user_server(111, int(second["id"]))
    db.set_model(111, "openrouter", "remote-only", int(first["id"]))
    db.set_model(111, "opencode", "local-only", int(second["id"]))
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.whoami(update, _context(FakeClient()))

    assert "model: opencode/local-only" in update.effective_message.sent[-1]


@pytest.mark.asyncio
async def test_whoami_shows_default_model_for_unset_server(monkeypatch):
    db = FakeDB(_row())
    first = db.add_server("A", "http://a:1")
    second = db.add_server("B", "http://b:2")
    db.set_user_server(111, int(second["id"]))
    db.set_model(111, "openrouter", "remote-only", int(first["id"]))
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.whoami(update, _context(FakeClient()))

    assert "model: default" in update.effective_message.sent[-1]


def test_model_payload_uses_per_server_model(monkeypatch):
    db = FakeDB(_row())
    first = db.add_server("A", "http://a:1")
    second = db.add_server("B", "http://b:2")
    db.set_model(111, "openrouter", "remote-only", int(first["id"]))
    monkeypatch.setattr(handlers, "database", db)

    assert handlers.model_payload(111, int(first["id"])) == {
        "providerID": "openrouter",
        "modelID": "remote-only",
    }
    assert handlers.model_payload(111, int(second["id"])) is None


@pytest.mark.asyncio
async def test_model_selection_is_per_server(monkeypatch):
    db = FakeDB(_row())
    first = db.add_server("A", "http://a:1")
    second = db.add_server("B", "http://b:2")
    db.set_user_server(111, int(first["id"]))
    monkeypatch.setattr(handlers, "database", db)
    handlers.remember_models(
        111, [{"providerID": "openrouter", "modelID": "remote-only", "name": "R"}]
    )
    query = FakeQuery("mdl:m0")
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.model_callback(update, _context(FakeClient()))

    assert db.get_model(111, int(first["id"])) == ("openrouter", "remote-only")
    assert db.get_model(111, int(second["id"])) is None


@pytest.mark.asyncio
async def test_model_selection_resets_when_server_switches(monkeypatch):
    db = FakeDB(_row())
    first = db.add_server("A", "http://a:1")
    second = db.add_server("B", "http://b:2")
    db.set_user_server(111, int(first["id"]))
    monkeypatch.setattr(handlers, "database", db)
    handlers.remember_models(
        111, [{"providerID": "openrouter", "modelID": "remote-only", "name": "R"}]
    )
    query = FakeQuery("mdl:m0")
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.model_callback(update, _context(FakeClient()))

    db.set_user_server(111, int(second["id"]))

    assert handlers.model_payload(111, int(second["id"])) is None


@pytest.mark.asyncio
async def test_models_command_selection_persists_per_server(monkeypatch):
    db = FakeDB(_row())
    first = db.add_server("A", "http://a:1")
    second = db.add_server("B", "http://b:2")
    db.set_user_server(111, int(second["id"]))
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.list_models = AsyncMock(
        return_value=[
            {"providerID": "openrouter", "modelID": "remote-only", "name": "R"}
        ]
    )
    monkeypatch.setattr(handlers, "_client", lambda context, server_row=None: client)

    update = _update()
    await handlers.models_command(update, _context(client))

    query = FakeQuery("mdl:m0")
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.model_callback(cb_update, _context(FakeClient()))

    assert db.get_model(111, int(second["id"])) == ("openrouter", "remote-only")
    assert db.get_model(111, int(first["id"])) is None
    assert db.get_model(111) is None


@pytest.mark.asyncio
async def test_models_command_sends_paginated_buttons(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.list_models = AsyncMock(return_value=_models(10))

    update = _update()
    await handlers.models_command(update, _context(client))

    client.list_models.assert_awaited_once()
    assert handlers.SELECT_MODEL_TEXT in update.effective_message.sent
    markup = update.effective_message.reply_markups[-1]
    assert markup is not None
    _assert_callback_data_within_limit(markup)


@pytest.mark.asyncio
async def test_models_command_without_models(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    update = _update()
    await handlers.models_command(update, _context(client))

    assert handlers.NO_MODELS_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_model_callback_persists_selection(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    handlers.remember_models(
        111, [{"providerID": "opencode-go", "modelID": "deepseek", "name": "D"}]
    )
    query = FakeQuery("mdl:m0")
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.model_callback(update, _context(FakeClient()))

    assert db.models[111] == ("opencode-go", "deepseek")
    query.answer.assert_awaited()
    query.edit_message_text.assert_awaited_once()
    edit_kwargs = query.edit_message_text.await_args.kwargs
    assert edit_kwargs["reply_markup"] is None
    assert "opencode-go/deepseek" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_model_callback_pagination_edits_markup(monkeypatch):
    handlers.remember_models(111, _models(20))
    query = FakeQuery("mdl:pg:1")
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.model_callback(update, _context(FakeClient()))

    query.answer.assert_awaited()
    query.edit_message_reply_markup.assert_awaited_once()
    keyboard = query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    _assert_callback_data_within_limit(keyboard)


@pytest.mark.asyncio
async def test_session_command_lists_sessions_for_workdir(monkeypatch):
    db = FakeDB(_row())
    db.workdirs[111] = "D:/projects/demo"
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.list_sessions = AsyncMock(
        return_value=[{"id": "ses_1", "title": "First"}, {"id": "ses_2"}]
    )

    update = _update()
    await handlers.session_command(update, _context(client))

    client.list_sessions.assert_awaited_once_with(
        directory="D:/projects/demo", limit=50
    )
    markup = update.effective_message.reply_markups[-1]
    data = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]
    assert "ses:ses_1" in data
    assert "ses:ses_2" in data
    assert handlers.SESSION_NEW_CALLBACK in data
    _assert_callback_data_within_limit(markup)


@pytest.mark.asyncio
async def test_session_callback_switches_active_session(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    query = FakeQuery("ses:ses_abc")
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.session_callback(update, _context(FakeClient()))

    assert db.sessions[1]["session_id"] == "ses_abc"
    assert db.sessions[1]["directory"] == str(handlers.config.OPENCODE_DIRECTORY)
    query.answer.assert_awaited()
    assert "ses_abc" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_session_callback_new_session_clears(monkeypatch):
    db = FakeDB(_row())
    db.sessions[1] = {"session_id": "ses_abc", "directory": "D:/repo"}
    monkeypatch.setattr(handlers, "database", db)
    query = FakeQuery(handlers.SESSION_NEW_CALLBACK)
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.session_callback(update, _context(FakeClient()))

    assert db.sessions == {}
    assert query.edit_message_text.await_args.args[0] == handlers.SESSION_RESET_TEXT


@pytest.mark.asyncio
async def test_workdir_command_browse_error_shows_error_text(monkeypatch):
    from opencode_client import OpenCodeError

    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.list_directory = AsyncMock(side_effect=OpenCodeError("UnknownError"))

    update = _update()
    await handlers.workdir_command(
        update, _command_context(client, args=["/does/not/exist"])
    )

    assert not db.workdirs
    client.create_session.assert_not_awaited()
    assert handlers.WORKDIR_BROWSE_ERROR_TEXT.format(path="/does/not/exist") in (
        update.effective_message.sent
    )


def test_parent_dir_posix():
    assert handlers._parent_dir("/a/b") == "/a"
    assert handlers._parent_dir("/a") == "/"
    assert handlers._parent_dir("/") is None


def test_parent_dir_windows():
    assert handlers._parent_dir("C:\\a\\b") == "C:\\a"
    assert handlers._parent_dir("C:\\") is None
    assert handlers._parent_dir("C:/a/b") == "C:\\a"


def test_parent_dir_unc():
    assert handlers._parent_dir("\\\\srv\\share\\x") == "\\\\srv\\share"
    assert handlers._parent_dir("\\\\srv\\share") is None


def test_path_name_handles_both_separators():
    assert handlers._path_name("/Users/nix/src") == "src"
    assert handlers._path_name("C:\\Users\\nix\\src") == "src"
    assert handlers._path_name("/Users/nix/src/") == "src"


@pytest.mark.asyncio
async def test_list_server_directory_filters_and_sorts(monkeypatch):
    client = FakeClient()
    client.list_directory = AsyncMock(
        return_value=[
            {"name": "Zeta", "absolute": "/root/Zeta", "type": "directory"},
            {"name": "alpha", "absolute": "/root/alpha", "type": "directory"},
            {"name": "b.txt", "absolute": "/root/b.txt", "type": "file"},
            {"name": "a.txt", "absolute": "/root/a.txt", "type": "file"},
            {"name": ".git", "absolute": "/root/.git", "type": "directory", "ignored": True},
        ]
    )

    children, files, total = await handlers.list_server_directory(client, "/root")

    assert children == ["/root/alpha", "/root/Zeta"]
    assert files == ["a.txt", "b.txt"]
    assert total == 2


@pytest.mark.asyncio
async def test_open_browser_renders_server_listing(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.list_directory = AsyncMock(
        return_value=[
            {"name": "src", "absolute": "/root/src", "type": "directory"},
            {"name": "note.txt", "absolute": "/root/note.txt", "type": "file"},
        ]
    )

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=["/root"]))

    state = handlers.WORKDIR_BROWSE[111]
    assert state["path"] == "/root"
    assert state["children"] == ["/root/src"]
    assert state["files"] == ["note.txt"]
    assert state["total_files"] == 1
    text = update.effective_message.sent[-1]
    assert "📂 /root" in text
    assert "note.txt" in text
    markup = update.effective_message.reply_markups[-1]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "📁 src" in labels
    assert "⬆️ Parent" in labels

@pytest.mark.asyncio
async def test_workdir_command_opens_browser_without_applying(monkeypatch):
    db = FakeDB(_row())
    db.models[111] = ("opencode-go", "deepseek")
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.list_directory = AsyncMock(
        return_value=[{"name": "child", "absolute": "/root/child", "type": "directory"}]
    )

    update = _update()
    await handlers.workdir_command(
        update, _command_context(client, args=["/root"])
    )

    assert db.workdirs == {}
    client.create_session.assert_not_awaited()
    assert handlers.WORKDIR_BROWSE[111]["path"] == "/root"
    markup = update.effective_message.reply_markups[-1]
    data = [
        button.callback_data for row in markup.inline_keyboard for button in row
    ]
    assert "wdir:use" in data
    assert any(text.startswith("📂 /root") for text in update.effective_message.sent)


@pytest.mark.asyncio
async def test_workdir_command_passes_path_verbatim_to_server(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    update = _update()
    await handlers.workdir_command(
        update, _command_context(client, args=["/Users/nix/work"])
    )

    assert handlers.WORKDIR_BROWSE[111]["path"] == "/Users/nix/work"
    client.list_directory.assert_awaited_once_with("/Users/nix/work")


@pytest.mark.asyncio
async def test_workdir_command_no_args_opens_browser_at_current(monkeypatch):
    db = FakeDB(_row())
    db.workdirs[111] = "/Users/nix"
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=[]))

    assert handlers.WORKDIR_BROWSE[111]["path"] == "/Users/nix"
    client.create_session.assert_not_awaited()
    assert any("/Users/nix" in text for text in update.effective_message.sent)


@pytest.mark.asyncio
async def test_workdir_command_no_args_uses_server_default(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.default_directory = AsyncMock(return_value="/Users/nix")

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=[]))

    assert handlers.WORKDIR_BROWSE[111]["path"] == "/Users/nix"
    client.default_directory.assert_awaited_once()


def _server_listing(*entries):
    return [
        {
            "name": entry[0],
            "absolute": entry[1],
            "type": entry[2],
        }
        for entry in entries
    ]


@pytest.mark.asyncio
async def test_workdir_callback_navigates_into_child(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    async def list_directory(path):
        if path == "/root":
            return _server_listing(
                ("alpha", "/root/alpha", "directory"),
                ("beta", "/root/beta", "directory"),
            )
        return []

    client.list_directory = AsyncMock(side_effect=list_directory)

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=["/root"]))
    children = handlers.WORKDIR_BROWSE[111]["children"]
    beta_index = children.index("/root/beta")

    query = FakeQuery(f"wdir:o:{beta_index}", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    query.edit_message_text.assert_awaited_once()
    assert query.answer.await_count == 1
    assert handlers.WORKDIR_BROWSE[111]["path"] == "/root/beta"
    assert handlers.WORKDIR_BROWSE[111]["page"] == 0


@pytest.mark.asyncio
async def test_workdir_callback_up_goes_to_parent(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    handlers.WORKDIR_BROWSE[111] = {
        "owner": 111,
        "path": "/root/child",
        "page": 0,
        "children": [],
    }
    query = FakeQuery("wdir:up", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    assert handlers.WORKDIR_BROWSE[111]["path"] == "/root"
    query.edit_message_text.assert_awaited_once()
    assert query.answer.await_count == 1


@pytest.mark.asyncio
async def test_workdir_callback_up_at_root_alerts(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    handlers.WORKDIR_BROWSE[111] = {
        "owner": 111,
        "path": "/",
        "page": 0,
        "children": [],
    }
    query = FakeQuery("wdir:up", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.workdir_callback(cb_update, _context(client))

    assert query.answer.await_count == 1
    assert query.answer.await_args.kwargs.get("show_alert") is True
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_workdir_callback_page_is_clamped(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.list_directory = AsyncMock(
        return_value=_server_listing(
            *[(f"d{index:02d}", f"/root/d{index:02d}", "directory") for index in range(15)]
        )
    )

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=["/root"]))
    assert handlers.WORKDIR_BROWSE[111]["page"] == 0

    query = FakeQuery("wdir:pg:99", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    assert handlers.WORKDIR_BROWSE[111]["page"] == 1
    assert query.answer.await_count == 1


@pytest.mark.asyncio
async def test_workdir_callback_refresh_rebuilds(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    listing = {"entries": [("one", "/root/one", "directory")]}
    client.list_directory = AsyncMock(
        side_effect=lambda path: _server_listing(*listing["entries"])
    )

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=["/root"]))
    assert handlers.WORKDIR_BROWSE[111]["children"] == ["/root/one"]

    listing["entries"].append(("two", "/root/two", "directory"))
    query = FakeQuery("wdir:refresh", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    assert handlers.WORKDIR_BROWSE[111]["children"] == ["/root/one", "/root/two"]
    query.edit_message_text.assert_awaited_once()
    assert query.answer.await_count == 1


@pytest.mark.asyncio
async def test_workdir_callback_browse_error_shows_error_text(monkeypatch):
    from opencode_client import OpenCodeError

    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.list_directory = AsyncMock(
        return_value=_server_listing(("one", "/root/one", "directory"))
    )

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=["/root"]))

    client.list_directory = AsyncMock(side_effect=OpenCodeError("UnknownError"))
    query = FakeQuery("wdir:refresh", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    assert query.edit_message_text.await_args.args[0] == (
        handlers.WORKDIR_BROWSE_ERROR_TEXT.format(path="/root")
    )


@pytest.mark.asyncio
async def test_workdir_callback_use_persists_and_confirms(monkeypatch):
    db = FakeDB(_row())
    db.models[111] = ("opencode-go", "deepseek")
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.create_session = AsyncMock(return_value={"id": "ses_browser"})
    client.list_directory = AsyncMock(return_value=[])

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=["/root"]))
    query = FakeQuery("wdir:use", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    assert db.workdirs[111] == "/root"
    assert db.sessions[1]["session_id"] == "ses_browser"
    assert db.sessions[1]["directory"] == "/root"
    create_kwargs = client.create_session.await_args.kwargs
    assert create_kwargs["directory"] == "/root"
    assert create_kwargs["model"] == {
        "providerID": "opencode-go",
        "modelID": "deepseek",
    }
    assert query.answer.await_count == 1
    assert query.edit_message_text.await_args.args[0] == handlers.WORKDIR_SET_TEXT.format(
        path="/root"
    )
    assert 111 not in handlers.WORKDIR_BROWSE


@pytest.mark.asyncio
async def test_workdir_callback_use_failure_does_not_persist_session(monkeypatch):
    from opencode_client import OpenCodeError

    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.list_directory = AsyncMock(return_value=[])
    client.create_session = AsyncMock(side_effect=OpenCodeError("boom"))

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=["/root"]))
    query = FakeQuery("wdir:use", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    assert 1 not in db.sessions
    assert query.answer.await_count == 1
    assert query.edit_message_text.await_args.args[0] == handlers.OPENCODE_ERROR_TEXT


@pytest.mark.asyncio
async def test_workdir_callback_unauthenticated_is_refused(monkeypatch):
    db = FakeDB(_row(authenticated=0))
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    handlers.WORKDIR_BROWSE[111] = {
        "owner": 111,
        "path": "/root",
        "page": 0,
        "children": [],
    }

    query = FakeQuery("wdir:use", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    assert db.workdirs == {}
    client.create_session.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()
    assert query.answer.await_count == 1
    assert query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_workdir_callback_rejects_other_owner(monkeypatch):
    db = FakeDB(_row(telegram_id=222))
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    handlers.WORKDIR_BROWSE[222] = {
        "owner": 111,
        "path": "/root",
        "page": 0,
        "children": [],
    }

    query = FakeQuery("wdir:use", from_user=SimpleNamespace(id=222))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=222)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    client.create_session.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()
    assert query.answer.await_count == 1
    assert query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_workdir_callback_without_state_alerts(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    query = FakeQuery("wdir:refresh", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    query.edit_message_text.assert_not_awaited()
    assert query.answer.await_count == 1
    assert query.answer.await_args.kwargs.get("show_alert") is True


def test_format_workdir_message_uses_server_files():
    text = handlers.format_workdir_message(
        "/root", ["/root/a", "/root/b"], ["x.txt", "y.txt"], 5, 0
    )
    assert "📂 /root" in text
    assert "📁 Subdirectories: 2" in text
    assert "📄 Files: 5" in text
    assert "x.txt" in text and "y.txt" in text
    assert "and 3 more" in text
    assert "Page 1/1" in text


def test_build_workdir_keyboard_contains_use_and_indexed_children():
    children = [f"/root/d{index}" for index in range(3)]
    keyboard = handlers.build_workdir_keyboard(children, 0)

    data = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "wdir:use" in data
    assert all(f"wdir:o:{index}" in data for index in range(3))
    assert "wdir:refresh" in data
    assert "wdir:up" in data


def test_build_workdir_keyboard_pagination_and_callback_limits():
    children = [f"/root/d{index}" for index in range(30)]
    keyboard = handlers.build_workdir_keyboard(children, 0)

    data = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "wdir:pg:1" in data
    assert "wdir:pg:0" not in data

    last = handlers.build_workdir_keyboard(children, 2)
    last_data = [
        button.callback_data for row in last.inline_keyboard for button in row
    ]
    assert "wdir:pg:1" in last_data
    assert "wdir:pg:2" not in last_data

    for button_row in keyboard.inline_keyboard:
        for button in button_row:
            assert len(button.callback_data.encode("utf-8")) <= 64


def test_build_workdir_keyboard_omits_parent_at_root():
    keyboard = handlers.build_workdir_keyboard([], 0, has_parent=False)
    data = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "wdir:up" not in data


def test_parse_workdir_callback_round_trips():
    assert handlers.parse_workdir_callback("wdir:use") == ("use", None)
    assert handlers.parse_workdir_callback("wdir:up") == ("up", None)
    assert handlers.parse_workdir_callback("wdir:refresh") == ("refresh", None)
    assert handlers.parse_workdir_callback("wdir:o:3") == ("o", "3")
    assert handlers.parse_workdir_callback("wdir:pg:2") == ("pg", "2")
    assert handlers.parse_workdir_callback("wdir:o:x") is None
    assert handlers.parse_workdir_callback("wdir:pg:-1") is None
    assert handlers.parse_workdir_callback("wdir:") is None
    assert handlers.parse_workdir_callback("mdl:m0") is None
    assert handlers.parse_workdir_callback("") is None


@pytest.mark.asyncio
async def test_compact_command_replies_immediately_and_schedules_task(monkeypatch):
    db = FakeDB(_row())
    db.sessions[1] = {"session_id": "ses_abc", "directory": "D:/repo"}
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    application = FakeApplication()

    update = _update()
    await handlers.compact_command(update, _context(client, application))

    client.compact_session.assert_not_awaited()
    assert handlers.COMPACT_STARTED_TEXT in update.effective_message.sent
    assert len(application.tasks) == 1
    await application.tasks[0]


@pytest.mark.asyncio
async def test_compact_command_passes_selected_model_to_background(monkeypatch):
    db = FakeDB(_row())
    db.sessions[1] = {"session_id": "ses_abc", "directory": "D:/repo"}
    db.models[111] = ("opencode-go", "muse-spark-1.3-contributor")
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    application = FakeApplication()

    update = _update()
    context = _context(client, application)
    await handlers.compact_command(update, context)

    client.compact_session.assert_not_awaited()
    await application.tasks[0]
    client.compact_session.assert_awaited_once_with(
        "ses_abc",
        model={
            "providerID": "opencode-go",
            "modelID": "muse-spark-1.3-contributor",
        },
    )
    assert handlers.COMPACT_DONE_TEXT in [
        call.kwargs["text"] for call in context.bot.send_message.await_args_list
    ]


@pytest.mark.asyncio
async def test_run_compaction_success_sends_confirmation(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    context = _context(client)

    await handlers._run_compaction(context, "ses_abc", 111, None)

    client.compact_session.assert_awaited_once_with("ses_abc", model=None)
    context.bot.send_message.assert_awaited_once_with(
        chat_id=111, text=handlers.COMPACT_DONE_TEXT
    )


@pytest.mark.asyncio
async def test_run_compaction_failure_sends_reason_without_raising(monkeypatch):
    from opencode_client import OpenCodeError

    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.compact_session = AsyncMock(side_effect=OpenCodeError("summarize 500 boom"))
    context = _context(client)

    await handlers._run_compaction(context, "ses_abc", 111, None)

    kwargs = context.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 111
    assert kwargs["text"] == handlers.COMPACT_FAILED_TEXT.format(
        reason="summarize 500 boom"
    )


@pytest.mark.asyncio
async def test_run_compaction_timeout_sends_reason_without_raising(monkeypatch):
    import asyncio

    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.compact_session = AsyncMock(side_effect=asyncio.TimeoutError())
    context = _context(client)

    await handlers._run_compaction(context, "ses_abc", 111, None)

    kwargs = context.bot.send_message.await_args.kwargs
    assert kwargs["text"] == handlers.COMPACT_FAILED_TEXT.format(
        reason=handlers.COMPACT_TIMEOUT_REASON
    )


@pytest.mark.asyncio
async def test_run_compaction_unexpected_error_does_not_raise(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.compact_session = AsyncMock(side_effect=RuntimeError("kaboom"))
    context = _context(client)

    await handlers._run_compaction(context, "ses_abc", 111, None)

    kwargs = context.bot.send_message.await_args.kwargs
    assert "kaboom" in kwargs["text"]


@pytest.mark.asyncio
async def test_compact_command_without_session(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    update = _update()
    await handlers.compact_command(update, _context(client))

    client.compact_session.assert_not_awaited()
    assert handlers.NO_SESSION_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_mcp_command_formats_servers_status_and_tools(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.get_mcp_status = AsyncMock(
        return_value={"playwright": {"status": "connected"}}
    )
    client.list_tool_ids = AsyncMock(return_value=["bash", "read"])

    update = _update()
    await handlers.mcp_command(update, _context(client))

    combined = "\n".join(update.effective_message.sent)
    assert "playwright" in combined
    assert "connected" in combined
    assert "bash" in combined
    assert "read" in combined


def test_format_elapsed_seconds_minutes_hours():
    assert handlers.format_elapsed(0) == "0s"
    assert handlers.format_elapsed(45) == "45s"
    assert handlers.format_elapsed(133) == "2m 13s"
    assert handlers.format_elapsed(3840) == "1h 04m"


def test_format_todos_icons_and_summary():
    todos = [
        {"content": "first", "status": "pending", "priority": "high"},
        {"content": "second", "status": "in_progress", "priority": "high"},
        {"content": "third", "status": "completed", "priority": "low"},
        {"content": "fourth", "status": "cancelled", "priority": "low"},
        {"content": "fifth", "status": "weird", "priority": "low"},
    ]

    text = handlers.format_todos(todos)

    assert "⬜ first" in text
    assert "🔄 second" in text
    assert "✅ third" in text
    assert "❌ fourth" in text
    assert "• fifth" in text
    assert "1/5 done" in text


def test_format_activity_none():
    assert handlers.format_activity(None) is None


def test_format_activity_tool_with_status():
    part = {
        "type": "tool",
        "tool": "bash",
        "state": {"status": "running", "input": {"command": "ls"}},
    }
    assert handlers.format_activity(part) == "tool:bash (running)"


def test_format_activity_tool_without_state():
    assert handlers.format_activity({"type": "tool", "tool": "read"}) == "tool:read"
    assert handlers.format_activity({"type": "tool"}) == "tool:?"


def test_format_activity_reasoning_and_text():
    assert handlers.format_activity({"type": "reasoning", "text": "x"}) == "reasoning"
    assert handlers.format_activity({"type": "text", "text": "hello"}) == "text: hello"
    assert handlers.format_activity({"type": "text", "text": "   "}) == "text"
    assert handlers.format_activity({"type": "text", "text": "y" * 200}) == (
        "text: " + "y" * 120
    )


def test_format_activity_unknown_type():
    assert handlers.format_activity({"type": "weird"}) == "weird"


def test_format_todos_empty():
    assert handlers.format_todos([]) == handlers.STATUS_NO_TODOS_TEXT


@pytest.mark.asyncio
async def test_process_prompt_tracks_active_task_and_clears(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    observed: dict = {}

    async def side_effect(*args, **kwargs):
        observed.update(handlers.ACTIVE_TASKS.get(111, {}))
        return "done"

    client = FakeClient()
    client.send_prompt = AsyncMock(side_effect=side_effect)
    update = _update(text="hi")
    await handlers.text_message(update, _context(client))

    assert observed.get("session_id") == "ses_new"
    assert observed.get("prompt") == "hi"
    assert 111 not in handlers.ACTIVE_TASKS


@pytest.mark.asyncio
async def test_process_prompt_clears_active_task_on_error(monkeypatch):
    from opencode_client import OpenCodeError

    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.send_prompt = AsyncMock(side_effect=OpenCodeError("boom"))

    update = _update(text="hi")
    await handlers.text_message(update, _context(client))

    assert 111 not in handlers.ACTIVE_TASKS


@pytest.mark.asyncio
async def test_status_command_without_session(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    update = _update()
    await handlers.status_command(update, _context(client))

    assert handlers.STATUS_NONE_TEXT in update.effective_message.sent
    client.get_session_status.assert_not_awaited()
    client.get_session_todos.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_command_busy_includes_todos(monkeypatch):
    db = FakeDB(_row())
    db.sessions[1] = {"session_id": "ses_1", "directory": "D:/repo"}
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.get_session_status = AsyncMock(
        return_value={"ses_1": {"type": "busy"}}
    )
    client.get_session_todos = AsyncMock(
        return_value=[
            {"content": "write tests", "status": "in_progress", "priority": "high"},
            {"content": "ship it", "status": "pending", "priority": "low"},
        ]
    )

    update = _update()
    await handlers.status_command(update, _context(client))

    combined = "\n".join(update.effective_message.sent)
    assert "busy" in combined
    assert "ses_1" in combined
    assert "🔄 write tests" in combined
    assert "⬜ ship it" in combined
    assert "0/2 done" in combined
    client.get_session_todos.assert_awaited_once_with(
        "ses_1", directory=str(handlers.config.OPENCODE_DIRECTORY)
    )


@pytest.mark.asyncio
async def test_status_command_includes_activity_line(monkeypatch):
    db = FakeDB(_row())
    db.sessions[1] = {"session_id": "ses_1", "directory": "D:/repo"}
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.get_session_status = AsyncMock(return_value={"ses_1": {"type": "busy"}})
    client.get_last_activity = AsyncMock(
        return_value={"type": "tool", "tool": "bash", "state": {"status": "running"}}
    )

    update = _update()
    await handlers.status_command(update, _context(client))

    combined = "\n".join(update.effective_message.sent)
    assert "Activity: tool:bash (running)" in combined
    client.get_last_activity.assert_awaited_once_with(
        "ses_1", directory=str(handlers.config.OPENCODE_DIRECTORY)
    )


@pytest.mark.asyncio
async def test_status_command_survives_activity_error(monkeypatch):
    db = FakeDB(_row())
    db.sessions[1] = {"session_id": "ses_1", "directory": "D:/repo"}
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.get_session_status = AsyncMock(return_value={"ses_1": {"type": "busy"}})
    client.get_last_activity = AsyncMock(side_effect=RuntimeError("nope"))

    update = _update()
    await handlers.status_command(update, _context(client))

    combined = "\n".join(update.effective_message.sent)
    assert "busy" in combined
    assert "Activity:" not in combined


@pytest.mark.asyncio
async def test_status_command_retry_includes_attempt_and_message(monkeypatch):
    db = FakeDB(_row())
    db.sessions[1] = {"session_id": "ses_1", "directory": "D:/repo"}
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.get_session_status = AsyncMock(
        return_value={
            "ses_1": {
                "type": "retry",
                "attempt": 3,
                "message": "rate limited",
                "next": 5,
            }
        }
    )

    update = _update()
    await handlers.status_command(update, _context(client))

    combined = "\n".join(update.effective_message.sent)
    assert "retry (attempt 3): rate limited" in combined


@pytest.mark.asyncio
async def test_status_command_running_with_only_local_task(monkeypatch):
    db = FakeDB(_row())
    db.sessions[1] = {"session_id": "ses_1", "directory": "D:/repo"}
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    handlers.mark_task_started(111, "ses_1", "do the thing")

    update = _update()
    await handlers.status_command(update, _context(client))

    combined = "\n".join(update.effective_message.sent)
    assert handlers.STATUS_LOCAL_ONLY_TEXT in combined
    assert "Elapsed:" in combined
    assert "do the thing" in combined


@pytest.mark.asyncio
async def test_status_command_idle_without_local_task(monkeypatch):
    db = FakeDB(_row())
    db.sessions[1] = {"session_id": "ses_1", "directory": "D:/repo"}
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.get_session_status = AsyncMock(
        return_value={"ses_1": {"type": "idle"}}
    )

    update = _update()
    await handlers.status_command(update, _context(client))

    assert handlers.STATUS_NONE_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_status_command_status_error_replies_unexpected(monkeypatch):
    from opencode_client import OpenCodeError

    db = FakeDB(_row())
    db.sessions[1] = {"session_id": "ses_1", "directory": "D:/repo"}
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.get_session_status = AsyncMock(side_effect=OpenCodeError("boom"))

    update = _update()
    await handlers.status_command(update, _context(client))

    assert handlers.UNEXPECTED_ERROR_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_status_command_unauthenticated_refused(monkeypatch):
    db = FakeDB(_row(authenticated=0))
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    update = _update()
    await handlers.status_command(update, _context(client))

    assert handlers.ACCESS_PENDING_TEXT in update.effective_message.sent
    client.get_session_status.assert_not_awaited()
    client.get_session_todos.assert_not_awaited()


def _keyboard_data(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def _keyboard_labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def _fake_storage():
    return SimpleNamespace(resolve_local_path=lambda path: Path(path))


def test_build_voice_keyboard_marks_current_state():
    enabled = handlers.build_voice_keyboard(True)
    assert _keyboard_data(enabled) == ["voice:on", "voice:off"]
    labels = _keyboard_labels(enabled)
    assert labels[0].startswith("✅")
    assert not labels[1].startswith("✅")

    disabled = handlers.build_voice_keyboard(False)
    labels = _keyboard_labels(disabled)
    assert labels[1].startswith("✅")
    assert not labels[0].startswith("✅")


def test_parse_voice_callback_round_trips():
    assert handlers.parse_voice_callback("voice:on") is True
    assert handlers.parse_voice_callback("voice:off") is False
    assert handlers.parse_voice_callback("voice:maybe") is None
    assert handlers.parse_voice_callback("") is None


@pytest.mark.asyncio
async def test_voice_command_no_args_includes_keyboard(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.voice_command(update, _command_context(FakeClient(), args=[]))

    markup = update.effective_message.reply_markups[-1]
    assert markup is not None
    assert _keyboard_data(markup) == ["voice:on", "voice:off"]


@pytest.mark.asyncio
async def test_voice_command_on_and_off_persist(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.voice_command(update, _command_context(FakeClient(), args=["on"]))
    assert db.voice is True
    assert update.effective_message.reply_markups[-1] is not None

    update = _update()
    await handlers.voice_command(update, _command_context(FakeClient(), args=["off"]))
    assert db.voice is False


@pytest.mark.asyncio
async def test_voice_command_invalid_args_shows_usage(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.voice_command(
        update, _command_context(FakeClient(), args=["maybe"])
    )

    assert handlers.VOICE_USAGE_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_voice_callback_persists_and_edits(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    query = FakeQuery("voice:on", from_user=SimpleNamespace(id=111))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.voice_callback(update, _context(FakeClient()))

    assert db.voice is True
    assert query.answer.await_count == 1
    query.edit_message_text.assert_awaited_once()
    assert query.edit_message_text.await_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_voice_callback_unauthenticated_refused(monkeypatch):
    db = FakeDB(_row(authenticated=0))
    monkeypatch.setattr(handlers, "database", db)
    query = FakeQuery("voice:on", from_user=SimpleNamespace(id=111))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.voice_callback(update, _context(FakeClient()))

    assert db.voice is False
    query.edit_message_text.assert_not_awaited()
    assert query.answer.await_count == 1
    assert query.answer.await_args.kwargs.get("show_alert") is True


def test_build_media_keyboard_callback_data():
    rows = [
        {"id": 1, "media_type": "photo"},
        {"id": 2, "media_type": "voice"},
        {"id": 3, "media_type": "document"},
    ]

    keyboard = handlers.build_media_keyboard(rows)

    assert _keyboard_data(keyboard) == ["media:1", "media:2", "media:3"]
    assert all(len(row) <= 2 for row in keyboard.inline_keyboard)
    assert all(
        len(button.callback_data.encode("utf-8")) <= 64
        for row in keyboard.inline_keyboard
        for button in row
    )


def test_build_media_keyboard_empty():
    keyboard = handlers.build_media_keyboard([])
    assert len(keyboard.inline_keyboard) == 0


def test_parse_media_callback_round_trips():
    assert handlers.parse_media_callback("media:12") == 12
    assert handlers.parse_media_callback("media:x") is None
    assert handlers.parse_media_callback("media:") is None
    assert handlers.parse_media_callback("mdl:m0") is None
    assert handlers.parse_media_callback("") is None


@pytest.mark.asyncio
async def test_list_command_attaches_media_keyboard(monkeypatch):
    db = FakeDB(_row())
    db.media[1] = {
        "id": 1,
        "user_id": 1,
        "telegram_id": 111,
        "media_type": "photo",
        "local_path": "111/u1.jpg",
        "file_name": "pic.jpg",
        "file_unique_id": "u1",
    }
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.list_command(update, _context(FakeClient()))

    markup = update.effective_message.reply_markups[-1]
    assert markup is not None
    assert _keyboard_data(markup) == ["media:1"]


@pytest.mark.asyncio
async def test_get_command_sends_media_for_owner(monkeypatch, tmp_path):
    path = tmp_path / "clip.ogg"
    path.write_text("x", encoding="utf-8")
    db = FakeDB(_row())
    db.media[5] = {
        "id": 5,
        "user_id": 1,
        "telegram_id": 111,
        "media_type": "voice",
        "local_path": str(path),
        "file_name": None,
        "file_unique_id": "u5",
    }
    monkeypatch.setattr(handlers, "database", db)
    monkeypatch.setattr(handlers, "storage", _fake_storage())

    update = _update()
    await handlers.get_command(update, _command_context(FakeClient(), args=["5"]))

    assert update.effective_message.voices == [str(path)]


@pytest.mark.asyncio
async def test_get_command_rejects_other_users_media(monkeypatch, tmp_path):
    path = tmp_path / "secret.ogg"
    path.write_text("x", encoding="utf-8")
    db = FakeDB(_row())
    db.media[6] = {
        "id": 6,
        "user_id": 1,
        "telegram_id": 999,
        "media_type": "voice",
        "local_path": str(path),
        "file_name": None,
        "file_unique_id": "u6",
    }
    monkeypatch.setattr(handlers, "database", db)
    monkeypatch.setattr(handlers, "storage", _fake_storage())

    update = _update()
    await handlers.get_command(update, _command_context(FakeClient(), args=["6"]))

    assert update.effective_message.voices == []
    assert handlers.MEDIA_NOT_FOUND_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_media_callback_sends_for_owner(monkeypatch, tmp_path):
    path = tmp_path / "pic.jpg"
    path.write_text("x", encoding="utf-8")
    db = FakeDB(_row())
    db.media[7] = {
        "id": 7,
        "user_id": 1,
        "telegram_id": 111,
        "media_type": "photo",
        "local_path": str(path),
        "file_name": "pic.jpg",
        "file_unique_id": "u7",
    }
    monkeypatch.setattr(handlers, "database", db)
    monkeypatch.setattr(handlers, "storage", _fake_storage())
    query = FakeQuery("media:7", from_user=SimpleNamespace(id=111))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.media_callback(update, _context(FakeClient()))

    assert query.answer.await_count == 1
    assert query.message.photos == [str(path)]


@pytest.mark.asyncio
async def test_media_callback_rejects_other_user(monkeypatch, tmp_path):
    path = tmp_path / "pic.jpg"
    path.write_text("x", encoding="utf-8")
    db = FakeDB(_row())
    db.media[7] = {
        "id": 7,
        "user_id": 1,
        "telegram_id": 111,
        "media_type": "photo",
        "local_path": str(path),
        "file_name": "pic.jpg",
        "file_unique_id": "u7",
    }
    monkeypatch.setattr(handlers, "database", db)
    monkeypatch.setattr(handlers, "storage", _fake_storage())
    query = FakeQuery("media:7", from_user=SimpleNamespace(id=222))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=222)
    )

    await handlers.media_callback(update, _context(FakeClient()))

    assert query.message.photos == []
    assert query.answer.await_count == 1
    assert query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_media_callback_missing_id_alerts(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    query = FakeQuery("media:999", from_user=SimpleNamespace(id=111))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.media_callback(update, _context(FakeClient()))

    assert query.message.photos == []
    assert query.answer.await_count == 1
    assert query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_media_callback_falls_back_to_bot_when_send_fails(monkeypatch, tmp_path):
    path = tmp_path / "pic.jpg"
    path.write_text("x", encoding="utf-8")
    db = FakeDB(_row())
    db.media[8] = {
        "id": 8,
        "user_id": 1,
        "telegram_id": 111,
        "media_type": "photo",
        "local_path": str(path),
        "file_name": "pic.jpg",
        "file_unique_id": "u8",
    }
    monkeypatch.setattr(handlers, "database", db)
    monkeypatch.setattr(handlers, "storage", _fake_storage())

    async def boom(*args, **kwargs):
        raise RuntimeError("inaccessible message")

    monkeypatch.setattr(handlers, "_send_media", boom)
    context = _context(FakeClient())
    query = FakeQuery("media:8", from_user=SimpleNamespace(id=111))
    query.message.chat_id = 555
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.media_callback(update, context)

    assert query.answer.await_count == 1
    context.bot.send_document.assert_awaited_once()
    kwargs = context.bot.send_document.await_args.kwargs
    assert kwargs["chat_id"] == 555
    assert kwargs["document"] == str(path)


@pytest.mark.asyncio
async def test_media_callback_fallback_uses_caller_when_no_chat(monkeypatch, tmp_path):
    path = tmp_path / "pic.jpg"
    path.write_text("x", encoding="utf-8")
    db = FakeDB(_row())
    db.media[9] = {
        "id": 9,
        "user_id": 1,
        "telegram_id": 777,
        "media_type": "photo",
        "local_path": str(path),
        "file_name": "pic.jpg",
        "file_unique_id": "u9",
    }
    monkeypatch.setattr(handlers, "database", db)
    monkeypatch.setattr(handlers, "storage", _fake_storage())
    monkeypatch.setattr(handlers, "_send_media", AsyncMock(side_effect=RuntimeError("x")))
    context = _context(FakeClient())
    query = FakeQuery("media:9", from_user=SimpleNamespace(id=777))
    query.message = None
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=777)
    )

    await handlers.media_callback(update, context)

    kwargs = context.bot.send_document.await_args.kwargs
    assert kwargs["chat_id"] == 777


def _media_info(media_type="photo", file_name=None, mime_type=None):
    return SimpleNamespace(
        media_type=media_type,
        file_id="f1",
        file_unique_id="u1",
        file_name=file_name,
        mime_type=mime_type,
        file_size=10,
    )


def _media_storage(info, path):
    return SimpleNamespace(
        extract_media=lambda message: info,
        download_media=AsyncMock(return_value=path),
        to_relative_path=lambda local: "111/u1.bin",
    )


@pytest.mark.asyncio
async def test_media_handler_authenticated_photo_prompts_opencode(monkeypatch, tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_text("x", encoding="utf-8")
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    monkeypatch.setattr(
        handlers, "storage", _media_storage(_media_info("photo"), path)
    )
    client = FakeClient()

    update = _update()
    await handlers.media_handler(update, _context(client))

    client.send_prompt.assert_awaited_once()
    prompt = client.send_prompt.await_args.args[1]
    assert "photo" in prompt
    assert str(path) in prompt
    assert all(
        not text.startswith("Stored") for text in update.effective_message.sent
    )


@pytest.mark.asyncio
async def test_media_handler_document_with_caption_includes_caption(
    monkeypatch, tmp_path
):
    path = tmp_path / "report.pdf"
    path.write_text("x", encoding="utf-8")
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    monkeypatch.setattr(
        handlers,
        "storage",
        _media_storage(_media_info("document", file_name="report.pdf"), path),
    )
    client = FakeClient()

    update = _update()
    update.effective_message.caption = "please summarize"
    await handlers.media_handler(update, _context(client))

    prompt = client.send_prompt.await_args.args[1]
    assert "The user sent a document." in prompt
    assert f"See the document from this path: {path}" in prompt
    assert "Caption: please summarize" in prompt


@pytest.mark.asyncio
async def test_media_handler_document_without_caption_omits_caption(
    monkeypatch, tmp_path
):
    path = tmp_path / "report.pdf"
    path.write_text("x", encoding="utf-8")
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    monkeypatch.setattr(
        handlers,
        "storage",
        _media_storage(_media_info("document", file_name="report.pdf"), path),
    )
    client = FakeClient()

    update = _update()
    await handlers.media_handler(update, _context(client))

    prompt = client.send_prompt.await_args.args[1]
    assert "Caption:" not in prompt


@pytest.mark.asyncio
async def test_media_handler_unauthenticated_stores_only(monkeypatch, tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_text("x", encoding="utf-8")
    db = FakeDB(_row(authenticated=0))
    monkeypatch.setattr(handlers, "database", db)
    monkeypatch.setattr(
        handlers, "storage", _media_storage(_media_info("photo"), path)
    )
    client = FakeClient()

    update = _update()
    await handlers.media_handler(update, _context(client))

    client.send_prompt.assert_not_awaited()
    client.create_session.assert_not_awaited()
    assert any(
        text.startswith("Stored photo as id") for text in update.effective_message.sent
    )


@pytest.mark.asyncio
async def test_media_handler_unsupported_media(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    monkeypatch.setattr(
        handlers,
        "storage",
        SimpleNamespace(extract_media=lambda message: None),
    )
    client = FakeClient()

    update = _update()
    await handlers.media_handler(update, _context(client))

    assert "Unsupported media type." in update.effective_message.sent
    client.send_prompt.assert_not_awaited()


def _data_url(mime, data):
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


@pytest.mark.asyncio
async def test_fetch_media_bytes_data_url_base64():
    data, mime = await handlers._fetch_media_bytes(
        _data_url("image/png", b"abc"), max_bytes=100
    )
    assert data == b"abc"
    assert mime == "image/png"


@pytest.mark.asyncio
async def test_fetch_media_bytes_data_url_plain():
    data, mime = await handlers._fetch_media_bytes(
        "data:text/plain,hello%20world", max_bytes=100
    )
    assert data == b"hello world"
    assert mime == "text/plain"


@pytest.mark.asyncio
async def test_fetch_media_bytes_data_url_oversize():
    with pytest.raises(ValueError):
        await handlers._fetch_media_bytes(
            _data_url("image/png", b"x" * 20), max_bytes=10
        )


@pytest.mark.asyncio
async def test_fetch_media_bytes_file_url_and_containment(tmp_path):
    inside = tmp_path / "a.png"
    inside.write_bytes(b"hi")

    data, mime = await handlers._fetch_media_bytes(
        inside.as_uri(), max_bytes=100, directory=tmp_path
    )
    assert data == b"hi"
    assert mime is None

    outside = tmp_path.parent / "opencode_voice_outside.png"
    outside.write_bytes(b"nope")
    with pytest.raises(ValueError):
        await handlers._fetch_media_bytes(
            outside.as_uri(), max_bytes=100, directory=tmp_path
        )


@pytest.mark.asyncio
async def test_fetch_media_bytes_unsupported_scheme():
    with pytest.raises(ValueError):
        await handlers._fetch_media_bytes("ftp://example.com/file.png", max_bytes=10)


@pytest.mark.asyncio
async def test_send_outbound_media_file_part_image_photo(tmp_path):
    message = FakeMessage()
    parts = [
        {
            "type": "file",
            "url": _data_url("image/png", b"png"),
            "mime": "image/png",
            "filename": "pic.png",
        }
    ]

    await handlers._send_outbound_media(
        message, session_id="ses_1", directory=tmp_path, parts=parts, diff_files=[]
    )

    assert len(message.photos) == 1
    assert message.documents == []


@pytest.mark.asyncio
async def test_send_outbound_media_non_image_document(tmp_path):
    message = FakeMessage()
    parts = [
        {
            "type": "file",
            "url": _data_url("application/pdf", b"pdf"),
            "mime": "application/pdf",
            "filename": "doc.pdf",
        }
    ]

    await handlers._send_outbound_media(
        message, session_id="ses_1", directory=tmp_path, parts=parts, diff_files=[]
    )

    assert len(message.documents) == 1
    assert message.photos == []


@pytest.mark.asyncio
async def test_send_outbound_media_patch_and_diff_inside_directory(tmp_path):
    media = tmp_path / "pic.png"
    media.write_bytes(b"x")
    message = FakeMessage()
    parts = [{"type": "patch", "files": [str(media)]}]

    await handlers._send_outbound_media(
        message,
        session_id="ses_1",
        directory=tmp_path,
        parts=parts,
        diff_files=[{"path": str(media)}],
    )

    assert len(message.photos) == 1
    assert message.documents == []


@pytest.mark.asyncio
async def test_send_outbound_media_diff_file_inside_directory(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    message = FakeMessage()

    await handlers._send_outbound_media(
        message,
        session_id="ses_1",
        directory=tmp_path,
        parts=[],
        diff_files=[{"path": str(media)}],
    )

    assert len(message.documents) == 1


@pytest.mark.asyncio
async def test_send_outbound_media_skips_invalid_paths(tmp_path):
    outside = tmp_path.parent / "opencode_voice_outside.png"
    outside.write_bytes(b"nope")
    non_media = tmp_path / "notes.txt"
    non_media.write_text("x", encoding="utf-8")
    missing = tmp_path / "missing.png"
    message = FakeMessage()

    await handlers._send_outbound_media(
        message,
        session_id="ses_1",
        directory=tmp_path,
        parts=[],
        diff_files=[
            {"path": str(outside)},
            {"path": str(non_media)},
            {"path": str(missing)},
        ],
    )

    assert message.photos == []
    assert message.documents == []


@pytest.mark.asyncio
async def test_send_outbound_media_skips_oversize_local(tmp_path, monkeypatch):
    media = tmp_path / "big.png"
    media.write_bytes(b"x")
    monkeypatch.setattr(handlers.config, "MEDIA_MAX_MB", 0)
    message = FakeMessage()

    await handlers._send_outbound_media(
        message,
        session_id="ses_1",
        directory=tmp_path,
        parts=[],
        diff_files=[{"path": str(media)}],
    )

    assert message.photos == []


@pytest.mark.asyncio
async def test_send_outbound_media_deduplicates_per_session(tmp_path):
    message = FakeMessage()
    parts = [
        {
            "type": "file",
            "url": _data_url("image/png", b"png"),
            "mime": "image/png",
            "filename": "pic.png",
        }
    ]

    await handlers._send_outbound_media(
        message, session_id="ses_1", directory=tmp_path, parts=parts, diff_files=[]
    )
    await handlers._send_outbound_media(
        message, session_id="ses_1", directory=tmp_path, parts=parts, diff_files=[]
    )
    assert len(message.photos) == 1

    other = FakeMessage()
    await handlers._send_outbound_media(
        other, session_id="ses_2", directory=tmp_path, parts=parts, diff_files=[]
    )
    assert len(other.photos) == 1


@pytest.mark.asyncio
async def test_send_outbound_media_failures_do_not_raise(tmp_path):
    message = FakeMessage()
    message.reply_photo = AsyncMock(side_effect=RuntimeError("send boom"))
    parts = [
        {
            "type": "file",
            "url": _data_url("image/png", b"png"),
            "mime": "image/png",
            "filename": "pic.png",
        }
    ]

    await handlers._send_outbound_media(
        message, session_id="ses_1", directory=tmp_path, parts=parts, diff_files=[]
    )

    bad = FakeMessage()
    bad_parts = [
        {
            "type": "file",
            "url": "data:image/png;base64,!!!not-base64!!!",
            "mime": "image/png",
            "filename": "bad.png",
        }
    ]
    await handlers._send_outbound_media(
        bad, session_id="ses_1", directory=tmp_path, parts=bad_parts, diff_files=[]
    )
    assert bad.photos == []
    assert bad.documents == []


@pytest.mark.asyncio
async def test_process_prompt_sends_outbound_media(monkeypatch, tmp_path):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.get_turn_assistant_parts = AsyncMock(
        return_value=[
            {
                "type": "file",
                "url": _data_url("image/png", b"png"),
                "mime": "image/png",
                "filename": "pic.png",
            }
        ]
    )

    update = _update(text="hi")
    await handlers.text_message(update, _context(client))

    assert "hello from opencode" in update.effective_message.edits
    assert len(update.effective_message.photos) == 1


@pytest.mark.asyncio
async def test_process_prompt_survives_media_delivery_error(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.get_turn_assistant_parts = AsyncMock(side_effect=RuntimeError("boom"))

    update = _update(text="hi")
    await handlers.text_message(update, _context(client))

    assert "hello from opencode" in update.effective_message.edits


@pytest.mark.asyncio
async def test_process_prompt_sends_patch_media_from_earlier_message(
    monkeypatch, tmp_path
):
    media = tmp_path / "shot.png"
    media.write_bytes(b"x")
    db = FakeDB(_row())
    db.workdirs[111] = str(tmp_path)
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.get_turn_assistant_parts = AsyncMock(
        return_value=[{"type": "patch", "files": [str(media)]}]
    )

    update = _update(text="make a screenshot")
    await handlers.text_message(update, _context(client))

    assert len(update.effective_message.photos) == 1

    second = _update(text="again")
    await handlers.text_message(second, _context(client))

    assert second.effective_message.photos == []


def _question_state(request_id="que_1", questions=None, index=0, directory=None):
    return {
        "request_id": request_id,
        "session_id": "ses_1",
        "directory": directory,
        "questions": questions
        or [
            {
                "question": "Proceed?",
                "header": "Confirm",
                "options": [{"label": "Yes"}, {"label": "No"}],
            }
        ],
        "index": index,
        "answers": [],
        "selections": set(),
        "awaiting_text": False,
        "chat_id": 111,
        "message_id": 5,
    }


def _all_callback_data(keyboard):
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


def test_build_question_keyboard_single_select():
    keyboard = handlers.build_question_keyboard(
        {"options": [{"label": "Yes"}, {"label": "No"}]},
        0,
        set(),
        multiple=False,
        custom=False,
    )

    data = _all_callback_data(keyboard)
    assert "q:a:0:0" in data
    assert "q:a:0:1" in data
    assert "q:r" in data
    assert "q:d:0" not in data
    assert "q:x:0" in data
    assert all(len(item.encode("utf-8")) <= 64 for item in data)


def test_build_question_keyboard_multiple_marks_selected():
    keyboard = handlers.build_question_keyboard(
        {"options": [{"label": "A"}, {"label": "B"}]},
        1,
        {"A"},
        multiple=True,
        custom=True,
    )

    data = _all_callback_data(keyboard)
    assert "q:t:1:0" in data
    assert "q:t:1:1" in data
    assert "q:d:1" in data
    assert "q:x:1" in data
    assert "q:r" in data
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert labels[0].startswith("✅")
    assert not labels[1].startswith("✅")
    assert all(len(item.encode("utf-8")) <= 64 for item in data)


def test_parse_question_callback_round_trips():
    assert handlers.parse_question_callback("q:a:0:1") == ("a", 0, 1)
    assert handlers.parse_question_callback("q:t:1:2") == ("t", 1, 2)
    assert handlers.parse_question_callback("q:d:0") == ("d", 0, None)
    assert handlers.parse_question_callback("q:x:0") == ("x", 0, None)
    assert handlers.parse_question_callback("q:r") == ("r", None, None)
    assert handlers.parse_question_callback("q:a:0") is None
    assert handlers.parse_question_callback("q:z:0:0") is None
    assert handlers.parse_question_callback("q:a:x:0") is None
    assert handlers.parse_question_callback("mdl:m0") is None
    assert handlers.parse_question_callback("") is None


@pytest.mark.asyncio
async def test_question_callback_single_select_submits(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    handlers.PENDING_QUESTIONS[111] = _question_state()
    context = _context(client)
    query = FakeQuery("q:a:0:0", from_user=SimpleNamespace(id=111))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.question_callback(update, context)

    client.reply_question.assert_awaited_once_with("que_1", [["Yes"]])
    assert 111 not in handlers.PENDING_QUESTIONS
    assert query.answer.await_count == 1
    assert context.bot.edit_message_text.await_args.kwargs["text"] == (
        handlers.QUESTION_SUBMITTED_TEXT
    )


@pytest.mark.asyncio
async def test_question_callback_multi_toggle_then_done(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    question = {
        "question": "Pick some",
        "options": [{"label": "A"}, {"label": "B"}],
        "multiple": True,
    }
    handlers.PENDING_QUESTIONS[111] = _question_state(questions=[question])
    context = _context(client)

    toggle_a = FakeQuery("q:t:0:0", from_user=SimpleNamespace(id=111))
    await handlers.question_callback(
        SimpleNamespace(
            callback_query=toggle_a, effective_user=SimpleNamespace(id=111)
        ),
        context,
    )
    assert toggle_a.answer.await_count == 1
    assert handlers.PENDING_QUESTIONS[111]["selections"] == {"A"}
    assert context.bot.edit_message_text.await_args.kwargs["reply_markup"] is not None

    toggle_b = FakeQuery("q:t:0:1", from_user=SimpleNamespace(id=111))
    await handlers.question_callback(
        SimpleNamespace(
            callback_query=toggle_b, effective_user=SimpleNamespace(id=111)
        ),
        context,
    )
    assert handlers.PENDING_QUESTIONS[111]["selections"] == {"A", "B"}

    done = FakeQuery("q:d:0", from_user=SimpleNamespace(id=111))
    await handlers.question_callback(
        SimpleNamespace(callback_query=done, effective_user=SimpleNamespace(id=111)),
        context,
    )
    assert done.answer.await_count == 1
    client.reply_question.assert_awaited_once_with("que_1", [["A", "B"]])
    assert 111 not in handlers.PENDING_QUESTIONS


@pytest.mark.asyncio
async def test_question_callback_custom_then_text_message(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    question = {"question": "Name?", "options": [], "custom": True}
    handlers.PENDING_QUESTIONS[111] = _question_state(questions=[question])
    context = _context(client)

    type_button = FakeQuery("q:x:0", from_user=SimpleNamespace(id=111))
    await handlers.question_callback(
        SimpleNamespace(
            callback_query=type_button, effective_user=SimpleNamespace(id=111)
        ),
        context,
    )
    assert handlers.PENDING_QUESTIONS[111]["awaiting_text"] is True

    update = _update(text="my free answer")
    await handlers.text_message(update, context)

    client.reply_question.assert_awaited_once_with("que_1", [["my free answer"]])
    client.send_prompt.assert_not_awaited()
    assert 111 not in handlers.PENDING_QUESTIONS


@pytest.mark.asyncio
async def test_question_callback_skip_rejects(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    handlers.PENDING_QUESTIONS[111] = _question_state()
    context = _context(client)
    query = FakeQuery("q:r", from_user=SimpleNamespace(id=111))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.question_callback(update, context)

    client.reject_question.assert_awaited_once_with("que_1")
    client.reply_question.assert_not_awaited()
    assert 111 not in handlers.PENDING_QUESTIONS
    assert query.answer.await_count == 1
    assert context.bot.edit_message_text.await_args.kwargs["text"] == (
        handlers.QUESTION_SKIPPED_TEXT
    )


@pytest.mark.asyncio
async def test_question_callback_expired_alerts(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    query = FakeQuery("q:r", from_user=SimpleNamespace(id=111))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.question_callback(update, _context(client))

    client.reject_question.assert_not_awaited()
    assert query.answer.await_count == 1
    assert query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_question_callback_unauthenticated_refused(monkeypatch):
    db = FakeDB(_row(authenticated=0))
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    handlers.PENDING_QUESTIONS[111] = _question_state()
    query = FakeQuery("q:r", from_user=SimpleNamespace(id=111))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.question_callback(update, _context(client))

    client.reject_question.assert_not_awaited()
    assert 111 in handlers.PENDING_QUESTIONS
    assert query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_question_asked_event_sends_message(monkeypatch):
    row = _row()
    db = FakeDB(row)
    db.sessions[1] = {"session_id": "ses_1", "directory": "D:/x"}
    monkeypatch.setattr(handlers, "database", db)
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=7))
    )
    event = {
        "type": "question.asked",
        "properties": {
            "id": "que_1",
            "sessionID": "ses_1",
            "questions": [{"question": "Proceed?", "options": [{"label": "Yes"}]}],
        },
    }

    await handlers.handle_event(event, bot=bot, client=FakeClient())

    bot.send_message.assert_awaited_once()
    assert 111 in handlers.PENDING_QUESTIONS
    assert handlers.PENDING_QUESTIONS[111]["message_id"] == 7
    data = _all_callback_data(
        bot.send_message.await_args.kwargs["reply_markup"]
    )
    assert "q:r" in data


@pytest.mark.asyncio
async def test_question_asked_event_ignores_unknown_session(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    bot = SimpleNamespace(send_message=AsyncMock())

    await handlers.handle_event(
        {
            "type": "question.asked",
            "properties": {"id": "que_1", "sessionID": "ses_unknown", "questions": []},
        },
        bot=bot,
        client=FakeClient(),
    )

    bot.send_message.assert_not_awaited()
    assert handlers.PENDING_QUESTIONS == {}


@pytest.mark.asyncio
async def test_question_replied_event_clears_state(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    handlers.PENDING_QUESTIONS[111] = _question_state()
    bot = SimpleNamespace(send_message=AsyncMock())

    await handlers.handle_event(
        {
            "type": "question.replied",
            "properties": {"sessionID": "ses_1", "requestID": "que_1"},
        },
        bot=bot,
        client=FakeClient(),
    )

    assert handlers.PENDING_QUESTIONS == {}


@pytest.mark.asyncio
async def test_reconcile_questions_surfaces_pending(monkeypatch):
    db = FakeDB(_row())
    db.session_rows = [
        {
            "session_id": "ses_1",
            "directory": "D:/x",
            "id": 1,
            "telegram_id": 111,
            "is_authenticated": 1,
        }
    ]
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.list_questions = AsyncMock(
        return_value=[
            {
                "id": "que_1",
                "sessionID": "ses_1",
                "questions": [{"question": "Proceed?", "options": [{"label": "Yes"}]}],
            }
        ]
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=9))
    )
    application = SimpleNamespace(bot_data={"opencode": client}, bot=bot)

    await handlers.reconcile_questions(application)

    client.list_questions.assert_awaited_once_with(directory="D:/x")
    assert 111 in handlers.PENDING_QUESTIONS
    assert handlers.PENDING_QUESTIONS[111]["directory"] == "D:/x"
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_questions_tolerates_client_errors(monkeypatch):
    from opencode_client import OpenCodeError

    db = FakeDB(_row())
    db.session_rows = [
        {
            "session_id": "ses_1",
            "directory": "D:/x",
            "id": 1,
            "telegram_id": 111,
            "is_authenticated": 1,
        }
    ]
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.list_questions = AsyncMock(side_effect=OpenCodeError("boom"))
    application = SimpleNamespace(bot_data={"opencode": client}, bot=object())

    await handlers.reconcile_questions(application)

    assert handlers.PENDING_QUESTIONS == {}


@pytest.mark.asyncio
async def test_reconcile_questions_skips_unauthenticated(monkeypatch):
    db = FakeDB(_row(authenticated=0))
    db.session_rows = [
        {
            "session_id": "ses_1",
            "directory": "D:/x",
            "id": 1,
            "telegram_id": 111,
            "is_authenticated": 0,
        }
    ]
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    application = SimpleNamespace(bot_data={"opencode": client}, bot=object())

    await handlers.reconcile_questions(application)

    client.list_questions.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_while_question_pending_is_consumed_as_custom_answer(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    handlers.PENDING_QUESTIONS[111] = _question_state()
    context = _context(client)

    update = _update(text="typed instead of tapping")
    await handlers.text_message(update, context)

    client.reply_question.assert_awaited_once_with(
        "que_1", [["typed instead of tapping"]]
    )
    client.send_prompt.assert_not_awaited()
    client.create_session.assert_not_awaited()
    assert 111 not in handlers.PENDING_QUESTIONS


@pytest.mark.asyncio
async def test_text_while_awaiting_text_is_consumed(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    state = _question_state(
        questions=[{"question": "Name?", "options": [], "custom": True}]
    )
    state["awaiting_text"] = True
    handlers.PENDING_QUESTIONS[111] = state

    update = _update(text="my typed answer")
    await handlers.text_message(update, _context(client))

    client.reply_question.assert_awaited_once_with("que_1", [["my typed answer"]])
    client.send_prompt.assert_not_awaited()
    assert 111 not in handlers.PENDING_QUESTIONS


@pytest.mark.asyncio
async def test_text_while_custom_question_pending_is_consumed(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    handlers.PENDING_QUESTIONS[111] = _question_state(
        questions=[{"question": "Name?", "options": [], "custom": True}]
    )

    update = _update(text="custom answer")
    await handlers.text_message(update, _context(client))

    client.reply_question.assert_awaited_once_with("que_1", [["custom answer"]])
    client.send_prompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_command_with_pending_question(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    handlers.PENDING_QUESTIONS[111] = _question_state()
    context = _context(client)

    update = _update()
    await handlers.status_command(update, context)

    combined = "\n".join(update.effective_message.sent)
    assert "❓ Waiting for your answer: Confirm" in combined
    context.bot.send_message.assert_awaited()
    client.get_session_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_present_question_same_request_edits_not_resends(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=3)),
        edit_message_text=AsyncMock(),
    )
    request = {
        "id": "que_1",
        "sessionID": "ses_1",
        "questions": [{"question": "Q?", "options": [{"label": "A"}]}],
    }

    await handlers.present_question(bot, db.row, request)
    await handlers.present_question(bot, db.row, request)

    assert bot.send_message.await_count == 1
    bot.edit_message_text.assert_awaited_once()
    assert handlers.PENDING_QUESTIONS[111]["request_id"] == "que_1"


@pytest.mark.asyncio
async def test_present_question_stores_directory(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=3)),
        edit_message_text=AsyncMock(),
    )
    request = {
        "id": "que_1",
        "sessionID": "ses_1",
        "questions": [{"question": "Q?", "options": [{"label": "A"}]}],
    }

    await handlers.present_question(bot, db.row, request, directory="D:/jcp")

    assert handlers.PENDING_QUESTIONS[111]["directory"] == "D:/jcp"


@pytest.mark.asyncio
async def test_question_callback_answer_passes_directory(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    handlers.PENDING_QUESTIONS[111] = _question_state(directory="D:/jcp")
    query = FakeQuery("q:a:0:0", from_user=SimpleNamespace(id=111))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.question_callback(update, _context(client))

    client.reply_question.assert_awaited_once_with(
        "que_1", [["Yes"]], directory="D:/jcp"
    )


@pytest.mark.asyncio
async def test_question_callback_skip_passes_directory(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    handlers.PENDING_QUESTIONS[111] = _question_state(directory="D:/jcp")
    query = FakeQuery("q:r", from_user=SimpleNamespace(id=111))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.question_callback(update, _context(client))

    client.reject_question.assert_awaited_once_with("que_1", directory="D:/jcp")


@pytest.mark.asyncio
async def test_present_question_newer_request_replaces_state(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=3)),
        edit_message_text=AsyncMock(),
    )
    first = {
        "id": "que_1",
        "sessionID": "ses_1",
        "questions": [{"question": "First?", "options": [{"label": "A"}]}],
    }
    second = {
        "id": "que_2",
        "sessionID": "ses_1",
        "questions": [{"question": "Second?", "options": [{"label": "B"}]}],
    }

    await handlers.present_question(bot, db.row, first)
    await handlers.present_question(bot, db.row, second)

    assert handlers.PENDING_QUESTIONS[111]["request_id"] == "que_2"
    assert bot.send_message.await_count == 2


def _html_edits(message):
    return [
        call
        for call in message.edit_text.await_args_list
        if call.kwargs.get("parse_mode") == "HTML"
    ]


@pytest.mark.asyncio
async def test_process_prompt_sends_markdown_as_html(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient(reply="**bold**\n\n```python\nprint('x')\n```")

    update = _update(text="hi")
    await handlers.text_message(update, _context(client))

    html_edits = _html_edits(update.effective_message)
    assert html_edits
    sent = html_edits[0].args[0]
    assert "<b>bold</b>" in sent
    assert '<pre><code class="language-python">' in sent


@pytest.mark.asyncio
async def test_process_prompt_falls_back_when_html_rejected(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient(reply="**bold**")

    update = _update(text="hi")
    message = update.effective_message
    calls: list[tuple] = []

    async def edit(text, **kwargs):
        calls.append((text, kwargs.get("parse_mode")))
        if kwargs.get("parse_mode") == "HTML":
            raise BadRequest("can't parse entities")
        message.edits.append(text)

    message.edit_text = AsyncMock(side_effect=edit)
    await handlers.text_message(update, _context(client))

    assert calls[0][1] == "HTML"
    assert calls[-1] == ("**bold**", None)
    assert "**bold**" in message.edits


@pytest.mark.asyncio
async def test_voice_mode_synthesizes_plain_text_but_sends_html(monkeypatch):
    db = FakeDB(_row(), voice=True)
    monkeypatch.setattr(handlers, "database", db)
    synthesize = AsyncMock()
    monkeypatch.setattr(handlers.tts, "synthesize", synthesize)
    client = FakeClient(reply="**Bold** and `code`")

    update = _update(text="hi")
    await handlers.text_message(update, _context(client))

    spoken = synthesize.await_args.args[0]
    assert "<" not in spoken
    assert "**" not in spoken
    assert "`" not in spoken
    assert "Bold" in spoken
    assert "code" in spoken

    html_edits = _html_edits(update.effective_message)
    assert html_edits
    assert "<b>Bold</b>" in html_edits[0].args[0]


@pytest.mark.asyncio
async def test_long_markdown_reply_splits_and_converts_each_chunk(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient(reply="A" + "**bold** " * 700)

    update = _update(text="hi")
    message = update.effective_message
    await handlers.text_message(update, _context(client))

    edit_html = _html_edits(message)
    reply_html = [
        call
        for call in message.reply_text.await_args_list
        if call.kwargs.get("parse_mode") == "HTML"
    ]
    assert len(edit_html) == 1
    assert len(reply_html) == 1
    assert "<b>bold</b>" in edit_html[0].args[0]
    assert "<b>bold</b>" in reply_html[0].args[0]


def test_request_event_refresh_sets_event():
    event = asyncio.Event()
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"refresh_events": event})
    )
    handlers.request_event_refresh(context)
    assert event.is_set()


def test_request_event_refresh_noop_when_absent():
    handlers.request_event_refresh(
        SimpleNamespace(application=SimpleNamespace(bot_data={}))
    )
    handlers.request_event_refresh(SimpleNamespace())
    handlers.request_event_refresh(SimpleNamespace(application=None))


@pytest.mark.asyncio
async def test_ensure_session_requests_event_refresh(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    event = asyncio.Event()
    client = FakeClient()
    context = SimpleNamespace(
        bot_data={"opencode": client},
        application=SimpleNamespace(bot_data={"refresh_events": event}),
    )

    session_id = await handlers.ensure_session(db.row, client, context=context)

    assert session_id == "ses_new"
    assert event.is_set()


@pytest.mark.asyncio
async def test_apply_workdir_requests_event_refresh(monkeypatch, tmp_path):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    event = asyncio.Event()
    client = FakeClient()
    context = SimpleNamespace(
        bot_data={"opencode": client},
        application=SimpleNamespace(bot_data={"refresh_events": event}),
    )

    error = await handlers._apply_workdir(context, db.row, str(tmp_path))

    assert error is None
    assert event.is_set()


@pytest.mark.asyncio
async def test_new_command_requests_event_refresh(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    event = asyncio.Event()
    context = SimpleNamespace(
        bot_data={"opencode": FakeClient()},
        bot=AsyncMock(),
        application=SimpleNamespace(bot_data={"refresh_events": event}),
    )

    update = _update()
    await handlers.new_command(update, context)

    assert event.is_set()


def test_build_auth_keyboard_callback_data_within_limit():
    keyboard = handlers.build_auth_keyboard(111)
    data = _keyboard_data(keyboard)
    assert data == ["auth:ok:111", "auth:no:111"]
    assert all(
        len(button.callback_data.encode("utf-8")) <= 64
        for row in keyboard.inline_keyboard
        for button in row
    )


def test_parse_auth_callback_round_trips():
    assert handlers.parse_auth_callback("auth:ok:111") == (True, 111)
    assert handlers.parse_auth_callback("auth:no:222") == (False, 222)
    assert handlers.parse_auth_callback("auth:ok:") is None
    assert handlers.parse_auth_callback("auth:maybe:1") is None
    assert handlers.parse_auth_callback("") is None


@pytest.mark.asyncio
async def test_notify_admin_new_user_includes_profile_and_sets_flag(monkeypatch):
    row = _row(authenticated=0)
    db = FakeDB(row)
    monkeypatch.setattr(handlers, "database", db)
    bot = FakeBot()

    await handlers.notify_admin_new_user(_context_with_bot(FakeClient(), bot), row)

    assert len(bot.messages) == 1
    sent = bot.messages[0]
    assert sent["chat_id"] == config.ADMIN_USER_ID
    assert "Alice A" in sent["text"]
    assert "@alice" in sent["text"]
    assert "111" in sent["text"]
    assert "en" in sent["text"]
    assert _keyboard_data(sent["reply_markup"]) == ["auth:ok:111", "auth:no:111"]
    assert db.approval_requested_at is not None


@pytest.mark.asyncio
async def test_notify_admin_new_user_skips_authenticated(monkeypatch):
    row = _row(authenticated=1)
    db = FakeDB(row)
    monkeypatch.setattr(handlers, "database", db)
    bot = FakeBot()

    await handlers.notify_admin_new_user(_context_with_bot(FakeClient(), bot), row)

    assert bot.messages == []
    assert db.approval_requested_at is None


@pytest.mark.asyncio
async def test_notify_admin_new_user_skips_already_requested(monkeypatch):
    row = _row(authenticated=0)
    row["approval_requested_at"] = "2026-01-01T00:00:00+00:00"
    db = FakeDB(row)
    monkeypatch.setattr(handlers, "database", db)
    bot = FakeBot()

    await handlers.notify_admin_new_user(_context_with_bot(FakeClient(), bot), row)

    assert bot.messages == []


@pytest.mark.asyncio
async def test_notify_admin_new_user_handles_forbidden(monkeypatch):
    row = _row(authenticated=0)
    db = FakeDB(row)
    monkeypatch.setattr(handlers, "database", db)
    bot = FakeBot(error=Forbidden("bot was blocked by the user"))

    await handlers.notify_admin_new_user(_context_with_bot(FakeClient(), bot), row)

    assert db.approval_requested_at is None


@pytest.mark.asyncio
async def test_request_access_notifies_and_replies(monkeypatch):
    row = _row(authenticated=0)
    db = FakeDB(row)
    monkeypatch.setattr(handlers, "database", db)
    bot = FakeBot()
    update = _update(text="/help")

    await handlers.request_access(
        update, _context_with_bot(FakeClient(), bot), row
    )

    assert handlers.ACCESS_PENDING_TEXT in update.effective_message.sent
    assert len(bot.messages) == 1


@pytest.mark.asyncio
async def test_guard_unauthenticated_blocks_and_notifies_admin(monkeypatch):
    row = _row(authenticated=0)
    db = FakeDB(row)
    monkeypatch.setattr(handlers, "database", db)
    bot = FakeBot()
    update = _update(text="/help")

    with pytest.raises(ApplicationHandlerStop):
        await handlers.guard_unauthenticated(
            update, _context_with_bot(FakeClient(), bot)
        )

    assert handlers.ACCESS_PENDING_TEXT in update.effective_message.sent
    assert len(bot.messages) == 1
    assert _keyboard_data(bot.messages[0]["reply_markup"]) == [
        "auth:ok:111",
        "auth:no:111",
    ]
    assert db.approval_requested_at is not None


@pytest.mark.asyncio
async def test_guard_unauthenticated_allows_start_and_id(monkeypatch):
    for text in ("/start", "/start@MyBot", "/id", "/id 5"):
        row = _row(authenticated=0)
        db = FakeDB(row)
        monkeypatch.setattr(handlers, "database", db)
        bot = FakeBot()
        update = _update(text=text)

        await handlers.guard_unauthenticated(
            update, _context_with_bot(FakeClient(), bot)
        )

        assert handlers.ACCESS_PENDING_TEXT not in update.effective_message.sent


@pytest.mark.asyncio
async def test_guard_unauthenticated_does_not_renotify(monkeypatch):
    row = _row(authenticated=0)
    db = FakeDB(row)
    monkeypatch.setattr(handlers, "database", db)
    bot = FakeBot()

    for _ in range(2):
        update = _update(text="/help")
        with pytest.raises(ApplicationHandlerStop):
            await handlers.guard_unauthenticated(
                update, _context_with_bot(FakeClient(), bot)
            )

    assert len(bot.messages) == 1


@pytest.mark.asyncio
async def test_guard_unauthenticated_renotifies_after_rejection(monkeypatch):
    row = _row(authenticated=0)
    db = FakeDB(row)
    monkeypatch.setattr(handlers, "database", db)
    bot = FakeBot()

    update = _update(text="/help")
    with pytest.raises(ApplicationHandlerStop):
        await handlers.guard_unauthenticated(
            update, _context_with_bot(FakeClient(), bot)
        )

    db.set_approval_requested(111, False)
    db.set_authenticated(111, False)

    update = _update(text="/help")
    with pytest.raises(ApplicationHandlerStop):
        await handlers.guard_unauthenticated(
            update, _context_with_bot(FakeClient(), bot)
        )

    assert len(bot.messages) == 2


@pytest.mark.asyncio
async def test_guard_unauthenticated_allows_authenticated(monkeypatch):
    row = _row(authenticated=1)
    db = FakeDB(row)
    monkeypatch.setattr(handlers, "database", db)
    bot = FakeBot()
    update = _update(text="/help")

    await handlers.guard_unauthenticated(
        update, _context_with_bot(FakeClient(), bot)
    )

    assert update.effective_message.sent == []
    assert bot.messages == []


@pytest.mark.asyncio
async def test_guard_unauthenticated_allows_admin(monkeypatch):
    row = _row(telegram_id=config.ADMIN_USER_ID, authenticated=0)
    db = FakeDB(row)
    monkeypatch.setattr(handlers, "database", db)
    bot = FakeBot()
    update = _update(text="/help", telegram_id=config.ADMIN_USER_ID)

    await handlers.guard_unauthenticated(
        update, _context_with_bot(FakeClient(), bot)
    )

    assert update.effective_message.sent == []
    assert bot.messages == []


def _server_state(step="url"):
    return {
        "step": step,
        "base_url": "http://new:4096",
        "label": None,
        "username": None,
        "password": None,
    }


@pytest.mark.asyncio
async def test_guard_clears_pending_server_input_for_command(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    handlers.PENDING_SERVER_INPUT[111] = _server_state()
    update = _update(text="/help")

    await handlers.guard_unauthenticated(
        update, _context_with_bot(FakeClient(), FakeBot())
    )

    assert 111 not in handlers.PENDING_SERVER_INPUT


@pytest.mark.asyncio
async def test_guard_keeps_pending_server_input_for_plain_text(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    handlers.PENDING_SERVER_INPUT[111] = _server_state()
    update = _update(text="http://new:4096")

    await handlers.guard_unauthenticated(
        update, _context_with_bot(FakeClient(), FakeBot())
    )

    assert handlers.PENDING_SERVER_INPUT.get(111) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/skip", "/cancel"])
async def test_guard_keeps_pending_server_input_for_skip_and_cancel(
    monkeypatch, command
):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    handlers.PENDING_SERVER_INPUT[111] = _server_state()
    update = _update(text=command)

    await handlers.guard_unauthenticated(
        update, _context_with_bot(FakeClient(), FakeBot())
    )

    assert handlers.PENDING_SERVER_INPUT.get(111) is not None


def _admin_update(data):
    query = FakeQuery(data, from_user=SimpleNamespace(id=config.ADMIN_USER_ID))
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=config.ADMIN_USER_ID),
    )


@pytest.mark.asyncio
async def test_auth_callback_approve_persists_and_dms(monkeypatch):
    row = _row(authenticated=0)
    db = FakeDB(row)
    db.set_approval_requested(111, True)
    monkeypatch.setattr(handlers, "database", db)
    bot = FakeBot()
    update = _admin_update("auth:ok:111")

    await handlers.auth_callback(update, _context_with_bot(FakeClient(), bot))

    assert row["is_authenticated"] == 1
    assert db.approval_requested_at is None
    update.callback_query.edit_message_text.assert_awaited_once()
    assert "✅ Approved 111" in update.callback_query.edit_message_text.await_args.args[0]
    assert len(bot.messages) == 1
    assert bot.messages[0]["chat_id"] == 111
    assert "approved" in bot.messages[0]["text"]


@pytest.mark.asyncio
async def test_auth_callback_reject_persists_and_dms(monkeypatch):
    row = _row(authenticated=1)
    db = FakeDB(row)
    db.set_approval_requested(111, True)
    row["is_authenticated"] = 0
    monkeypatch.setattr(handlers, "database", db)
    bot = FakeBot()
    update = _admin_update("auth:no:111")

    await handlers.auth_callback(update, _context_with_bot(FakeClient(), bot))

    assert row["is_authenticated"] == 0
    assert db.approval_requested_at is None
    assert "🚫 Rejected 111" in update.callback_query.edit_message_text.await_args.args[0]
    assert bot.messages[0]["chat_id"] == 111


@pytest.mark.asyncio
async def test_auth_callback_non_admin_is_refused(monkeypatch):
    row = _row(authenticated=0)
    db = FakeDB(row)
    monkeypatch.setattr(handlers, "database", db)
    bot = FakeBot()
    query = FakeQuery("auth:ok:111", from_user=SimpleNamespace(id=111))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.auth_callback(update, _context_with_bot(FakeClient(), bot))

    assert row["is_authenticated"] == 0
    query.edit_message_text.assert_not_awaited()
    assert bot.messages == []
    assert query.answer.await_count == 2
    assert query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_auth_callback_unknown_user_is_handled(monkeypatch):
    row = _row(authenticated=0)
    db = FakeDB(row)
    monkeypatch.setattr(handlers, "database", db)
    monkeypatch.setattr(db, "get_user_by_telegram_id", lambda telegram_id: None)
    bot = FakeBot()
    update = _admin_update("auth:ok:999")

    await handlers.auth_callback(update, _context_with_bot(FakeClient(), bot))

    assert bot.messages == []
    update.callback_query.edit_message_text.assert_awaited_once()
    assert "999" in update.callback_query.edit_message_text.await_args.args[0]


def test_normalize_server_url_accepts_http_and_strips_slash():
    assert (
        handlers.normalize_server_url("http://localhost:4096")
        == "http://localhost:4096"
    )
    assert (
        handlers.normalize_server_url(" http://localhost:4096/ ")
        == "http://localhost:4096"
    )
    assert (
        handlers.normalize_server_url("https://example.com/oc/")
        == "https://example.com/oc"
    )


def test_normalize_server_url_rejects_invalid():
    assert handlers.normalize_server_url("") is None
    assert handlers.normalize_server_url("   ") is None
    assert handlers.normalize_server_url("localhost:4096") is None
    assert handlers.normalize_server_url("ftp://example.com") is None
    assert handlers.normalize_server_url("http://") is None
    assert handlers.normalize_server_url("http://a b:4096") is None


def test_build_servers_keyboard_marks_active_and_adds_button():
    servers = [
        {"id": 1, "label": "Local", "base_url": "http://localhost:4096"},
        {
            "id": 2,
            "label": "Remote",
            "base_url": "http://host.docker.internal:4096",
        },
    ]
    keyboard = handlers.build_servers_keyboard(servers, 2)

    data = _keyboard_data(keyboard)
    assert "srv:use:1" in data
    assert "srv:use:2" in data
    assert "srv:add" in data
    assert "srv:rm:1" not in data
    labels = _keyboard_labels(keyboard)
    assert labels[0] == "Local · localhost:4096"
    assert labels[1].startswith("✅ ")
    _assert_callback_data_within_limit(keyboard)


def test_build_servers_keyboard_admin_shows_remove():
    servers = [{"id": 1, "label": "Local", "base_url": "http://localhost:4096"}]
    keyboard = handlers.build_servers_keyboard(servers, 1, is_admin=True)
    assert "srv:rm:1" in _keyboard_data(keyboard)


def test_server_label_falls_back_to_host():
    assert (
        handlers.server_label({"label": "Local", "base_url": "http://localhost:4096"})
        == "Local · localhost:4096"
    )
    assert handlers.server_label({"label": "", "base_url": "http://a:1"}) == "a:1"


@pytest.mark.asyncio
async def test_server_command_lists_with_active_marked(monkeypatch):
    db = FakeDB(_row())
    first = db.add_server("Local", "http://localhost:4096")
    second = db.add_server("Remote", "http://host.docker.internal:4096")
    db.set_user_server(111, second["id"])
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.server_command(update, _command_context(FakeClient(), args=[]))

    markup = update.effective_message.reply_markups[-1]
    assert markup is not None
    data = _keyboard_data(markup)
    assert "srv:use:1" in data
    assert "srv:use:2" in data
    assert "srv:add" in data


@pytest.mark.asyncio
async def test_server_command_empty_registry(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.server_command(update, _command_context(FakeClient(), args=[]))

    assert handlers.SERVER_NONE_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_server_command_add_starts_credential_flow(monkeypatch):
    db = FakeDB(_row())
    db.add_server("Local", "http://localhost:4096")
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.server_command(
        update,
        _command_context(FakeClient(), args=["add", "http://new:4096", "New"]),
    )

    state = handlers.PENDING_SERVER_INPUT[111]
    assert state["step"] == "username"
    assert state["base_url"] == "http://new:4096"
    assert state["label"] == "New"
    assert state["username"] is None
    assert db.get_server_by_url("http://new:4096") is None
    assert (
        handlers.SERVER_USERNAME_PROMPT_TEXT in update.effective_message.sent
    )


@pytest.mark.asyncio
async def test_server_command_add_invalid_url_persists_nothing(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.server_command(
        update, _command_context(FakeClient(), args=["add", "not-a-url"])
    )

    assert db.list_servers() == []
    assert 111 not in handlers.PENDING_SERVER_INPUT
    assert handlers.SERVER_INVALID_URL_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_server_command_add_unhealthy_persists_nothing(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    _patch_check_server(monkeypatch, False)

    update = _update(text="pw")
    handlers.PENDING_SERVER_INPUT[111] = {
        "step": "password",
        "base_url": "http://dead:4096",
        "label": None,
        "username": "alice",
        "password": None,
    }
    await handlers.text_message(update, _context(FakeClient()))

    assert db.list_servers() == []
    assert 111 not in handlers.PENDING_SERVER_INPUT
    assert any("Cannot reach" in text for text in update.effective_message.sent)


@pytest.mark.asyncio
async def test_check_server_does_not_block_event_loop(monkeypatch):
    db = FakeDB(_row())
    db.add_server("Local", "http://localhost:4096")
    monkeypatch.setattr(handlers, "database", db)
    order: list[str] = []

    async def slow_health(base_url, username, password):
        await asyncio.sleep(0.2)
        order.append("health")
        return True

    monkeypatch.setattr(handlers, "_check_server", slow_health)

    async def competitor():
        await asyncio.sleep(0.01)
        order.append("competitor")

    task = asyncio.create_task(competitor())
    update = _update(text="pw")
    handlers.PENDING_SERVER_INPUT[111] = {
        "step": "password",
        "base_url": "http://new:4096",
        "label": "New",
        "username": "alice",
        "password": None,
    }
    await handlers.text_message(update, _context(FakeClient()))
    await task

    assert order == ["competitor", "health"]
    assert db.get_server_by_url("http://new:4096") is not None


@pytest.mark.asyncio
async def test_server_command_use_switches_and_deletes_session(monkeypatch):
    db = FakeDB(_row())
    first = db.add_server("Local", "http://localhost:4096")
    second = db.add_server("Remote", "http://host.docker.internal:4096")
    db.set_user_server(111, first["id"])
    db.sessions[1] = {
        "session_id": "ses_old",
        "directory": "D:/repo",
        "server_id": first["id"],
    }
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.server_command(
        update, _command_context(FakeClient(), args=["use", str(second["id"])])
    )

    assert db.user_servers[111] == second["id"]
    assert 1 not in db.sessions
    assert any("Switched to" in text for text in update.effective_message.sent)


@pytest.mark.asyncio
async def test_server_command_use_same_server_keeps_session(monkeypatch):
    db = FakeDB(_row())
    first = db.add_server("Local", "http://localhost:4096")
    db.set_user_server(111, first["id"])
    db.sessions[1] = {
        "session_id": "ses_old",
        "directory": "D:/repo",
        "server_id": first["id"],
    }
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.server_command(
        update, _command_context(FakeClient(), args=["use", str(first["id"])])
    )

    assert 1 in db.sessions
    assert any("Already using" in text for text in update.effective_message.sent)


@pytest.mark.asyncio
async def test_server_command_remove_requires_admin(monkeypatch):
    db = FakeDB(_row())
    first = db.add_server("Local", "http://localhost:4096")
    second = db.add_server("Remote", "http://host.docker.internal:4096")
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.server_command(
        update, _command_context(FakeClient(), args=["remove", str(second["id"])])
    )

    assert db.get_server(second["id"]) is not None
    assert handlers.SERVER_ADMIN_ONLY_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_server_command_remove_as_admin(monkeypatch):
    admin_id = config.ADMIN_USER_ID
    db = FakeDB(_row(telegram_id=admin_id))
    first = db.add_server("Local", "http://localhost:4096")
    second = db.add_server("Remote", "http://host.docker.internal:4096")
    monkeypatch.setattr(handlers, "database", db)

    update = _update(telegram_id=admin_id)
    await handlers.server_command(
        update, _command_context(FakeClient(), args=["remove", str(second["id"])])
    )

    assert db.get_server(second["id"]) is None
    assert any("Removed" in text for text in update.effective_message.sent)


@pytest.mark.asyncio
async def test_server_command_usage_for_unknown_action(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.server_command(
        update, _command_context(FakeClient(), args=["bogus"])
    )

    assert handlers.SERVER_USAGE_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_server_callback_add_sets_pending_input(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    query = FakeQuery("srv:add", from_user=SimpleNamespace(id=111))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.server_callback(update, _context(FakeClient()))

    assert handlers.PENDING_SERVER_INPUT[111]["step"] == "url"
    query.answer.assert_awaited()
    query.edit_message_text.assert_awaited_once()
    assert (
        query.edit_message_text.await_args.args[0] == handlers.SERVER_ADD_PROMPT_TEXT
    )


@pytest.mark.asyncio
async def test_server_callback_use_switches(monkeypatch):
    db = FakeDB(_row())
    first = db.add_server("Local", "http://localhost:4096")
    second = db.add_server("Remote", "http://host.docker.internal:4096")
    db.set_user_server(111, first["id"])
    db.sessions[1] = {
        "session_id": "ses_old",
        "directory": "D:/repo",
        "server_id": first["id"],
    }
    monkeypatch.setattr(handlers, "database", db)
    query = FakeQuery(f"srv:use:{second['id']}", from_user=SimpleNamespace(id=111))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.server_callback(update, _context(FakeClient()))

    assert db.user_servers[111] == second["id"]
    assert 1 not in db.sessions
    query.answer.assert_awaited()
    query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_server_callback_remove_requires_admin(monkeypatch):
    db = FakeDB(_row())
    first = db.add_server("Local", "http://localhost:4096")
    second = db.add_server("Remote", "http://host.docker.internal:4096")
    monkeypatch.setattr(handlers, "database", db)
    query = FakeQuery(f"srv:rm:{second['id']}", from_user=SimpleNamespace(id=111))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.server_callback(update, _context(FakeClient()))

    assert db.get_server(second["id"]) is not None
    query.edit_message_text.assert_not_awaited()
    assert query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_server_callback_remove_as_admin(monkeypatch):
    admin_id = config.ADMIN_USER_ID
    db = FakeDB(_row(telegram_id=admin_id))
    first = db.add_server("Local", "http://localhost:4096")
    second = db.add_server("Remote", "http://host.docker.internal:4096")
    monkeypatch.setattr(handlers, "database", db)
    query = FakeQuery(
        f"srv:rm:{second['id']}", from_user=SimpleNamespace(id=admin_id)
    )
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=admin_id)
    )

    await handlers.server_callback(update, _context(FakeClient()))

    assert db.get_server(second["id"]) is None
    query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_server_callback_unauthenticated_refused(monkeypatch):
    db = FakeDB(_row(authenticated=0))
    monkeypatch.setattr(handlers, "database", db)
    query = FakeQuery("srv:add", from_user=SimpleNamespace(id=111))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )

    await handlers.server_callback(update, _context(FakeClient()))

    assert handlers.PENDING_SERVER_INPUT.get(111) is None
    query.edit_message_text.assert_not_awaited()
    assert query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_text_message_consumes_pending_server_url(monkeypatch):
    db = FakeDB(_row())
    db.add_server("Local", "http://localhost:4096")
    monkeypatch.setattr(handlers, "database", db)
    _patch_check_server(monkeypatch, True)
    handlers.PENDING_SERVER_INPUT[111] = {
        "step": "url",
        "base_url": None,
        "label": None,
        "username": None,
        "password": None,
    }

    update = _update(text="http://new:4096")
    await handlers.text_message(update, _context(FakeClient()))

    assert handlers.PENDING_SERVER_INPUT[111]["step"] == "username"
    assert handlers.PENDING_SERVER_INPUT[111]["base_url"] == "http://new:4096"
    assert db.get_server_by_url("http://new:4096") is None


@pytest.mark.asyncio
async def test_text_message_invalid_url_at_url_step_keeps_flow(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    handlers.PENDING_SERVER_INPUT[111] = {
        "step": "url",
        "base_url": None,
        "label": None,
        "username": None,
        "password": None,
    }
    client = FakeClient()

    update = _update(text="nope")
    await handlers.text_message(update, _context(client))

    assert handlers.PENDING_SERVER_INPUT[111]["step"] == "url"
    assert handlers.SERVER_INVALID_URL_TEXT in update.effective_message.sent
    assert db.list_servers() == []
    client.send_prompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_question_takes_precedence_over_pending_server_input(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    handlers.PENDING_QUESTIONS[111] = _question_state()
    handlers.PENDING_SERVER_INPUT[111] = {
        "step": "url",
        "base_url": None,
        "label": None,
        "username": None,
        "password": None,
    }

    update = _update(text="my answer")
    await handlers.text_message(update, _context(client))

    client.reply_question.assert_awaited_once_with("que_1", [["my answer"]])
    assert 111 not in handlers.PENDING_QUESTIONS
    assert handlers.PENDING_SERVER_INPUT[111]["step"] == "url"


def test_client_returns_default_when_server_matches():
    default = FakeClient()
    context = SimpleNamespace(bot_data={"opencode": default}, bot=AsyncMock())
    server = {
        "id": 1,
        "label": "L",
        "base_url": handlers.config.OPENCODE_BASE_URL,
    }

    assert handlers._client(context, server) is default
    assert handlers._client(context, None) is default


def test_client_returns_registry_client_for_custom_server(monkeypatch):
    created: list[str] = []

    class _StubClient:
        def __init__(self, base_url=None, **kwargs):
            self.base_url = base_url
            created.append(base_url)

    monkeypatch.setattr(handlers, "OpenCodeClient", _StubClient)
    default = FakeClient()
    context = SimpleNamespace(bot_data={"opencode": default}, bot=AsyncMock())
    server = {"id": 2, "label": "R", "base_url": "http://other:4096"}

    client = handlers._client(context, server)
    assert client is not default
    assert client.base_url == "http://other:4096"
    assert context.bot_data["opencode_clients"]["http://other:4096"] is client

    assert handlers._client(context, server) is client
    assert created == ["http://other:4096"]


def test_server_id_for_client_resolves_registered_server(monkeypatch):
    db = FakeDB(_row())
    server = db.add_server("Local", "http://localhost:4096")
    monkeypatch.setattr(handlers, "database", db)

    assert handlers._server_id_for_client(FakeClient()) == server["id"]


@pytest.mark.asyncio
async def test_permission_event_records_server_base_url(monkeypatch):
    row = _row()
    db = FakeDB(row)
    db.get_user_by_session_id = lambda session_id: row
    monkeypatch.setattr(handlers, "database", db)
    bot = SimpleNamespace(send_message=AsyncMock())
    client = FakeClient()
    client.base_url = "http://other:4096"
    event = {
        "type": "permission.asked",
        "properties": {"id": "per_9", "sessionID": "ses_1", "permission": "bash"},
    }

    await handlers.handle_event(event, bot=bot, client=client)

    assert handlers.PERMISSION_SERVERS["per_9"] == "http://other:4096"


@pytest.mark.asyncio
async def test_process_prompt_resolves_client_inside_lock(monkeypatch):
    db = FakeDB(_row())
    server = db.add_server("Local", "http://localhost:4096")
    db.set_user_server(111, int(server["id"]))
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    real_lock = asyncio.Lock()
    observed: list[tuple[str, bool]] = []

    class RecordingLock:
        async def __aenter__(self):
            await real_lock.acquire()
            observed.append(("lock", True))

        async def __aexit__(self, *exc):
            real_lock.release()

    monkeypatch.setattr(handlers, "_lock_for", lambda telegram_id: RecordingLock())

    def fake_client(context, server_row=None):
        observed.append(("client", real_lock.locked()))
        return client

    monkeypatch.setattr(handlers, "_client", fake_client)

    update = _update(text="hi")
    await handlers._process_prompt(update, _context(client), db.row, "hi")

    assert observed == [("lock", True), ("client", True)]


def test_parse_server_url_with_embedded_credentials():
    url, username, password = handlers.parse_server_url_with_credentials(
        "http://alice:s3cret@host:4096"
    )
    assert url == "http://host:4096"
    assert username == "alice"
    assert password == "s3cret"


def test_parse_server_url_without_credentials():
    assert handlers.parse_server_url_with_credentials("http://host:4096/") == (
        "http://host:4096",
        None,
        None,
    )
    assert handlers.parse_server_url_with_credentials("nope") is None
    assert handlers.parse_server_url_with_credentials("ftp://host") is None


def test_server_credentials_label_masks_secret():
    assert (
        handlers.server_credentials_label(
            {"username": "alice", "password": "s3cret"}
        )
        == " · 🔒 alice"
    )
    assert (
        handlers.server_credentials_label({"username": None, "password": "tok"})
        == " · 🔒 token"
    )
    assert handlers.server_credentials_label({"username": None, "password": None}) == ""
    assert (
        "s3cret"
        not in handlers.server_credentials_label(
            {"username": "alice", "password": "s3cret"}
        )
    )
    assert (
        "s3cr3t-token-value"
        not in handlers.server_credentials_label(
            {"username": None, "password": "s3cr3t-token-value"}
        )
    )


def test_server_label_includes_masked_credentials():
    row = {
        "label": "Local",
        "base_url": "http://h:1",
        "username": "alice",
        "password": "s3cret",
    }
    assert handlers.server_label(row) == "Local · h:1 · 🔒 alice"


@pytest.mark.asyncio
async def test_server_command_list_masks_credentials(monkeypatch):
    db = FakeDB(_row())
    db.add_server(
        "Local", "http://localhost:4096", username="alice", password="s3cret"
    )
    monkeypatch.setattr(handlers, "database", db)

    update = _update()
    await handlers.server_command(
        update, _command_context(FakeClient(), args=["list"])
    )

    labels = _keyboard_labels(update.effective_message.reply_markups[-1])
    assert any("🔒 alice" in label for label in labels)
    assert not any("s3cret" in label for label in labels)
    assert "s3cret" not in "\n".join(update.effective_message.sent)


@pytest.mark.asyncio
async def test_server_add_flow_basic_persists_and_selects(monkeypatch):
    db = FakeDB(_row())
    db.add_server("Local", "http://localhost:4096")
    monkeypatch.setattr(handlers, "database", db)
    calls: list = []
    _patch_check_server(monkeypatch, True, calls)
    context = _context(FakeClient())
    handlers.PENDING_SERVER_INPUT[111] = {
        "step": "url",
        "base_url": None,
        "label": "New",
        "username": None,
        "password": None,
    }

    url_update = _update(text="http://new:4096")
    await handlers.text_message(url_update, context)
    assert handlers.PENDING_SERVER_INPUT[111]["step"] == "username"
    assert (
        handlers.SERVER_USERNAME_PROMPT_TEXT in url_update.effective_message.sent
    )

    user_update = _update(text="alice")
    await handlers.text_message(user_update, context)
    assert handlers.PENDING_SERVER_INPUT[111]["step"] == "password"
    assert (
        handlers.SERVER_PASSWORD_PROMPT_TEXT in user_update.effective_message.sent
    )

    pass_update = _update(text="s3cret")
    await handlers.text_message(pass_update, context)

    assert 111 not in handlers.PENDING_SERVER_INPUT
    created = db.get_server_by_url("http://new:4096")
    assert created is not None
    assert created["username"] == "alice"
    assert created["password"] == "s3cret"
    assert db.user_servers[111] == created["id"]
    assert calls == [("http://new:4096", "alice", "s3cret")]
    pass_update.effective_message.delete.assert_awaited_once()
    assert not any("s3cret" in text for text in pass_update.effective_message.sent)
    assert any("🔒 alice" in text for text in pass_update.effective_message.sent)


@pytest.mark.asyncio
async def test_server_add_flow_skip_username_uses_bearer(monkeypatch):
    db = FakeDB(_row())
    db.add_server("Local", "http://localhost:4096")
    monkeypatch.setattr(handlers, "database", db)
    calls: list = []
    _patch_check_server(monkeypatch, True, calls)
    context = _context(FakeClient())
    handlers.PENDING_SERVER_INPUT[111] = {
        "step": "url",
        "base_url": None,
        "label": None,
        "username": None,
        "password": None,
    }

    await handlers.text_message(_update(text="http://new:4096"), context)
    await handlers.text_message(_update(text="/skip"), context)
    assert handlers.PENDING_SERVER_INPUT[111]["step"] == "token"

    await handlers.text_message(_update(text="tok123"), context)

    created = db.get_server_by_url("http://new:4096")
    assert created is not None
    assert created["username"] is None
    assert created["password"] == "tok123"
    assert calls == [("http://new:4096", None, "tok123")]


@pytest.mark.asyncio
async def test_server_add_flow_skip_at_token_means_no_auth(monkeypatch):
    db = FakeDB(_row())
    db.add_server("Local", "http://localhost:4096")
    monkeypatch.setattr(handlers, "database", db)
    calls: list = []
    _patch_check_server(monkeypatch, True, calls)
    context = _context(FakeClient())
    handlers.PENDING_SERVER_INPUT[111] = {
        "step": "url",
        "base_url": None,
        "label": None,
        "username": None,
        "password": None,
    }

    await handlers.text_message(_update(text="http://new:4096"), context)
    await handlers.text_message(_update(text="/skip"), context)
    await handlers.text_message(_update(text="/skip"), context)

    created = db.get_server_by_url("http://new:4096")
    assert created is not None
    assert created["username"] is None
    assert created["password"] is None
    assert calls == [("http://new:4096", None, None)]


@pytest.mark.asyncio
async def test_server_add_flow_cancel_command_clears(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    handlers.PENDING_SERVER_INPUT[111] = _server_state("username")

    update = _update(text="/cancel")
    await handlers.cancel_command(update, _command_context(FakeClient()))

    assert 111 not in handlers.PENDING_SERVER_INPUT
    assert handlers.SERVER_CANCELLED_TEXT in update.effective_message.sent
    assert db.list_servers() == []


@pytest.mark.asyncio
async def test_server_add_flow_cancel_literal_clears(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    handlers.PENDING_SERVER_INPUT[111] = _server_state("url")

    update = _update(text="cancel")
    await handlers.text_message(update, _context(FakeClient()))

    assert 111 not in handlers.PENDING_SERVER_INPUT
    assert handlers.SERVER_CANCELLED_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_skip_command_without_flow(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)

    update = _update(text="/skip")
    await handlers.skip_command(update, _command_context(FakeClient()))

    assert handlers.SERVER_NOTHING_TO_SKIP_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_cancel_command_without_flow(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)

    update = _update(text="/cancel")
    await handlers.cancel_command(update, _command_context(FakeClient()))

    assert handlers.SERVER_NOTHING_TO_CANCEL_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_skip_command_advances_username_to_token(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    handlers.PENDING_SERVER_INPUT[111] = {
        "step": "username",
        "base_url": "http://new:4096",
        "label": None,
        "username": None,
        "password": None,
    }

    update = _update(text="/skip")
    await handlers.skip_command(update, _command_context(FakeClient()))

    assert handlers.PENDING_SERVER_INPUT[111]["step"] == "token"
    assert handlers.SERVER_TOKEN_PROMPT_TEXT in update.effective_message.sent


@pytest.mark.asyncio
async def test_server_add_url_step_extracts_embedded_credentials(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    handlers.PENDING_SERVER_INPUT[111] = {
        "step": "url",
        "base_url": None,
        "label": None,
        "username": None,
        "password": None,
    }

    update = _update(text="http://bob:pw@host:4096")
    await handlers.text_message(update, _context(FakeClient()))

    state = handlers.PENDING_SERVER_INPUT[111]
    assert state["base_url"] == "http://host:4096"
    assert state["username"] == "bob"
    assert state["password"] == "pw"
    assert state["step"] == "username"
    assert not any("pw" in text for text in update.effective_message.sent)


@pytest.mark.asyncio
async def test_finalize_server_add_invalidates_client_cache(monkeypatch):
    db = FakeDB(_row())
    db.add_server("Local", "http://localhost:4096")
    monkeypatch.setattr(handlers, "database", db)
    _patch_check_server(monkeypatch, True)
    context = _context(FakeClient())
    stale = object()
    context.bot_data["opencode_clients"] = {"http://new:4096": stale}
    handlers.PENDING_SERVER_INPUT[111] = {
        "step": "password",
        "base_url": "http://new:4096",
        "label": None,
        "username": "alice",
        "password": None,
    }

    await handlers.text_message(_update(text="pw"), context)

    assert "http://new:4096" not in context.bot_data["opencode_clients"]


@pytest.mark.asyncio
async def test_remove_server_invalidates_client_cache(monkeypatch):
    admin_id = config.ADMIN_USER_ID
    db = FakeDB(_row(telegram_id=admin_id))
    db.add_server("Local", "http://localhost:4096")
    second = db.add_server("Remote", "http://remote:4096")
    monkeypatch.setattr(handlers, "database", db)
    context = _command_context(
        FakeClient(), args=["remove", str(second["id"])]
    )
    context.bot_data["opencode_clients"] = {"http://remote:4096": object()}

    await handlers.server_command(
        _update(telegram_id=admin_id), context
    )

    assert "http://remote:4096" not in context.bot_data["opencode_clients"]


def test_client_for_base_url_uses_stored_credentials(monkeypatch):
    db = FakeDB(_row())
    db.add_server(
        "R", "http://other:4096", username="alice", password="s3cret"
    )
    monkeypatch.setattr(handlers, "database", db)
    captured: dict = {}

    class _StubClient:
        def __init__(self, base_url=None, **kwargs):
            self.base_url = base_url
            captured["base_url"] = base_url
            captured["username"] = kwargs.get("username")
            captured["password"] = kwargs.get("password")

    monkeypatch.setattr(handlers, "OpenCodeClient", _StubClient)
    default = FakeClient()
    context = SimpleNamespace(bot_data={"opencode": default}, bot=AsyncMock())
    server = {"id": 2, "label": "R", "base_url": "http://other:4096"}

    client = handlers._client(context, server)

    assert client is not default
    assert captured == {
        "base_url": "http://other:4096",
        "username": "alice",
        "password": "s3cret",
    }


@pytest.mark.asyncio
async def test_check_server_uses_credentials(monkeypatch):
    captured: dict = {}

    class _StubClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def health(self):
            return True

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(handlers, "OpenCodeClient", _StubClient)

    assert await handlers._check_server("http://x", "u", "p") is True
    assert captured["base_url"] == "http://x"
    assert captured["username"] == "u"
    assert captured["password"] == "p"
    assert captured["timeout"] == 5.0
    assert captured["closed"] is True


@pytest.mark.asyncio
async def test_server_add_logging_never_includes_secret(monkeypatch, caplog):
    db = FakeDB(_row())
    db.add_server("Local", "http://localhost:4096")
    monkeypatch.setattr(handlers, "database", db)
    _patch_check_server(monkeypatch, True)
    context = _context(FakeClient())
    handlers.PENDING_SERVER_INPUT[111] = {
        "step": "password",
        "base_url": "http://new:4096",
        "label": None,
        "username": "alice",
        "password": None,
    }

    with caplog.at_level(logging.INFO):
        await handlers.text_message(_update(text="topsecret"), context)

    assert "topsecret" not in caplog.text
