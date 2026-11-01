"""Filesystem layout and process commands for the control plane.

Everything the app writes lives under opencode's own config directory
(``~/.config/opencode/opencode-voice``) so it can rely on opencode being
installed without inventing a second location. Both roots are overridable with
environment variables so tests never touch the real home directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "opencode-voice"
ENV_FILE_OVERRIDE = "OCV_ENV_FILE"
CONFIG_DIR_OVERRIDE = "OCV_CONFIG_DIR"
PORT_ENV = "OCV_PORT"
DEFAULT_PORT = 8787

REPO_ROOT = Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_OVERRIDE)
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".config" / "opencode" / APP_NAME


def env_file() -> Path:
    """Settings file: explicit override, the repo ``.env`` in a checkout, else app dir."""
    override = os.environ.get(ENV_FILE_OVERRIDE)
    if override:
        return Path(override).expanduser().resolve()
    repo_env = REPO_ROOT / ".env"
    if repo_env.exists():
        return repo_env
    return config_dir() / ".env"


def data_dir() -> Path:
    return config_dir() / "data"


def media_dir() -> Path:
    return config_dir() / "media"


def logs_dir() -> Path:
    return config_dir() / "logs"


def run_dir() -> Path:
    return config_dir() / "run"


def bot_log() -> Path:
    return logs_dir() / "bot.log"


def panel_log() -> Path:
    return logs_dir() / "panel.log"


def admin_token_file() -> Path:
    return run_dir() / "admin-token"


def pid_file() -> Path:
    return run_dir() / "panel.pid"


def default_db_path() -> Path:
    return data_dir() / "bot.db"


def ensure_dirs() -> None:
    for directory in (config_dir(), data_dir(), media_dir(), logs_dir(), run_dir()):
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass


def command_cwd() -> Path:
    """Working directory for spawned commands (repo root in a source checkout)."""
    return REPO_ROOT


def bot_command() -> list[str]:
    """Command that runs the Telegram bot (child of the panel)."""
    return [sys.executable, "-m", "control", "bot"]


def panel_command(*, port: int) -> list[str]:
    """Command that runs the panel in the foreground (used by services)."""
    return [sys.executable, "-m", "control", "serve", "--no-open", "--port", str(port)]


def default_port() -> int:
    raw = os.environ.get(PORT_ENV, "")
    try:
        return int(raw) if raw.strip() else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT
