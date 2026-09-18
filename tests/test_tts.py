from __future__ import annotations

from pathlib import Path

import pytest

import config
import tts
import tts.edge as tts_edge


class FakeCommunicate:
    instances: list["FakeCommunicate"] = []

    def __init__(self, text, voice, *, rate=None, volume=None, pitch=None):
        self.text = text
        self.voice = voice
        self.rate = rate
        self.volume = volume
        self.pitch = pitch
        self.saved_paths: list[str] = []
        FakeCommunicate.instances.append(self)

    async def save(self, path: str) -> None:
        Path(path).write_bytes(b"saved-audio-bytes")
        self.saved_paths.append(path)

    async def stream(self):
        yield {"type": "WordBoundary", "data": b"metadata"}
        yield {"type": "audio", "data": b"aa"}
        yield {"type": "audio", "data": b"bb"}


@pytest.fixture
def fake_communicate(monkeypatch):
    FakeCommunicate.instances = []
    monkeypatch.setattr(tts_edge.edge_tts, "Communicate", FakeCommunicate)
    return FakeCommunicate


@pytest.mark.asyncio
async def test_synthesize_writes_file_and_returns_path(
    tmp_path, monkeypatch, fake_communicate
):
    monkeypatch.setattr(config, "TTS_VOICE", "en-US-TestNeural")
    monkeypatch.setattr(config, "TTS_RATE", "+10%")
    monkeypatch.setattr(config, "TTS_VOLUME", "+5%")
    monkeypatch.setattr(config, "TTS_PITCH", "+2Hz")

    output = tmp_path / "nested" / "out.mp3"
    result = await tts.synthesize("hello", output)

    assert result == output
    assert isinstance(result, Path)
    assert output.read_bytes() == b"saved-audio-bytes"

    instance = fake_communicate.instances[-1]
    assert instance.text == "hello"
    assert instance.voice == "en-US-TestNeural"
    assert instance.rate == "+10%"
    assert instance.volume == "+5%"
    assert instance.pitch == "+2Hz"


@pytest.mark.asyncio
async def test_synthesize_forwards_explicit_options(tmp_path, fake_communicate):
    await tts.synthesize(
        "hi",
        tmp_path / "out.mp3",
        voice="en-GB-RyanNeural",
        rate="-20%",
        volume="+0%",
        pitch="-1Hz",
    )

    instance = fake_communicate.instances[-1]
    assert instance.voice == "en-GB-RyanNeural"
    assert instance.rate == "-20%"
    assert instance.volume == "+0%"
    assert instance.pitch == "-1Hz"


@pytest.mark.asyncio
async def test_synthesize_bytes_concatenates_audio_chunks_only(fake_communicate):
    data = await tts.synthesize_bytes("hello")
    assert data == b"aabb"


@pytest.mark.asyncio
async def test_synthesize_bytes_uses_config_defaults(monkeypatch, fake_communicate):
    monkeypatch.setattr(config, "TTS_VOICE", "en-US-DefaultNeural")

    await tts.synthesize_bytes("hello")

    assert fake_communicate.instances[-1].voice == "en-US-DefaultNeural"
