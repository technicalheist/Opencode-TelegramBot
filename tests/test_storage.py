from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from telegram_bot import storage


def _message(**overrides) -> SimpleNamespace:
    base = dict(
        photo=None,
        video=None,
        audio=None,
        voice=None,
        document=None,
        animation=None,
        video_note=None,
        sticker=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_extract_media_photo_picks_largest():
    message = _message(
        photo=[
            SimpleNamespace(file_id="small", file_unique_id="u-small", file_size=10),
            SimpleNamespace(file_id="large", file_unique_id="u-large", file_size=99),
        ]
    )
    info = storage.extract_media(message)
    assert info is not None
    assert info.media_type == "photo"
    assert info.file_id == "large"
    assert info.file_unique_id == "u-large"
    assert info.file_extension == ".jpg"


def test_extract_media_document_uses_filename_extension():
    message = _message(
        document=SimpleNamespace(
            file_id="doc",
            file_unique_id="u-doc",
            file_name="Report.PDF",
            mime_type="application/pdf",
            file_size=2048,
        )
    )
    info = storage.extract_media(message)
    assert info is not None
    assert info.media_type == "document"
    assert info.file_name == "Report.PDF"
    assert info.file_extension == ".pdf"
    assert info.file_size == 2048


def test_extract_media_document_falls_back_to_mime():
    message = _message(
        document=SimpleNamespace(
            file_id="doc",
            file_unique_id="u-doc",
            file_name=None,
            mime_type="application/pdf",
            file_size=1,
        )
    )
    info = storage.extract_media(message)
    assert info is not None
    assert info.file_extension == ".pdf"


def test_extract_media_voice():
    message = _message(
        voice=SimpleNamespace(
            file_id="voice",
            file_unique_id="u-voice",
            mime_type="audio/ogg",
            file_size=512,
        )
    )
    info = storage.extract_media(message)
    assert info is not None
    assert info.media_type == "voice"
    assert info.file_extension == ".ogg"


def test_extract_media_animated_sticker():
    message = _message(
        sticker=SimpleNamespace(
            file_id="sticker",
            file_unique_id="u-sticker",
            is_animated=True,
            is_video=False,
            file_size=42,
        )
    )
    info = storage.extract_media(message)
    assert info is not None
    assert info.media_type == "sticker"
    assert info.file_extension == ".tgs"


def test_extract_media_unsupported_returns_none():
    assert storage.extract_media(_message()) is None


def test_sanitize_filename_strips_paths_and_unsafe_characters():
    assert storage.sanitize_filename("..\\..\\evil name?.jpg") == "evil_name_.jpg"
    assert storage.sanitize_filename("") == "file"
    assert storage.sanitize_filename("..") == "file"


def test_resolve_local_path_joins_media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    resolved = storage.resolve_local_path("42/u1.jpg")
    assert resolved == (tmp_path / "media" / "42" / "u1.jpg").resolve()


def test_resolve_local_path_rejects_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    with pytest.raises(ValueError):
        storage.resolve_local_path("../secrets.txt")


class _FakeFile:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def download_to_drive(self, custom_path: str) -> Path:
        path = Path(custom_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload)
        return path


class _FakeBot:
    async def get_file(self, file_id: str) -> _FakeFile:
        return _FakeFile(b"hello")


@pytest.mark.asyncio
async def test_download_media_writes_to_media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    info = storage.MediaInfo(
        media_type="voice",
        file_id="file-id",
        file_unique_id="uniq-1",
        file_extension=".ogg",
    )
    message = SimpleNamespace(
        chat_id=123,
        message_id=1,
        from_user=SimpleNamespace(id=123),
    )

    path = await storage.download_media(_FakeBot(), message, info)

    assert path.exists()
    assert path.read_bytes() == b"hello"
    assert path.name == "uniq-1.ogg"
    assert path.parent.name == "123"
    assert storage.to_relative_path(path) == "123/uniq-1.ogg"
