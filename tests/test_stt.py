from __future__ import annotations

import base64
import json

import httpx
import pytest

import config
from stt import STTError, transcribe, transcribe_bytes


@pytest.fixture
def stt_config(monkeypatch):
    monkeypatch.setattr(config, "STT_BASE_URL", "https://stt.example.test/api/v1/")
    monkeypatch.setattr(config, "STT_API_KEY", "test-openrouter-key")
    monkeypatch.setattr(config, "STT_MODEL", "test/model")


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_transcribe_bytes_posts_base64_and_returns_text(stt_config):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"text": "hello world"})

    async with _client(handler) as client:
        result = await transcribe_bytes(b"\x00\x01audio", "wav", client=client)

    assert result == "hello world"
    assert captured["url"] == "https://stt.example.test/api/v1/audio/transcriptions"
    assert captured["auth"] == "Bearer test-openrouter-key"

    payload = captured["payload"]
    assert payload["model"] == "test/model"
    assert payload["input_audio"]["format"] == "wav"
    assert base64.b64decode(payload["input_audio"]["data"]) == b"\x00\x01audio"
    assert "language" not in payload


@pytest.mark.asyncio
async def test_transcribe_bytes_includes_language_when_provided(stt_config):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"text": "bonjour"})

    async with _client(handler) as client:
        await transcribe_bytes(b"audio", "mp3", language="fr", client=client)

    assert captured["payload"]["language"] == "fr"


@pytest.mark.asyncio
async def test_transcribe_bytes_uses_explicit_model(stt_config):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"text": "ok"})

    async with _client(handler) as client:
        await transcribe_bytes(b"audio", "ogg", model="other/model", client=client)

    assert captured["payload"]["model"] == "other/model"


@pytest.mark.asyncio
async def test_transcribe_bytes_raises_on_error_status(stt_config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    async with _client(handler) as client:
        with pytest.raises(STTError):
            await transcribe_bytes(b"audio", "wav", client=client)


@pytest.mark.asyncio
async def test_transcribe_bytes_raises_on_missing_text(stt_config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    async with _client(handler) as client:
        with pytest.raises(STTError):
            await transcribe_bytes(b"audio", "wav", client=client)


@pytest.mark.asyncio
async def test_transcribe_infers_format_from_extension(stt_config, tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"text": "from file"})

    audio_path = tmp_path / "clip.MP3"
    audio_path.write_bytes(b"file-bytes")

    async with _client(handler) as client:
        result = await transcribe(audio_path, client=client)

    assert result == "from file"
    assert captured["payload"]["input_audio"]["format"] == "mp3"
    assert base64.b64decode(captured["payload"]["input_audio"]["data"]) == b"file-bytes"


@pytest.mark.asyncio
async def test_transcribe_rejects_unsupported_extension(stt_config, tmp_path):
    audio_path = tmp_path / "notes.txt"
    audio_path.write_bytes(b"not audio")

    with pytest.raises(STTError):
        await transcribe(audio_path)


@pytest.mark.asyncio
async def test_transcribe_bytes_rejects_unsupported_format(stt_config):
    with pytest.raises(STTError):
        await transcribe_bytes(b"data", "txt")
