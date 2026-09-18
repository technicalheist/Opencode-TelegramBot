from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
        self.get_session_diff = AsyncMock(return_value=[])
        self.list_questions = AsyncMock(return_value=[])
        self.reply_question = AsyncMock()
        self.reject_question = AsyncMock()


class FakeDB:
    def __init__(self, row, *, voice=False):
        self.row = row
        self.voice = voice
        self.sessions: dict[int, dict] = {}
        self.workdirs: dict[int, str] = {}
        self.models: dict[int, tuple[str, str]] = {}
        self.media: dict[int, dict] = {}
        self.session_rows: list[dict] = []

    def upsert_user(self, *args, **kwargs):
        return self.row

    def get_user_by_telegram_id(self, telegram_id):
        return self.row

    def get_voice_mode(self, telegram_id):
        return self.voice

    def set_voice_mode(self, telegram_id, enabled):
        self.voice = enabled
        return True

    def get_workdir(self, telegram_id):
        return self.workdirs.get(telegram_id)

    def set_workdir(self, telegram_id, directory):
        self.workdirs[telegram_id] = directory

    def get_model(self, telegram_id):
        return self.models.get(telegram_id)

    def set_model(self, telegram_id, provider_id, model_id):
        self.models[telegram_id] = (provider_id, model_id)

    def get_opencode_session(self, user_id):
        return self.sessions.get(user_id)

    def set_opencode_session(self, user_id, session_id, directory):
        self.sessions[user_id] = {
            "session_id": session_id,
            "directory": directory,
        }

    def delete_opencode_session(self, user_id):
        return self.sessions.pop(user_id, None) is not None

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
    }


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
    yield
    handlers.USER_LOCKS.clear()
    handlers.MODEL_CHOICES.clear()
    handlers.MODEL_LABELS.clear()
    handlers.WORKDIR_BROWSE.clear()
    handlers.ACTIVE_TASKS.clear()
    handlers.SENT_MEDIA.clear()
    handlers.PENDING_QUESTIONS.clear()


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
    db.sessions[1] = {"session_id": "ses_existing", "directory": "D:/repo"}
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    session_id = await handlers.ensure_session(db.row, client)

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


def test_current_directory_uses_stored_workdir(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)

    assert handlers.current_directory(db.row) == str(handlers.config.OPENCODE_DIRECTORY)

    db.workdirs[111] = "D:/projects/demo"
    assert handlers.current_directory(db.row) == "D:/projects/demo"


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
async def test_workdir_rejects_non_directory(monkeypatch):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    update = _update()
    await handlers.workdir_command(
        update, _command_context(client, args=["Z:/does/not/exist/at/all"])
    )

    assert not db.workdirs
    client.create_session.assert_not_awaited()
    assert any(
        handlers.WORKDIR_INVALID_TEXT.format(path="Z:\\does\\not\\exist\\at\\all")
        in text
        or "not a directory" in text
        for text in update.effective_message.sent
    )


@pytest.mark.asyncio
async def test_workdir_command_opens_browser_without_applying(monkeypatch, tmp_path):
    (tmp_path / "child").mkdir()
    (tmp_path / "note.txt").write_text("hi", encoding="utf-8")
    db = FakeDB(_row())
    db.models[111] = ("opencode-go", "deepseek")
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    update = _update()
    await handlers.workdir_command(
        update, _command_context(client, args=[str(tmp_path)])
    )

    assert db.workdirs == {}
    client.create_session.assert_not_awaited()
    assert handlers.WORKDIR_BROWSE[111]["path"] == str(tmp_path.resolve())
    markup = update.effective_message.reply_markups[-1]
    data = [
        button.callback_data for row in markup.inline_keyboard for button in row
    ]
    assert "wdir:use" in data
    assert any(text.startswith(f"📂 {str(tmp_path.resolve())}") for text in update.effective_message.sent)


@pytest.mark.asyncio
async def test_workdir_command_normalizes_before_browsing(monkeypatch, tmp_path):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    raw = str(tmp_path) + os.sep
    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=[raw]))

    assert handlers.WORKDIR_BROWSE[111]["path"] == str(tmp_path.resolve())

    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(tmp_path)
    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=["sub"]))

    assert handlers.WORKDIR_BROWSE[111]["path"] == str(sub.resolve())


