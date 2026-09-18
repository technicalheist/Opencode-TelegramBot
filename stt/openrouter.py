from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

import httpx

import config

SUPPORTED_FORMATS = {"wav", "mp3", "flac", "m4a", "ogg", "webm", "aac"}


class STTError(RuntimeError):
    pass


def _format_from_path(audio_path: str | Path) -> str:
    suffix = Path(audio_path).suffix.lower().lstrip(".")
    if suffix not in SUPPORTED_FORMATS:
        supported = ", ".join(sorted(SUPPORTED_FORMATS))
        raise STTError(
            f"Unsupported audio format {suffix or '<none>'!r}. "
            f"Supported formats: {supported}."
        )
    return suffix


async def transcribe(
    audio_path: str | Path,
    *,
    language: Optional[str] = None,
    model: Optional[str] = None,
    response_format: str = "json",
    timeout: float = 60.0,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    audio_format = _format_from_path(audio_path)
    data = Path(audio_path).read_bytes()
    return await transcribe_bytes(
        data,
        audio_format,
        language=language,
        model=model,
        response_format=response_format,
        timeout=timeout,
        client=client,
    )


async def transcribe_bytes(
    data: bytes,
    audio_format: str,
    *,
    language: Optional[str] = None,
    model: Optional[str] = None,
    response_format: str = "json",
    timeout: float = 60.0,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    base_url, api_key, default_model = config.require_stt_config()

    normalized_format = audio_format.lower().lstrip(".")
    if normalized_format not in SUPPORTED_FORMATS:
        supported = ", ".join(sorted(SUPPORTED_FORMATS))
        raise STTError(
            f"Unsupported audio format {normalized_format or '<none>'!r}. "
            f"Supported formats: {supported}."
        )

    payload: dict[str, object] = {
        "model": model or default_model,
        "input_audio": {
            "data": base64.b64encode(data).decode("ascii"),
            "format": normalized_format,
        },
    }
    if language:
        payload["language"] = language

    endpoint = f"{base_url.rstrip('/')}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}

    if client is None:
        async with httpx.AsyncClient(timeout=timeout) as owned_client:
            response = await owned_client.post(endpoint, json=payload, headers=headers)
    else:
        response = await client.post(endpoint, json=payload, headers=headers)

    if response.status_code >= 400:
        snippet = response.text[:500]
        raise STTError(
            f"STT request failed with status {response.status_code}: {snippet}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise STTError("STT response was not valid JSON.") from exc

    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str):
        raise STTError("STT response did not contain a 'text' field.")
    return text
