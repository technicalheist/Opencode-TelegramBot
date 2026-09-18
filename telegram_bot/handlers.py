from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import math
import os
import sqlite3
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Optional

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
import stt
import tts
from opencode_client import OpenCodeError, PermissionRequest
from telegram_bot import database, storage

logger = logging.getLogger(__name__)

WORKING_TEXT = "⏳ working…"
ACCESS_PENDING_TEXT = (
    "Your access is pending admin approval. "
    "You'll be able to use opencode once an admin authorizes you."
)
OPENCODE_ERROR_TEXT = "Sorry, opencode could not complete that request."
UNEXPECTED_ERROR_TEXT = "Something went wrong while contacting opencode."
EMPTY_REPLY_TEXT = "opencode returned an empty response."
EMPTY_TRANSCRIPT_TEXT = "I couldn't hear any speech in that audio."
STT_ERROR_TEXT = "Sorry, I couldn't transcribe that audio."
SESSION_RESET_TEXT = "Started a fresh opencode session for you."
NO_SESSION_TEXT = "You don't have an active opencode session yet."
STOPPED_TEXT = "Stopped the running opencode task."
STOP_ERROR_TEXT = "Could not stop the running task."
STATUS_NONE_TEXT = "No ongoing task found."
STATUS_NO_TODOS_TEXT = "No todo list."
STATUS_LOCAL_ONLY_TEXT = "running"
VOICE_ON_CALLBACK = "voice:on"
VOICE_OFF_CALLBACK = "voice:off"
VOICE_ON_LABEL = "🔊 ON"
VOICE_OFF_LABEL = "🔇 OFF"
VOICE_USAGE_TEXT = "Usage: /voice [on|off]"
MEDIA_CALLBACK_PREFIX = "media:"
MEDIA_NOT_FOUND_TEXT = "No media with that id."

OUTBOUND_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "bmp", "svg"}
OUTBOUND_VIDEO_EXTENSIONS = {"mp4", "mov", "webm", "mkv"}
OUTBOUND_AUDIO_EXTENSIONS = {"mp3", "wav", "ogg", "m4a", "flac"}
OUTBOUND_DOCUMENT_EXTENSIONS = {"pdf"}
OUTBOUND_MEDIA_EXTENSIONS = (
    OUTBOUND_IMAGE_EXTENSIONS
    | OUTBOUND_VIDEO_EXTENSIONS
    | OUTBOUND_AUDIO_EXTENSIONS
    | OUTBOUND_DOCUMENT_EXTENSIONS
    | {"gif"}
)
SENT_MEDIA: dict[str, set[str]] = {}
MAX_SENT_MEDIA_KEYS = 500
HTTP_MEDIA_TIMEOUT = 30.0
NO_MODELS_TEXT = "No connected models are available."
NO_SESSIONS_TEXT = "No sessions found for this working directory."
WORKDIR_USAGE_TEXT = "Usage: /workdir <path>"
WORKDIR_INVALID_TEXT = "That path is not a directory: {path}"
WORKDIR_SET_TEXT = "Working directory set to {path}. Started a fresh session."
COMPACT_STARTED_TEXT = "⏳ Session compaction started…"
COMPACT_DONE_TEXT = "✅ Session compacted."
COMPACT_FAILED_TEXT = "❌ Compaction failed: {reason}"
COMPACT_TIMEOUT_REASON = "timed out"
MAX_REASON_LENGTH = 200
SESSION_SWITCHED_TEXT = "Switched to session {session_id}."
MODEL_SET_TEXT = "Model set to {provider_id}/{model_id}."
SELECT_MODEL_TEXT = "Select a model:"
SELECT_SESSION_TEXT = "Select a session for {directory}:"
NEW_SESSION_LABEL = "New session"
SESSION_NEW_CALLBACK = "sesnew"

MODELS_PER_PAGE = 8
MAX_MODEL_TOKENS = 200

WORKDIR_PAGE_SIZE = 12
WORKDIR_PREVIEW_LIMIT = 8

WORKDIR_USE_CALLBACK = "wdir:use"
WORKDIR_UP_CALLBACK = "wdir:up"
WORKDIR_REFRESH_CALLBACK = "wdir:refresh"
WORKDIR_USE_LABEL = "✅ Use this directory"
WORKDIR_PARENT_LABEL = "⬆️ Parent"
WORKDIR_PREV_LABEL = "⬅️ Prev"
WORKDIR_NEXT_LABEL = "Next ➡️"
WORKDIR_REFRESH_LABEL = "🔄 Refresh"
WORKDIR_BROWSE_EXPIRED_TEXT = "The browser expired. Run /workdir again."
WORKDIR_BROWSE_ERROR_TEXT = "❌ Could not browse {path}."
WORKDIR_ROOT_TEXT = "Already at the filesystem root."
WORKDIR_DIR_GONE_TEXT = "That directory is no longer available."

MODEL_CHOICES: dict[int, dict[str, tuple[str, str]]] = {}
MODEL_LABELS: dict[int, dict[str, str]] = {}
WORKDIR_BROWSE: dict[int, dict] = {}

VALID_PERMISSION_REPLIES = {"once", "always", "reject"}
PERMISSION_LABELS = {
    "once": "allowed once",
    "always": "always allowed",
    "reject": "rejected",
}

HELP_TEXT = (
    "Available commands:\n"
    "/start - show a short welcome\n"
    "/help - list commands\n"
    "/id - show your Telegram and chat ids\n"
    "/whoami - show your stored profile\n"
    "/voice - show voice reply mode\n"
    "/voice on - reply with voice notes and text\n"
    "/voice off - reply with text only\n"
    "/new - start a fresh opencode session\n"
    "/stop - abort the running opencode task\n"
    "/models - choose the model opencode uses\n"
    "/session - switch the active opencode session\n"
    "/workdir - browse and set the working directory\n"
    "/compact - compact the active session\n"
    "/mcp - list MCP servers, status, and tools\n"
    "/status - show the current task status\n"
    "/list - list your recent stored media\n"
    "/get <id> - resend a stored media item\n\n"
    "Send text to chat with opencode. Send a voice note or audio file to "
    "transcribe it and send the transcript to opencode. Send any other media "
    "to store it."
)

_MEDIA_SENDERS = {
    "photo": "reply_photo",
    "video": "reply_video",
    "audio": "reply_audio",
    "voice": "reply_voice",
    "document": "reply_document",
    "animation": "reply_animation",
    "video_note": "reply_video_note",
    "sticker": "reply_sticker",
}

USER_LOCKS: dict[int, asyncio.Lock] = {}
_BACKGROUND_TASKS: set[asyncio.Task] = set()
ACTIVE_TASKS: dict[int, dict] = {}

TODO_ICONS = {
    "pending": "⬜",
    "in_progress": "🔄",
    "completed": "✅",
    "cancelled": "❌",
}


