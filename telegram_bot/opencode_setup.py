from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import logging

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
GLOBAL_CONFIG_DIR = Path.home() / ".config" / "opencode"
GLOBAL_CONFIG = GLOBAL_CONFIG_DIR / "opencode.json"
GLOBAL_SKILL_DIR = GLOBAL_CONFIG_DIR / "skills" / "telegram-media"
GLOBAL_SKILL = GLOBAL_SKILL_DIR / "SKILL.md"
MCP_SERVER_PATH = REPO_ROOT / "telegram_bot" / "mcp_server.py"
SEND_MEDIA_PATH = REPO_ROOT / "telegram_bot" / "send_media.py"
CONFIG_SCHEMA = "https://opencode.ai/config.json"


def _write_config(config: dict) -> None:
    GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_CONFIG.write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )


def install_mcp_config() -> bool:
    if GLOBAL_CONFIG.exists():
        try:
            parsed = json.loads(GLOBAL_CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning(
                "Skipping MCP install: %s is not valid JSON", GLOBAL_CONFIG
            )
            return False
        if not isinstance(parsed, dict):
            logger.warning(
                "Skipping MCP install: %s is not a JSON object", GLOBAL_CONFIG
            )
            return False
        config = parsed
    else:
        config = {}
    mcp = config.get("mcp")
    if not isinstance(mcp, dict):
        mcp = {}
        config["mcp"] = mcp
    if "telegram" in mcp:
        return False
    config.setdefault("$schema", CONFIG_SCHEMA)
    mcp["telegram"] = {
        "type": "local",
        "command": [sys.executable, str(MCP_SERVER_PATH)],
        "enabled": True,
    }
    _write_config(config)
    return True


def _skill_text() -> str:
    return (
        "---\n"
        "name: telegram-media\n"
        "description: Use when the user asks to send, return, share, or receive "
        "a file, image, screenshot, photo, video, or document via Telegram. "
        "Keywords: send image, send file, send screenshot, telegram media, "
        "send me the file.\n"
        "---\n\n"
        "# Sending files to Telegram\n\n"
        "To send a file or image to the Telegram user, call the MCP tool:\n\n"
        '    telegram_send_file(path="<absolute path>", caption="<optional>")\n\n'
        "If the MCP tool is unavailable, run the CLI instead:\n\n"
        f'    "{sys.executable}" "{SEND_MEDIA_PATH}" "<absolute path>" '
        '--caption "<optional>"\n\n'
        "Rules:\n"
        "- Use an absolute path to a file that already exists.\n"
        "- The file must be at most MEDIA_MAX_MB megabytes.\n"
        "- Images (png/jpg/jpeg/webp/bmp/svg) are sent as photos; everything "
        "else is sent as a document (gif is sent as a document).\n"
        "- Do not send secrets or the `.env` file.\n"
    )


def install_skill() -> bool:
    if GLOBAL_SKILL.exists():
        return False
    GLOBAL_SKILL.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_SKILL.write_text(_skill_text(), encoding="utf-8")
    return True


def install_all() -> dict:
    result = {"mcp": False, "skill": False}
    for key, installer in (("mcp", install_mcp_config), ("skill", install_skill)):
        try:
            result[key] = bool(installer())
        except Exception:
            logger.exception("Failed to install %s artifacts", key)
            result[key] = False
    return result
