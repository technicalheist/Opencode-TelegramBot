from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncio
import json
import logging
import os
from typing import Optional

from telegram_bot import database
from telegram_bot.send_media import SendMediaError, send_file

logger = logging.getLogger(__name__)

_TOOL_SPEC = {
    "name": "telegram_send_file",
    "description": (
        "Send a local file or image to the Telegram user who is chatting with "
        "the bot in this session. Use this to deliver screenshots, images, "
        "documents, or any file."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file to send.",
            },
            "caption": {
                "type": "string",
                "description": "Optional caption for the file.",
            },
        },
        "required": ["path"],
    },
}


def resolve_chat_id(session_id: Optional[str]) -> Optional[int]:
    if session_id:
        row = database.get_user_by_session_id(session_id)
        if row is not None:
            return int(row["telegram_id"])
    return database.get_chat_id_for_directory(os.getcwd())


async def telegram_send_file(
    path: str, caption: Optional[str] = None, chat_id: Optional[int] = None
) -> str:
    try:
        await send_file(path, caption=caption, chat_id=chat_id)
    except SendMediaError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        logger.exception("telegram_send_file failed")
        return f"Error: {exc}"
    return f"Sent {Path(path).name} to Telegram."


def _tool_result(text: str) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": text.startswith("Error:"),
    }


def _handle_request(request: dict) -> Optional[dict]:
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        params = request.get("params") or {}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "opencode-voice-telegram",
                    "version": "1.0.0",
                },
            },
        }
    if method in {"notifications/initialized", "initialized"}:
        return None
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": [_TOOL_SPEC]},
        }
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        if name != "telegram_send_file":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_result(f"Error: unknown tool {name}"),
            }
        arguments = params.get("arguments") or {}
        path = arguments.get("path")
        if not path:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_result("Error: path is required"),
            }
        meta = params.get("_meta") or {}
        chat_id = resolve_chat_id(meta.get("sessionID"))
        text = asyncio.run(
            telegram_send_file(path, arguments.get("caption"), chat_id=chat_id)
        )
        return {"jsonrpc": "2.0", "id": request_id, "result": _tool_result(text)}
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _run_fallback() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            logger.warning("Ignoring malformed JSON-RPC line")
            continue
        if not isinstance(request, dict):
            continue
        response = _handle_request(request)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


def run() -> None:
    _run_fallback()


if __name__ == "__main__":
    run()