def mark_task_started(telegram_id: int, session_id: str, prompt: str) -> None:
    ACTIVE_TASKS[telegram_id] = {
        "session_id": str(session_id),
        "prompt": prompt,
        "started": time.time(),
    }


def mark_task_finished(telegram_id: int) -> None:
    ACTIVE_TASKS.pop(telegram_id, None)


def current_task(telegram_id: int) -> Optional[dict]:
    return ACTIVE_TASKS.get(telegram_id)


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def format_todos(todos: list[dict]) -> str:
    if not todos:
        return STATUS_NO_TODOS_TEXT
    lines: list[str] = []
    done = 0
    for todo in todos:
        status = str(todo.get("status") or "").strip()
        icon = TODO_ICONS.get(status, "•")
        if status == "completed":
            done += 1
        content = str(todo.get("content") or "").strip() or "(untitled)"
        lines.append(f"{icon} {content}")
    lines.append(f"{done}/{len(todos)} done")
    return "\n".join(lines)


def format_activity(part: Optional[dict]) -> Optional[str]:
    if not isinstance(part, dict):
        return None
    part_type = str(part.get("type") or "")
    if part_type == "tool":
        tool = str(part.get("tool") or "?")
        state = part.get("state")
        status = state.get("status") if isinstance(state, dict) else None
        return f"tool:{tool} ({status})" if status else f"tool:{tool}"
    if part_type == "reasoning":
        return "reasoning"
    if part_type == "text":
        text = str(part.get("text") or "").strip()
        return f"text: {text[:120]}" if text else "text"
    return part_type or None


def _lock_for(telegram_id: int) -> asyncio.Lock:
    lock = USER_LOCKS.get(telegram_id)
    if lock is None:
        lock = asyncio.Lock()
        USER_LOCKS[telegram_id] = lock
    return lock


def split_message(text: str, limit: int = 4096) -> list[str]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return []
    return [text[index : index + limit] for index in range(0, len(text), limit)]


def build_permission_keyboard(request: PermissionRequest) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Allow once", callback_data=f"perm:once:{request.id}"
                ),
                InlineKeyboardButton(
                    "Always", callback_data=f"perm:always:{request.id}"
                ),
                InlineKeyboardButton(
                    "Reject", callback_data=f"perm:reject:{request.id}"
                ),
            ]
        ]
    )


def parse_permission_callback(data: str) -> Optional[tuple[str, str]]:
    parts = (data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "perm":
        return None
    reply, request_id = parts[1], parts[2]
    if reply not in VALID_PERMISSION_REPLIES or not request_id:
        return None
    return reply, request_id


def build_voice_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    on_label = f"✅ {VOICE_ON_LABEL}" if enabled else VOICE_ON_LABEL
    off_label = f"✅ {VOICE_OFF_LABEL}" if not enabled else VOICE_OFF_LABEL
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(on_label, callback_data=VOICE_ON_CALLBACK),
                InlineKeyboardButton(off_label, callback_data=VOICE_OFF_CALLBACK),
            ]
        ]
    )


def parse_voice_callback(data: str) -> Optional[bool]:
    raw = data or ""
    if raw == VOICE_ON_CALLBACK:
        return True
    if raw == VOICE_OFF_CALLBACK:
        return False
    return None


