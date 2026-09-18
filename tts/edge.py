from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator, Optional

import edge_tts

import config


class TTSError(RuntimeError):
    pass


def _resolve(value: Optional[str], default: str) -> str:
    return value if value is not None else default


def _communicate(
    text: str,
    voice: Optional[str],
    rate: Optional[str],
    volume: Optional[str],
    pitch: Optional[str],
) -> "edge_tts.Communicate":
    return edge_tts.Communicate(
        text,
        _resolve(voice, config.TTS_VOICE),
        rate=_resolve(rate, config.TTS_RATE),
        volume=_resolve(volume, config.TTS_VOLUME),
        pitch=_resolve(pitch, config.TTS_PITCH),
    )


async def synthesize(
    text: str,
    output_path: str | Path,
    *,
    voice: Optional[str] = None,
    rate: Optional[str] = None,
    volume: Optional[str] = None,
    pitch: Optional[str] = None,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    communicate = _communicate(text, voice, rate, volume, pitch)
    await communicate.save(str(path))
    return path


async def synthesize_bytes(
    text: str,
    *,
    voice: Optional[str] = None,
    rate: Optional[str] = None,
    volume: Optional[str] = None,
    pitch: Optional[str] = None,
) -> bytes:
    communicate = _communicate(text, voice, rate, volume, pitch)
    chunks: list[bytes] = []
    stream: AsyncIterator[dict] = communicate.stream()
    async for chunk in stream:
        if chunk.get("type") == "audio" and chunk.get("data"):
            chunks.append(chunk["data"])
    return b"".join(chunks)