@pytest.mark.asyncio
async def test_workdir_command_no_args_opens_browser_at_current(monkeypatch, tmp_path):
    db = FakeDB(_row())
    db.workdirs[111] = str(tmp_path)
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=[]))

    assert handlers.WORKDIR_BROWSE[111]["path"] == str(tmp_path)
    client.create_session.assert_not_awaited()
    assert any(str(tmp_path) in text for text in update.effective_message.sent)


@pytest.mark.asyncio
async def test_workdir_rejects_nonexistent_directory(monkeypatch, tmp_path):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    missing = tmp_path / "missing"
    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=[str(missing)]))

    assert not db.workdirs
    client.create_session.assert_not_awaited()
    assert any("not a directory" in text for text in update.effective_message.sent)


def test_normalize_workdir_leading_slash_on_windows():
    if sys.platform != "win32":
        pytest.skip("Windows-only drive-relative path behavior")
    normalized = handlers.normalize_workdir("/")
    assert normalized is not None
    assert normalized != "\\"
    assert Path(normalized).is_absolute()
    assert Path(normalized).drive


def _make_tree(root, dirs=(), files=()):
    for name in dirs:
        (root / name).mkdir()
    for name in files:
        (root / name).write_text("x", encoding="utf-8")


def _browser_data(markup, prefix):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data.startswith(prefix)
    ]


@pytest.mark.asyncio
async def test_workdir_callback_navigates_into_child(monkeypatch, tmp_path):
    _make_tree(tmp_path, dirs=["alpha", "beta"])
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=[str(tmp_path)]))
    children = handlers.WORKDIR_BROWSE[111]["children"]
    beta_index = children.index(str((tmp_path / "beta").resolve()))

    query = FakeQuery(f"wdir:o:{beta_index}", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    query.edit_message_text.assert_awaited_once()
    assert query.answer.await_count == 1
    assert handlers.WORKDIR_BROWSE[111]["path"] == str((tmp_path / "beta").resolve())
    assert handlers.WORKDIR_BROWSE[111]["page"] == 0


@pytest.mark.asyncio
async def test_workdir_callback_up_goes_to_parent(monkeypatch, tmp_path):
    _make_tree(tmp_path, dirs=["child"])
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    handlers.WORKDIR_BROWSE[111] = {
        "owner": 111,
        "path": str((tmp_path / "child").resolve()),
        "page": 0,
        "children": [],
    }
    query = FakeQuery("wdir:up", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    assert handlers.WORKDIR_BROWSE[111]["path"] == str(tmp_path.resolve())
    query.edit_message_text.assert_awaited_once()
    assert query.answer.await_count == 1


@pytest.mark.asyncio
async def test_workdir_callback_up_at_root_alerts(monkeypatch, tmp_path):
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    root = str(Path(tmp_path).anchor)
    handlers.WORKDIR_BROWSE[111] = {
        "owner": 111,
        "path": root,
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
async def test_workdir_callback_page_is_clamped(monkeypatch, tmp_path):
    _make_tree(tmp_path, dirs=[f"d{index:02d}" for index in range(15)])
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=[str(tmp_path)]))
    assert handlers.WORKDIR_BROWSE[111]["page"] == 0

    query = FakeQuery("wdir:pg:99", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    assert handlers.WORKDIR_BROWSE[111]["page"] == 1
    assert query.answer.await_count == 1


@pytest.mark.asyncio
async def test_workdir_callback_refresh_rebuilds(monkeypatch, tmp_path):
    _make_tree(tmp_path, dirs=["one"])
    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=[str(tmp_path)]))
    assert handlers.WORKDIR_BROWSE[111]["children"] == [str((tmp_path / "one").resolve())]

    (tmp_path / "two").mkdir()
    query = FakeQuery("wdir:refresh", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    assert handlers.WORKDIR_BROWSE[111]["children"] == [
        str((tmp_path / "one").resolve()),
        str((tmp_path / "two").resolve()),
    ]
    query.edit_message_text.assert_awaited_once()
    assert query.answer.await_count == 1


@pytest.mark.asyncio
async def test_workdir_callback_use_persists_and_confirms(monkeypatch, tmp_path):
    db = FakeDB(_row())
    db.models[111] = ("opencode-go", "deepseek")
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.create_session = AsyncMock(return_value={"id": "ses_browser"})

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=[str(tmp_path)]))
    query = FakeQuery("wdir:use", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    assert db.workdirs[111] == str(tmp_path.resolve())
    assert db.sessions[1]["session_id"] == "ses_browser"
    assert db.sessions[1]["directory"] == str(tmp_path.resolve())
    create_kwargs = client.create_session.await_args.kwargs
    assert create_kwargs["directory"] == str(tmp_path.resolve())
    assert create_kwargs["model"] == {
        "providerID": "opencode-go",
        "modelID": "deepseek",
    }
    assert query.answer.await_count == 1
    assert query.edit_message_text.await_args.args[0] == handlers.WORKDIR_SET_TEXT.format(
        path=str(tmp_path.resolve())
    )
    assert 111 not in handlers.WORKDIR_BROWSE


@pytest.mark.asyncio
async def test_workdir_callback_use_failure_does_not_persist_session(monkeypatch, tmp_path):
    from opencode_client import OpenCodeError

    db = FakeDB(_row())
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    client.create_session = AsyncMock(side_effect=OpenCodeError("boom"))

    update = _update()
    await handlers.workdir_command(update, _command_context(client, args=[str(tmp_path)]))
    query = FakeQuery("wdir:use", from_user=SimpleNamespace(id=111))
    cb_update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=111)
    )
    await handlers.workdir_callback(cb_update, _context(client))

    assert 1 not in db.sessions
    assert query.answer.await_count == 1
    assert query.edit_message_text.await_args.args[0] == handlers.OPENCODE_ERROR_TEXT


