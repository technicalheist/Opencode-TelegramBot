"""Settings schema and ``.env`` persistence for the control plane.

The panel edits the same dotenv file the bot reads, so there is exactly one
source of truth. Secrets never travel back to the browser: the API exposes an
empty string plus a "configured" flag, and a saved payload without a key keeps
the stored value untouched.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from control import paths

logger = logging.getLogger(__name__)

PLACEHOLDERS = {
    "",
    "your-bot-token-here",
    "your-telegram-user-id",
    "your-openrouter-api-key",
    "your-opencode-server-password",
    "change-me-secret-path",
    "change-me-secret-token",
    "changeme",
    "replace-me",
}

_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    kind: str = "text"
    default: str = ""
    required: bool = False
    secret: bool = False
    help: str = ""
    choices: tuple[str, ...] = ()
    placeholder: str = ""


GROUPS = ("Telegram", "Speech", "opencode", "Webhook", "Advanced")

FIELDS: tuple[Field, ...] = (
    Field(
        "TELEGRAM_BOT_TOKEN",
        "Bot token",
        "Telegram",
        kind="secret",
        required=True,
        secret=True,
        placeholder="123456:ABC-DEF...",
        help="From @BotFather. Sent to the Telegram Bot API, so keep it private.",
    ),
    Field(
        "ADMIN_USER_ID",
        "Admin Telegram id",
        "Telegram",
        kind="int",
        required=True,
        placeholder="123456789",
        help="Numeric Telegram id that approves new users. Send /id to the bot.",
    ),
    Field("STT_BASE_URL", "STT base URL", "Speech", kind="url", default="https://openrouter.ai/api/v1"),
    Field(
        "STT_API_KEY",
        "OpenRouter API key",
        "Speech",
        kind="secret",
        secret=True,
        placeholder="sk-or-v1-...",
        help="Used for voice-note transcription. Leave empty to disable STT.",
    ),
    Field("STT_MODEL", "STT model", "Speech", default="openai/whisper-large-v3"),
    Field("STT_PROVIDER", "STT provider", "Speech", default="api"),
    Field("TTS_VOICE", "Voice", "Speech", default="en-US-AriaNeural"),
    Field("TTS_RATE", "Rate", "Speech", default="+0%"),
    Field("TTS_VOLUME", "Volume", "Speech", default="+0%"),
    Field("TTS_PITCH", "Pitch", "Speech", default="+0Hz"),
    Field(
        "OPENCODE_BASE_URL",
        "Server URL",
        "opencode",
        kind="url",
        default="http://localhost:4096",
        required=True,
    ),
    Field(
        "OPENCODE_DIRECTORY",
        "Default directory",
        "opencode",
        kind="path",
        help="Working directory for new sessions. Empty uses the server default.",
    ),
    Field("OPENCODE_AGENT", "Agent", "opencode", default="build"),
    Field("OPENCODE_TIMEOUT", "Prompt timeout (s)", "opencode", kind="int", default="600"),
    Field(
        "OPENCODE_SERVER_USERNAME",
        "Server username",
        "opencode",
        default="opencode",
    ),
    Field(
        "OPENCODE_SERVER_PASSWORD",
        "Server password",
        "opencode",
        kind="secret",
        secret=True,
        help="HTTP basic auth password; must match how the server was started.",
    ),
    Field("MEDIA_MAX_MB", "Max media size (MB)", "opencode", kind="int", default="20"),
    Field(
        "OPENCODE_COMMAND",
        "opencode CLI",
        "opencode",
        default="opencode",
        help="Command used to start/stop the background server.",
    ),
    Field(
        "TELEGRAM_WEBHOOK_URL",
        "Webhook base URL",
        "Webhook",
        kind="url",
        help="Empty uses long polling instead of a webhook.",
    ),
    Field("TELEGRAM_WEBHOOK_PORT", "Webhook port", "Webhook", kind="int", default="8080"),
    Field("TELEGRAM_WEBHOOK_PATH", "Webhook path", "Webhook", placeholder="change-me-secret-path"),
    Field(
        "TELEGRAM_WEBHOOK_SECRET",
        "Webhook secret",
        "Webhook",
        kind="secret",
        secret=True,
        placeholder="change-me-secret-token",
    ),
    Field("CLOUDFLARED_COMMAND", "cloudflared command", "Webhook", default="cloudflared"),
    Field(
        "CLOUDFLARED_TUNNEL_TOKEN",
        "cloudflared tunnel token",
        "Webhook",
        kind="secret",
        secret=True,
        help="Only needed when the panel should manage the tunnel.",
    ),
    Field("DB_PATH", "Database path", "Advanced", kind="path"),
    Field("MEDIA_DIR", "Media directory", "Advanced", kind="path"),
    Field(
        "LOG_LEVEL",
        "Log level",
        "Advanced",
        kind="choice",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    ),
)

FIELDS_BY_KEY = {field.key: field for field in FIELDS}


def defaults() -> dict[str, str]:
    values = {field.key: field.default for field in FIELDS}
    values["DB_PATH"] = str(paths.default_db_path())
    values["MEDIA_DIR"] = str(paths.media_dir())
    return values


def fields_by_group() -> dict[str, list[Field]]:
    groups: dict[str, list[Field]] = {name: [] for name in GROUPS}
    for field in FIELDS:
        groups.setdefault(field.group, []).append(field)
    return groups


def _read_text(target: Path) -> str | None:
    """Return the file text, ``None`` when absent, raise when unreadable.

    An undecodable file must never be treated as empty: the next save would
    replace it and silently drop every other setting and secret.
    """
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{target} is not valid UTF-8 (saved as UTF-16 or ANSI?). "
            "Convert it to UTF-8 before saving."
        ) from exc
    except OSError as exc:
        raise ValueError(f"Cannot read {target}: {exc}") from exc


def _parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        match = _LINE_RE.match(raw)
        if not match:
            continue
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def read_env(path: Path | str | None = None) -> dict[str, str]:
    target = Path(path) if path is not None else paths.env_file()
    try:
        text = _read_text(target)
    except ValueError:
        logger.warning("Ignoring unreadable settings file %s", target)
        return {}
    return _parse(text) if text else {}


_QUOTE_TRIGGERS = set(' #"\'\t')


def _encode(value: str) -> str:
    """Encode one dotenv value so python-dotenv reads back the same string."""
    if "\n" in value or "\r" in value:
        raise ValueError("Setting values cannot contain line breaks.")
    if value == "" or not any(char in _QUOTE_TRIGGERS for char in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_env(values: Mapping[str, str]) -> str:
    lines = ["# opencode-voice settings (managed by the panel and `ocv config`).", ""]
    known = set()
    for group, fields in fields_by_group().items():
        lines.append(f"# {group}")
        for field in fields:
            known.add(field.key)
            lines.append(f"{field.key}={_encode(values.get(field.key, field.default))}")
        lines.append("")
    extra = {key: value for key, value in values.items() if key not in known}
    if extra:
        lines.append("# Other")
        for key in sorted(extra):
            lines.append(f"{key}={_encode(extra[key])}")
        lines.append("")
    return "\n".join(lines)


def write_env(values: Mapping[str, str], path: Path | str | None = None) -> Path:
    target = Path(path) if path is not None else paths.env_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = render_env(values)
    handle, temp_name = tempfile.mkstemp(
        prefix=".env.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        if os.name != "nt":
            os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return target


def load() -> dict[str, str]:
    return read_env()


def _is_set(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in PLACEHOLDERS


def _is_url(value: str) -> bool:
    return bool(re.match(r"^https?://[^\s/]+", value.strip(), re.IGNORECASE))


def _as_int(value: str) -> int | None:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def validate(values: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)``; errors block saving."""
    errors: list[str] = []
    warnings: list[str] = []

    token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not _is_set(token):
        errors.append("Bot token is required.")
    elif not _TOKEN_RE.match(token):
        errors.append("Bot token does not look like a Telegram token (123456:ABC...).")

    admin = values.get("ADMIN_USER_ID", "").strip()
    if not _is_set(admin):
        errors.append("Admin Telegram id is required.")
    elif (_as_int(admin) or 0) <= 0:
        errors.append("Admin Telegram id must be a positive integer.")

    base_url = values.get("OPENCODE_BASE_URL", "").strip()
    if not _is_set(base_url):
        errors.append("opencode server URL is required.")
    elif not _is_url(base_url):
        errors.append("opencode server URL must start with http:// or https://.")

    for key in ("STT_BASE_URL", "TELEGRAM_WEBHOOK_URL"):
        value = values.get(key, "").strip()
        if value and not _is_url(value):
            errors.append(f"{FIELDS_BY_KEY[key].label} must start with http:// or https://.")

    port = _as_int(values.get("TELEGRAM_WEBHOOK_PORT", "8080"))
    if port is None or not 1 <= port <= 65535:
        errors.append("Webhook port must be between 1 and 65535.")

    timeout = values.get("OPENCODE_TIMEOUT", "600").strip()
    try:
        if float(timeout) <= 0:
            raise ValueError
    except ValueError:
        errors.append("Prompt timeout must be a positive number.")

    max_mb = _as_int(values.get("MEDIA_MAX_MB", "20"))
    if max_mb is None or max_mb <= 0:
        errors.append("Max media size must be a positive integer.")

    level = values.get("LOG_LEVEL", "INFO").strip().upper()
    if level not in FIELDS_BY_KEY["LOG_LEVEL"].choices:
        errors.append("Log level must be one of DEBUG, INFO, WARNING, ERROR.")

    for key in ("DB_PATH", "MEDIA_DIR"):
        if not values.get(key, "").strip():
            errors.append(f"{FIELDS_BY_KEY[key].label} is required.")

    if not _is_set(values.get("STT_API_KEY", "")):
        warnings.append("No OpenRouter key: voice notes cannot be transcribed.")
    if not _is_set(values.get("OPENCODE_SERVER_PASSWORD", "")):
        warnings.append("No server password: the opencode server must be unsecured.")
    webhook_url = values.get("TELEGRAM_WEBHOOK_URL", "").strip()
    if webhook_url:
        if not values.get("TELEGRAM_WEBHOOK_PATH", "").strip():
            errors.append("Webhook path is required when a webhook URL is set.")
        if not _is_set(values.get("TELEGRAM_WEBHOOK_SECRET", "")):
            warnings.append("Webhook has no secret token: anyone with the URL can post updates.")
    return errors, warnings


