from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncio
import json
import logging
from typing import Optional

from telegram_bot.send_media import SendMediaError, send_file

logger = logging.getLogger(__name__)

try:
    from mcp.server.fastmcp import FastMCP
except Exception:
    FastMCP = None

_TOOL_SPEC = {
    "name": "telegram_send_file",
    "description": (
        "Send a local file or image to the Telegram user via the bot. "
        "Use this to deliver screenshots, images, documents, or any file."
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


async def telegram_send_file(path: str, caption: Optional[str] = None) -> str:
    try:
        await send_file(path, caption=caption, chat_id=None)
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
        text = asyncio.run(telegram_send_file(path, arguments.get("caption")))
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


def _build_fastmcp():
    server = FastMCP("opencode-voice-telegram")
    server.tool(name="telegram_send_file")(telegram_send_file)
    return server


def run() -> None:
    if FastMCP is not None:
        _build_fastmcp().run()
    else:
        _run_fallback()


if __name__ == "__main__":
    run()