@pytest.mark.asyncio
async def test_workdir_callback_unauthenticated_is_refused(monkeypatch, tmp_path):
    db = FakeDB(_row(authenticated=0))
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    handlers.WORKDIR_BROWSE[111] = {
        "owner": 111,
        "path": str(tmp_path),
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
async def test_workdir_callback_rejects_other_owner(monkeypatch, tmp_path):
    db = FakeDB(_row(telegram_id=222))
    monkeypatch.setattr(handlers, "database", db)
    client = FakeClient()
    handlers.WORKDIR_BROWSE[222] = {
        "owner": 111,
        "path": str(tmp_path),
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


def test_list_child_dirs_only_dirs_sorted_case_insensitively(tmp_path):
    _make_tree(
        tmp_path,
        dirs=["Zeta", "alpha", "Beta"],
        files=["note.txt", "image.png"],
    )

    children = handlers.list_child_dirs(str(tmp_path))

    assert [Path(child).name for child in children] == ["alpha", "Beta", "Zeta"]
    assert all(Path(child).is_dir() for child in children)


def test_list_child_dirs_raises_clean_oserror(tmp_path):
    with pytest.raises(OSError):
        handlers.list_child_dirs(str(tmp_path / "missing"))


def test_preview_files_returns_files_and_total(tmp_path):
    _make_tree(tmp_path, dirs=["sub"], files=["b.txt", "a.txt", "c.txt"])

    names, total = handlers.preview_files(str(tmp_path), limit=2)

    assert names == ["a.txt", "b.txt"]
    assert total == 3


def test_preview_files_oserror_safe(tmp_path):
    names, total = handlers.preview_files(str(tmp_path / "missing"))
    assert names == []
    assert total == 0


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
    client.get_last_assistant_message = AsyncMock(
        return_value={
            "info": {"role": "assistant"},
            "parts": [
                {
                    "type": "file",
                    "url": _data_url("image/png", b"png"),
                    "mime": "image/png",
                    "filename": "pic.png",
                }
            ],
        }
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
    client.get_last_assistant_message = AsyncMock(side_effect=RuntimeError("boom"))

    update = _update(text="hi")
    await handlers.text_message(update, _context(client))

    assert "hello from opencode" in update.effective_message.edits


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
