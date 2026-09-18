from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import config

MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "application/pdf": ".pdf",
}


@dataclass
class MediaInfo:
    media_type: str
    file_id: str
    file_unique_id: str
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    file_extension: Optional[str] = None


def sanitize_filename(name: str) -> str:
    base = Path(name).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")
    return cleaned or "file"


def _extension_from_name_or_mime(
    file_name: Optional[str], mime_type: Optional[str], default: str
) -> str:
    if file_name:
        suffix = Path(file_name).suffix
        if suffix:
            return suffix.lower()
    if mime_type:
        normalised = mime_type.split(";")[0].strip().lower()
        if normalised in MIME_EXTENSIONS:
            return MIME_EXTENSIONS[normalised]
        if "/" in normalised:
            subtype = normalised.split("/", 1)[1]
            if re.fullmatch(r"[a-z0-9]+", subtype):
                return f".{subtype}"
    return default


def extract_media(message: Any) -> Optional[MediaInfo]:
    photo = getattr(message, "photo", None)
    if photo:
        largest = photo[-1]
        return MediaInfo(
            media_type="photo",
            file_id=largest.file_id,
            file_unique_id=largest.file_unique_id,
            file_size=getattr(largest, "file_size", None),
            file_extension=".jpg",
        )

    video = getattr(message, "video", None)
    if video is not None:
        return MediaInfo(
            media_type="video",
            file_id=video.file_id,
            file_unique_id=video.file_unique_id,
            file_name=getattr(video, "file_name", None),
            mime_type=getattr(video, "mime_type", None),
            file_size=getattr(video, "file_size", None),
            file_extension=_extension_from_name_or_mime(
                getattr(video, "file_name", None),
                getattr(video, "mime_type", None),
                ".mp4",
            ),
        )

    audio = getattr(message, "audio", None)
    if audio is not None:
        return MediaInfo(
            media_type="audio",
            file_id=audio.file_id,
            file_unique_id=audio.file_unique_id,
            file_name=getattr(audio, "file_name", None),
            mime_type=getattr(audio, "mime_type", None),
            file_size=getattr(audio, "file_size", None),
            file_extension=_extension_from_name_or_mime(
                getattr(audio, "file_name", None),
                getattr(audio, "mime_type", None),
                ".mp3",
            ),
        )

    voice = getattr(message, "voice", None)
    if voice is not None:
        return MediaInfo(
            media_type="voice",
            file_id=voice.file_id,
            file_unique_id=voice.file_unique_id,
            mime_type=getattr(voice, "mime_type", None),
            file_size=getattr(voice, "file_size", None),
            file_extension=".ogg",
        )

    document = getattr(message, "document", None)
    if document is not None:
        return MediaInfo(
            media_type="document",
            file_id=document.file_id,
            file_unique_id=document.file_unique_id,
            file_name=getattr(document, "file_name", None),
            mime_type=getattr(document, "mime_type", None),
            file_size=getattr(document, "file_size", None),
            file_extension=_extension_from_name_or_mime(
                getattr(document, "file_name", None),
                getattr(document, "mime_type", None),
                ".bin",
            ),
        )

    animation = getattr(message, "animation", None)
    if animation is not None:
        return MediaInfo(
            media_type="animation",
            file_id=animation.file_id,
            file_unique_id=animation.file_unique_id,
            file_name=getattr(animation, "file_name", None),
            mime_type=getattr(animation, "mime_type", None),
            file_size=getattr(animation, "file_size", None),
            file_extension=_extension_from_name_or_mime(
                getattr(animation, "file_name", None),
                getattr(animation, "mime_type", None),
                ".mp4",
            ),
        )

    video_note = getattr(message, "video_note", None)
    if video_note is not None:
        return MediaInfo(
            media_type="video_note",
            file_id=video_note.file_id,
            file_unique_id=video_note.file_unique_id,
            file_size=getattr(video_note, "file_size", None),
            file_extension=".mp4",
        )

    sticker = getattr(message, "sticker", None)
    if sticker is not None:
        if getattr(sticker, "is_animated", False):
            extension = ".tgs"
        elif getattr(sticker, "is_video", False):
            extension = ".webm"
        else:
            extension = ".webp"
        return MediaInfo(
            media_type="sticker",
            file_id=sticker.file_id,
            file_unique_id=sticker.file_unique_id,
            file_size=getattr(sticker, "file_size", None),
            file_extension=extension,
        )

    return None


def media_directory(telegram_id: int) -> Path:
    directory = config.MEDIA_DIR / str(telegram_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def to_relative_path(absolute_path: Path) -> str:
    return absolute_path.resolve().relative_to(config.MEDIA_DIR.resolve()).as_posix()


def resolve_local_path(relative_path: str) -> Path:
    base = config.MEDIA_DIR.resolve()
    candidate = Path(relative_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Path escapes the media directory: {relative_path}") from exc
    return resolved


async def download_media(bot: Any, message: Any, media: MediaInfo) -> Path:
    sender_id = getattr(message, "chat_id", None)
    if sender_id is None:
        from_user = getattr(message, "from_user", None)
        sender_id = getattr(from_user, "id", 0)
    extension = media.file_extension or ".bin"
    if not extension.startswith("."):
        extension = f".{extension}"
    filename = f"{sanitize_filename(media.file_unique_id)}{extension}"
    destination = media_directory(int(sender_id)) / filename
    telegram_file = await bot.get_file(media.file_id)
    await telegram_file.download_to_drive(custom_path=str(destination))
    return destination