def build_media_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []
    for row in rows:
        media_id = int(row["id"])
        media_type = str(row["media_type"])
        buttons.append(
            InlineKeyboardButton(
                f"📎 {media_id} · {media_type}"[:60],
                callback_data=f"{MEDIA_CALLBACK_PREFIX}{media_id}",
            )
        )
    return InlineKeyboardMarkup(
        [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    )


def parse_media_callback(data: str) -> Optional[int]:
    raw = data or ""
    if not raw.startswith(MEDIA_CALLBACK_PREFIX):
        return None
    value = raw[len(MEDIA_CALLBACK_PREFIX) :]
    if not value.isdigit():
        return None
    return int(value)


def current_directory(user) -> str:
    stored = database.get_workdir(int(user["telegram_id"]))
    if stored:
        return stored
    return str(config.OPENCODE_DIRECTORY)


def normalize_workdir(raw: str) -> Optional[str]:
    candidate = (raw or "").strip()
    if not candidate:
        return None
    try:
        target = Path(candidate).expanduser().resolve()
    except OSError:
        return None
    if not target.is_absolute() or not target.is_dir():
        return None
    return str(target)


def list_child_dirs(path: str) -> list[str]:
    children: list[str] = []
    with os.scandir(path) as iterator:
        for entry in iterator:
            try:
                if entry.is_dir():
                    children.append(str(Path(entry.path).resolve()))
            except OSError:
                continue
    return sorted(children, key=lambda child: Path(child).name.casefold())


def preview_files(path: str, limit: int = WORKDIR_PREVIEW_LIMIT) -> tuple[list[str], int]:
    if limit <= 0:
        return [], 0
    names: list[str] = []
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                try:
                    if entry.is_file():
                        names.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return [], 0
    names.sort(key=str.casefold)
    return names[:limit], len(names)


def format_workdir_message(
    path: str, children: list[str], page: int, *, page_size: int = WORKDIR_PAGE_SIZE
) -> str:
    total_pages = max(1, math.ceil(len(children) / page_size))
    page = max(0, min(page, total_pages - 1))
    file_names, total_files = preview_files(path, WORKDIR_PREVIEW_LIMIT)
    lines = [
        f"📂 {path}",
        "",
        f"📁 Subdirectories: {len(children)}",
        f"📄 Files: {total_files}",
    ]
    if file_names:
        lines.append("")
        lines.extend(f"   • {name}" for name in file_names)
        if total_files > len(file_names):
            lines.append(f"   … and {total_files - len(file_names)} more")
    lines.append("")
    lines.append(f"Page {page + 1}/{total_pages}")
    return "\n".join(lines)


def build_workdir_keyboard(
    children: list[str],
    page: int,
    *,
    page_size: int = WORKDIR_PAGE_SIZE,
    has_parent: bool = True,
) -> InlineKeyboardMarkup:
    total_pages = max(1, math.ceil(len(children) / page_size))
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(WORKDIR_USE_LABEL, callback_data=WORKDIR_USE_CALLBACK)]
    ]
    for index in range(start, min(start + page_size, len(children))):
        label = Path(children[index]).name or children[index]
        rows.append(
            [
                InlineKeyboardButton(
                    f"📁 {label}"[:60], callback_data=f"wdir:o:{index}"
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(WORKDIR_PREV_LABEL, callback_data=f"wdir:pg:{page - 1}"))
    if has_parent:
        nav.append(
            InlineKeyboardButton(WORKDIR_PARENT_LABEL, callback_data=WORKDIR_UP_CALLBACK)
        )
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(WORKDIR_NEXT_LABEL, callback_data=f"wdir:pg:{page + 1}"))
    nav.append(
        InlineKeyboardButton(WORKDIR_REFRESH_LABEL, callback_data=WORKDIR_REFRESH_CALLBACK)
    )
    rows.append(nav)
    return InlineKeyboardMarkup(rows)


def parse_workdir_callback(data: str) -> Optional[tuple[str, Optional[str]]]:
    raw = data or ""
    if not raw.startswith("wdir:"):
        return None
    rest = raw[len("wdir:") :]
    if rest in {"use", "up", "refresh"}:
        return rest, None
    for action in ("o", "pg"):
        prefix = f"{action}:"
        if rest.startswith(prefix):
            value = rest[len(prefix) :]
            if value.isdigit():
                return action, value
            return None
    return None


def model_payload(telegram_id: int) -> Optional[dict]:
    selected = database.get_model(telegram_id)
    if selected is None:
        return None
    provider_id, model_id = selected
    return {"providerID": provider_id, "modelID": model_id}


def remember_models(telegram_id: int, models: list[dict]) -> None:
    choices: dict[str, tuple[str, str]] = {}
    labels: dict[str, str] = {}
    for index, model in enumerate(models[:MAX_MODEL_TOKENS]):
        token = f"m{index}"
        provider_id = str(model.get("providerID") or "")
        model_id = str(model.get("modelID") or "")
        if not provider_id or not model_id:
            continue
        choices[token] = (provider_id, model_id)
        labels[token] = str(model.get("name") or model_id)
    MODEL_CHOICES[telegram_id] = choices
    MODEL_LABELS[telegram_id] = labels


def model_page_count(telegram_id: int) -> int:
    count = len(MODEL_CHOICES.get(telegram_id, {}))
    return max(1, math.ceil(count / MODELS_PER_PAGE))


def build_models_keyboard(telegram_id: int, page: int = 0) -> InlineKeyboardMarkup:
    choices = MODEL_CHOICES.get(telegram_id, {})
    labels = MODEL_LABELS.get(telegram_id, {})
    tokens = list(choices)
    total_pages = max(1, math.ceil(len(tokens) / MODELS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    start = page * MODELS_PER_PAGE
    rows: list[list[InlineKeyboardButton]] = []
    for token in tokens[start : start + MODELS_PER_PAGE]:
        provider_id, model_id = choices[token]
        label = labels.get(token, model_id)
        rows.append(
            [
                InlineKeyboardButton(
                    label[:60], callback_data=f"mdl:{token}"
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"mdl:pg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=f"mdl:pg:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def parse_model_callback(data: str) -> Optional[tuple[str, str]]:
    raw = data or ""
    if not raw.startswith("mdl:"):
        return None
    rest = raw[len("mdl:") :]
    if not rest:
        return None
    if rest.startswith("pg:"):
        value = rest[len("pg:") :]
        if not value.isdigit():
            return None
        return "pg", value
    return rest, rest


def build_sessions_keyboard(sessions: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, session in enumerate(sessions, start=1):
        session_id = str(session.get("id") or "")
        if not session_id:
            continue
        title = str(session.get("title") or session_id)
        rows.append(
            [
                InlineKeyboardButton(
                    f"{index}. {title}"[:60], callback_data=f"ses:{session_id}"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(NEW_SESSION_LABEL, callback_data=SESSION_NEW_CALLBACK)]
    )
    return InlineKeyboardMarkup(rows)


def parse_session_callback(data: str) -> Optional[str]:
    raw = data or ""
    if raw == SESSION_NEW_CALLBACK:
        return SESSION_NEW_CALLBACK
    if raw.startswith("ses:") and len(raw) > len("ses:"):
        return raw[len("ses:") :]
    return None


def _client(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data["opencode"]


def _ensure_user_row(update: Update) -> sqlite3.Row:
    telegram_user = update.effective_user
    assert telegram_user is not None
    return database.upsert_user(
        telegram_user.id,
        telegram_user.username,
        telegram_user.first_name,
        telegram_user.last_name,
        telegram_user.language_code,
    )


def _ensure_user(update: Update) -> int:
    return int(_ensure_user_row(update)["id"])


async def _require_authenticated(update: Update) -> Optional[sqlite3.Row]:
    row = _ensure_user_row(update)
    if not bool(row["is_authenticated"]):
        await update.effective_message.reply_text(ACCESS_PENDING_TEXT)
        return None
    return row


async def ensure_session(
    user_row: sqlite3.Row,
    client,
    *,
    directory: Optional[str] = None,
    model: Optional[dict] = None,
) -> str:
    existing = database.get_opencode_session(int(user_row["id"]))
    if existing is not None:
        return str(existing["session_id"])
    target_directory = directory or str(
        getattr(client, "directory", config.OPENCODE_DIRECTORY)
    )
    session = await client.create_session(
        title=f"Telegram {user_row['telegram_id']}",
        agent=config.OPENCODE_AGENT,
        model=model,
        directory=target_directory,
    )
    session_id = session.get("id") if isinstance(session, dict) else None
    if not session_id:
        raise OpenCodeError("opencode did not return a session id.")
    database.set_opencode_session(
        int(user_row["id"]), str(session_id), target_directory
    )
    return str(session_id)


async def _reply_or_edit(message, placeholder, text: str) -> None:
    if placeholder is not None:
        try:
            await placeholder.edit_text(text)
            return
        except Exception:
            logger.warning("Could not edit placeholder message", exc_info=True)
    try:
        await message.reply_text(text)
    except Exception:
        logger.exception("Could not send error message")


async def _send_voice_reply(message, text: str, telegram_id: int) -> None:
    directory = config.MEDIA_DIR / str(telegram_id) / "tts"
    path = directory / f"{uuid.uuid4().hex}.mp3"
    try:
        await tts.synthesize(text, path)
        await message.reply_voice(str(path))
    except Exception:
        logger.exception("Failed to synthesize or send voice reply")
    finally:
        try:
            path.unlink()
        except OSError:
            pass


async def _deliver_reply(
    message,
    placeholder,
    reply_text: str,
    *,
    telegram_id: int,
    voice_mode: bool,
) -> None:
    chunks = split_message(reply_text or "")
    if not chunks:
        await placeholder.edit_text(EMPTY_REPLY_TEXT)
        return
    await placeholder.edit_text(chunks[0])
    for chunk in chunks[1:]:
        await message.reply_text(chunk)
    if voice_mode:
        await _send_voice_reply(message, reply_text, telegram_id)


def _is_media_filename(name: str) -> bool:
    suffix = Path(name or "").suffix.lower().lstrip(".")
    return suffix in OUTBOUND_MEDIA_EXTENSIONS


def _filename_from_url(url: str) -> str:
    try:
        path = urllib.parse.urlparse(url).path
    except ValueError:
        path = ""
    return Path(path).name or "media"


def _entry_path(entry) -> Optional[str]:
    if isinstance(entry, str):
        return entry or None
    if isinstance(entry, dict):
        for key in ("path", "file", "filename"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _session_media_file(path: str, base: Path) -> Optional[Path]:
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(base)
    except ValueError:
        return None
    if not resolved.is_file():
        return None
    if not _is_media_filename(resolved.name):
        return None
    return resolved


async def _fetch_media_bytes(
    url: str, *, max_bytes: int, directory: Optional[Path] = None
) -> tuple[bytes, Optional[str]]:
    raw = str(url or "")
    if raw.startswith("data:"):
        header, separator, payload = raw[len("data:") :].partition(",")
        if not separator:
            raise ValueError("Malformed data URL")
        mime = header.split(";")[0].strip() or None
        if "base64" in header.split(";")[1:]:
            try:
                data = base64.b64decode(payload, validate=False)
            except Exception as exc:
                raise ValueError("Invalid base64 data URL") from exc
        else:
            data = urllib.parse.unquote_to_bytes(payload)
        if len(data) > max_bytes:
            raise ValueError("Media exceeds size limit")
        return data, mime
    if raw.startswith("file:"):
        parsed = urllib.parse.urlparse(raw)
        path_str = urllib.parse.unquote(parsed.path or parsed.netloc)
        if (
            os.name == "nt"
            and len(path_str) > 2
            and path_str[0] == "/"
            and path_str[2] == ":"
        ):
            path_str = path_str[1:]
        try:
            resolved = Path(path_str).expanduser().resolve()
        except OSError as exc:
            raise ValueError("Invalid file URL") from exc
        if directory is not None:
            try:
                resolved.relative_to(Path(directory).resolve())
            except ValueError as exc:
                raise ValueError("File URL is outside the session directory") from exc
        if not resolved.is_file():
            raise ValueError("File URL does not exist")
        data = resolved.read_bytes()
        if len(data) > max_bytes:
            raise ValueError("Media exceeds size limit")
        return data, None
    if raw.startswith("http://") or raw.startswith("https://"):
        content_type: Optional[str] = None
        async with httpx.AsyncClient(timeout=HTTP_MEDIA_TIMEOUT) as client:
            async with client.stream("GET", raw) as response:
                if response.status_code >= 400:
                    raise ValueError(f"HTTP {response.status_code} fetching media")
                content_type = response.headers.get("content-type")
                length = response.headers.get("content-length")
                if length is not None and length.isdigit() and int(length) > max_bytes:
                    raise ValueError("Media exceeds size limit")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("Media exceeds size limit")
                    chunks.append(chunk)
        mime = content_type.split(";")[0].strip() if content_type else None
        return b"".join(chunks), mime or None
    raise ValueError(f"Unsupported media URL: {raw[:64]}")


def _is_photo_media(mime: Optional[str], filename: str) -> bool:
    normalized = (mime or "").split(";")[0].strip().lower()
    if normalized:
        return normalized.startswith("image/") and normalized != "image/gif"
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    return suffix in OUTBOUND_IMAGE_EXTENSIONS


def _already_sent(session_id: str, key: str) -> bool:
    return key in SENT_MEDIA.get(session_id, set())


def _mark_sent(session_id: str, key: str) -> None:
    keys = SENT_MEDIA.setdefault(session_id, set())
    if len(keys) >= MAX_SENT_MEDIA_KEYS:
        keys.clear()
    keys.add(key)


async def _send_media_bytes(
    message, data: bytes, filename: str, mime: Optional[str]
) -> None:
    buffer = io.BytesIO(data)
    if _is_photo_media(mime, filename):
        await message.reply_photo(buffer, filename=filename)
    else:
        await message.reply_document(buffer, filename=filename)


async def _send_outbound_media(
    message, *, session_id: str, directory, parts, diff_files
) -> None:
    try:
        max_bytes = max(0, int(config.MEDIA_MAX_MB)) * 1024 * 1024
    except (TypeError, ValueError):
        max_bytes = 20 * 1024 * 1024
    base = Path(directory).resolve()
    candidates: list[dict] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "file":
            url = part.get("url")
            if isinstance(url, str) and url:
                candidates.append(
                    {
                        "kind": "url",
                        "url": url,
                        "mime": part.get("mime") or part.get("mimeType"),
                        "filename": part.get("filename") or part.get("name"),
                    }
                )
        elif part_type == "patch":
            for entry in part.get("files") or []:
                path = _entry_path(entry)
                if path:
                    candidates.append({"kind": "path", "path": path})
    for entry in diff_files or []:
        path = _entry_path(entry)
        if path:
            candidates.append({"kind": "path", "path": path})

    for candidate in candidates:
        try:
            if candidate["kind"] == "path":
                local = _session_media_file(candidate["path"], base)
                if local is None:
                    continue
                if local.stat().st_size > max_bytes:
                    continue
                key = f"path:{local}"
                data = local.read_bytes()
                mime = None
                filename = local.name
            else:
                filename = candidate.get("filename") or _filename_from_url(
                    candidate["url"]
                )
                mime = candidate.get("mime")
                key = "url:" + hashlib.sha1(
                    candidate["url"].encode("utf-8")
                ).hexdigest()
                data, fetched_mime = await _fetch_media_bytes(
                    candidate["url"], max_bytes=max_bytes, directory=base
                )
                mime = mime or fetched_mime
                filename = Path(str(filename)).name or "media"
        except Exception:
            logger.exception("Skipping outbound media candidate %s", candidate)
            continue
        if not data or len(data) > max_bytes:
            continue
        if _already_sent(session_id, key):
            continue
        try:
            await _send_media_bytes(message, data, filename, mime)
        except Exception:
            logger.exception("Failed to send outbound media %s", filename)
            continue
        _mark_sent(session_id, key)


async def _process_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_row: sqlite3.Row, prompt: str
) -> None:
    message = update.effective_message
    telegram_id = int(user_row["telegram_id"])
    client = _client(context)
    lock = _lock_for(telegram_id)
    directory = current_directory(user_row)
    model = model_payload(telegram_id)
    async with lock:
        placeholder = None
        try:
            session_id = await ensure_session(
                user_row, client, directory=directory, model=model
            )
            placeholder = await message.reply_text(WORKING_TEXT)
            mark_task_started(telegram_id, session_id, prompt)
            try:
                reply_text = await client.send_prompt(
                    session_id,
                    prompt,
                    agent=config.OPENCODE_AGENT,
                    model=model,
                    directory=directory,
                )
            finally:
                mark_task_finished(telegram_id)
        except OpenCodeError:
            logger.exception("opencode prompt failed for telegram_id %s", telegram_id)
            await _reply_or_edit(message, placeholder, OPENCODE_ERROR_TEXT)
            return
        except Exception:
            logger.exception(
                "Unexpected error handling prompt for telegram_id %s", telegram_id
            )
            await _reply_or_edit(message, placeholder, UNEXPECTED_ERROR_TEXT)
            return
        voice_mode = database.get_voice_mode(telegram_id)
        await _deliver_reply(
            message,
            placeholder,
            reply_text,
            telegram_id=telegram_id,
            voice_mode=voice_mode,
        )
        try:
            last_message = await client.get_last_assistant_message(
                session_id, directory=directory
            )
            parts = (last_message or {}).get("parts") or []
            try:
                diff_files = await client.get_session_diff(
                    session_id, directory=directory
                )
            except Exception:
                logger.exception(
                    "Failed to fetch session diff for %s", session_id
                )
                diff_files = []
            await _send_outbound_media(
                message,
                session_id=session_id,
                directory=directory,
                parts=parts,
                diff_files=diff_files,
            )
        except Exception:
            logger.exception(
                "Outbound media delivery failed for session %s", session_id
            )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_user(update)
    await update.effective_message.reply_text(
        "Welcome to opencode-voice. Send text to chat with opencode, send a "
        "voice note to transcribe and ask it, or send media to store it."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    telegram_user = update.effective_user
    assert telegram_user is not None
    await update.effective_message.reply_text(
        f"Your Telegram user id: {telegram_user.id}\n"
        f"Chat id: {chat.id if chat is not None else telegram_user.id}"
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    assert telegram_user is not None
    row = database.get_user_by_telegram_id(telegram_user.id)
    if row is None:
        _ensure_user(update)
        row = database.get_user_by_telegram_id(telegram_user.id)
    if row is None:
        await update.effective_message.reply_text("No profile found.")
        return
    name = " ".join(part for part in (row["first_name"], row["last_name"]) if part)
    username = f"@{row['username']}" if row["username"] else "-"
    telegram_id = int(row["telegram_id"])
    workdir = database.get_workdir(telegram_id) or str(config.OPENCODE_DIRECTORY)
    selected_model = database.get_model(telegram_id)
    model_label = (
        f"{selected_model[0]}/{selected_model[1]}" if selected_model else "default"
    )
    await update.effective_message.reply_text(
        f"id: {row['id']}\n"
        f"telegram_id: {row['telegram_id']}\n"
        f"name: {name or '-'}\n"
        f"username: {username}\n"
        f"role: {row['role']}\n"
        f"authenticated: {bool(row['is_authenticated'])}\n"
        f"voice_mode: {'on' if bool(row['voice_mode']) else 'off'}\n"
        f"workdir: {workdir}\n"
        f"model: {model_label}"
    )


async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = await _require_authenticated(update)
    if row is None:
        return
    telegram_id = int(row["telegram_id"])
    message = update.effective_message
    args = [argument.lower() for argument in (context.args or [])]
    if args and args[0] not in {"on", "off"}:
        await message.reply_text(VOICE_USAGE_TEXT)
        return
    if args:
        enabled = args[0] == "on"
        database.set_voice_mode(telegram_id, enabled)
        text = f"Voice replies {'enabled' if enabled else 'disabled'}."
    else:
        enabled = database.get_voice_mode(telegram_id)
        text = (
            f"Voice replies are {'ON' if enabled else 'OFF'}. "
            "Tap a button or use /voice on or /voice off."
        )
    await message.reply_text(text, reply_markup=build_voice_keyboard(enabled))


async def voice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    enabled = parse_voice_callback(query.data or "")
    if enabled is None:
        await query.answer()
        return
    caller = getattr(query, "from_user", None)
    row = (
        database.get_user_by_telegram_id(int(caller.id))
        if caller is not None
        else None
    )
    if row is None or not bool(row["is_authenticated"]):
        await query.answer("Not authorized.", show_alert=True)
        return
    telegram_id = int(row["telegram_id"])
    database.set_voice_mode(telegram_id, enabled)
    await query.answer()
    text = f"Voice replies {'enabled' if enabled else 'disabled'}."
    try:
        await query.edit_message_text(text, reply_markup=build_voice_keyboard(enabled))
    except Exception:
        logger.warning("Could not update voice message", exc_info=True)


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = await _require_authenticated(update)
    if row is None:
        return
    database.delete_opencode_session(int(row["id"]))
    await update.effective_message.reply_text(SESSION_RESET_TEXT)


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = await _require_authenticated(update)
    if row is None:
        return
    session = database.get_opencode_session(int(row["id"]))
    if session is None:
        await update.effective_message.reply_text(NO_SESSION_TEXT)
        return
    client = _client(context)
    try:
        await client.abort(str(session["session_id"]))
    except OpenCodeError:
        logger.exception("Failed to abort session for telegram_id %s", row["telegram_id"])
        await update.effective_message.reply_text(STOP_ERROR_TEXT)
        return
    await update.effective_message.reply_text(STOPPED_TEXT)


def _format_status_line(status_type: Optional[str], entry, task: Optional[dict]) -> str:
    if status_type == "retry":
        attempt = entry.get("attempt") if isinstance(entry, dict) else None
        detail = str(entry.get("message") or "").strip() if isinstance(entry, dict) else ""
        label = f"retry (attempt {attempt})" if attempt is not None else "retry"
        return f"{label}: {detail}" if detail else label
    if status_type:
        return status_type
    if task is not None:
        return STATUS_LOCAL_ONLY_TEXT
    return "unknown"


async def _build_status_text(
    context: ContextTypes.DEFAULT_TYPE, row
) -> str:
    telegram_id = int(row["telegram_id"])
    session = database.get_opencode_session(int(row["id"]))
    if session is None:
        return STATUS_NONE_TEXT
    client = _client(context)
    status = await client.get_session_status()
    entry = status.get(str(session["session_id"])) if isinstance(status, dict) else None
    status_type = (
        str(entry.get("type")) if isinstance(entry, dict) and entry.get("type") else None
    )
    task = current_task(telegram_id)
    if status_type not in {"busy", "retry"} and task is None:
        return STATUS_NONE_TEXT
    lines = [
        f"⏳ Ongoing task — session {session['session_id']}",
        f"Status: {_format_status_line(status_type, entry, task)}",
    ]
    started = task.get("started") if task else None
    if started is None:
        lines.append("Elapsed unknown")
    else:
        lines.append(f"Elapsed: {format_elapsed(time.time() - float(started))}")
    if task and task.get("prompt"):
        prompt = " ".join(str(task["prompt"]).split())
        if len(prompt) > 120:
            prompt = prompt[:117] + "…"
        lines.append(f"Prompt: {prompt}")
    try:
        activity_part = await client.get_last_activity(
            str(session["session_id"]), directory=current_directory(row)
        )
    except Exception:
        logger.exception(
            "Failed to fetch last activity for session %s", session["session_id"]
        )
        activity_part = None
    activity = format_activity(activity_part)
    if activity:
        lines.append(f"Activity: {activity}")
    try:
        todos = await client.get_session_todos(
            str(session["session_id"]), directory=current_directory(row)
        )
    except Exception:
        logger.exception("Failed to fetch todos for session %s", session["session_id"])
        todos = []
    lines.append("")
    lines.append(format_todos(todos))
    return "\n".join(lines)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = await _require_authenticated(update)
    if row is None:
        return
    message = update.effective_message
    try:
        text = await _build_status_text(context, row)
    except OpenCodeError:
        logger.exception("Failed to fetch status for telegram_id %s", row["telegram_id"])
        await message.reply_text(UNEXPECTED_ERROR_TEXT)
        return
    except Exception:
        logger.exception(
            "Unexpected error fetching status for telegram_id %s", row["telegram_id"]
        )
        await message.reply_text(UNEXPECTED_ERROR_TEXT)
        return
    for chunk in split_message(text):
        await message.reply_text(chunk)


async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = await _require_authenticated(update)
    if row is None:
        return
    telegram_id = int(row["telegram_id"])
    client = _client(context)
    try:
        models = await client.list_models()
    except OpenCodeError:
        logger.exception("Failed to list models for telegram_id %s", telegram_id)
        await update.effective_message.reply_text(OPENCODE_ERROR_TEXT)
        return
    if not models:
        await update.effective_message.reply_text(NO_MODELS_TEXT)
        return
    remember_models(telegram_id, models)
    await update.effective_message.reply_text(
        SELECT_MODEL_TEXT, reply_markup=build_models_keyboard(telegram_id, 0)
    )


async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = await _require_authenticated(update)
    if row is None:
        return
    telegram_id = int(row["telegram_id"])
    directory = current_directory(row)
    client = _client(context)
    try:
        sessions = await client.list_sessions(directory=directory, limit=50)
    except OpenCodeError:
        logger.exception("Failed to list sessions for telegram_id %s", telegram_id)
        await update.effective_message.reply_text(OPENCODE_ERROR_TEXT)
        return
    text = SELECT_SESSION_TEXT.format(directory=directory)
    if not sessions:
        text = f"{text}\n\n{NO_SESSIONS_TEXT}"
    await update.effective_message.reply_text(
        text, reply_markup=build_sessions_keyboard(sessions)
    )


async def _apply_workdir(
    context: ContextTypes.DEFAULT_TYPE, row, directory: str
) -> Optional[str]:
    telegram_id = int(row["telegram_id"])
    user_id = int(row["id"])
    database.set_workdir(telegram_id, directory)
    client = _client(context)
    try:
        session = await client.create_session(
            title=f"Telegram {telegram_id}",
            agent=config.OPENCODE_AGENT,
            model=model_payload(telegram_id),
            directory=directory,
        )
    except OpenCodeError:
        logger.exception("Failed to create session in %s", directory)
        return OPENCODE_ERROR_TEXT
    except Exception:
        logger.exception("Unexpected error creating session in %s", directory)
        return UNEXPECTED_ERROR_TEXT
    session_id = session.get("id") if isinstance(session, dict) else None
    if not session_id:
        return OPENCODE_ERROR_TEXT
    database.set_opencode_session(user_id, str(session_id), directory)
    return None


async def _open_browser(
    target, telegram_id: int, path: str, page: int = 0, *, edit: bool = False
) -> None:
    try:
        children = list_child_dirs(path)
    except OSError:
        logger.exception("Could not browse directory %s", path)
        text = WORKDIR_BROWSE_ERROR_TEXT.format(path=path)
        if edit:
            try:
                await target.edit_message_text(text, reply_markup=None)
            except Exception:
                logger.warning("Could not show browse error", exc_info=True)
        else:
            await target.reply_text(text)
        return
    total_pages = max(1, math.ceil(len(children) / WORKDIR_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    WORKDIR_BROWSE[telegram_id] = {
        "owner": telegram_id,
        "path": path,
        "page": page,
        "children": children,
    }
    text = format_workdir_message(path, children, page)
    keyboard = build_workdir_keyboard(
        children, page, has_parent=Path(path).parent != Path(path)
    )
    if edit:
        await target.edit_message_text(text, reply_markup=keyboard)
    else:
        await target.reply_text(text, reply_markup=keyboard)


async def workdir_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = await _require_authenticated(update)
    if row is None:
        return
    message = update.effective_message
    telegram_id = int(row["telegram_id"])
    if not context.args:
        await _open_browser(message, telegram_id, current_directory(row), 0)
        return
    raw = " ".join(context.args).strip()
    if not raw:
        await message.reply_text(WORKDIR_USAGE_TEXT)
        return
    directory = normalize_workdir(raw)
    if directory is None:
        try:
            attempted = str(Path(raw).expanduser().resolve())
        except OSError:
            attempted = raw
        await message.reply_text(WORKDIR_INVALID_TEXT.format(path=attempted))
        return
    await _open_browser(message, telegram_id, directory, 0)


async def _edit_browser_error(query, text: str) -> None:
    try:
        await query.edit_message_text(text, reply_markup=None)
    except Exception:
        logger.warning("Could not show workdir error", exc_info=True)


async def workdir_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    parsed = parse_workdir_callback(query.data or "")
    if parsed is None:
        await query.answer()
        return
    caller = getattr(query, "from_user", None)
    telegram_id = int(caller.id) if caller is not None else None
    row = (
        database.get_user_by_telegram_id(telegram_id)
        if telegram_id is not None
        else None
    )
    if row is None or not bool(row["is_authenticated"]):
        await query.answer("Not authorized.", show_alert=True)
        return
    state = WORKDIR_BROWSE.get(telegram_id)
    if state is None:
        await query.answer(WORKDIR_BROWSE_EXPIRED_TEXT, show_alert=True)
        return
    if int(state.get("owner", -1)) != telegram_id:
        await query.answer("This browser belongs to another user.", show_alert=True)
        return
    action, arg = parsed
    path = str(state.get("path") or "")
    page = int(state.get("page") or 0)
    children = list(state.get("children") or [])
    try:
        if action == "use":
            await query.answer()
            error = await _apply_workdir(context, row, path)
            if error is not None:
                await _edit_browser_error(query, error)
                return
            WORKDIR_BROWSE.pop(telegram_id, None)
            await _edit_browser_error(query, WORKDIR_SET_TEXT.format(path=path))
            return
        if action == "o":
            index = int(arg) if arg is not None else -1
            if index < 0 or index >= len(children):
                await query.answer(WORKDIR_DIR_GONE_TEXT, show_alert=True)
                return
            await query.answer()
            await _open_browser(query, telegram_id, children[index], 0, edit=True)
            return
        if action == "up":
            parent = Path(path).parent
            if parent == Path(path):
                await query.answer(WORKDIR_ROOT_TEXT, show_alert=True)
                return
            await query.answer()
            await _open_browser(query, telegram_id, str(parent), 0, edit=True)
            return
        if action == "pg":
            await query.answer()
            new_page = int(arg) if arg is not None else 0
            await _open_browser(query, telegram_id, path, new_page, edit=True)
            return
        if action == "refresh":
            await query.answer()
            await _open_browser(query, telegram_id, path, page, edit=True)
            return
    except OSError:
        logger.exception("Workdir browse failed at %s", path)
        await _edit_browser_error(
            query, WORKDIR_BROWSE_ERROR_TEXT.format(path=path)
        )


def _short_reason(exc: BaseException) -> str:
    message = str(exc).strip().splitlines()[0].strip() if str(exc).strip() else ""
    if not message:
        return exc.__class__.__name__
    return message[:MAX_REASON_LENGTH]


async def _send_compaction_status(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str
) -> None:
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        logger.exception("Could not send compaction status to chat %s", chat_id)


async def _run_compaction(
    context: ContextTypes.DEFAULT_TYPE,
    session_id: str,
    chat_id: int,
    model: Optional[dict],
) -> None:
    try:
        await asyncio.wait_for(
            _client(context).compact_session(session_id, model=model),
            timeout=config.OPENCODE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("Compaction timed out for session %s", session_id)
        await _send_compaction_status(
            context,
            chat_id,
            COMPACT_FAILED_TEXT.format(reason=COMPACT_TIMEOUT_REASON),
        )
        return
    except OpenCodeError as exc:
        logger.exception("Compaction failed for session %s", session_id)
        await _send_compaction_status(
            context, chat_id, COMPACT_FAILED_TEXT.format(reason=_short_reason(exc))
        )
        return
    except Exception as exc:
        logger.exception("Unexpected compaction error for session %s", session_id)
        await _send_compaction_status(
            context, chat_id, COMPACT_FAILED_TEXT.format(reason=_short_reason(exc))
        )
        return
    await _send_compaction_status(context, chat_id, COMPACT_DONE_TEXT)


def _schedule_background(context: ContextTypes.DEFAULT_TYPE, coroutine) -> None:
    application = getattr(context, "application", None)
    if application is not None:
        task = application.create_task(coroutine, name="opencode-compact")
    else:
        task = asyncio.ensure_future(coroutine)
    if isinstance(task, asyncio.Task):
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)


async def compact_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = await _require_authenticated(update)
    if row is None:
        return
    session = database.get_opencode_session(int(row["id"]))
    if session is None:
        await update.effective_message.reply_text(NO_SESSION_TEXT)
        return
    telegram_id = int(row["telegram_id"])
    await update.effective_message.reply_text(COMPACT_STARTED_TEXT)
    _schedule_background(
        context,
        _run_compaction(
            context,
            str(session["session_id"]),
            telegram_id,
            model_payload(telegram_id),
        ),
    )


async def mcp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = await _require_authenticated(update)
    if row is None:
        return
    telegram_id = int(row["telegram_id"])
    directory = current_directory(row)
    client = _client(context)
    try:
        servers = await client.get_mcp_status(directory=directory)
        tools = await client.list_tool_ids(directory=directory)
    except OpenCodeError:
        logger.exception("Failed to fetch MCP status for telegram_id %s", telegram_id)
        await update.effective_message.reply_text(OPENCODE_ERROR_TEXT)
        return

    lines = [f"MCP servers for {directory}:"]
    if servers:
        for name in sorted(servers):
            info = servers[name]
            status = info.get("status") if isinstance(info, dict) else info
            lines.append(f"• {name}: {status or 'unknown'}")
    else:
        lines.append("• (none)")
    lines.append("")
    lines.append("Tools:")
    lines.append(", ".join(sorted(tools)) if tools else "(none)")
    for chunk in split_message("\n".join(lines)):
        await update.effective_message.reply_text(chunk)


async def _send_media(message, media_row) -> None:
    try:
        path = storage.resolve_local_path(media_row["local_path"])
    except ValueError:
        logger.exception("Rejected unsafe media path for id %s", media_row["id"])
        await message.reply_text("Stored path is invalid.")
        return
    if not path.exists():
        await message.reply_text("The stored file is missing on disk.")
        return
    method_name = _MEDIA_SENDERS.get(media_row["media_type"])
    sender = getattr(message, method_name, None) if method_name else None
    if sender is not None:
        try:
            await sender(str(path))
            return
        except Exception:
            logger.warning(
                "Direct send failed for media id %s, falling back to document",
                media_row["id"],
                exc_info=True,
            )
    reply_document = getattr(message, "reply_document", None)
    if reply_document is not None:
        await reply_document(str(path), filename=path.name)
        return
    chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        chat_id = getattr(getattr(message, "chat", None), "id", None)
    if chat_id is None:
        chat_id = media_row["telegram_id"]
    await message.send_document(chat_id=chat_id, document=str(path), filename=path.name)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _ensure_user(update)
    rows = database.list_media(user_id=user_id, limit=20)
    if not rows:
        await update.effective_message.reply_text("No stored media yet.")
        return
    lines = ["Recent media:"]
    for row in rows:
        label = row["file_name"] or row["file_unique_id"]
        lines.append(f"{row['id']}: {row['media_type']} - {label}")
    await update.effective_message.reply_text(
        "\n".join(lines), reply_markup=build_media_keyboard(rows)
    )


async def get_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    args = context.args or []
    if not args or not args[0].isdigit():
        await message.reply_text("Usage: /get <id>")
        return
    media_row = database.get_media(int(args[0]))
    if media_row is None:
        await message.reply_text(MEDIA_NOT_FOUND_TEXT)
        return
    requester = update.effective_user
    requester_id = getattr(requester, "id", None)
    if media_row["telegram_id"] != requester_id and requester_id != config.ADMIN_USER_ID:
        await message.reply_text(MEDIA_NOT_FOUND_TEXT)
        return
    await _send_media(message, media_row)


def _callback_chat_id(query) -> Optional[int]:
    message = getattr(query, "message", None)
    chat_id = getattr(message, "chat_id", None)
    if chat_id is not None:
        return int(chat_id)
    chat = getattr(message, "chat", None)
    if chat is not None and getattr(chat, "id", None) is not None:
        return int(chat.id)
    caller = getattr(query, "from_user", None)
    if caller is not None and getattr(caller, "id", None) is not None:
        return int(caller.id)
    return None


async def _fallback_send_document(context, query, media_row) -> None:
    try:
        path = storage.resolve_local_path(media_row["local_path"])
    except Exception:
        logger.exception("Could not resolve media path for id %s", media_row["id"])
        return
    chat_id = _callback_chat_id(query)
    if chat_id is None:
        logger.warning("No chat id for media fallback %s", media_row["id"])
        return
    try:
        await context.bot.send_document(chat_id=chat_id, document=str(path))
    except Exception:
        logger.exception("Fallback document send failed for media %s", media_row["id"])


async def media_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    media_id = parse_media_callback(query.data or "")
    if media_id is None:
        await query.answer()
        return
    media_row = database.get_media(media_id)
    if media_row is None:
        await query.answer(MEDIA_NOT_FOUND_TEXT, show_alert=True)
        return
    caller = getattr(query, "from_user", None)
    caller_id = getattr(caller, "id", None)
    if media_row["telegram_id"] != caller_id and caller_id != config.ADMIN_USER_ID:
        await query.answer(MEDIA_NOT_FOUND_TEXT, show_alert=True)
        return
    await query.answer()
    message = query.message
    if message is None:
        message = context.bot
    try:
        await _send_media(message, media_row)
    except Exception:
        logger.exception("Failed to send media %s from callback", media_id)
        await _fallback_send_document(context, query, media_row)


def _media_prompt(media_type: str, local_path, caption: Optional[str]) -> str:
    lines = [
        f"The user sent a {media_type}.",
        f"See the {media_type} from this path: {local_path}",
    ]
    if caption:
        lines.append(f"Caption: {caption}")
    return "\n".join(lines)


async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    try:
        info = storage.extract_media(message)
        if info is None:
            await message.reply_text("Unsupported media type.")
            return
        user_row = _ensure_user_row(update)
        telegram_id = int(user_row["telegram_id"])
        local_path = await storage.download_media(context.bot, message, info)
        relative_path = storage.to_relative_path(local_path)
        media_id = database.insert_media(
            user_id=int(user_row["id"]),
            telegram_id=telegram_id,
            message_id=getattr(message, "message_id", None),
            media_type=info.media_type,
            file_id=info.file_id,
            file_unique_id=info.file_unique_id,
            local_path=relative_path,
            file_name=info.file_name,
            mime_type=info.mime_type,
            file_size=info.file_size,
        )
    except Exception:
        logger.exception("Failed to store incoming media")
        await message.reply_text("Failed to store that media. Please try again.")
        return
    if not bool(user_row["is_authenticated"]):
        await message.reply_text(f"Stored {info.media_type} as id {media_id}.")
        return
    prompt = _media_prompt(
        info.media_type, local_path, getattr(message, "caption", None)
    )
    await _process_prompt(update, context, user_row, prompt)


async def voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    info = storage.extract_media(message)
    if info is None:
        await message.reply_text("Unsupported media type.")
        return
    try:
        user_row = _ensure_user_row(update)
        telegram_id = int(update.effective_user.id)
        local_path = await storage.download_media(context.bot, message, info)
        relative_path = storage.to_relative_path(local_path)
        database.insert_media(
            user_id=int(user_row["id"]),
            telegram_id=telegram_id,
            message_id=getattr(message, "message_id", None),
            media_type=info.media_type,
            file_id=info.file_id,
            file_unique_id=info.file_unique_id,
            local_path=relative_path,
            file_name=info.file_name,
            mime_type=info.mime_type,
            file_size=info.file_size,
        )
    except Exception:
        logger.exception("Failed to store incoming voice media")
        await message.reply_text("Failed to store that media. Please try again.")
        return

    if not bool(user_row["is_authenticated"]):
        await message.reply_text(ACCESS_PENDING_TEXT)
        return

    try:
        transcript = await stt.transcribe(local_path)
    except Exception:
        logger.exception("STT failed for telegram_id %s", update.effective_user.id)
        await message.reply_text(STT_ERROR_TEXT)
        return

    transcript = (transcript or "").strip()
    if not transcript:
        await message.reply_text(EMPTY_TRANSCRIPT_TEXT)
        return

    await message.reply_text(f"📝 {transcript}")
    await _process_prompt(update, context, user_row, transcript)


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_row = await _require_authenticated(update)
    if user_row is None:
        return
    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        return
    await _process_prompt(update, context, user_row, text)


async def permission_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return
    parsed = parse_permission_callback(query.data or "")
    if parsed is None:
        await query.answer()
        return
    reply, request_id = parsed
    client = _client(context)
    try:
        await client.reply_permission(request_id, reply)
    except Exception:
        logger.exception("Failed to reply to permission %s", request_id)
        await query.answer("Could not send that decision.", show_alert=True)
        return
    await query.answer()
    original = getattr(getattr(query, "message", None), "text", None) or "Permission request"
    try:
        await query.edit_message_text(
            f"{original}\n\nDecision: {PERMISSION_LABELS[reply]}",
            reply_markup=None,
        )
    except Exception:
        logger.warning("Could not edit permission message", exc_info=True)


async def model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    parsed = parse_model_callback(query.data or "")
    if parsed is None:
        await query.answer()
        return
    telegram_user = update.effective_user
    if telegram_user is None:
        await query.answer()
        return
    telegram_id = int(telegram_user.id)
    kind, value = parsed
    if kind == "pg":
        await query.answer()
        try:
            await query.edit_message_reply_markup(
                reply_markup=build_models_keyboard(telegram_id, int(value))
            )
        except Exception:
            logger.warning("Could not update model page", exc_info=True)
        return
    selected = MODEL_CHOICES.get(telegram_id, {}).get(kind)
    if selected is None:
        await query.answer(
            "That model list expired. Run /models again.", show_alert=True
        )
        return
    provider_id, model_id = selected
    database.set_model(telegram_id, provider_id, model_id)
    await query.answer()
    try:
        await query.edit_message_text(
            MODEL_SET_TEXT.format(provider_id=provider_id, model_id=model_id),
            reply_markup=None,
        )
    except Exception:
        logger.warning("Could not confirm model selection", exc_info=True)


async def session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    value = parse_session_callback(query.data or "")
    if value is None:
        await query.answer()
        return
    telegram_user = update.effective_user
    row = (
        database.get_user_by_telegram_id(int(telegram_user.id))
        if telegram_user is not None
        else None
    )
    if row is None or not bool(row["is_authenticated"]):
        await query.answer("Not authorized.", show_alert=True)
        return
    user_id = int(row["id"])
    if value == SESSION_NEW_CALLBACK:
        database.delete_opencode_session(user_id)
        await query.answer()
        try:
            await query.edit_message_text(SESSION_RESET_TEXT, reply_markup=None)
        except Exception:
            logger.warning("Could not confirm new session", exc_info=True)
        return
    database.set_opencode_session(user_id, value, current_directory(row))
    await query.answer()
    try:
        await query.edit_message_text(
            SESSION_SWITCHED_TEXT.format(session_id=value), reply_markup=None
        )
    except Exception:
        logger.warning("Could not confirm session switch", exc_info=True)


async def handle_event(event: dict, *, bot, client) -> None:
    if not isinstance(event, dict) or event.get("type") != "permission.asked":
        return
    request = PermissionRequest.from_event(event)
    if not request.id or not request.session_id:
        logger.warning("Ignoring malformed permission.asked event: %s", event)
        return
    row = database.get_user_by_session_id(request.session_id)
    if row is None:
        logger.info("Permission request for unknown session %s", request.session_id)
        return
    text = f"opencode needs permission: {request.permission}"
    if request.patterns:
        text += "\n\n" + "\n".join(f"• {pattern}" for pattern in request.patterns)
    try:
        await bot.send_message(
            chat_id=int(row["telegram_id"]),
            text=text,
            reply_markup=build_permission_keyboard(request),
        )
    except Exception:
        logger.exception("Failed to surface permission request %s", request.id)


def admin_only(update: Update) -> bool:
    telegram_user = update.effective_user
    return telegram_user is not None and telegram_user.id == config.ADMIN_USER_ID
