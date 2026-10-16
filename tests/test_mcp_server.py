from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from telegram_bot import mcp_server


@pytest.mark.asyncio
async def test_telegram_send_file_success(monkeypatch, tmp_path):
    target = tmp_path / "shot.png"
    target.write_bytes(b"x")
    monkeypatch.setattr(
        mcp_server, "send_file", AsyncMock(return_value={"ok": True})
    )

    result = await mcp_server.telegram_send_file(str(target))

    assert result == "Sent shot.png to Telegram."


@pytest.mark.asyncio
async def test_telegram_send_file_missing_path_returns_error(tmp_path):
    result = await mcp_server.telegram_send_file(str(tmp_path / "missing.png"))

    assert result.startswith("Error:")
    assert "does not exist" in result


def test_fallback_tools_list_contains_send_file():
    response = mcp_server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )

    assert response is not None
    tools = response["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["telegram_send_file"]
    schema = tools[0]["inputSchema"]
    assert schema["required"] == ["path"]


def test_fallback_initialize_returns_server_info():
    response = mcp_server._handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
    )

    assert response["result"]["serverInfo"]["name"] == "opencode-voice-telegram"


def test_fallback_tools_call_unknown_tool():
    response = mcp_server._handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        }
    )

    assert response["result"]["isError"] is True


def test_resolve_chat_id_from_session(monkeypatch, tmp_path):
    target = tmp_path / "shot.png"
    target.write_bytes(b"x")
    fake_row = {"telegram_id": 424242}
    monkeypatch.setattr(
        mcp_server.database, "get_user_by_session_id", lambda sid: fake_row
    )
    captured = {}
    send_file_mock = AsyncMock(return_value={"ok": True})

    def capture_chat(path, *, chat_id, **kwargs):
        captured["chat_id"] = chat_id
        return send_file_mock()

    monkeypatch.setattr(mcp_server, "send_file", capture_chat)
    captured["chat_id"] = None

    response = mcp_server._handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "telegram_send_file",
                "arguments": {"path": str(target)},
                "_meta": {"sessionID": "ses_abc"},
            },
        }
    )

    assert response["result"]["isError"] is False
    assert captured["chat_id"] == 424242


def _capture_send_file(monkeypatch):
    captured = {}

    async def capture_chat(path, *, chat_id, **kwargs):
        captured["chat_id"] = chat_id
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "send_file", capture_chat)
    captured["chat_id"] = "unset"
    return captured


def test_resolve_chat_id_without_session_uses_directory(monkeypatch, tmp_path):
    target = tmp_path / "shot.png"
    target.write_bytes(b"x")
    monkeypatch.setattr(
        mcp_server.database, "get_chat_id_for_directory", lambda directory: 987654
    )
    captured = _capture_send_file(monkeypatch)

    response = mcp_server._handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "telegram_send_file",
                "arguments": {"path": str(target)},
            },
        }
    )

    assert response["result"]["isError"] is False
    assert captured["chat_id"] == 987654


def test_resolve_chat_id_unknown_session_uses_directory(monkeypatch, tmp_path):
    target = tmp_path / "shot.png"
    target.write_bytes(b"x")
    monkeypatch.setattr(
        mcp_server.database, "get_user_by_session_id", lambda sid: None
    )
    monkeypatch.setattr(
        mcp_server.database, "get_chat_id_for_directory", lambda directory: 246810
    )
    captured = _capture_send_file(monkeypatch)

    response = mcp_server._handle_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "telegram_send_file",
                "arguments": {"path": str(target)},
                "_meta": {"sessionID": "ses_unknown"},
            },
        }
    )

    assert response["result"]["isError"] is False
    assert captured["chat_id"] == 246810


def test_resolve_chat_id_unresolved_returns_none(monkeypatch):
    monkeypatch.setattr(
        mcp_server.database, "get_user_by_session_id", lambda sid: None
    )
    monkeypatch.setattr(
        mcp_server.database, "get_chat_id_for_directory", lambda directory: None
    )
    assert mcp_server.resolve_chat_id(None) is None
    assert mcp_server.resolve_chat_id("ses_unknown") is None
