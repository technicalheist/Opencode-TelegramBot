from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import argparse
import asyncio
import logging
import mimetypes
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "bmp", "svg"}


class SendMediaError(RuntimeError):
    pass


def resolve_media_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise SendMediaError(f"Invalid path: {path}") from exc
    if not resolved.exists():
        raise SendMediaError(f"File does not exist: {resolved}")
    if not resolved.is_file():
        raise SendMediaError(f"Not a file: {resolved}")
    return resolved


def choose_method(path: str | Path) -> tuple[str, str]:
    resolved = Path(path)
    extension = resolved.suffix.lower().lstrip(".")
    if extension in IMAGE_EXTENSIONS:
        mime = "image/jpeg" if extension in {"jpg", "jpeg"} else f"image/{extension}"
        return "sendPhoto", mime
    mime = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    return "sendDocument", mime


def _max_bytes() -> int:
    return int(config.MEDIA_MAX_MB) * 1024 * 1024


def _target_chat(chat_id: Optional[int]) -> int:
    return int(chat_id) if chat_id is not None else int(config.ADMIN_USER_ID)


def _parse_response(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        raise SendMediaError(f"Telegram API returned status {response.status_code}.")
    try:
        body = response.json()
    except ValueError as exc:
        raise SendMediaError("Telegram API returned invalid JSON.") from exc
    if not isinstance(body, dict) or not body.get("ok"):
        raise SendMediaError("Telegram API did not accept the file.")
    return body


async def send_file(
    path: str | Path,
    *,
    caption: Optional[str] = None,
    chat_id: Optional[int] = None,
) -> dict:
    resolved = resolve_media_path(path)
    if resolved.stat().st_size > _max_bytes():
        raise SendMediaError(
            f"File exceeds MEDIA_MAX_MB ({config.MEDIA_MAX_MB} MB)."
        )
    token = config.require_telegram_token()
    method, mime = choose_method(resolved)
    field = "photo" if method == "sendPhoto" else "document"
    data: dict = {"chat_id": str(_target_chat(chat_id))}
    if caption:
        data["caption"] = caption
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        with resolved.open("rb") as handle:
            files = {field: (resolved.name, handle, mime)}
            response = await client.post(url, data=data, files=files)
    return _parse_response(response)


def send_file_sync(
    path: str | Path,
    *,
    caption: Optional[str] = None,
    chat_id: Optional[int] = None,
) -> dict:
    resolved = resolve_media_path(path)
    if resolved.stat().st_size > _max_bytes():
        raise SendMediaError(
            f"File exceeds MEDIA_MAX_MB ({config.MEDIA_MAX_MB} MB)."
        )
    token = config.require_telegram_token()
    method, mime = choose_method(resolved)
    field = "photo" if method == "sendPhoto" else "document"
    data: dict = {"chat_id": str(_target_chat(chat_id))}
    if caption:
        data["caption"] = caption
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    with httpx.Client(timeout=60.0) as client:
        with resolved.open("rb") as handle:
            files = {field: (resolved.name, handle, mime)}
            response = client.post(url, data=data, files=files)
    return _parse_response(response)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="python -m telegram_bot.send_media",
        description="Send a local file to the configured Telegram admin.",
    )
    parser.add_argument("path", help="Path to the file to send.")
    parser.add_argument("--caption", default=None, help="Optional caption.")
    parser.add_argument(
        "--chat-id", dest="chat_id", type=int, default=None, help="Target chat id."
    )
    args = parser.parse_args(argv)
    try:
        send_file_sync(args.path, caption=args.caption, chat_id=args.chat_id)
    except SendMediaError as exc:
        logger.error("Failed to send media: %s", exc)
        return 1
    except Exception as exc:
        logger.error("Unexpected error sending media: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
