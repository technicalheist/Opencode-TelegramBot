from __future__ import annotations

import httpx
import pytest

import config
from telegram_bot import send_media


def test_resolve_media_path_accepts_file(tmp_path):
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"x")
    assert send_media.resolve_media_path(target) == target.resolve()


def test_resolve_media_path_rejects_missing(tmp_path):
    with pytest.raises(send_media.SendMediaError):
        send_media.resolve_media_path(tmp_path / "missing.png")


def test_resolve_media_path_rejects_directory(tmp_path):
    with pytest.raises(send_media.SendMediaError):
        send_media.resolve_media_path(tmp_path)


def test_choose_method_images_and_documents():
    assert send_media.choose_method("shot.png")[0] == "sendPhoto"
    assert send_media.choose_method("shot.jpg")[0] == "sendPhoto"
    assert send_media.choose_method("animated.gif")[0] == "sendDocument"
    assert send_media.choose_method("report.pdf")[0] == "sendDocument"


def _patch_async_client(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        send_media.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler)),
    )


@pytest.fixture(autouse=True)
def _fake_config(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:FAKE-TOKEN")
    monkeypatch.setattr(config, "ADMIN_USER_ID", 12345)
    monkeypatch.delenv("OPENCODE_VOICE_CHAT_ID", raising=False)
    monkeypatch.setattr(
        send_media.database, "get_chat_id_for_directory", lambda directory: 12345
    )


def test_resolve_chat_id_explicit_wins(monkeypatch):
    monkeypatch.setenv("OPENCODE_VOICE_CHAT_ID", "222")
    assert send_media.resolve_chat_id(111, "ses_x") == 111


def test_resolve_chat_id_env(monkeypatch):
    monkeypatch.setenv("OPENCODE_VOICE_CHAT_ID", "222")
    monkeypatch.setattr(send_media.database, "get_user_by_session_id", lambda sid: None)
    monkeypatch.setattr(
        send_media.database, "get_chat_id_for_directory", lambda directory: 333
    )
    assert send_media.resolve_chat_id(None, "ses_x") == 222


def test_resolve_chat_id_from_session(monkeypatch):
    monkeypatch.setattr(
        send_media.database,
        "get_user_by_session_id",
        lambda sid: {"telegram_id": 444},
    )
    assert send_media.resolve_chat_id(None, "ses_x") == 444


def test_resolve_chat_id_from_directory(monkeypatch):
    monkeypatch.setattr(send_media.database, "get_user_by_session_id", lambda sid: None)
    seen: dict = {}

    def fake_directory(directory):
        seen["directory"] = directory
        return 555

    monkeypatch.setattr(send_media.database, "get_chat_id_for_directory", fake_directory)
    assert send_media.resolve_chat_id(None, "ses_x") == 555
    assert seen["directory"] == send_media.os.getcwd()


def test_resolve_chat_id_unresolved_raises(monkeypatch):
    monkeypatch.setattr(send_media.database, "get_user_by_session_id", lambda sid: None)
    monkeypatch.setattr(
        send_media.database, "get_chat_id_for_directory", lambda directory: None
    )
    with pytest.raises(send_media.SendMediaError):
        send_media.resolve_chat_id(None, "ses_x")


@pytest.mark.asyncio
async def test_send_file_unresolved_recipient_raises(monkeypatch, tmp_path):
    target = tmp_path / "shot.png"
    target.write_bytes(b"x")
    monkeypatch.setattr(send_media.database, "get_user_by_session_id", lambda sid: None)
    monkeypatch.setattr(
        send_media.database, "get_chat_id_for_directory", lambda directory: None
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent")

    _patch_async_client(monkeypatch, handler)
    with pytest.raises(send_media.SendMediaError):
        await send_media.send_file(target)



@pytest.mark.asyncio
async def test_send_file_posts_photo(monkeypatch, tmp_path):
    target = tmp_path / "shot.png"
    target.write_bytes(b"image-bytes")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = request.read()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    _patch_async_client(monkeypatch, handler)
    result = await send_media.send_file(target, caption="look at this")

    assert result["ok"] is True
    assert captured["method"] == "POST"
    assert captured["path"].endswith("/sendPhoto")
    assert captured["path"].startswith("/bot")
    body = captured["body"]
    assert b'name="photo"' in body
    assert b'name="chat_id"' in body
    assert b"12345" in body
    assert b"look at this" in body


@pytest.mark.asyncio
async def test_send_file_posts_document_and_explicit_chat(monkeypatch, tmp_path):
    target = tmp_path / "report.pdf"
    target.write_bytes(b"pdf-bytes")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = request.read()
        return httpx.Response(200, json={"ok": True})

    _patch_async_client(monkeypatch, handler)
    await send_media.send_file(target, chat_id=999)

    assert captured["path"].endswith("/sendDocument")
    assert b'name="document"' in captured["body"]
    assert b"999" in captured["body"]


@pytest.mark.asyncio
async def test_send_file_rejects_oversize(monkeypatch, tmp_path):
    target = tmp_path / "big.png"
    target.write_bytes(b"x")
    monkeypatch.setattr(config, "MEDIA_MAX_MB", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent")

    _patch_async_client(monkeypatch, handler)
    with pytest.raises(send_media.SendMediaError):
        await send_media.send_file(target)


@pytest.mark.asyncio
async def test_send_file_raises_on_not_ok(monkeypatch, tmp_path):
    target = tmp_path / "shot.png"
    target.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "no"})

    _patch_async_client(monkeypatch, handler)
    with pytest.raises(send_media.SendMediaError):
        await send_media.send_file(target)


@pytest.mark.asyncio
async def test_send_file_raises_on_http_error(monkeypatch, tmp_path):
    target = tmp_path / "shot.png"
    target.write_bytes(b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False})

    _patch_async_client(monkeypatch, handler)
    with pytest.raises(send_media.SendMediaError):
        await send_media.send_file(target)
