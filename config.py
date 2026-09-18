from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
ENV_FILE = ROOT_DIR / ".env"

load_dotenv(ENV_FILE)

PLACEHOLDER_VALUES = {
    "",
    "your-bot-token-here",
    "your-bot-token",
    "<bot token>",
    "changeme",
    "replace-me",
    "your-openrouter-api-key",
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _resolve_path(raw_value: str | None, default: str) -> Path:
    candidate = Path(raw_value.strip() if raw_value and raw_value.strip() else default)
    if not candidate.is_absolute():
        candidate = ROOT_DIR / candidate
    return candidate.resolve()


def _parse_admin_id() -> int:
    raw = _env("ADMIN_USER_ID")
    if not raw:
        raise RuntimeError(
            "ADMIN_USER_ID is required. Set your Telegram numeric id in "
            f"{ENV_FILE}."
        )
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"ADMIN_USER_ID must be an integer, got {raw!r}.") from exc


TELEGRAM_BOT_TOKEN: str = _env("TELEGRAM_BOT_TOKEN")
ADMIN_USER_ID: int = _parse_admin_id()
DB_PATH: Path = _resolve_path(os.getenv("DB_PATH"), "data/bot.db")
MEDIA_DIR: Path = _resolve_path(os.getenv("MEDIA_DIR"), "media")
LOG_LEVEL: str = _env("LOG_LEVEL", "INFO").upper() or "INFO"

STT_BASE_URL: str = _env("STT_BASE_URL", "https://openrouter.ai/api/v1")
STT_API_KEY: str = _env("STT_API_KEY")
STT_MODEL: str = _env("STT_MODEL")
STT_PROVIDER: str = _env("STT_PROVIDER", "api")

TTS_VOICE: str = _env("TTS_VOICE", "en-US-AriaNeural")
TTS_RATE: str = _env("TTS_RATE", "+0%")
TTS_VOLUME: str = _env("TTS_VOLUME", "+0%")
TTS_PITCH: str = _env("TTS_PITCH", "+0Hz")


def _parse_opencode_timeout() -> float:
    raw = _env("OPENCODE_TIMEOUT", "600")
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"OPENCODE_TIMEOUT must be numeric, got {raw!r}.") from exc


def _parse_media_max_mb() -> int:
    raw = _env("MEDIA_MAX_MB", "20")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"MEDIA_MAX_MB must be an integer, got {raw!r}.") from exc
    if value <= 0:
        raise RuntimeError(f"MEDIA_MAX_MB must be positive, got {raw!r}.")
    return value


OPENCODE_BASE_URL: str = _env("OPENCODE_BASE_URL", "http://localhost:4096")
OPENCODE_DIRECTORY: Path = _resolve_path(os.getenv("OPENCODE_DIRECTORY"), ".")
OPENCODE_AGENT: str = _env("OPENCODE_AGENT", "build")
OPENCODE_TIMEOUT: float = _parse_opencode_timeout()
OPENCODE_SERVE_COMMAND: str = _env("OPENCODE_SERVE_COMMAND", "opencode")
MEDIA_MAX_MB: int = _parse_media_max_mb()


def _is_missing(value: str) -> bool:
    return not value or value.lower() in PLACEHOLDER_VALUES or value.startswith("<")


def require_telegram_token() -> str:
    if _is_missing(TELEGRAM_BOT_TOKEN):
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing or a placeholder. "
            f"Set a real token in {ENV_FILE}."
        )
    return TELEGRAM_BOT_TOKEN


def require_stt_config() -> tuple[str, str, str]:
    if _is_missing(STT_API_KEY):
        raise RuntimeError(
            "STT_API_KEY is missing or a placeholder. "
            f"Set a real OpenRouter key in {ENV_FILE}."
        )
    if _is_missing(STT_MODEL):
        raise RuntimeError(f"STT_MODEL is not configured. Set it in {ENV_FILE}.")
    return STT_BASE_URL, STT_API_KEY, STT_MODEL


def require_opencode_config() -> tuple[str, Path, str, float]:
    if _is_missing(OPENCODE_BASE_URL):
        raise RuntimeError(
            "OPENCODE_BASE_URL is missing or a placeholder. "
            f"Set a real base URL in {ENV_FILE}."
        )
    return OPENCODE_BASE_URL, OPENCODE_DIRECTORY, OPENCODE_AGENT, OPENCODE_TIMEOUT


def ensure_dirs() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