def missing_required(values: Mapping[str, str]) -> list[str]:
    """Keys the first-run wizard still needs."""
    errors, _ = validate(values)
    missing = []
    for key in ("TELEGRAM_BOT_TOKEN", "ADMIN_USER_ID", "OPENCODE_BASE_URL"):
        if not _is_set(values.get(key, "")):
            missing.append(key)
    if not missing and errors:
        return ["SETTINGS"]
    return missing


_SAVE_LOCK = threading.Lock()


def save(changes: Mapping[str, str]) -> dict[str, str]:
    """Merge ``changes`` onto the stored values, validate, and write."""
    with _SAVE_LOCK:
        text = _read_text(paths.env_file())
        merged = {**defaults(), **(_parse(text) if text else {})}
        for key, value in changes.items():
            if key not in FIELDS_BY_KEY:
                continue
            merged[key] = "" if value is None else str(value).strip()
        errors, _ = validate(merged)
        if errors:
            raise ValueError("\n".join(errors))
        write_env(merged)
        return merged


def payload() -> dict:
    """JSON-safe view for the panel (secrets are never included)."""
    values = load()
    effective = {**defaults(), **values}
    public = {}
    secrets_set = {}
    for field in FIELDS:
        value = effective.get(field.key, "")
        if field.secret:
            public[field.key] = ""
            secrets_set[field.key] = _is_set(value)
        else:
            public[field.key] = value
    _, warnings = validate(effective)
    return {
        "values": public,
        "secrets_set": secrets_set,
        "fields": [
            {
                "key": field.key,
                "label": field.label,
                "group": field.group,
                "kind": field.kind,
                "required": field.required,
                "secret": field.secret,
                "help": field.help,
                "choices": list(field.choices),
                "placeholder": field.placeholder,
            }
            for field in FIELDS
        ],
        "groups": list(GROUPS),
        "env_file": str(paths.env_file()),
        "warnings": warnings,
        "configured": not missing_required(effective),
    }
