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
